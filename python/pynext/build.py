"""Discover and validate the build inputs, then write a versioned manifest.

Build modes (Next.js ``experimentalBuildMode`` equivalent, track E P3-12):

- ``compile`` (``pynext build --only-islands``): bundle the island modules
  with esbuild and record them in the manifest, without rendering static
  pages. Static pages rendered by an earlier build are reused when the
  sources still match; when the island digests changed, the reused pages
  are stale (they embed the old bundle digests), so the ``generate`` stage
  is dropped from the manifest until it runs again.
- ``generate`` (``pynext build --only-static``): render the static pages
  (and public assets) from the island bundles of an earlier compile, without
  re-running esbuild. Island sources must be unchanged since that compile.
- ``full`` (plain ``pynext build``): both stages, as before.

A manifest records the stages it completed in ``stages``; ``load_manifest``
refuses a partial manifest so ``pynext start`` fails with an actionable
message instead of serving a half-built app. Manifests written before this
feature have no ``stages`` key and are treated as complete.

Static-only export (``pynext build --static-only``) is intentionally narrow:
it exports the rendered SSG pages, public assets and island bundles into a
plain directory servable with ``python -m http.server``. It is *not* a
full ``output: export`` port (REPORT.md §4.3): apps with dynamic SSR, ISR
or API routes are refused instead of being silently degraded.
"""
import hashlib
import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import _native
from .assets import discover_public
from .client import PREFIX as CLIENT_PREFIX
from .client import discover_client, read_client_artifacts

MANIFEST = ".pynext/manifest.json"
VERSION = 9
MAX_STATIC_PAGES = 10_000
# One cap for every static page rendered at build time, streaming or
# not: a non-streaming page used to have no size limit at all while a
# streaming page was capped at 8 MiB.
MAX_STATIC_PAGE_BYTES = 8 * 1024 * 1024
MAX_STREAMED_STATIC_BYTES = MAX_STATIC_PAGE_BYTES
MAX_STREAMED_STATIC_SECONDS = 30

# Build stages, in the order they run. A full build completes both.
STAGES = ("compile", "generate")
# Keys of a manifest that describe the discovered sources. Two builds agree
# on these exactly when nothing relevant changed between them.
_DISCOVER_KEYS = ("schema_version", "routes", "sources", "public_sources", "client_sources")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_build_paths(root):
    build_dir = root / ".pynext"
    if build_dir.is_symlink() or (build_dir.exists() and not build_dir.is_dir()):
        raise ValueError(".pynext must be a directory inside the application root")
    static_dir = build_dir / "static"
    if static_dir.is_symlink() or (static_dir.exists() and not static_dir.is_dir()):
        raise ValueError(".pynext/static must be a directory inside the application root")
    if (build_dir / "manifest.json").is_symlink():
        raise ValueError("The build manifest must not be a symbolic link")


def discover(root):
    root = Path(root).resolve()
    app_dir = root / "app"
    if app_dir.is_symlink():
        raise ValueError(f"App directory must not be a symbolic link: {app_dir}")
    if not app_dir.is_dir():
        raise ValueError(f"App directory not found: {app_dir}")
    # rglob does not descend into symlinked directories, so a symlinked
    # directory was silently skipped (its page.py never became a route)
    # while a symlinked file pointing outside was rejected: reject every
    # symlink under app/ instead, like discover_public does for public/.
    for current, dirs, files in os.walk(app_dir, followlinks=False):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise ValueError(f"App symlink is not allowed: {Path(current) / name}")
    files = sorted(p for p in app_dir.rglob("*.py") if "__pycache__" not in p.parts)
    for path in files:
        if not path.resolve().is_relative_to(app_dir.resolve()):
            raise ValueError(f"Python file escapes app directory: {path}")
    for name in ("middleware.py", "resources.py"):
        extra = root / name
        if extra.is_symlink():
            raise ValueError(f"{name} must not be a symbolic link")
        if extra.is_file():
            if not extra.resolve().is_relative_to(root):
                raise ValueError(f"{name} escapes project root")
            files.append(extra)
    routes = {}
    for path in files:
        if path.parent == root:
            continue
        if path.name not in ("page.py", "route.py"):
            continue
        relative_parent = path.parent.relative_to(app_dir)
        # Route groups (parenthesised directories) organise files but do
        # not appear in the URL; layouts inside a group still apply.
        parts = []
        for part in relative_parent.parts:
            if _route_group(part) is None:
                parts.append(part)
        pattern = "/" + "/".join(parts)
        if pattern in routes:
            if routes[pattern]["kind"] != path.stem:
                raise ValueError(f"A page and an API cannot share a path: {pattern}")
            raise ValueError(f"Duplicate route path: {pattern}")
        dirs = [app_dir]
        current = app_dir
        for part in relative_parent.parts:
            current = current / part
            dirs.append(current)
        layouts = [str((d / "layout.py").relative_to(root)) for d in dirs if (d / "layout.py").is_file()]
        routes[pattern] = {"entry": str(path.relative_to(root)), "kind": path.stem,
                           "layouts": layouts if path.stem == "page" else []}
    if not routes:
        raise ValueError("No page.py or route.py files found")
    router = _native.Router(list(routes))
    public_sources = discover_public(root)
    for path in list(routes) + list(public_sources):
        if path == "/_pynext" or path.startswith("/_pynext/"):
            raise ValueError(f"Reserved PyNext path: {path}")
    conflicts = set(public_sources).intersection(routes)
    if conflicts:
        raise ValueError(f"Public file conflicts with a page or API route: {sorted(conflicts)[0]}")
    return {"schema_version": VERSION, "routes": {p: routes[p] for p in router.ordered_routes()},
            "sources": {str(p.relative_to(root)): _hash(p) for p in files},
            "public_sources": public_sources, "client_sources": discover_client(root)}


def _normalize_stages(stages):
    """Validate a requested stage set and return it in canonical order."""
    if stages is None:
        return list(STAGES)
    selected = [stage for stage in STAGES if stage in set(stages)]
    if not selected or len(selected) != len(set(stages)):
        raise ValueError(
            f"Build stages must be a non-empty subset of {list(STAGES)}: {sorted(stages)!r}")
    return selected


def _read_manifest_file(root):
    """Read the existing manifest, or None when no build exists yet."""
    target = root / MANIFEST
    if not target.is_file():
        return None
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"Invalid build manifest {target}: {exc}; rebuild the application") from exc
    if not isinstance(saved, dict):
        raise ValueError(
            f"Invalid build manifest {target}: expected a JSON object; rebuild the application")
    if saved.get("schema_version") != VERSION:
        raise ValueError("Incompatible manifest version; rebuild the application")
    return saved


def _write_manifest_file(root, manifest):
    target = root / MANIFEST
    _atomic_write(target, (json.dumps(manifest, ensure_ascii=False, indent=2) +
                           "\n").encode("utf-8"))


def _write_client_files(root, client_assets):
    for path, body in client_assets.items():
        digest = path.rsplit("/", 1)[1].removesuffix(".js")
        _atomic_write(root / ".pynext" / "static" / f"{digest}.js", body)


def _write_public_files(root, public_assets):
    assets = {}
    for path, (body, media_type) in public_assets.items():
        digest = hashlib.sha256(body).hexdigest()
        _atomic_write(root / ".pynext" / "static" / f"{digest}.asset", body)
        assets[path] = {"sha256": digest, "content_type": media_type}
    return assets


def _write_static_files(root, rendered):
    static_pages = {}
    for path, (pattern, body) in rendered.items():
        digest = hashlib.sha256(body).hexdigest()
        destination = root / ".pynext" / "static" / f"{digest}.html"
        _atomic_write(destination, body)
        static_pages[path] = {"pattern": pattern, "sha256": digest}
    return static_pages


def _check_sources_unchanged(root, manifest):
    if discover(root) != manifest:
        raise ValueError("Application sources changed during build; retry the build")


def _finish_manifest(root, manifest, app, rendered, *, previous=None):
    """Fill a discovered manifest with build outputs and write it.

    ``rendered`` is the ``_render_static_pages`` result (or None for a
    compile-only build, which reuses the previous static pages). ``previous``
    is the manifest of an earlier build whose static pages/assets may be
    reused; it is ignored when ``rendered`` is given.
    """
    manifest["render_modes"] = app.render_modes
    manifest["isr_policies"] = app.isr_policies
    manifest["isr_queries"] = app.isr_queries
    manifest["client"] = app.client_manifest
    if rendered is None:
        if previous is not None:
            manifest["static_pages"] = previous.get("static_pages", {})
            manifest["assets"] = previous.get("assets", {})
        else:
            manifest["static_pages"] = {}
            manifest["assets"] = {}
    else:
        manifest["static_pages"] = _write_static_files(root, rendered)
        manifest["assets"] = _write_public_files(root, app.public_assets)
    _write_client_files(root, app.client_assets)
    _write_manifest_file(root, manifest)
    return manifest


def write_manifest(root, stages=None):
    """Build the application, or only the requested stages.

    ``stages`` is a subset of ``("compile", "generate")``; the default is
    both (a full build). See the module docstring for what each stage does
    and what a partial build reuses from the previous one.
    """
    root = Path(root).resolve()
    _check_build_paths(root)
    selected = _normalize_stages(stages)
    if selected == list(STAGES):
        manifest = discover(root)
        # Loading user code and running its generators are intentional build steps.
        from .app import App
        app = App(root, manifest)
        rendered = asyncio.run(_render_static_pages(app))
        _check_sources_unchanged(root, manifest)
        manifest["stages"] = list(STAGES)
        return _finish_manifest(root, manifest, app, rendered)
    if selected == ["compile"]:
        return _write_compile_only_manifest(root)
    return _write_generate_only_manifest(root)


def _write_compile_only_manifest(root):
    """Compile island bundles without rendering static pages."""
    manifest = discover(root)
    # Loading user code is an intentional build step; the App also validates
    # the routes and computes the render modes / ISR policies.
    from .app import App
    app = App(root, manifest)
    previous = _read_manifest_file(root)
    previous_client = previous.get("client") if isinstance(previous, dict) else None
    reused = (previous is not None and isinstance(previous_client, dict) and
              isinstance(previous_client.get("bundles"), dict) and
              {key: previous.get(key) for key in _DISCOVER_KEYS} ==
              {key: manifest.get(key) for key in _DISCOVER_KEYS})
    _check_sources_unchanged(root, manifest)
    if reused:
        # Static pages embed island bundle digests, so they stay valid only
        # while the bundles are unchanged. A changed digest drops the
        # generate stage: the pages are stale until they are re-rendered.
        old_digests = {previous.get("client", {}).get("runtime"),
                       *previous.get("client", {}).get("bundles", {}).values()}
        new_digests = {app.client_manifest.get("runtime"),
                       *app.client_manifest.get("bundles", {}).values()}
        previous_stages = previous.get("stages") or list(STAGES)
        stages = [stage for stage in STAGES
                  if stage == "compile" or
                  (stage in previous_stages and old_digests == new_digests)]
    else:
        previous = None
        stages = ["compile"]
    manifest["stages"] = stages
    return _finish_manifest(root, manifest, app, None, previous=previous)


def _write_generate_only_manifest(root):
    """Render static pages from the island bundles of an earlier compile."""
    manifest = discover(root)
    previous = _read_manifest_file(root)
    if previous is None:
        raise ValueError("No previous build found; run `pynext build --only-islands` first")
    if "compile" not in (previous.get("stages") or list(STAGES)):
        raise ValueError(
            "Island bundles were not compiled; run `pynext build --only-islands` first")
    if previous.get("client_sources") != manifest["client_sources"]:
        raise ValueError("Island sources changed since the last compile; "
                         "run `pynext build --only-islands` first")
    client_manifest, client_assets = read_client_artifacts(root, previous)
    # Loading user code and running its generators are intentional build steps.
    # The client bundles are reused from the previous compile, so esbuild
    # does not run again.
    from .app import App
    app = App(root, manifest, reuse_client=(client_manifest, client_assets))
    rendered = asyncio.run(_render_static_pages(app))
    _check_sources_unchanged(root, manifest)
    previous_stages = previous.get("stages") or list(STAGES)
    manifest["stages"] = [stage for stage in STAGES
                          if stage == "generate" or stage in previous_stages]
    return _finish_manifest(root, manifest, app, rendered)


def export_static(root, manifest=None, output=None):
    """Export a static-only site: SSG pages, public assets, island bundles.

    The output directory holds plain files (``index.html`` per page, public
    assets at their URL paths, island bundles under ``_pynext/static/``) and
    is servable with ``python -m http.server --directory <output>`` without
    a PyNext server. Apps with dynamic SSR, ISR or API routes are refused:
    there is no server to run them, and silently dropping them would ship a
    broken site. This is deliberately not a full ``output: export`` port.
    """
    root = Path(root).resolve()
    if manifest is None:
        manifest = load_manifest(root)
    dynamic = sorted(pattern for pattern, mode in manifest.get("render_modes", {}).items()
                     if mode != "ssg")
    api = sorted(pattern for pattern, spec in manifest.get("routes", {}).items()
                 if spec.get("kind") == "route")
    if dynamic or api:
        blocked = [f"{pattern} (render={manifest['render_modes'][pattern]})" for pattern in dynamic]
        blocked += [f"{pattern} (API route)" for pattern in api]
        raise ValueError("Static-only export cannot include dynamic routes: " + ", ".join(blocked) +
                         "; make them SSG or remove them first")
    static_dir = root / ".pynext" / "static"
    output = Path(output) if output is not None else root / ".pynext" / "export"
    if not output.is_absolute():
        output = (root / output).resolve()
    if output.is_symlink():
        raise ValueError(f"Export directory must not be a symbolic link: {output}")
    if output == root or root / "app" in output.parents or root / "public" in output.parents:
        raise ValueError(f"Export directory must not overlap the application sources: {output}")
    if output.exists() and any(output.iterdir()):
        if output != root / ".pynext" / "export":
            raise ValueError(
                f"Export directory is not empty: {output}; remove it or choose another directory")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        pages = 0
        for path, entry in sorted(manifest.get("static_pages", {}).items()):
            digest = entry["sha256"]
            body = _read_static_file(static_dir, f"{digest}.html", digest)
            relative = ("index.html" if path == "/" else
                        "/".join(path.strip("/").split("/")) + "/index.html")
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            pages += 1
        assets = 0
        for path, entry in sorted(manifest.get("assets", {}).items()):
            digest = entry["sha256"]
            body = _read_static_file(static_dir, f"{digest}.asset", digest)
            target = output / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            assets += 1
        bundles = 0
        client = manifest.get("client", {})
        if not isinstance(client, dict) or not isinstance(client.get("bundles", {}), dict):
            raise ValueError("Invalid client manifest; rebuild the application")
        for digest in {client.get("runtime"), *client.get("bundles", {}).values()}:
            if digest is None:
                continue
            body = _read_static_file(static_dir, f"{digest}.js", digest)
            target = output / CLIENT_PREFIX.lstrip("/") / f"{digest}.js"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            bundles += 1
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return {"pages": pages, "assets": assets, "bundles": bundles, "output": str(output)}


def _read_static_file(static_dir, name, digest):
    file = static_dir / name
    if not file.resolve().is_relative_to(static_dir.resolve()) or not file.is_file():
        raise ValueError(f"Missing build output {name}; rebuild the application")
    body = file.read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise ValueError(f"Build output {name} failed integrity check; rebuild the application")
    return body


def _atomic_write(path, contents):
    # Build outputs (manifest.json, static .html/.asset/.js) are read back
    # by the server process, which may run as a different user than the
    # build user, so they must be world-readable. NamedTemporaryFile
    # creates its file 0600, which would make a build done by one user
    # unreadable (and the app unstartable) for another; use the normal
    # readable default 0644 instead of inheriting that restriction.
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pynext-", delete=False) as file:
            name = file.name
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def _segment_param(segment):
    """Parse one route segment.

    Returns (name, kind) where kind is "dynamic", "catch_all" or
    "optional", or None for a static segment.
    """
    if not segment.startswith("["):
        return None
    match = re.fullmatch(r"\[(\[)?(\.\.\.)?([A-Za-z0-9_-]+)\](\])?", segment)
    if match is None:
        raise ValueError(f"Invalid route segment: {segment}")
    double_open, dots, name, double_close = match.groups()
    if bool(double_open) != bool(double_close):
        raise ValueError(f"Invalid route segment: {segment}")
    if double_open and not dots:
        raise ValueError(f"Optional catch-all must use [[...name]]: {segment}")
    kind = "optional" if double_open else ("catch_all" if dots else "dynamic")
    return name, kind


def _route_group(part):
    """Return the group name for a `(group)` directory segment, else None.

    Route groups do not appear in the URL. A segment that looks like a
    group but is malformed (unbalanced or empty parentheses) is an error.
    """
    if not (part.startswith("(") or part.endswith(")")):
        return None
    match = re.fullmatch(r"\(([A-Za-z0-9_-]+)\)", part)
    if match is None:
        raise ValueError(f"Invalid route group: {part}")
    return match.group(1)


def _materialize(pattern, params):
    segments = pattern.strip("/").split("/") if pattern != "/" else []
    keys = {}
    for segment in segments:
        parsed = _segment_param(segment)
        if parsed:
            keys[parsed[0]] = parsed[1]
    if not isinstance(params, dict) or set(params) != set(keys):
        raise ValueError(f"Static params for {pattern} must contain exactly {sorted(keys)}")
    output = []
    for segment in segments:
        parsed = _segment_param(segment)
        if not parsed:
            output.append(segment)
            continue
        name, kind = parsed
        value = params[name]
        if kind == "catch_all":
            if not isinstance(value, list) or not value:
                raise ValueError(f"Catch-all parameter {name} must be a nonempty list")
            values = value
        elif kind == "optional":
            if not isinstance(value, list):
                raise ValueError(f"Optional catch-all parameter {name} must be a list")
            values = value
        else:
            values = [value]
        for part in values:
            if (not isinstance(part, str) or not part or part in {".", ".."} or
                    any(c in "/\\?#" or ord(c) < 32 or ord(c) == 127 for c in part)):
                raise ValueError(f"Invalid static parameter in {pattern}: {part!r}")
            try:
                part.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"Invalid Unicode static parameter in {pattern}") from exc
            output.append(part)
    return "/" + "/".join(output)


async def _render_static_pages(app):
    from .app import _invoke
    from .http import StreamingResponse
    rendered = {}
    for pattern, generator in app.static_generators.items():
        params_list = [{}] if generator is None else await _invoke(generator)
        if not isinstance(params_list, list):
            raise ValueError(f"generate_static_params for {pattern} must return a list")
        if len(params_list) + len(rendered) > MAX_STATIC_PAGES:
            raise ValueError(f"Static page limit ({MAX_STATIC_PAGES}) exceeded")
        for params in params_list:
            path = _materialize(pattern, params)
            if path in rendered:
                raise ValueError(f"Duplicate generated path: {path}")
            if path in app.public_assets:
                raise ValueError(f"Public file shadows a generated page: {path}")
            match = app.router.matches(path)
            if match is None or match[0] != pattern:
                raise ValueError(f"Generated path {path} is not reachable through {pattern}")
            response = await app.render_page(pattern, None, params)
            if isinstance(response, StreamingResponse):
                body = bytearray()
                try:
                    async with asyncio.timeout(MAX_STREAMED_STATIC_SECONDS):
                        while (chunk := await response._next_chunk()) is not None:
                            if len(body) + len(chunk) > MAX_STREAMED_STATIC_BYTES:
                                raise ValueError("Streamed static page exceeds build size limit")
                            body.extend(chunk)
                except asyncio.TimeoutError as exc:
                    raise ValueError(
                        f"Streamed static page for {path} exceeded the build time limit "
                        f"({MAX_STREAMED_STATIC_SECONDS}s)") from exc
                finally:
                    await response.aclose()
                rendered[path] = (pattern, bytes(body))
            else:
                if len(response.body) > MAX_STATIC_PAGE_BYTES:
                    raise ValueError("Static page exceeds build size limit")
                rendered[path] = (pattern, response.body)
    return rendered


def read_static_pages(root, manifest, router):
    pages = manifest.get("static_pages")
    if not isinstance(pages, dict):
        raise ValueError("Invalid static page manifest; rebuild the application")
    result = {}
    from .app import _decode_path
    for path, entry in pages.items():
        if not isinstance(path, str) or not isinstance(entry, dict) or set(entry) != {"pattern", "sha256"}:
            raise ValueError("Invalid static page entry; rebuild the application")
        digest = entry["sha256"]
        pattern = entry["pattern"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid static page digest; rebuild the application")
        try:
            valid_path = _decode_path(path) == path and (path == "/" or not path.endswith("/"))
        except (ValueError, UnicodeError):
            valid_path = False
        matched = router.matches(path) if valid_path else None
        if (not matched or matched[0] != pattern or
                manifest["render_modes"].get(pattern) != "ssg"):
            raise ValueError(f"Invalid static page route: {path!r}")
        directory = root / ".pynext" / "static"
        file = directory / f"{digest}.html"
        if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
            raise ValueError(f"Missing static page for {path}; rebuild the application")
        body = file.read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError(f"Static page for {path} failed integrity check; rebuild the application")
        result[path] = body
    for pattern, mode in manifest["render_modes"].items():
        if mode == "ssg" and "[" not in pattern and pattern not in result:
            raise ValueError(f"Missing static page for {pattern}; rebuild the application")
    return result


def load_manifest(root):
    root = Path(root).resolve()
    _check_build_paths(root)
    target = root / MANIFEST
    if not target.is_file():
        raise ValueError("No build manifest found; run `pynext build` first")
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid build manifest {target}: {exc}; rebuild the application") from exc
    if not isinstance(saved, dict):
        raise ValueError(f"Invalid build manifest {target}: expected a JSON object; rebuild the application")
    if saved.get("schema_version") != VERSION:
        raise ValueError("Incompatible manifest version; rebuild the application")
    stages = saved.get("stages")
    if stages is not None:
        # Manifests written before build stages existed have no key and are
        # treated as complete builds.
        if not isinstance(stages, list) or not set(stages) <= set(STAGES):
            raise ValueError("Invalid build stages in manifest; rebuild the application")
        if "compile" not in stages:
            raise ValueError("Island bundles were not compiled; "
                             "run `pynext build --only-islands` or a full `pynext build` first")
        if "generate" not in stages:
            raise ValueError("Static pages were not generated; "
                             "run `pynext build --only-static` or a full `pynext build` first")
    expected = discover(root)
    if {key: saved.get(key) for key in expected} != expected:
        raise ValueError("Application sources changed since build; run `pynext build` again")
    return saved
