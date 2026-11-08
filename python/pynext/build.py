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
VERSION = 7
MAX_STATIC_PAGES = 10_000


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
    if not app_dir.is_dir():
        raise ValueError(f"App directory not found: {app_dir}")
    files = sorted(p for p in app_dir.rglob("*.py") if "__pycache__" not in p.parts)
    for path in files:
        if not path.resolve().is_relative_to(app_dir.resolve()):
            raise ValueError(f"Python file escapes app directory: {path}")
    routes = {}
    for path in files:
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
    # Loading user code and running its generators are intentional build steps.
    from .app import App
    app = App(root, manifest)
    rendered = asyncio.run(_render_static_pages(app))
    if discover(root) != manifest:
        raise ValueError("Application sources changed during build; retry the build")
    manifest["render_modes"] = app.render_modes
    manifest["isr_policies"] = app.isr_policies
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
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pynext-", delete=False) as file:
            name = file.name
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
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
    saved = json.loads(target.read_text(encoding="utf-8"))
    if saved.get("schema_version") != VERSION:
        raise ValueError("Incompatible manifest version; rebuild the application")
    expected = discover(root)
    if {key: saved.get(key) for key in expected} != expected:
        raise ValueError("Application sources changed since build; run `pynext build` again")
    return saved
