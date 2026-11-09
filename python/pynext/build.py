"""Discover and validate the build inputs, then write a versioned manifest."""
import hashlib
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from . import _native
from .assets import discover_public
from .client import discover_client

MANIFEST = ".pynext/manifest.json"
VERSION = 9
MAX_STATIC_PAGES = 10_000
# One cap for every static page rendered at build time, streaming or
# not: a non-streaming page used to have no size limit at all while a
# streaming page was capped at 8 MiB.
MAX_STATIC_PAGE_BYTES = 8 * 1024 * 1024
MAX_STREAMED_STATIC_BYTES = MAX_STATIC_PAGE_BYTES
MAX_STREAMED_STATIC_SECONDS = 30


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _short_repr(value, limit=200):
    """One-line repr of a config value, truncated so a huge user value
    cannot flood the error message."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _config_error(summary, details=(), fixes=()):
    """Format a config/route conflict error: what happened, where, and how
    to fix it.

    Follows the Next.js ensure-static shape: the conflicting values with
    their source files, then a "Possible fixes" list. ``details`` is a
    sequence of (label, value) pairs; both ``details`` and ``fixes`` may
    be empty.
    """
    lines = [summary]
    for label, value in details:
        lines.append(f"  {label}: {value}")
    if fixes:
        lines.append("Possible fixes:")
        for fix in fixes:
            lines.append(f"  - {fix}")
    return "\n".join(lines)


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
            try:
                group = _route_group(part)
            except ValueError as exc:
                raise ValueError(_config_error(
                    str(exc),
                    [("file", str(path.relative_to(root))), ("segment", part)],
                    ["Rename the directory to a valid route group like (name)",
                     "Remove the parentheses to make it a literal URL segment"])) from exc
            if group is None:
                parts.append(part)
        pattern = "/" + "/".join(parts)
        if pattern in routes:
            entry = str(path.relative_to(root))
            existing = routes[pattern]
            if existing["kind"] != path.stem:
                page_entry = entry if path.stem == "page" else existing["entry"]
                api_entry = entry if path.stem == "route" else existing["entry"]
                raise ValueError(_config_error(
                    f"A page and an API cannot share a path: {pattern}",
                    [("page", page_entry), ("API route", api_entry)],
                    ["Move one of the files to a different route directory",
                     "Delete the file you do not need"]))
            raise ValueError(_config_error(
                f"Duplicate route path: {pattern}",
                [("first", existing["entry"]), ("second", entry)],
                ["Keep only one of the two files",
                 "Move one of them to a different route directory"]))
        dirs = [app_dir]
        current = app_dir
        for part in relative_parent.parts:
            current = current / part
            dirs.append(current)
        layouts = [str((d / "layout.py").relative_to(root)) for d in dirs if (d / "layout.py").is_file()]
        routes[pattern] = {"entry": str(path.relative_to(root)), "kind": path.stem,
                           "layouts": layouts if path.stem == "page" else []}
    if not routes:
        raise ValueError(_config_error(
            "No page.py or route.py files found",
            [("app directory", "app")],
            ["Add an app/page.py file with a @page() Page function",
             "Add an app/route.py file with @route handlers"]))
    router = _native.Router(list(routes))
    public_sources = discover_public(root)
    for path in list(routes) + list(public_sources):
        if path == "/_pynext" or path.startswith("/_pynext/"):
            source = routes[path]["entry"] if path in routes else f"public{path}"
            raise ValueError(_config_error(
                f"Reserved PyNext path: {path}",
                [("source", source)],
                ["Rename the route or public file so its URL does not start with /_pynext"]))
    conflicts = set(public_sources).intersection(routes)
    if conflicts:
        path = sorted(conflicts)[0]
        raise ValueError(_config_error(
            f"Public file conflicts with a page or API route: {path}",
            [("public file", f"public{path}"), ("route", routes[path]["entry"])],
            ["Rename or move the public file so its URL does not match the route",
             "Move or rename the route"]))
    return {"schema_version": VERSION, "routes": {p: routes[p] for p in router.ordered_routes()},
            "sources": {str(p.relative_to(root)): _hash(p) for p in files},
            "public_sources": public_sources, "client_sources": discover_client(root)}


def write_manifest(root):
    root = Path(root).resolve()
    _check_build_paths(root)
    manifest = discover(root)
    # Loading user code and running its generators are intentional build steps.
    from .app import App
    app = App(root, manifest)
    rendered = asyncio.run(_render_static_pages(app))
    if discover(root) != manifest:
        raise ValueError("Application sources changed during build; retry the build")
    manifest["render_modes"] = app.render_modes
    manifest["isr_policies"] = app.isr_policies
    manifest["isr_queries"] = app.isr_queries
    manifest["client"] = app.client_manifest
    manifest["static_pages"] = {}
    for path, (pattern, body) in rendered.items():
        digest = hashlib.sha256(body).hexdigest()
        destination = root / ".pynext" / "static" / f"{digest}.html"
        _atomic_write(destination, body)
        manifest["static_pages"][path] = {"pattern": pattern, "sha256": digest}
    manifest["assets"] = {}
    for path, (body, media_type) in app.public_assets.items():
        digest = hashlib.sha256(body).hexdigest()
        _atomic_write(root / ".pynext" / "static" / f"{digest}.asset", body)
        manifest["assets"][path] = {"sha256": digest, "content_type": media_type}
    for path, body in app.client_assets.items():
        digest = path.rsplit("/", 1)[1].removesuffix(".js")
        _atomic_write(root / ".pynext" / "static" / f"{digest}.js", body)
    target = root / MANIFEST
    _atomic_write(target, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest


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


def _materialize(pattern, params, entry):
    segments = pattern.strip("/").split("/") if pattern != "/" else []
    keys = {}
    for segment in segments:
        try:
            parsed = _segment_param(segment)
        except ValueError as exc:
            raise ValueError(_config_error(
                str(exc),
                [("file", entry), ("pattern", pattern)],
                ["Use [name], [...name], or [[...name]] with a name matching [A-Za-z0-9_-]+",
                 "Remove the brackets to make it a literal URL segment"])) from exc
        if parsed:
            keys[parsed[0]] = parsed[1]
    if not isinstance(params, dict) or set(params) != set(keys):
        got = sorted(params) if isinstance(params, dict) else f"{type(params).__name__}: {_short_repr(params)}"
        raise ValueError(_config_error(
            f"Static params for {pattern} must contain exactly {sorted(keys)}",
            [("file", entry), ("got", got)],
            [f"Return one dict per path with exactly these keys: {sorted(keys)}"]))
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
                raise ValueError(_config_error(
                    f"Catch-all parameter {name} must be a nonempty list",
                    [("file", entry), ("pattern", pattern), ("got", _short_repr(value))],
                    [f"Return a nonempty list of strings for {name!r} in generate_static_params"]))
            values = value
        elif kind == "optional":
            if not isinstance(value, list):
                raise ValueError(_config_error(
                    f"Optional catch-all parameter {name} must be a list",
                    [("file", entry), ("pattern", pattern), ("got", _short_repr(value))],
                    [f"Return a list of strings for {name!r} in generate_static_params (use [] for no segments)"]))
            values = value
        else:
            values = [value]
        for part in values:
            if (not isinstance(part, str) or not part or part in {".", ".."} or
                    any(c in "/\\?#" or ord(c) < 32 or ord(c) == 127 for c in part)):
                raise ValueError(_config_error(
                    f"Invalid static parameter in {pattern}: {part!r}",
                    [("file", entry), ("parameter", name)],
                    ["Use a nonempty string without /, \\, ?, #, or control characters",
                     "Use '.' or '..' only as literal route segments, not as parameter values"]))
            try:
                part.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(_config_error(
                    f"Invalid Unicode static parameter in {pattern}",
                    [("file", entry), ("parameter", name), ("got", _short_repr(part))],
                    ["Return a valid UTF-8 string for the parameter"])) from exc
            output.append(part)
    return "/" + "/".join(output)


async def _render_static_pages(app):
    from .app import _invoke
    from .http import StreamingResponse
    rendered = {}
    rendered_params = {}
    for pattern, generator in app.static_generators.items():
        entry = app.manifest["routes"][pattern]["entry"]
        params_list = [{}] if generator is None else await _invoke(generator)
        if not isinstance(params_list, list):
            raise ValueError(_config_error(
                f"generate_static_params for {pattern} must return a list",
                [("file", entry), ("got", f"{type(params_list).__name__}: {_short_repr(params_list)}")],
                ["Return a list of dicts, one per path (use [] when the route has no paths)"]))
        if len(params_list) + len(rendered) > MAX_STATIC_PAGES:
            raise ValueError(_config_error(
                f"Static page limit ({MAX_STATIC_PAGES}) exceeded",
                [("file", entry), ("pattern", pattern),
                 ("pages", f"{len(rendered)} already rendered + {len(params_list)} from this route")],
                ["Return fewer paths from generate_static_params",
                 "Split the route into smaller routes"]))
        for params in params_list:
            path = _materialize(pattern, params, entry)
            if path in rendered:
                raise ValueError(_config_error(
                    f"Duplicate generated path: {path}",
                    [("file", entry), ("pattern", pattern),
                     ("first", _short_repr(rendered_params[path])),
                     ("second", _short_repr(params))],
                    ["Make generate_static_params return unique parameter sets"]))
            if path in app.public_assets:
                raise ValueError(_config_error(
                    f"Public file shadows a generated page: {path}",
                    [("public file", f"public{path}"),
                     ("generated by", f"{entry} ({pattern})")],
                    ["Rename or move the public file",
                     f"Change generate_static_params in {entry} so it does not generate {path}"]))
            match = app.router.matches(path)
            if match is None or match[0] != pattern:
                raise ValueError(_config_error(
                    f"Generated path {path} is not reachable through {pattern}",
                    [("file", entry), ("parameters", _short_repr(params))],
                    ["Return only parameter values that match the route pattern",
                     "Check that the parameter names match the [param] segments of the route"]))
            response = await app.render_page(pattern, None, params)
            if isinstance(response, StreamingResponse):
                body = bytearray()
                try:
                    async with asyncio.timeout(MAX_STREAMED_STATIC_SECONDS):
                        while (chunk := await response._next_chunk()) is not None:
                            if len(body) + len(chunk) > MAX_STREAMED_STATIC_BYTES:
                                raise ValueError(_config_error(
                                    f"Streamed static page {path} exceeds the build size limit "
                                    f"({MAX_STREAMED_STATIC_BYTES} bytes)",
                                    [("file", entry), ("pattern", pattern)],
                                    ["Split the page or reduce its content"]))
                            body.extend(chunk)
                except asyncio.TimeoutError as exc:
                    raise ValueError(_config_error(
                        f"Streamed static page for {path} exceeded the build time limit "
                        f"({MAX_STREAMED_STATIC_SECONDS}s)",
                        [("file", entry), ("pattern", pattern)],
                        ["Reduce the page content or make it render faster",
                         "Split the page into smaller pages"])) from exc
                finally:
                    await response.aclose()
                rendered[path] = (pattern, bytes(body))
            else:
                if len(response.body) > MAX_STATIC_PAGE_BYTES:
                    raise ValueError(_config_error(
                        f"Static page {path} exceeds the build size limit "
                        f"({MAX_STATIC_PAGE_BYTES} bytes)",
                        [("file", entry), ("pattern", pattern)],
                        ["Split the page or reduce its content"]))
                rendered[path] = (pattern, response.body)
            rendered_params[path] = params
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
    expected = discover(root)
    if {key: saved.get(key) for key in expected} != expected:
        raise ValueError("Application sources changed since build; run `pynext build` again")
    return saved
