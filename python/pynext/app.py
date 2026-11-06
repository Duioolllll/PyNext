import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes

from . import _native
from .assets import read_public_artifacts, read_public_sources
from .cache import CacheBusy, ISRCache
from .client import compile_client, read_client_artifacts
from .components import bind_islands
from .build import discover, load_manifest
from .http import BadRequest, Request, Response

log = logging.getLogger("pynext")
MAX_BODY = 1_048_576


def _load(root, relative):
    path = root / relative
    name = "_pynext_" + hashlib.sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        # Compile the current source rather than potentially stale timestamp-based pyc.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _check_signature(fn, allowed, label):
    for p in inspect.signature(fn).parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise ValueError(f"{label} must use named parameters from {sorted(allowed)}")
        if p.name not in allowed and p.default is p.empty:
            raise ValueError(f"Unsupported required parameter {p.name!r} in {label}")


async def _invoke(fn, **values):
    names = inspect.signature(fn).parameters
    kwargs = {key: value for key, value in values.items() if key in names}
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    result = await asyncio.to_thread(fn, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _decode_path(path, raw_path=None):
    if raw_path is not None:
        if re.search(br"%(?![0-9a-fA-F]{2})|%(?:2[fF]|5[cC]|00)", raw_path):
            raise ValueError("Invalid or encoded path separator")
        path = unquote_to_bytes(raw_path).decode("utf-8", errors="strict")
    if (not path.startswith("/") or "//" in path or "\\" in path or
            any(ord(c) < 32 or ord(c) == 127 for c in path) or
            any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("Invalid request path")
    return path


class App:
    def __init__(self, root, manifest, *, production=False):
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.production = production
        self.router = _native.Router(list(manifest["routes"]))
        self.entries = {}
        self.modules = {}
        self.render_modes = {}
        self.isr_policies = {}
        self.isr = ISRCache()
        self.static_generators = {}
        self.static_pages = {}
        self.public_assets = {}
        self.client_assets = {}
        self.client_manifest = {}
        # Application imports are resolved from the project's root.
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        for pattern, spec in manifest["routes"].items():
            module = self._module(spec["entry"])
            if spec["kind"] == "page":
                fn = getattr(module, "Page", None)
                if not callable(fn) or not getattr(fn, "__pynext_page__", False):
                    raise ValueError(f"{spec['entry']} must export a @page() Page function")
                _check_signature(fn, {"request", "params"}, spec["entry"])
                mode = getattr(fn, "__pynext_render__", "ssr")
                if mode not in {"ssr", "ssg", "isr"}:
                    raise ValueError(f"Unsupported render mode in {spec['entry']}")
                if mode in {"ssg", "isr"} and "request" in inspect.signature(fn).parameters:
                    raise ValueError(f"{mode.upper()} page {spec['entry']} cannot accept request")
                self.render_modes[pattern] = mode
                if mode == "isr":
                    policy = getattr(fn, "__pynext_cache_policy__", None)
                    if not isinstance(policy, dict) or set(policy) != {"revalidate", "stale"}:
                        raise ValueError(f"Invalid ISR policy in {spec['entry']}")
                    if getattr(module, "generate_static_params", None) is not None:
                        raise ValueError(f"ISR page {spec['entry']} cannot export generate_static_params yet")
                    self.isr_policies[pattern] = policy
                if mode == "ssg":
                    dynamic = any(segment.startswith("[") for segment in pattern.split("/"))
                    generator = getattr(module, "generate_static_params", None)
                    if dynamic and (not callable(generator) or inspect.signature(generator).parameters):
                        raise ValueError(f"Dynamic SSG page {spec['entry']} must export generate_static_params() with no arguments")
                    if not dynamic and generator is not None:
                        raise ValueError(f"Static SSG page {spec['entry']} cannot export generate_static_params")
                    self.static_generators[pattern] = generator
                layouts = []
                for relative in spec["layouts"]:
                    layout_fn = getattr(self._module(relative), "Layout", None)
                    if not callable(layout_fn) or not getattr(layout_fn, "__pynext_layout__", False):
                        raise ValueError(f"{relative} must export a @layout Layout function")
                    _check_signature(layout_fn, {"children"}, relative)
                    if "children" not in inspect.signature(layout_fn).parameters:
                        raise ValueError(f"{relative} must accept children")
                    layouts.append(layout_fn)
                self.entries[pattern] = (fn, layouts)
            else:
                handlers = {}
                for _, fn in inspect.getmembers(module, inspect.isfunction):
                    method = getattr(fn, "__pynext_method__", None)
                    if not method or fn.__module__ != module.__name__:
                        continue
                    if method in handlers:
                        raise ValueError(f"Duplicate {method} handler in {spec['entry']}")
                    _check_signature(fn, {"request", "params"}, spec["entry"])
                    handlers[method] = fn
                if not handlers:
                    raise ValueError(f"{spec['entry']} has no @route handlers")
                self.entries[pattern] = handlers
        if production:
            from .build import read_static_pages
            if manifest.get("render_modes") != self.render_modes:
                raise ValueError("Render modes changed since build; run `pynext build` again")
            if manifest.get("isr_policies") != self.isr_policies:
                raise ValueError("ISR policies changed since build; run `pynext build` again")
            self.client_manifest, self.client_assets = read_client_artifacts(self.root, manifest)
            self.static_pages = read_static_pages(self.root, manifest, self.router)
            self.public_assets = read_public_artifacts(self.root, manifest)
            if set(self.static_pages).intersection(self.public_assets):
                raise ValueError("A public file conflicts with a generated page; rebuild the application")
        else:
            self.public_assets = read_public_sources(self.root, manifest["public_sources"])
            self.client_manifest, self.client_assets = compile_client(self.root, manifest["client_sources"])

    def _module(self, path):
        if path not in self.modules:
            self.modules[path] = _load(self.root, path)
        return self.modules[path]

    async def render_page(self, pattern, request, params):
        fn, layouts = self.entries[pattern]
        tree = await _invoke(fn, request=request, params=params)
        if not isinstance(tree, dict):
            raise TypeError(f"Page {pattern} must return a render node dict, got {type(tree).__name__}")
        for layout_fn in reversed(layouts):
            tree = await _invoke(layout_fn, children=tree)
            if not isinstance(tree, dict):
                raise TypeError(f"Layout for {pattern} must return a render node dict, got {type(tree).__name__}")
        tree = bind_islands(tree, self.client_manifest)
        payload = json.dumps(tree, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        html = await asyncio.to_thread(_native.render, payload)
        if isinstance(tree, dict) and tree.get("tag", "").lower() == "html":
            html = "<!DOCTYPE html>" + html
        return Response(html, media_type="text/html; charset=utf-8")

    async def dispatch(self, request):
        """Return a buffered response. Exceptions are converted at the transport boundary."""
        path = request.path
        if path != "/" and path.endswith("/"):
            location = quote(path.rstrip("/"), safe="/!$&'()*+,;=:@-._~").encode("ascii")
            if request.query_string:
                if any(c < 32 or c == 127 for c in request.query_string):
                    return Response("Bad Request", status=400)
                location += b"?" + request.query_string
            return Response("", status=308, headers=[(b"location", location)])
        if path == "/_pynext" or path.startswith("/_pynext/"):
            if path not in self.client_assets:
                return Response("Not Found", status=404)
            allow = b"GET, HEAD, OPTIONS"
            if request.method == "OPTIONS":
                return Response(b"", status=204, headers=[(b"allow", allow)], media_type=None)
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", allow)])
            response = Response(self.client_assets[path], media_type="text/javascript; charset=utf-8", headers=[
                (b"cache-control", b"public, max-age=31536000, immutable"),
                (b"x-content-type-options", b"nosniff"),
            ])
            if request.method == "HEAD":
                response.body = b""
            return response
        if path in self.public_assets:
            allow = b"GET, HEAD, OPTIONS"
            if request.method == "OPTIONS":
                return Response(b"", status=204, headers=[(b"allow", allow)], media_type=None)
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", allow)])
            body, media_type = self.public_assets[path]
            response = Response(body, media_type=media_type, headers=[
                (b"cache-control", b"public, max-age=0, must-revalidate"),
                (b"x-content-type-options", b"nosniff"),
            ])
            if request.method == "HEAD":
                response.body = b""
            return response
        match = self.router.matches(path)
        if match is None:
            return Response("Not Found", status=404)
        pattern, raw_params = match
        params = {k: v if f"[...{k}]" in pattern else v[0] for k, v in raw_params.items()}
        request = Request(request.method, request.path, request.query_string, request.headers, request.body, params)
        spec = self.manifest["routes"][pattern]
        entry = self.entries[pattern]
        if self.production and self.render_modes.get(pattern) == "ssg" and path not in self.static_pages:
            return Response("Not Found", status=404)
        if spec["kind"] == "page":
            allowed = {"GET", "HEAD", "OPTIONS"}
        else:
            allowed = set(entry)
            if "GET" in allowed:
                allowed.add("HEAD")
            allowed.add("OPTIONS")
        allow = ", ".join(sorted(allowed)).encode("ascii")
        if request.method == "OPTIONS" and (spec["kind"] == "page" or "OPTIONS" not in entry):
            return Response(b"", status=204, headers=[(b"allow", allow)], media_type=None)
        if request.method not in allowed:
            return Response("Method Not Allowed", status=405, headers=[(b"allow", allow)])
        if spec["kind"] == "page":
            if self.production and self.render_modes[pattern] == "ssg":
                response = Response(self.static_pages[path], media_type="text/html; charset=utf-8",
                                    headers=[(b"vary", b"Cookie, Authorization")])
            elif self.production and self.render_modes[pattern] == "isr":
                private = any(name.lower() in {b"authorization", b"cookie"} for name, _ in request.headers)
                if private:
                    response = await self.render_page(pattern, None, params)
                    response.headers.extend([
                        (b"cache-control", b"private, no-store"),
                        (b"vary", b"Cookie, Authorization"),
                    ])
                else:
                    try:
                        body = await self.isr.get_or_render(
                            path, self.isr_policies[pattern],
                            lambda: self.render_page(pattern, None, params),
                        )
                    except CacheBusy:
                        return Response("Server Busy", status=503)
                    except TimeoutError:
                        return Response("Gateway Timeout", status=504)
                    response = Response(body, media_type="text/html; charset=utf-8",
                                        headers=[
                                            (b"cache-control", b"public, max-age=0, must-revalidate"),
                                            (b"vary", b"Cookie, Authorization"),
                                        ])
            else:
                response = await self.render_page(pattern, request, params)
        else:
            method = "GET" if request.method == "HEAD" and "HEAD" not in entry else request.method
            response = await _invoke(entry[method], request=request, params=params)
            if not isinstance(response, Response):
                raise TypeError("API handlers must return a Response")
        if request.method == "HEAD":
            response.body = b""
        return response

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self.isr.open()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.isr.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            raise RuntimeError(f"Unsupported ASGI scope: {scope['type']}")
        try:
            path = _decode_path(scope["path"], scope.get("raw_path"))
        except (ValueError, UnicodeError):
            response = Response("Bad Request", status=400)
        else:
            chunks, total = [], 0
            try:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        raise RuntimeError("Unexpected ASGI message")
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > MAX_BODY:
                        response = Response("Payload Too Large", status=413)
                        break
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        request = Request(scope["method"].upper(), path, scope.get("query_string", b""),
                                          tuple(scope.get("headers", [])), b"".join(chunks))
                        response = await self.dispatch(request)
                        break
<<<<<<< HEAD
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
=======
            except BadRequest:
>>>>>>> origin/main
                response = Response("Bad Request", status=400)
            except Exception:
                log.exception("Request failed")
                response = Response("Internal Server Error", status=500)
        if scope["method"].upper() == "HEAD":
            response.body = b""
        await send({"type": "http.response.start", "status": response.status, "headers": response.headers})
        await send({"type": "http.response.body", "body": response.body})


def create_app(root=".", *, production=False):
    root = Path(root).resolve()
    return App(root, load_manifest(root) if production else discover(root), production=production)
