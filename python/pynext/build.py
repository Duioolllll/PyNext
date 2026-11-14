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
import inspect
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import _native
from .assets import discover_public
from .catchall import normalize_catch_all_routes, verify_static_enumeration
from .client import discover_client
from .dynamic import StaticGenBailoutError, is_prerender_bailout
from .env import load_env

log = logging.getLogger("pynext")
from .sortable_routes import sort_routes
from .client import PREFIX as CLIENT_PREFIX
from .client import discover_client, read_client_artifacts
from .telemetry import record_build_failed

MANIFEST = ".pynext/manifest.json"
BUILD_ID_FILE = ".pynext/BUILD_ID"
LOCK_FILE = ".pynext/.build.lock"
# A build ID is 16 lowercase hex chars: short enough for a header value
# and an asset URL, long enough to be unique per source set.
BUILD_ID_LENGTH = 16
MAX_DEPLOYMENT_ID_LENGTH = 128
BUILD_REPORT = ".pynext/build-report.json"
BUILD_REPORT_VERSION = 1
DIAGNOSTICS_FILE = ".pynext/diagnostics.json"
# Anonymous build-failure events kept in diagnostics.json (local only, no
# transmission). Each entry is {event, stage, duration_ms, timestamp} —
# never user code, filesystem paths or error messages.
MAX_DIAGNOSTIC_EVENTS = 50
VERSION = 10
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
# Byte offset of the Windows byte-range lock inside the lock file.
# The lock must NOT cover any byte a read of the file can touch: on
# Windows ``msvcrt.locking`` blocks reads of the locked range from any
# other handle, and the tests (and the lock-timeout path) read the
# holder's PID out of this file while the lock is held — locking byte 0
# made ``read_text()`` raise Permission denied on Windows. The PID is
# a few bytes, but reads happen in 8 KiB chunks, so the lock byte sits
# 1 MiB in: far beyond the PID text and beyond any single read chunk,
# so no read can overlap it under either lock-check reading. The
# offset is fixed: a PID-dependent offset would let two processes lock
# different bytes and break mutual exclusion.
_LOCK_BYTE_OFFSET = 1 << 20

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
    """Take an exclusive, non-blocking lock on an open file. True when taken.

    The lock covers the single byte at :data:`_LOCK_BYTE_OFFSET`: on
    Windows ``msvcrt.locking`` locks the range starting at the current
    file position (unlike POSIX ``flock``, which locks the whole file),
    so the position is pinned first — otherwise the lock and the later
    unlock would target different bytes and the unlock would fail.
    The offset sits past the PID text written by :class:`BuildLock`,
    so the PID stays readable while the lock is held.
    """
    file.seek(_LOCK_BYTE_OFFSET)
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
    """Release the lock taken by :func:`_flock_nonblocking`.

    Seeks to :data:`_LOCK_BYTE_OFFSET` first, for the same reason as
    the lock: the unlock must target the byte the lock was taken on.
    Unlocking the range at the end of the written pid (where the
    position sits after ``__enter__``) raises Permission denied on
    Windows, because that range was never locked.
    """
    file.seek(_LOCK_BYTE_OFFSET)
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
    through unchanged, as does a :class:`StaticGenBailoutError`: a
    prerender bailout stays distinguishable from a page bug (see
    :func:`is_prerender_bailout`) instead of being wrapped.
    """
    try:
        return func(*args, **kwargs)
    except (BuildError, StaticGenBailoutError):
        raise
    except Exception as exc:
        raise BuildError(stage, str(exc) or exc.__class__.__name__) from exc


def _build_app_and_render(root, manifest, *, _on_pattern_error=None):
    """Build the App and render static pages, incrementally.

    Returns ``(app, rendered, reused, reused_bytes)``: the App, freshly
    rendered ``{path: (pattern, body)}``, reused ``{path: manifest
    entry}`` from the previous build, and the total bytes of the reused
    pages. A first build (no previous manifest) renders everything.
    """
    # Loading user code and running its generators are intentional build steps.
    from .app import App
    app = App(root, manifest)
    previous = _load_previous_manifest(root)
    rendered, reused, reused_bytes = _render_incremental(
        app, manifest, previous, _on_pattern_error=_on_pattern_error)
    return app, rendered, reused, reused_bytes


def _validate_unchanged(root, manifest):
    if discover(root) != manifest:
        raise ValueError("Application sources changed during build; retry the build")


def _write_build_assets(root, manifest, rendered, app, reused=None):
    # Reused pages keep their previous manifest entries (their digest
    # files are already on disk, verified by _render_incremental).
    manifest["static_pages"] = dict(reused) if reused else {}
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

# Build stages, in the order they run. A full build completes both.
STAGES = ("compile", "generate")
# Keys of a manifest that describe the discovered sources. Two builds agree
# on these exactly when nothing relevant changed between them.
_DISCOVER_KEYS = ("schema_version", "routes", "sources", "public_sources", "client_sources")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_build_id(manifest):
    """Deterministic build ID from the manifest's source hashes.

    SHA-256 over the canonical JSON of every source hash (app, public and
    island sources), first 16 hex chars. Same sources give the same ID,
    so a rebuild is reproducible; any source change gives a new ID.
    Like Next.js's ``generateBuildId`` avoiding ``/ad/i``, an ID
    containing ``ad`` (case-insensitive) is re-hashed with a counter
    suffix, so an ad blocker never matches the ID inside an asset URL.
    """
    inputs = {key: manifest.get(key, {}) for key in ("sources", "public_sources", "client_sources")}
    counter = 0
    while True:
        payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if counter:
            payload += f"\0{counter}"
        candidate = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:BUILD_ID_LENGTH]
        if "ad" not in candidate.lower():
            return candidate
        counter += 1


def _validate_deployment_id(value):
    """True when ``value`` is a usable deployment ID.

    1-128 printable ASCII characters without whitespace, so it is safe
    in a header value, in JSON and on a log line.
    """
    return (isinstance(value, str) and 0 < len(value) <= MAX_DEPLOYMENT_ID_LENGTH
            and all(33 <= ord(c) <= 126 for c in value))


def resolve_deployment_id(explicit=None):
    """Resolve the deployment ID: explicit value, ``PYNEXT_DEPLOYMENT_ID``, else None.

    A deployment ID is an operator-chosen label (for example a rolling
    deploy's replica-set name) that names the deployment a build belongs
    to, separate from the build ID that names the build's sources.
    """
    value = explicit if explicit is not None else os.environ.get("PYNEXT_DEPLOYMENT_ID")
    if value is None:
        return None
    if not _validate_deployment_id(value):
        raise ValueError(
            "Deployment ID must be 1-128 printable ASCII characters without whitespace")
    return value


def read_build_id(root):
    """Return the build ID recorded by the last successful build, else None.

    The ID lives in the build manifest (``manifest["build_id"]``); the
    ``.pynext/BUILD_ID`` file holds the manifest-integrity seal (the
    SHA-256 of the manifest bytes), not the build ID.
    """
    target = Path(root).resolve() / MANIFEST
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    value = saved.get("build_id")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % BUILD_ID_LENGTH, value):
        return None
    return value


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
        for name in dirs:
            if name.startswith("@"):
                raise ValueError(
                    f"Parallel routes (@slot) are not supported: {Path(current) / name}; "
                    "rename the directory without the @ prefix or remove it — "
                    f"a @slot directory would otherwise silently become a literal /{name} "
                    "URL segment")
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
        if path.name == "default.py":
            raise ValueError(
                f"Parallel routes are not supported: {path}; a default.py slot "
                "fallback has no effect, remove the file")
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
                    [("file", path.relative_to(root).as_posix()), ("segment", part)],
                    ["Rename the directory to a valid route group like (name)",
                     "Remove the parentheses to make it a literal URL segment"])) from exc
            if group is None:
                parts.append(part)
        pattern = "/" + "/".join(parts)
        if pattern in routes:
            entry = path.relative_to(root).as_posix()
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
        layouts = [(d / "layout.py").relative_to(root).as_posix() for d in dirs if (d / "layout.py").is_file()]
        # Segment error boundaries (error.py / not-found.py / loading.py):
        # the nearest file walking up from the route directory wins, like
        # layouts. Only pages get boundaries; API routes keep the plain
        # framework error responses.
        if path.stem == "page":
            boundaries = {key: _nearest_boundary(dirs, filename, root) for key, filename in
                          (("error", "error.py"), ("not_found", "not-found.py"),
                           ("loading", "loading.py"))}
        else:
            boundaries = {"error": None, "not_found": None, "loading": None}
        routes[pattern] = {"entry": path.relative_to(root).as_posix(), "kind": path.stem,
                           "layouts": layouts if path.stem == "page" else [], **boundaries}
    if not routes:
        raise ValueError(_config_error(
            "No page.py or route.py files found",
            [("app directory", "app")],
            ["Add an app/page.py file with a @page() Page function",
             "Add an app/route.py file with @route handlers"]))
    router = validate_routes(routes)
    # Global boundaries live at the app root. They are also the nearest
    # boundary for every route (the walk above includes the app directory),
    # but route misses have no route, so the global not-found needs its
    # own top-level entry; the global error is the last-resort fallback
    # when a nearer boundary fails to render.
    global_error = _nearest_boundary([app_dir], "error.py", root)
    global_not_found = _nearest_boundary([app_dir], "not-found.py", root)
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
    # The manifest lists routes in specificity order. The trie already
    # produces that order (Router.ordered_routes), but sorting here through
    # the shared comparator (pynext.sortable_routes, translated from
    # Next.js sortable-routes.ts) makes the manifest order an explicit,
    # documented rule instead of an emergent property of the trie; the
    # property test in tests/test_sortable_routes.py pins the two equal.
    return {"schema_version": VERSION, "routes": {p: routes[p] for p in sort_routes(router.ordered_routes())},
            "error": global_error, "not_found": global_not_found,
            "sources": {p.relative_to(root).as_posix(): _hash(p) for p in files},
            "public_sources": public_sources, "client_sources": discover_client(root)}


# Fix guidance for the router's error messages, keyed by the message
# fragment the Rust router reports. Wording follows Next.js
# build/validate-app-paths.ts and shared/lib/router/utils/sorted-routes.ts
# so users can search for the error text.
_ROUTE_FIX_HINTS = (
    ("Different slug names for the same dynamic path",
     "Use the same parameter name for the same dynamic path in every route "
     "(for example, use [id] in all routes under /blog instead of mixing [id] and [slug])."),
    ("Catch-all must be the last part of the URL",
     "Move the [...name] segment to the end of the route."),
    ("Cannot combine a catch-all and an optional catch-all",
     "Use either [...name] or [[...name]] at the same level, not both."),
    ("Duplicate route",
     "Remove one of the page.py/route.py files that map to this path."),
    ("Repeated or ambiguous parameter name",
     "Rename one of the parameters so each name appears only once in the route."),
    ("Optional catch-all must use [[...name]]",
     "Write the optional catch-all as [[...name]]."),
    ("Optional or malformed route parameters are not supported",
     "Write an optional catch-all as [[...name]]; a single optional parameter [[name]] "
     "is not supported."),
    ("Invalid parameter name",
     "Use only letters, digits, _ and - in parameter names."),
    ("Malformed route segment",
     "Write dynamic segments as [name], [...name] or [[...name]]."),
    ("Route must be an absolute normalized path",
     "Route patterns must start with / and must not contain // or a trailing slash."),
    ("Invalid route path",
     "Route patterns must not contain . or .. segments or ?, #, \\ or NUL characters."),
)


def _fix_hint(message):
    for fragment, hint in _ROUTE_FIX_HINTS:
        if fragment in message:
            return hint
    return "Fix the route pattern and rebuild."


def _validate_route_pattern(pattern):
    """Python-side checks whose messages the Rust router cannot give.

    The Rust router rejects these too, but only with a generic message;
    here the cause is named the way Next.js names it.
    """
    if pattern == "/":
        return
    for segment in pattern.strip("/").split("/"):
        if "…" in segment:
            raise ValueError(
                f"Detected a three-dot character ('…') in the route segment {segment!r} "
                f"of {pattern}: did you mean ('...')?")
        # "[...rest]" is a catch-all, not a name starting with a period:
        # only "[.id]" / "[..id]" spellings are erroneous periods.
        if segment.startswith("[.") and not segment.startswith("[..."):
            raise ValueError(
                f"Segment names may not start with erroneous periods ('.') in {pattern}: "
                f"rename the parameter in {segment!r}.")


def validate_routes(routes):
    """Build-time route validation (Next.js validate-app-paths.ts layer).

    ``routes`` maps each pattern to its spec (with ``"entry"``, the source
    file relative to the project root). Every failure names the pattern,
    the file it came from and how to fix it, so a bad route fails the
    build with an actionable message instead of a bare router error.
    Returns the built native router.
    """
    for pattern, spec in routes.items():
        try:
            _validate_route_pattern(pattern)
        except ValueError as exc:
            raise ValueError(
                f"Invalid route {pattern} (from {Path(spec['entry']).as_posix()}): {exc}"
            ) from exc
    try:
        return _native.Router(list(routes))
    except ValueError as exc:
        # Attribute the failure to the route that introduced it: rebuild
        # incrementally so the message can name the offending file.
        built = []
        for pattern in routes:
            try:
                _native.Router([*built, pattern])
            except ValueError as inner:
                entry = Path(routes[pattern]["entry"]).as_posix()
                raise ValueError(
                    f"Invalid route {pattern} (from {entry}): {inner}. "
                    f"Fix: {_fix_hint(str(inner))}") from inner
            built.append(pattern)
        raise



def _pynext_version():
    try:
        from importlib.metadata import version as _distribution_version
        return _distribution_version("pynext-framework")
    except Exception:
        return "unknown"


def update_build_diagnostics(root, stage, *, failed_route=None, duration_ms=None, event=None):
    """Merge one build-diagnostics record into ``.pynext/diagnostics.json``.

    Best effort: failures to read or write the file are swallowed so a
    diagnostics problem can never mask (or cause) a build failure. The
    file records only the framework version, the build stage, an optional
    failed route *pattern* and anonymous timing — never user code, file
    system paths or error messages, and nothing is transmitted anywhere.
    A successful build closes the record with ``stage="done"`` and clears
    a stale ``failed_route``; the anonymous failure-event history is kept.
    """
    root = Path(root).resolve()
    try:
        _check_build_paths(root)
    except ValueError:
        return
    path = root / DIAGNOSTICS_FILE
    data = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError, UnicodeDecodeError):
        data = {}
    updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data["pynext_version"] = _pynext_version()
    data["stage"] = stage
    data["updated_at"] = updated_at
    if failed_route is not None:
        data["failed_route"] = failed_route
    else:
        data.pop("failed_route", None)
    rounded = round(float(duration_ms), 3) if isinstance(duration_ms, (int, float)) else None
    if rounded is not None:
        data["duration_ms"] = rounded
    if event == "build_failed":
        events = data.get("events")
        if not isinstance(events, list):
            events = []
        events.append({
            "event": "build_failed",
            "stage": stage,
            "duration_ms": rounded,
            "timestamp": updated_at,
        })
        data["events"] = events[-MAX_DIAGNOSTIC_EVENTS:]
    try:
        _atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except OSError:
        pass


def write_manifest(root, deployment_id=None, *, lock_timeout=LOCK_TIMEOUT_SECONDS,
                     stages=None):
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
    a manifest from an interrupted build is rejected at start. The manifest
    also records the deterministic 16-hex ``build_id`` (source-hash prefix)
    and the optional ``deployment_id`` served as skew-protection headers.

    ``stages`` selects the build modes (Next.js ``experimentalBuildMode``
    equivalent): a subset of ``("compile", "generate")``; the default
    is both (a full build). ``compile`` (``--only-islands``) bundles the
    island modules without rendering static pages; ``generate``
    (``--only-static``) renders the static pages from an earlier
    compile without re-running esbuild. A partial build records the
    stages it completed in ``stages``; ``load_manifest`` refuses it
    so ``pynext start`` names the missing stage instead of serving
    a half-built app.

    On failure, one anonymous ``build_failed`` telemetry event is recorded
    locally (stage + duration only -- never paths, error messages, or user
    code; nothing is transmitted). The original exception still propagates.
    Build diagnostics (stage, failed route pattern, duration) are also
    merged into ``.pynext/diagnostics.json``; see :func:`update_build_diagnostics`.
    """
    root = Path(root).resolve()
    deployment_id = resolve_deployment_id(deployment_id)
    selected = _normalize_stages(stages)
    started = time.monotonic()
    tracker = _BuildStage()
    failed = {}

    def _note_failed_route(pattern):
        failed["route"] = pattern

    try:
        result = _write_manifest(root, deployment_id, lock_timeout, selected, tracker,
                                _on_pattern_error=_note_failed_route)
    except Exception:
        record_build_failed(root, tracker.stage, int((time.monotonic() - started) * 1000))
        update_build_diagnostics(root, tracker.stage, failed_route=failed.get("route"),
                                 duration_ms=(time.monotonic() - started) * 1000,
                                 event="build_failed")
        raise
    update_build_diagnostics(root, "done", duration_ms=(time.monotonic() - started) * 1000)
    return result


class _BuildStage:
    """Mutable build-stage tracker shared by write_manifest/_write_manifest."""

    def __init__(self):
        self.stage = "discover"


def _write_manifest(root, deployment_id, lock_timeout, selected, tracker,
                    _on_pattern_error=None):
    with BuildLock(root, timeout=lock_timeout):
        _preflight_checks(root)
        # Production env files are loaded before any user code runs, so static
        # rendering and island bundling both see them (Next.js loads .env
        # before loadConfig in the same way).
        loaded = load_env(root, mode="production")
        if selected != list(STAGES):
            if selected == ["compile"]:
                return _write_compile_only_manifest(root, deployment_id)
            return _write_generate_only_manifest(root, deployment_id)
        manifest = _run_stage("discover", discover, root)
        for route, shadowers in find_unreachable_routes(manifest["routes"]).items():
            print(
                f"warning: unreachable route {route} is shadowed by "
                f"{', '.join(shadowers)} and can never match a request",
                file=sys.stderr,
            )
        tracker.stage = "render"
        build_started = time.monotonic()
        app, rendered, reused, reused_bytes = _run_stage(
            "render", _build_app_and_render, root, manifest,
            _on_pattern_error=_on_pattern_error)
        isr_static_paths = _run_stage("isr-warmup", lambda: asyncio.run(_enumerate_isr_static_paths(app)))
        _run_stage("validate", _validate_unchanged, root, manifest)
        # Catch-all normalization over the enumerated static paths (reduced port
        # of Next.js normalizeCatchAllRoutes): report which static pages a
        # catch-all route would also match and which catch-all routes serve only
        # paths generate_static_params did not enumerate, and fail the build if
        # the enumeration disagrees with the router about who owns a path.
        # Only page routes take part; API routes do not enumerate static paths.
        page_patterns = [pattern for pattern, spec in manifest["routes"].items()
                         if spec["kind"] == "page"]
        static_paths = {**{path: entry["pattern"] for path, entry in reused.items()},
                        **{path: pattern for path, (pattern, _body) in rendered.items()}}
        verify_static_enumeration(page_patterns, static_paths)
        manifest["catch_all_coverage"] = normalize_catch_all_routes(
            page_patterns, static_paths).to_json()
        manifest["render_modes"] = app.render_modes
        manifest["isr_policies"] = app.isr_policies
        manifest["layout_policies"] = app.layout_policies
        manifest["isr_queries"] = app.isr_queries
        manifest["ssg_fallback"] = app.ssg_fallback
        manifest["isr_static_paths"] = isr_static_paths
        manifest["dynamic_modes"] = app.dynamic_modes
        manifest["client"] = app.client_manifest
        # Prerender report: dynamic accesses recorded while rendering SSG pages
        # (empty when every static page stayed static). ISR accesses recorded
        # later at runtime are not part of the build manifest.
        manifest["dynamic_accesses"] = {pattern: accesses for pattern, accesses in app.dynamic_accesses.items()
                                        if accesses}
        # Accesses dropped by the 32-access cap (A1-L4): the stored
        # entries alone would hide how much was lost.
        manifest["dynamic_accesses_dropped"] = {pattern: count for pattern, count in app.dynamic_accesses_dropped.items()
                                                if count}
        manifest["isr_tags"] = app.isr_tags
        # Only the file names and the public-variable keys are recorded, never
        # values: secrets stay out of the manifest, and the public keys show
        # which variables were inlined into the island bundles.
        manifest["env_files"] = list(loaded.files)
        manifest["public_env_keys"] = sorted(loaded.public_values)
        # The build ID names this build's sources (deterministic, so a
        # rebuild of unchanged sources keeps the ID); the deployment ID
        # names the deployment the build belongs to, when one is configured.
        # Both are recorded after the "sources changed during build" check,
        # so the check still compares plain discover() output — and before
        # the manifest bytes are rendered, so the BUILD_ID seal covers them.
        manifest["stages"] = list(STAGES)
        manifest["build_id"] = generate_build_id(manifest)
        manifest["deployment_id"] = deployment_id
        tracker.stage = "write"
        _run_stage("write-assets", _write_build_assets, root, manifest, rendered, app, reused)
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _run_stage("write-manifest", _atomic_write, root / MANIFEST, manifest_bytes)
        _run_stage("write-build-id", _write_build_id, root, manifest_bytes)
        static_bytes = reused_bytes + sum(len(body) for _pattern, body in rendered.values())
        _write_build_report(root, manifest, app, rendered, reused, static_bytes,
                            time.monotonic() - build_started)
        _run_stage("prune", _prune_orphans, root, manifest)
        return manifest


def _nearest_boundary(dirs, filename, root):
    """Nearest ``filename`` walking up from the route directory, else None.

    ``dirs`` runs from the app directory down to the route directory, so
    the reversed walk finds the closest boundary first. Returns the path
    relative to the project root, like the ``layouts`` entries.
    """
    for directory in reversed(dirs):
        candidate = directory / filename
        if candidate.is_file():
            return candidate.relative_to(root).as_posix()
    return None


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


def _finish_manifest(root, manifest, app, rendered, *, previous=None, deployment_id=None):
    """Fill a discovered manifest with build outputs and write it.

    ``rendered`` is the ``_render_static_pages`` result (or None for a
    compile-only build, which reuses the previous static pages). ``previous``
    is the manifest of an earlier build whose static pages/assets may be
    reused; it is ignored when ``rendered`` is given. The manifest records
    the same policy keys a full build records, so a production app can
    start from it, plus the deterministic ``build_id``, the
    ``deployment_id`` and the BUILD_ID seal ``load_manifest`` checks.
    """
    manifest["render_modes"] = app.render_modes
    manifest["isr_policies"] = app.isr_policies
    manifest["layout_policies"] = app.layout_policies
    manifest["isr_queries"] = app.isr_queries
    manifest["ssg_fallback"] = app.ssg_fallback
    manifest["dynamic_modes"] = app.dynamic_modes
    manifest["isr_tags"] = app.isr_tags
    manifest["client"] = app.client_manifest
    # Prerender report, as in a full build (empty when every static page
    # stayed static). ISR accesses recorded later at runtime are not part
    # of the build manifest.
    manifest["dynamic_accesses"] = {pattern: accesses for pattern, accesses in app.dynamic_accesses.items()
                                    if accesses}
    manifest["dynamic_accesses_dropped"] = {pattern: count for pattern, count in app.dynamic_accesses_dropped.items()
                                            if count}
    manifest["isr_static_paths"] = asyncio.run(_enumerate_isr_static_paths(app))
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
    manifest["build_id"] = generate_build_id(manifest)
    manifest["deployment_id"] = deployment_id
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(root / MANIFEST, manifest_bytes)
    _write_build_id(root, manifest_bytes)
    return manifest


def _write_compile_only_manifest(root, deployment_id=None):
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
    _validate_unchanged(root, manifest)
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
    return _finish_manifest(root, manifest, app, None, previous=previous,
                            deployment_id=deployment_id)


def _write_generate_only_manifest(root, deployment_id=None):
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
    _validate_unchanged(root, manifest)
    previous_stages = previous.get("stages") or list(STAGES)
    manifest["stages"] = [stage for stage in STAGES
                          if stage == "generate" or stage in previous_stages]
    return _finish_manifest(root, manifest, app, rendered, deployment_id=deployment_id)


def _load_previous_manifest(root):
    """Return the previous build manifest, or None when there is none usable.

    A missing, unreadable, or outdated manifest is not an error: the
    build simply renders every static page from scratch.
    """
    target = root / MANIFEST
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(saved, dict) or saved.get("schema_version") != VERSION:
        return None
    return saved


def _is_page_scoped_source(relative):
    """True when a source file can only affect the pages it belongs to.

    A ``page.py``/``route.py`` entry or a ``layout.py`` under ``app/``
    only feeds the routes it is attached to. Every other source —
    ``middleware.py``, ``resources.py``, and shared helpers imported by
    pages — can be imported by any page, so a change to one of them
    invalidates every static page.
    """
    parts = Path(relative).parts
    return len(parts) >= 2 and parts[0] == "app" and parts[-1] in {"page.py", "route.py", "layout.py"}


def _page_dependencies(manifest, pattern):
    """Source files a static page is rendered from: its entry + layouts.

    The previous manifest's entry/layouts are included too, so a deleted
    layout (gone from the new manifest) still invalidates the page.
    """
    routes = manifest.get("routes") if isinstance(manifest, dict) else None
    if not isinstance(routes, dict):
        return set()
    spec = routes.get(pattern)
    if not isinstance(spec, dict):
        return set()
    deps = set()
    entry = spec.get("entry")
    if isinstance(entry, str):
        deps.add(entry)
    layouts = spec.get("layouts")
    if isinstance(layouts, list):
        deps.update(path for path in layouts if isinstance(path, str))
    return deps


def _previous_static_pages(previous):
    """Validated ``static_pages`` entries from a previous manifest."""
    if not isinstance(previous, dict):
        return {}
    pages = previous.get("static_pages")
    if not isinstance(pages, dict):
        return {}
    result = {}
    for path, entry in pages.items():
        if (isinstance(path, str) and isinstance(entry, dict) and set(entry) == {"pattern", "sha256"}
                and isinstance(entry["pattern"], str) and isinstance(entry["sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            result[path] = entry
    return result


async def _enumerate_static_patterns(app, patterns):
    """Enumerate current generator paths for the given patterns.

    Returns ``{pattern: set(paths)}`` without rendering anything.
    A pattern whose generator raises is omitted, so the caller can
    mark it stale and let the render pass report the failure instead
    of failing the build here. Force-dynamic patterns are not
    enumerated: they render nothing at build time.
    """
    enumerated = {}
    for pattern in sorted(patterns):
        if app.dynamic_modes.get(pattern) == "force-dynamic":
            continue
        generator = app.static_generators.get(pattern)
        try:
            enumerated[pattern] = set(await _materialize_params(app, pattern, generator))
        except Exception:
            continue
    return enumerated


def _render_incremental(app, manifest, previous, *, _on_pattern_error=None):
    """Render only the static pages whose inputs changed since the last build.

    Returns ``(rendered, reused, reused_bytes)``: freshly rendered
    ``{path: (pattern, body)}``, reused ``{path: manifest entry}`` from
    the previous build, and the total bytes of the reused pages. A page
    is reused only when its entry and layouts are unchanged, no shared
    source changed, the public files and island sources are unchanged
    (island digests are embedded in the HTML), and its digest file is
    still on disk with a matching hash. Anything else re-renders the
    whole pattern, which also re-renders the pattern's reused paths;
    the reused entries win and those duplicate fresh copies are
    dropped (their inputs are unchanged, so they can only reproduce
    the same body).
    """
    prev_pages = _previous_static_pages(previous)
    prev_sources = previous.get("sources", {}) if isinstance(previous, dict) else {}
    new_sources = manifest["sources"]
    changed = {rel for rel, digest in new_sources.items() if prev_sources.get(rel) != digest}
    changed |= {rel for rel in prev_sources if rel not in new_sources}
    global_change = (
        previous is None
        or any(not _is_page_scoped_source(rel) for rel in changed)
        or previous.get("public_sources") != manifest["public_sources"]
        or previous.get("client_sources") != manifest["client_sources"])
    static_dir = app.root / ".pynext" / "static"
    stale_patterns = set()
    reused = {}
    reused_sizes = {}
    reused_bytes = 0
    for path, entry in prev_pages.items():
        pattern = entry["pattern"]
        if pattern not in app.static_generators:
            continue
        if global_change:
            stale_patterns.add(pattern)
            continue
        deps = _page_dependencies(manifest, pattern) | _page_dependencies(previous, pattern)
        if deps & changed:
            stale_patterns.add(pattern)
            continue
        try:
            body = (static_dir / f"{entry['sha256']}.html").read_bytes()
        except OSError:
            stale_patterns.add(pattern)
            continue
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            stale_patterns.add(pattern)
            continue
        reused[path] = entry
        reused_sizes[path] = len(body)
        reused_bytes += len(body)
    # A generator's output may have changed since the last build even
    # when the page module did not: the data it reads (a JSON file, a
    # database, the environment) is not part of `sources`, so the
    # pattern never goes stale and its reused pages are taken at face
    # value. Re-enumerate the patterns that still have reused pages:
    # paths the generator no longer lists are pruned from the manifest
    # instead of being served forever, and newly listed paths make the
    # pattern stale so the render pass picks them up. A generator that
    # now fails is also marked stale so the render pass reports it.
    patterns_with_reused = {entry["pattern"] for entry in reused.values()}
    if patterns_with_reused:
        enumerated = asyncio.run(_enumerate_static_patterns(app, patterns_with_reused))
        for pattern in sorted(patterns_with_reused):
            current = enumerated.get(pattern)
            if current is None:
                # Generator failed (or the pattern is force-dynamic and
                # was not enumerated): re-render the pattern so the
                # render pass reports the failure; a force-dynamic
                # pattern renders nothing and keeps its reused pages.
                stale_patterns.add(pattern)
                continue
            kept = {path for path, entry in reused.items() if entry["pattern"] == pattern}
            if current - kept:
                stale_patterns.add(pattern)
            for path in kept - current:
                reused_bytes -= reused_sizes.pop(path)
                del reused[path]
    for pattern in app.static_generators:
        if not any(entry["pattern"] == pattern for entry in reused.values()):
            stale_patterns.add(pattern)
    rendered = asyncio.run(_render_static_pages(
        app, patterns=stale_patterns, existing=len(reused), _on_pattern_error=_on_pattern_error))
    # A stale pattern re-renders whole, so paths of that pattern that
    # were already verified and reused are rendered again. The reused
    # entry is authoritative: its inputs are unchanged (dependencies,
    # layouts, shared sources and digest file all verified above), so
    # the fresh render can only reproduce the same body. Drop the
    # duplicate fresh copies instead of failing the build. Genuine
    # duplicates are still caught inside _render_static_pages, which
    # rejects two parameter sets materialising to the same path, and a
    # path rendered by one pattern can never belong to another (the
    # router reachability check there rejects it), so no real collision
    # is masked here.
    for path in set(rendered) & set(reused):
        del rendered[path]
    shadowed = set(reused) & set(app.public_assets)
    if shadowed:
        raise ValueError(f"Public file shadows a generated page: {sorted(shadowed)[0]}")
    return rendered, reused, reused_bytes


def _route_counts(manifest):
    counts = {"total": len(manifest["routes"]), "ssg": 0, "isr": 0, "ssr": 0, "api": 0}
    for pattern, spec in manifest["routes"].items():
        if spec["kind"] == "route":
            counts["api"] += 1
        else:
            counts[manifest["render_modes"].get(pattern, "ssr")] += 1
    return counts


def _write_build_report(root, manifest, app, rendered, reused, static_bytes, duration):
    """Write ``.pynext/build-report.json`` summarising this build.

    The report lives next to the manifest, not inside it: durations
    vary between builds, and the manifest hash feeds the ISR cache
    namespace, so build metadata must not change the manifest.
    """
    islands = app.client_report or {"esbuild_version": None, "bundles": {}}
    report = {
        "version": BUILD_REPORT_VERSION,
        "duration_seconds": round(duration, 3),
        "routes": _route_counts(manifest),
        "static_pages": {"rendered": len(rendered), "reused": len(reused),
                         "total": len(manifest["static_pages"]), "bytes": static_bytes},
        "islands": islands,
        "public_assets": {"count": len(manifest["assets"]),
                          "bytes": sum(len(body) for body, _ in app.public_assets.values())},
    }
    _atomic_write(root / BUILD_REPORT,
                  (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_build_report(root):
    """Return the last build report, or None when there is none usable."""
    root = Path(root).resolve()
    try:
        report = json.loads((root / BUILD_REPORT).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(report, dict) or report.get("version") != BUILD_REPORT_VERSION:
        return None
    return report


def format_bytes(count):
    """Format a byte count as e.g. ``12.3 KB`` for build summaries."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024

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

    Returns (name, kind, prefix, suffix) where kind is "dynamic",
    "catch_all" or "optional", or None for a static segment. Delegates to
    ``pynext.routing`` so build-time parsing and public ``url_for()``
    share one grammar.
    """
    from .routing import _segment_param as _parse_segment

    return _parse_segment(segment)


def _route_group(part):
    """Return the group name for a `(group)` directory segment, else None.

    Route groups do not appear in the URL. A segment that looks like a
    group but is malformed (unbalanced or empty parentheses) is an error.
    """
    if not (part.startswith("(") or part.endswith(")")):
        return None
    match = re.fullmatch(r"\(([A-Za-z0-9_-]+)\)", part)
    if match is None:
        if part in ("(.)", "(..)", "(...)", "(..)(..)"):
            raise ValueError(f"Invalid route group: {part} (interception routes are not supported)")
        raise ValueError(f"Invalid route group: {part}")
    return match.group(1)


# Unreachable-route detection (REPORT.md §2.3 P2, tracks/a-routing.md
# §2.10). Adapted from Next.js `isRoutePatternCovered()` /
# `patternCoversAtLength()` / `prefixCovers()` in
# `shared/lib/router/utils/interception-routes.ts`, with one difference:
# Next.js only asks whether some ordinary route matches the same URLs,
# while PyNext must also ask whether that route *wins* the trie match
# (static > dynamic > catch-all > optional catch-all, with backtracking).
# A route is unreachable only when every path it could match is both
# matched and won by another route. Partial shadowing is normal -- e.g.
# `/docs/[...path]` loses its one-segment paths to `/docs/[slug]` but
# still serves longer paths -- and is not reported.
#
# The check is symbolic: for a fixed-length candidate there is a single
# length to test, and for an unbounded candidate (catch-all tail) the
# step kinds are uniform past both prefixes, so one unbounded stealer
# covering at max(min lengths) covers every longer length, and only the
# finite lengths below that need per-length checks.

# Trie-step kind ranks: the smaller rank wins when two routes first
# diverge on the same path. "end" is the endpoint reached when a pattern
# stops exactly at the end of the path; it beats an optional catch-all
# that would match zero further segments.
_STEP_RANK = {"static": 0, "dynamic": 1, "catch_all": 2, "optional": 3, "end": -1}


def _parse_route_pattern(pattern):
    """Split a route pattern into (prefix, tail).

    prefix is a list of ("static", literal) / ("dynamic", name) steps;
    tail is None (fixed length), "catch_all" or "optional".
    """
    segments = pattern.strip("/").split("/") if pattern != "/" else []
    tail = None
    if segments:
        last = segments[-1]
        if last.startswith("[[...") and last.endswith("]]"):
            tail = "optional"
            segments = segments[:-1]
        elif last.startswith("[...") and last.endswith("]"):
            tail = "catch_all"
            segments = segments[:-1]
    prefix = []
    for segment in segments:
        parsed = _segment_param(segment)
        if parsed is None:
            prefix.append(("static", segment))
        else:
            name, kind, seg_prefix, seg_suffix = parsed
            if kind != "dynamic":
                raise ValueError(f"Catch-all must be the last part of the URL: {pattern}")
            if seg_prefix or seg_suffix:
                # Affixed parameter (v[version], [name].json): it only
                # matches values carrying that prefix/suffix, so for
                # coverage it behaves like a distinct literal, not a
                # bare dynamic segment that matches any value.
                prefix.append(("static", segment))
            else:
                prefix.append(("dynamic", name))
    return prefix, tail


def _pattern_min_length(parsed):
    prefix, tail = parsed
    return len(prefix) + (1 if tail == "catch_all" else 0)


def _pattern_accepts_length(parsed, length):
    prefix, tail = parsed
    if tail is None:
        return length == len(prefix)
    return length >= _pattern_min_length(parsed)


def _step_kind(parsed, index):
    """Kind of the trie step a pattern takes at path position `index`.

    Returns ("static", literal), ("dynamic",), ("catch_all",) or
    ("optional",). Past the fixed prefix an unbounded pattern is in its
    tail, which consumes every remaining position.
    """
    prefix, tail = parsed
    if index < len(prefix):
        kind, value = prefix[index]
        return (kind, value) if kind == "static" else (kind,)
    if tail == "catch_all":
        return ("catch_all",)
    if tail == "optional":
        return ("optional",)
    return ("end",)


def _covers_at_length(stealer, candidate, length):
    """Whether `stealer` matches and wins every length-`length` path that
    `candidate` matches.

    The stealer must accept `length` (the caller only asks about lengths
    the candidate accepts). A static step only matches paths whose
    segment is exactly that literal, so a stealer can take a static step
    only where the candidate has the identical static step; a dynamic
    step or a tail matches any value. The winner is decided by the first
    position where the two patterns take different trie steps: the
    smaller kind rank wins. If every step agrees, an endpoint beats an
    optional catch-all that would match zero further segments.
    """
    if not _pattern_accepts_length(stealer, length):
        return False
    divergence = None
    for index in range(length):
        stealer_step = _step_kind(stealer, index)
        candidate_step = _step_kind(candidate, index)
        if stealer_step[0] == "static":
            # Only the paths with exactly this literal are matched.
            if stealer_step != candidate_step:
                return False
        elif stealer_step[0] in ("catch_all", "optional"):
            # A tail consumes the rest, so it matches any values from
            # here on; only the kind at this position can still diverge.
            if divergence is None and stealer_step[0] != candidate_step[0]:
                divergence = (stealer_step[0], candidate_step[0])
            break
        if divergence is None and stealer_step[0] != candidate_step[0]:
            divergence = (stealer_step[0], candidate_step[0])
    if divergence is not None:
        return _STEP_RANK[divergence[0]] < _STEP_RANK[divergence[1]]
    # Every step agreed. A catch-all has already consumed its segments,
    # so it counts as ended; only an optional tail whose prefix ends
    # exactly here can still match zero further segments.
    stealer_end = "optional" if stealer[1] == "optional" and len(stealer[0]) == length else "end"
    candidate_end = "optional" if candidate[1] == "optional" and len(candidate[0]) == length else "end"
    return _STEP_RANK[stealer_end] < _STEP_RANK[candidate_end]


def _shadowing_routes(pattern, others):
    """Routes that together leave `pattern` no path it can win, or [].

    `others` is a list of (route, parsed pattern) pairs.
    """
    min_length = _pattern_min_length(pattern)
    if pattern[1] is None:
        for route, other in others:
            if _covers_at_length(other, pattern, min_length):
                return [route]
        return []
    start = None
    tail_shadow = None
    for route, other in others:
        if other[1] is None:
            continue
        first_covered = max(min_length, _pattern_min_length(other))
        if _covers_at_length(other, pattern, first_covered):
            if start is None or first_covered < start:
                start, tail_shadow = first_covered, route
    if start is None:
        return []
    shadowers = [tail_shadow]
    for length in range(min_length, start):
        for route, other in others:
            if _covers_at_length(other, pattern, length):
                shadowers.append(route)
                break
        else:
            return []
    return shadowers


def find_unreachable_routes(routes):
    """Return {route: [shadowing routes]} for routes that can never match.

    `routes` is an iterable of route patterns; a manifest "routes" dict
    works because iterating it yields the patterns. A route is reported
    only when *every* path it could match is matched and won by another
    route -- partial shadowing is normal and is not reported.
    """
    parsed = {}
    for route in routes:
        parsed[route] = _parse_route_pattern(route)
    unreachable = {}
    for route, pattern in parsed.items():
        others = [(other, other_pattern) for other, other_pattern in parsed.items()
                  if other != route]
        shadowers = _shadowing_routes(pattern, others)
        if shadowers:
            unreachable[route] = sorted(shadowers)
    return unreachable


def _materialize(pattern, params, entry=None):
    segments = pattern.strip("/").split("/") if pattern != "/" else []
    # Without an entry (file) the pattern itself labels the error
    # details: _materialize is also the 2-arg helper behind
    # url_for() and the dev static-path check, which have no file.
    entry = entry if entry is not None else pattern
    keys = {}
    seen = set()
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
            name, kind, _prefix, _suffix = parsed
            # The Rust router rejects a repeated name in one route; check
            # here too so _materialize is safe on its own. Names compare
            # after stripping non-alphanumerics, like the router does.
            comparable = re.sub(r"[^A-Za-z0-9]", "", name)
            if comparable in seen:
                raise ValueError(f"Repeated or ambiguous parameter name: {name}")
            seen.add(comparable)
            keys[name] = kind
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
        name, kind, prefix, suffix = parsed
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
            output.append(prefix + part + suffix)
    return "/" + "/".join(output)

async def _materialize_params(app, pattern, generator):
    """Call a ``generate_static_params`` generator and materialize its paths.

    Shared by SSG rendering and ISR warmup enumeration so both validate
    the same way: the generator must return a list, every entry must
    materialize to a distinct reachable path, and no generated path may
    be shadowed by a public file. Raises ``ValueError`` on any violation
    so a bad generator fails the build instead of surfacing at request
    time.
    """
    from .app import _invoke
    entry = app.manifest["routes"][pattern]["entry"]
    params_list = [{}] if generator is None else await _invoke(generator)
    if not isinstance(params_list, list):
        raise ValueError(f"generate_static_params for {pattern} must return a list")
    paths = []
    seen = set()
    for params in params_list:
        path = _materialize(pattern, params, entry)
        if path in seen:
            raise ValueError(f"Duplicate generated path: {path}")
        seen.add(path)
        if path in app.public_assets:
            raise ValueError(f"Public file shadows a generated page: {path}")
        match = app.router.matches(path)
        if match is None or match[0] != pattern:
            raise ValueError(f"Generated path {path} is not reachable through {pattern}")
        paths.append(path)
    return paths


async def _enumerate_isr_static_paths(app):
    """Enumerate (but do not render) ISR ``generate_static_params`` paths.

    The paths are recorded in the manifest so ``pynext start`` can warm
    the ISR cache from them; rendering itself happens at startup, in the
    background, so a slow data source never blocks the build or the
    server from starting.
    """
    enumerated = {}
    total = 0
    for pattern, generator in app.isr_generators.items():
        if generator is None:
            continue
        paths = await _materialize_params(app, pattern, generator)
        total += len(paths)
        if total > MAX_STATIC_PAGES:
            raise ValueError(f"Static page limit ({MAX_STATIC_PAGES}) exceeded")
        enumerated[pattern] = paths
    return enumerated


async def _render_static_pages(app, patterns=None, existing=0, _on_pattern_error=None):
    """Render every static page, isolating failures per page.

    One page failing used to abort the whole build at the first error,
    so a build with two broken pages only ever reported the first.
    Now each page (and each pattern's generator) is attempted, the
    failures are collected, and the build fails at the end listing
    every failed page. The build still fails — a missing SSG page
    would be a 404 in production — but the report is complete and the
    good pages were all rendered.

    ``patterns`` restricts rendering to those patterns (incremental
    builds re-render only stale patterns); ``existing`` counts pages
    reused from a previous build, so the ``MAX_STATIC_PAGES`` cap
    covers reused + freshly rendered pages.
    """
    from .app import _invoke, _strip_client_runtime
    from .http import StreamingResponse
    rendered = {}
    failures = []
    rendered_params = {}
    for pattern, generator in app.static_generators.items():
        if patterns is not None and pattern not in patterns:
            continue
        entry = app.manifest["routes"][pattern]["entry"]
        if app.dynamic_modes.get(pattern) == "force-dynamic":
            # Never generated at build time; served dynamically instead.
            continue
        try:
            params_list = [{}] if generator is None else await _invoke(generator, _app=app)
        except Exception as exc:
            failures.append((f"{pattern} (generate_static_params)", exc))
            continue
        if not isinstance(params_list, list):
            raise ValueError(_config_error(
                f"generate_static_params for {pattern} must return a list",
                [("file", entry), ("got", f"{type(params_list).__name__}: {_short_repr(params_list)}")],
                ["Return a list of dicts, one per path (use [] when the route has no paths)"]))
        if len(params_list) + len(rendered) + existing > MAX_STATIC_PAGES:
            raise ValueError(_config_error(
                f"Static page limit ({MAX_STATIC_PAGES}) exceeded",
                [("file", entry), ("pattern", pattern),
                 ("pages", f"{len(rendered) + existing} already rendered or reused + {len(params_list)} from this route")],
                ["Return fewer paths from generate_static_params",
                 "Split the route into smaller routes"]))
        for params in params_list:
            path = None
            try:
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
                # The prelude inspection in _streaming_page_response pulls the
                # first chunk inside render_page, so for streaming pages the
                # build time limit must cover the render call too — otherwise a
                # page that never yields would stall the build past the limit
                # instead of failing it. Non-streaming pages keep their
                # previous (unbounded) render.
                fn, _layouts = app.entries[pattern]
                if inspect.isasyncgenfunction(fn):
                    try:
                        async with asyncio.timeout(MAX_STREAMED_STATIC_SECONDS):
                            response = await app.render_page(pattern, None, params)
                    except asyncio.TimeoutError as exc:
                        raise ValueError(_config_error(
                            f"Streamed static page for {path} exceeded the build time limit "
                            f"({MAX_STREAMED_STATIC_SECONDS}s)",
                            [("file", entry), ("pattern", pattern)],
                            ["Reduce the page content or make it render faster",
                             "Split the page into smaller pages"])) from exc
                else:
                    response = await app.render_page(pattern, None, params)
                if isinstance(response, StreamingResponse):
                    body = bytearray()
                    try:
                        async with asyncio.timeout(MAX_STREAMED_STATIC_SECONDS):
                            # The loading placeholder is for live clients only;
                            # it must not be baked into the static page.
                            index = 0
                            loading_index = getattr(response, "_pynext_loading_index", None)
                            while (chunk := await response._next_chunk()) is not None:
                                if index == loading_index:
                                    index += 1
                                    continue
                                index += 1
                                if len(body) + len(chunk) > MAX_STREAMED_STATIC_BYTES:
                                    raise ValueError(_config_error(
                                        f"Streamed static page {path} exceeds build size limit "
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
                    # Byte-level backstop for the prelude inspection in
                    # _streaming_page_response: a page that yields nodes which
                    # serialize to nothing (or a buffered page below that
                    # renders to nothing) would otherwise be written out as a
                    # file holding only the client runtime script tag. The tag
                    # is boilerplate, not content, so it is stripped before the
                    # emptiness test. Fail the build instead of shipping it.
                    body_text = bytes(body).decode("utf-8", "replace")
                    if not _strip_client_runtime(body_text):
                        raise ValueError(_config_error(
                            f"Static page {path} rendered an empty document",
                            [("file", entry), ("pattern", pattern)],
                            ["Make the page render some content",
                             "Remove the page if it should not exist"]))
                    rendered[path] = (pattern, bytes(body))
                else:
                    if len(response.body) > MAX_STATIC_PAGE_BYTES:
                        raise ValueError(_config_error(
                            f"Static page {path} exceeds build size limit "
                            f"({MAX_STATIC_PAGE_BYTES} bytes)",
                            [("file", entry), ("pattern", pattern)],
                            ["Split the page or reduce its content"]))
                    if not _strip_client_runtime(response.body.decode("utf-8", "replace")):
                        raise ValueError(_config_error(
                            f"Static page {path} rendered an empty document",
                            [("file", entry), ("pattern", pattern)],
                            ["Make the page render some content",
                             "Remove the page if it should not exist"]))
                    rendered[path] = (pattern, response.body)
                rendered_params[path] = params
            except Exception as exc:
                if _on_pattern_error is not None:
                    _on_pattern_error(pattern)
                failures.append((path if path is not None else f"{pattern} {params}", exc))
    if failures:
        # A build whose every failure is a prerender bailout (dynamic
        # access in dynamic="error" mode) fails with the bailout itself,
        # as before staging: callers distinguish it from a page bug via
        # is_prerender_bailout(), and the CLI still reports it cleanly
        # (StaticGenBailoutError is a ValueError). Mixed or real page
        # failures aggregate into one staged BuildError instead.
        if all(is_prerender_bailout(exc) for _label, exc in failures):
            raise failures[0][1]
        lines = "\n".join(f"  {label}: {exc}" for label, exc in failures)
        raise BuildError(
            "render",
            f"{len(failures)} static page(s) failed to render:\n{lines}") from failures[0][1]
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
        if (mode == "ssg" and "[" not in pattern and pattern not in result
                and manifest.get("dynamic_modes", {}).get(pattern) != "force-dynamic"):
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
    build_id = saved.get("build_id")
    if build_id is not None and (not isinstance(build_id, str)
                                 or not re.fullmatch(r"[0-9a-f]{%d}" % BUILD_ID_LENGTH, build_id)):
        raise ValueError("Invalid build ID in manifest; rebuild the application")
    deployment_id = saved.get("deployment_id")
    if deployment_id is not None and not _validate_deployment_id(deployment_id):
        raise ValueError("Invalid deployment ID in manifest; rebuild the application")
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
