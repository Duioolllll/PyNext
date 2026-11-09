"""Discover and validate the build inputs, then write a versioned manifest."""
import hashlib
import asyncio
import json
import os
import re
import sys
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


def write_manifest(root):
    root = Path(root).resolve()
    _check_build_paths(root)
    manifest = discover(root)
    for route, shadowers in find_unreachable_routes(manifest["routes"]).items():
        print(
            f"warning: unreachable route {route} is shadowed by "
            f"{', '.join(shadowers)} and can never match a request",
            file=sys.stderr,
        )
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
            name, kind = parsed
            if kind != "dynamic":
                raise ValueError(f"Catch-all must be the last part of the URL: {pattern}")
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
    expected = discover(root)
    if {key: saved.get(key) for key in expected} != expected:
        raise ValueError("Application sources changed since build; run `pynext build` again")
    return saved
