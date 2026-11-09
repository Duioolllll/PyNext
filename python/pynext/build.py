"""Discover and validate the build inputs, then write a versioned manifest."""
import hashlib
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import _native
from .assets import discover_public
from .client import discover_client

log = logging.getLogger("pynext")

MANIFEST = ".pynext/manifest.json"
BUILD_ID_FILE = ".pynext/BUILD_ID"
LOCK_FILE = ".pynext/.build.lock"
VERSION = 9
MAX_STATIC_PAGES = 10_000
# One cap for every static page rendered at build time, streaming or
# not: a non-streaming page used to have no size limit at all while a
# streaming page was capped at 8 MiB.
MAX_STATIC_PAGE_BYTES = 8 * 1024 * 1024
MAX_STREAMED_STATIC_BYTES = MAX_STATIC_PAGE_BYTES
MAX_STREAMED_STATIC_SECONDS = 30
# A build that cannot write its outputs or would run the disk dry must
# fail in precheck, not halfway through: 64 MiB is far above what a
# manifest plus digest files need, and far below a real disk.
MIN_FREE_DISK_BYTES = 64 * 1024 * 1024
# How long a second build waits for the lock before giving up (0 fails
# immediately). Next.js retries its lockfile acquisition, then exits.
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.1

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None


class BuildError(ValueError):
    """A build failure, tagged with the stage that failed.

    ``stage`` names the pipeline step (``precheck``, ``discover``,
    ``render``, ``validate``, ``write-assets``, ``write-manifest``,
    ``write-build-id``, ``prune`` or ``lock``) so a failure says where
    it happened instead of only why. It subclasses ValueError so the
    CLI reports it as a clean ``parser.error`` instead of a traceback.
    """

    def __init__(self, stage, message):
        self.stage = stage
        super().__init__(f"build failed at stage '{stage}': {message}")


class BuildLockError(BuildError):
    """Another build already holds the lock."""

    def __init__(self, message):
        super().__init__("lock", message)


def _flock_nonblocking(file):
    """Take an exclusive, non-blocking lock on an open file. True when taken."""
    if fcntl is not None:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    import msvcrt
    try:
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _funlock(file):
    if fcntl is not None:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    else:
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


class BuildLock:
    """Exclusive build lock (``.pynext/.build.lock``), Next.js's
    ``Lockfile.acquireWithRetriesOrExit`` in miniature.

    Two concurrent ``pynext build`` runs used to write the same digest
    files and overwrite each other's manifest; the lock makes the
    second build wait up to ``timeout`` seconds and then fail with a
    clear error naming the lock file and the holding process id. The
    lock file is kept (never unlinked): unlinking it while another
    process waits would let two processes lock different inodes.
    ``write_manifest`` and ``build_standalone`` each take the lock, so
    a standalone copy always sees one consistent, completed build.
    """

    def __init__(self, root, timeout=LOCK_TIMEOUT_SECONDS):
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.path = self.root / LOCK_FILE
        self._file = None

    def __enter__(self):
        _check_build_paths(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        deadline = time.monotonic() + max(self.timeout, 0)
        while True:
            if _flock_nonblocking(self._file):
                self._file.seek(0)
                self._file.truncate()
                self._file.write(str(os.getpid()))
                self._file.flush()
                return self
            if time.monotonic() >= deadline:
                holder = ""
                try:
                    self._file.seek(0)
                    holder = self._file.read().strip()
                except OSError:
                    pass
                self._file.close()
                self._file = None
                detail = f" (held by process {holder})" if holder.isdigit() else ""
                raise BuildLockError(
                    f"another build is already running for {self.root}{detail}; "
                    f"wait for it to finish or remove {self.path} if it is stale")
            time.sleep(LOCK_POLL_SECONDS)

    def __exit__(self, *exc):
        if self._file is not None:
            try:
                _funlock(self._file)
            finally:
                self._file.close()
                self._file = None
        return False


def _preflight_checks(root):
    """Stage ``precheck``: the build directory must exist, be writable
    (Next.js's ``isWriteable``), and have enough free disk space.

    Runs inside the build lock, before any source is read, so a build
    that cannot finish fails fast instead of dying halfway through
    with a half-written output directory.
    """
    _check_build_paths(root)
    build_dir = root / ".pynext"
    build_dir.mkdir(parents=True, exist_ok=True)
    probe = None
    try:
        with tempfile.NamedTemporaryFile(dir=build_dir, prefix=".write-test-", delete=False) as file:
            probe = file.name
            file.write(b"pynext build write check")
            file.flush()
            os.fsync(file.fileno())
    except OSError as exc:
        raise BuildError("precheck", f".pynext is not writable: {exc}") from exc
    finally:
        if probe is not None and os.path.exists(probe):
            os.unlink(probe)
    try:
        free = shutil.disk_usage(build_dir).free
    except OSError as exc:
        raise BuildError("precheck", f"cannot check free disk space in {build_dir}: {exc}") from exc
    if free < MIN_FREE_DISK_BYTES:
        raise BuildError(
            "precheck",
            f"only {free} bytes free in {build_dir}; at least {MIN_FREE_DISK_BYTES} are required")


def _run_stage(stage, func, *args, **kwargs):
    """Run one build stage, tagging any failure with its stage name.

    A :class:`BuildError` raised inside the stage (for example the
    aggregated render failures) already carries its stage and passes
    through unchanged.
    """
    try:
        return func(*args, **kwargs)
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError(stage, str(exc) or exc.__class__.__name__) from exc


def _build_app_and_render(root, manifest):
    # Loading user code and running its generators are intentional build steps.
    from .app import App
    app = App(root, manifest)
    rendered = asyncio.run(_render_static_pages(app))
    return app, rendered


def _validate_unchanged(root, manifest):
    if discover(root) != manifest:
        raise ValueError("Application sources changed during build; retry the build")


def _write_build_assets(root, manifest, rendered, app):
    manifest["static_pages"] = {}
    for path, (pattern, body) in rendered.items():
        digest = hashlib.sha256(body).hexdigest()
        _atomic_write(root / ".pynext" / "static" / f"{digest}.html", body)
        manifest["static_pages"][path] = {"pattern": pattern, "sha256": digest}
    manifest["assets"] = {}
    for path, (body, media_type) in app.public_assets.items():
        digest = hashlib.sha256(body).hexdigest()
        _atomic_write(root / ".pynext" / "static" / f"{digest}.asset", body)
        manifest["assets"][path] = {"sha256": digest, "content_type": media_type}
    for path, body in app.client_assets.items():
        digest = path.rsplit("/", 1)[1].removesuffix(".js")
        _atomic_write(root / ".pynext" / "static" / f"{digest}.js", body)


def build_id_for(manifest_bytes):
    """The BUILD_ID for a manifest: its SHA-256.

    A manifest from an interrupted build (written but never completed)
    has no matching BUILD_ID, so it cannot pass as a finished build.
    """
    return hashlib.sha256(manifest_bytes).hexdigest()


def _write_build_id(root, manifest_bytes):
    _atomic_write(root / BUILD_ID_FILE, (build_id_for(manifest_bytes) + "\n").encode("utf-8"))


_DIGEST_FILE = re.compile(r"[0-9a-f]{64}\.(?:html|asset|js)")


def _referenced_static_files(manifest):
    """Digest files the manifest points at."""
    files = {f"{entry['sha256']}.html" for entry in manifest["static_pages"].values()}
    files.update(f"{entry['sha256']}.asset" for entry in manifest["assets"].values())
    client = manifest["client"]
    files.update(f"{digest}.js" for digest in
                 {client["runtime"], *client["bundles"].values()} if digest is not None)
    return files


def _prune_orphans(root, manifest):
    """Delete digest files a previous build left behind that the new
    manifest no longer references (orphan accumulation).

    Best-effort housekeeping: a failure to list or delete never fails
    the build — the files are content-addressed and harmless. Only
    files matching the digest naming scheme are touched.
    """
    static_dir = root / ".pynext" / "static"
    try:
        entries = list(static_dir.iterdir())
    except OSError as exc:
        log.warning("Build prune skipped: cannot list %s: %s", static_dir, exc)
        return 0
    referenced = _referenced_static_files(manifest)
    pruned = 0
    for entry in entries:
        if not entry.is_file() or _DIGEST_FILE.fullmatch(entry.name) is None or entry.name in referenced:
            continue
        try:
            entry.unlink()
            pruned += 1
        except OSError as exc:
            log.warning("Build prune skipped %s: %s", entry, exc)
    return pruned


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


def write_manifest(root, *, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Build the project: discover, render static pages, write assets,
    the manifest, and finally BUILD_ID — each as a separate stage.

    Stages: ``precheck`` → ``discover`` → ``render`` → ``validate`` →
    ``write-assets`` → ``write-manifest`` → ``write-build-id`` →
    ``prune``. A stage failure raises :class:`BuildError` naming the
    stage, and nothing after the failed stage runs — in particular a
    failed build never writes a manifest or a BUILD_ID, so it can
    never look like a completed build. ``BUILD_ID`` is written last
    (Next.js records its build id last for the same reason) and holds
    the SHA-256 of the manifest bytes; ``load_manifest`` checks it, so
    a manifest from an interrupted build is rejected at start.
    """
    root = Path(root).resolve()
    with BuildLock(root, timeout=lock_timeout):
        _preflight_checks(root)
        manifest = _run_stage("discover", discover, root)
        app, rendered = _run_stage("render", _build_app_and_render, root, manifest)
        _run_stage("validate", _validate_unchanged, root, manifest)
        manifest["render_modes"] = app.render_modes
        manifest["isr_policies"] = app.isr_policies
        manifest["isr_queries"] = app.isr_queries
        manifest["client"] = app.client_manifest
        _run_stage("write-assets", _write_build_assets, root, manifest, rendered, app)
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _run_stage("write-manifest", _atomic_write, root / MANIFEST, manifest_bytes)
        _run_stage("write-build-id", _write_build_id, root, manifest_bytes)
        _run_stage("prune", _prune_orphans, root, manifest)
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
    """Render every static page, isolating failures per page.

    One page failing used to abort the whole build at the first error,
    so a build with two broken pages only ever reported the first.
    Now each page (and each pattern's generator) is attempted, the
    failures are collected, and the build fails at the end listing
    every failed page. The build still fails — a missing SSG page
    would be a 404 in production — but the report is complete and the
    good pages were all rendered.
    """
    from .app import _invoke
    from .http import StreamingResponse
    rendered = {}
    failures = []
    for pattern, generator in app.static_generators.items():
        try:
            params_list = [{}] if generator is None else await _invoke(generator)
        except Exception as exc:
            failures.append((f"{pattern} (generate_static_params)", exc))
            continue
        if not isinstance(params_list, list):
            raise ValueError(f"generate_static_params for {pattern} must return a list")
        if len(params_list) + len(rendered) > MAX_STATIC_PAGES:
            raise ValueError(f"Static page limit ({MAX_STATIC_PAGES}) exceeded")
        for params in params_list:
            path = None
            try:
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
            except Exception as exc:
                failures.append((path if path is not None else f"{pattern} {params}", exc))
    if failures:
        lines = "\n".join(f"  {label}: {exc}" for label, exc in failures)
        raise BuildError(
            "render",
            f"{len(failures)} static page(s) failed to render:\n{lines}")
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
    # BUILD_ID is written last by a successful build and holds the
    # SHA-256 of the manifest bytes: a manifest left behind by a build
    # that died between writing it and finishing (or hand-edited since)
    # has no matching BUILD_ID and must not start.
    build_id_path = root / BUILD_ID_FILE
    try:
        recorded = build_id_path.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = None
    if recorded != build_id_for(target.read_bytes()):
        raise ValueError("Incomplete build: BUILD_ID is missing or does not match the manifest; "
                         "run `pynext build` again")
    expected = discover(root)
    if {key: saved.get(key) for key in expected} != expected:
        raise ValueError("Application sources changed since build; run `pynext build` again")
    return saved
