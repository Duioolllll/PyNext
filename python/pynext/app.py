import asyncio
import hashlib
import importlib.machinery
import importlib.util
import inspect
import json
import keyword
import logging
import re
import sys
import threading
import time
import uuid
import weakref
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes

from . import _native
from .assets import read_public_artifacts, read_public_sources
from .cache import CacheBusy, ISRCache
from .client import compile_client, read_client_artifacts
from .components import _isr_policy, _node, bind_islands
from .build import discover, load_manifest
from .http import (MAX_WS_MESSAGE, SAFE_METHODS, BadRequest, Request, Response, SecurityConfig,
                   StreamingResponse, WebSocket, WebSocketDisconnect, apply_cors_headers,
                   cors_preflight_response, new_csp_nonce, validate_csrf)
from .observability import (
    MetricsCollector,
    ObservabilityConfig,
    begin_request_context,
    configure_logging,
    get_request_id,
    queue_wait_var,
    render_ms_var,
    route_var,
    span,
)
from .ratelimit import rate_limited_response
from .static import asset_response

log = logging.getLogger("pynext")
MAX_BODY = 1_048_576


class _FreshSourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # Timestamp-based bytecode can be stale when an edited file keeps its size.
        return self.source_to_code(self.get_data(self.path), self.path)


class _ProjectSourceFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] not in _project_namespaces:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = _FreshSourceLoader(fullname, spec.loader.path)
        return spec


_project_namespaces = set()
_project_finder = _ProjectSourceFinder()
_project_lock = threading.RLock()


def _register_project(namespace):
    with _project_lock:
        if not _project_namespaces:
            sys.meta_path.insert(0, _project_finder)
        _project_namespaces.add(namespace)


def _module_part(part):
    # Route directories can contain characters such as [...slug].
    if part.isidentifier() and not keyword.iskeyword(part) and not part.startswith("_pynext_encoded_"):
        return part
    return "_pynext_encoded_" + part.encode("utf-8").hex()


def _clear_project_modules(namespace):
    with _project_lock:
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)
        _project_namespaces.discard(namespace)
        if not _project_namespaces and _project_finder in sys.meta_path:
            sys.meta_path.remove(_project_finder)


def _package(name, directory, parent=None, child=None, *, load_initializer=True):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    initializer = directory / "__init__.py"
    has_initializer = load_initializer and initializer.is_file()
    if has_initializer:
        spec = importlib.util.spec_from_file_location(
            name, initializer, submodule_search_locations=[str(directory)]
        )
    else:
        spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
        spec.submodule_search_locations = [str(directory)]
    if spec is None:
        raise ValueError(f"Cannot load package {directory}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if parent is not None:
        setattr(parent, child, module)
    try:
        if has_initializer:
            exec(compile(initializer.read_bytes(), str(initializer), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        if parent is not None and getattr(parent, child, None) is module:
            delattr(parent, child)
        raise
    return module


def _load(root, relative, namespace):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Application module escapes project root: {relative}")
    parts = path.relative_to(root).parts
    parent = sys.modules[namespace]
    name = namespace
    directory = root
    for part in parts[:-1]:
        directory /= part
        child = _module_part(part)
        name += "." + child
        parent = _package(name, directory, parent, child)
    child = _module_part(path.stem)
    name += "." + child
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load {relative}")
    # Keep the fresh loader on the spec so importlib.reload() also recompiles
    # the current source instead of reusing a timestamp-based bytecode cache.
    spec.loader = _FreshSourceLoader(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    setattr(parent, child, module)
    try:
        # Compile the current source rather than potentially stale timestamp-based pyc.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        if getattr(parent, child, None) is module:
            delattr(parent, child)
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


async def _send_response(response, send):
    if not isinstance(response, StreamingResponse):
        await send({"type": "http.response.start", "status": response.status, "headers": response.headers})
        await send({"type": "http.response.body", "body": response.body})
        return
    try:
        await send({"type": "http.response.start", "status": response.status, "headers": response.headers})
        if not response._head_only:
            while (chunk := await response._next_chunk()) is not None:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        await response.aclose()


async def _wait_for_disconnect(receive):
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        # A conforming server blocks here after the request body is complete.
        # Yield defensively if a custom ASGI receive returns extra messages.
        await asyncio.sleep(0.001)


async def _run_while_connected(work, receive):
    task = asyncio.create_task(work)
    disconnect = asyncio.create_task(_wait_for_disconnect(receive))
    try:
        done, _ = await asyncio.wait((task, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        else:
            await disconnect
    finally:
        for pending in (task, disconnect):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, disconnect, return_exceptions=True)


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


def _module_key(root, relative):
    # Casefold so the same file reached through a different case (e.g.
    # App/Page.py vs app/page.py on a case-insensitive filesystem) maps to
    # one cached module instead of being loaded twice.
    return str((root / relative).resolve()).casefold()


class App:
    def __init__(self, root, manifest, *, production=False, security=None, cache_backend="memory",
                 cache_path=None, observability=None, rate_limit=None):
        self._namespace = "_pynext_project_" + uuid.uuid4().hex
        self._release_modules = weakref.finalize(self, _clear_project_modules, self._namespace)
        if security is None:
            security = SecurityConfig()
        if not isinstance(security, SecurityConfig):
            raise TypeError("security must be a SecurityConfig")
        self.security = security
        self.observability_config = observability or ObservabilityConfig.from_env()
        self.metrics = MetricsCollector(enabled=self.observability_config.metrics)
        configure_logging(self.observability_config)
        # Per-process token-bucket limiter (spec §11, finding #49). None
        # disables it. The Axum adapter reads the same attribute in
        # server.start_native; see ratelimit.py for the trust rules.
        self.rate_limit = rate_limit
        try:
            _register_project(self._namespace)
            _package(self._namespace, Path(root).resolve(), load_initializer=False)
            self._initialize(root, manifest, production=production,
                             cache_backend=cache_backend, cache_path=cache_path)
        except BaseException:
            self._release_modules()
            raise

    def configure_observability(self, config):
        """Apply a new observability config to this app (logging + metrics)."""
        self.observability_config = config
        self.metrics.enabled = config.metrics
        configure_logging(config)
        return self

    def _initialize(self, root, manifest, *, production=False, cache_backend="memory", cache_path=None):
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.production = production
        self.router = _native.Router(list(manifest["routes"]))
        self.entries = {}
        self.websocket_entries = {}
        self.modules = {}
        self.render_modes = {}
        self.isr_policies = {}
        if cache_backend == "sqlite" and cache_path is None:
            cache_path = self.root / ".pynext" / "isr-cache.sqlite3"
        self.isr = ISRCache(cache_backend=cache_backend, cache_path=cache_path)
        self.static_generators = {}
        self.static_pages = {}
        self.public_assets = {}
        self.client_assets = {}
        self.client_manifest = {}
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
                    try:
                        policy = _isr_policy(policy["revalidate"], policy["stale"])
                    except ValueError as exc:
                        raise ValueError(f"Invalid ISR policy in {spec['entry']}: {exc}") from exc
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
                ws_handler = None
                for _, fn in inspect.getmembers(module, inspect.isfunction):
                    method = getattr(fn, "__pynext_method__", None)
                    if method and fn.__module__ == module.__name__:
                        if method in handlers:
                            raise ValueError(f"Duplicate {method} handler in {spec['entry']}")
                        _check_signature(fn, {"request", "params"}, spec["entry"])
                        handlers[method] = fn
                    if getattr(fn, "__pynext_websocket__", False) and fn.__module__ == module.__name__:
                        if ws_handler is not None:
                            raise ValueError(f"Duplicate WebSocket handler in {spec['entry']}")
                        _check_signature(fn, {"websocket", "params"}, spec["entry"])
                        ws_handler = fn
                if not handlers and ws_handler is None:
                    raise ValueError(f"{spec['entry']} has no @route handlers")
                if ws_handler is not None:
                    self.websocket_entries[pattern] = ws_handler
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
        assets = {**self.static_pages, **self.client_assets,
                  **{path: body for path, (body, _) in self.public_assets.items()}}
        self.asset_etags = {path: f'"{hashlib.sha256(body).hexdigest()}"' for path, body in assets.items()}
        for path, body in self.client_assets.items():
            self.metrics.observe_island_bundle(path, len(body))

    def _module(self, path):
        key = _module_key(self.root, path)
        if key not in self.modules:
            self.modules[key] = _load(self.root, path, self._namespace)
        return self.modules[key]

    async def revalidate(self, path):
        """Invalidate one decoded ISR path for this app instance."""
        if not isinstance(path, str):
            raise TypeError("ISR path must be a string")
        path = _decode_path(path)
        if path != "/" and path.endswith("/"):
            raise ValueError("ISR path must not have a trailing slash")
        match = self.router.matches(path)
        if (match is None or self.render_modes.get(match[0]) != "isr" or
                path in self.public_assets or path in self.static_pages or path in self.client_assets):
            raise ValueError("Path does not match an ISR page")
        return await self.isr.invalidate(path)

    async def _render_stream_chunk(self, node):
        """Serialize one streamed UI node to HTML on a worker thread."""
        bound = bind_islands(_node(node), self.client_manifest)
        payload = json.dumps(bound, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return await asyncio.to_thread(_native.render, payload)

    async def _streaming_page_response(self, pattern, chunks, layouts):
        """Wrap an async iterable of UI nodes in a StreamingResponse.

        The layout shell is rendered once with a marker in place of the page
        children, then split into a prefix and a suffix around the marker.
        The prefix is sent first, each page chunk is serialized and sent as
        it arrives, and the suffix closes the document. Layouts must render
        their children exactly once; anything else fails before headers are
        sent. Streaming is SSR-only: SSG and ISR need a complete body.
        """
        if self.render_modes.get(pattern) != "ssr":
            close = getattr(chunks, "aclose", None)
            if close is not None:
                await close()
            raise TypeError(
                f"Streaming page {pattern} must use render='ssr'; "
                f"it uses render='{self.render_modes.get(pattern)}'"
            )
        marker = "__pynext_stream_" + uuid.uuid4().hex + "__"
        shell = {"kind": "text", "value": marker}
        for layout_fn in reversed(layouts):
            shell = await _invoke(layout_fn, children=shell)
            if not isinstance(shell, dict):
                raise TypeError(
                    f"Layout for {pattern} must return a render node dict, "
                    f"got {type(shell).__name__}"
                )
        shell = bind_islands(shell, self.client_manifest)
        payload = json.dumps(shell, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        html = await asyncio.to_thread(_native.render, payload)
        parts = html.split(marker)
        if len(parts) != 2:
            raise ValueError(
                f"Layout for streaming page {pattern} must render its children exactly once"
            )
        prefix, suffix = parts
        if shell.get("tag", "").lower() == "html":
            prefix = "<!DOCTYPE html>" + prefix

        async def page_stream():
            try:
                if prefix:
                    yield prefix
                async for node in chunks:
                    html = await self._render_stream_chunk(node)
                    if html:
                        yield html
                if suffix:
                    yield suffix
            finally:
                close = getattr(chunks, "aclose", None)
                if close is not None:
                    await close()

        return StreamingResponse(page_stream(), media_type="text/html; charset=utf-8")

    async def render_page(self, pattern, request, params, *, csp_nonce=True):
        fn, layouts = self.entries[pattern]
        if inspect.isasyncgenfunction(fn):
            # Calling an async generator function only creates the iterator;
            # its body runs when the stream is first pulled. Call it directly
            # instead of _invoke, which would run it on a worker thread.
            kwargs = {key: value for key, value in (("request", request), ("params", params))
                      if key in inspect.signature(fn).parameters}
            result = fn(**kwargs)
        else:
            result = await _invoke(fn, request=request, params=params)
        if hasattr(result, "__aiter__"):
            return await self._streaming_page_response(pattern, result, layouts)
        tree = result
        if not isinstance(tree, dict):
            raise TypeError(f"Page {pattern} must return a render node dict, got {type(tree).__name__}")
        for layout_fn in reversed(layouts):
            tree = await _invoke(layout_fn, children=tree)
            if not isinstance(tree, dict):
                raise TypeError(f"Layout for {pattern} must return a render node dict, got {type(tree).__name__}")
        tree = bind_islands(tree, self.client_manifest)
        payload = json.dumps(tree, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        # One fresh nonce per rendered response. Cached renders must not get
        # one: a nonce baked into shared HTML would be reused across
        # responses, which defeats the point of the nonce, so the ISR cache
        # path passes csp_nonce=False and keeps the static CSP policy.
        nonce = new_csp_nonce() if csp_nonce and self.security.csp_nonce_enabled else None
        started = time.monotonic()
        with span("pynext.render", {"http.route": pattern},
                  enabled=self.observability_config.tracing):
            html = await asyncio.to_thread(_native.render, payload, nonce)
        render_ms = (time.monotonic() - started) * 1000
        render_ms_var.set((render_ms_var.get() or 0.0) + render_ms)
        if isinstance(tree, dict) and tree.get("tag", "").lower() == "html":
            html = "<!DOCTYPE html>" + html
        return Response(html, media_type="text/html; charset=utf-8", csp_nonce=nonce)

    async def dispatch(self, request):
        """Return a response. Exceptions are converted at the transport boundary."""
        preflight = cors_preflight_response(request, config=self.security)
        if preflight is not None:
            return preflight
        response = await self._dispatch_timed(request)
        apply_cors_headers(request, response, config=self.security)
        return response

    async def _dispatch_timed(self, request):
        """Timed wrapper around :meth:`_dispatch`: records metrics + access log.

        The request id is attached to the response as ``x-request-id``. Route
        labels are patterns (never raw URLs); streaming bodies report 0 bytes
        because their total size is unknown when dispatch returns.
        """
        started = time.monotonic()
        status, response = 500, None
        tracing = self.observability_config.tracing
        with span("pynext.request", {"http.method": request.method},
                  enabled=tracing) as request_span:
            try:
                response = await self._dispatch(request)
                status = response.status
                request_id = get_request_id()
                if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
                    response.headers.append((b"x-request-id", request_id.encode("ascii")))
                return response
            except BadRequest:
                status = 400
                raise
            except Exception:
                status = 500
                raise
            finally:
                request_span.set_attribute("http.status_code", status)
                latency_ms = (time.monotonic() - started) * 1000
                route = route_var.get() or "__unmatched__"
                request_span.set_attribute("http.route", route)
                rust_ms = render_ms_var.get() or 0.0
                queue_wait_ms = queue_wait_var.get()
                response_bytes = (len(response.body) if response is not None
                                  and not isinstance(response, StreamingResponse) else 0)
                self.metrics.observe_request(
                    route, request.method, status, latency_ms,
                    response_bytes=response_bytes, queue_wait_ms=queue_wait_ms,
                    python_ms=max(0.0, latency_ms - rust_ms), rust_ms=rust_ms or None,
                )
                log.info("request", extra={
                    "method": request.method, "route": route, "status": status,
                    "latency_ms": round(latency_ms, 3),
                    "response_bytes": response_bytes,
                    "queue_wait_ms": round(queue_wait_ms, 3) if queue_wait_ms is not None else None,
                    "python_ms": round(max(0.0, latency_ms - rust_ms), 3),
                    "rust_ms": round(rust_ms, 3) if rust_ms else None,
                })

    async def _dispatch(self, request):
        """Return a response. Streaming pages return a StreamingResponse."""
        path = request.path
        if path != "/" and path.endswith("/"):
            route_var.set("__redirect__")
            location = quote(path.rstrip("/"), safe="/!$&'()*+,;=:@-._~").encode("ascii")
            if request.query_string:
                if any(c < 32 or c == 127 for c in request.query_string):
                    return Response("Bad Request", status=400)
                location += b"?" + request.query_string
            return Response("", status=308, headers=[(b"location", location)])
        if path == "/_pynext" or path.startswith("/_pynext/"):
            route_var.set("__client__")
            if path not in self.client_assets:
                return Response("Not Found", status=404)
            allow = b"GET, HEAD, OPTIONS"
            if request.method == "OPTIONS":
                return Response(b"", status=204, headers=[(b"allow", allow)], media_type=None)
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", allow)])
            return asset_response(request, self.client_assets[path], "text/javascript; charset=utf-8",
                                  self.asset_etags[path], cache_control=b"public, max-age=31536000, immutable",
                                  extra_headers=[(b"x-content-type-options", b"nosniff")])
        if path in self.public_assets:
            route_var.set("__public__")
            allow = b"GET, HEAD, OPTIONS"
            if request.method == "OPTIONS":
                return Response(b"", status=204, headers=[(b"allow", allow)], media_type=None)
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", allow)])
            body, media_type = self.public_assets[path]
            return asset_response(request, body, media_type, self.asset_etags[path],
                                  cache_control=b"public, max-age=0, must-revalidate",
                                  extra_headers=[(b"x-content-type-options", b"nosniff")])
        match = self.router.matches(path)
        if match is None:
            route_var.set("__not_found__")
            return Response("Not Found", status=404)
        pattern, raw_params = match
        route_var.set(pattern)
        params = {k: v if f"[...{k}]" in pattern else v[0] for k, v in raw_params.items()}
        request = Request(request.method, request.path, request.query_string, request.headers, request.body, params, self)
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
                response = asset_response(request, self.static_pages[path], "text/html; charset=utf-8",
                                          self.asset_etags[path], cache_control=b"public, max-age=0, must-revalidate",
                                          extra_headers=[(b"vary", b"Cookie, Authorization")])
            elif self.production and self.render_modes[pattern] == "isr":
                private = any(name.lower() in {b"authorization", b"cookie"} for name, _ in request.headers)
                if private:
                    response = await self.render_page(pattern, None, params)
                    response.headers.extend([
                        (b"cache-control", b"private, no-store"),
                        (b"vary", b"Cookie, Authorization"),
                    ])
                else:
                    # Classify the outcome before rendering: a fresh entry is
                    # a hit, a stale entry is served while one regeneration
                    # runs in the background, a miss renders synchronously.
                    cache_before = self.isr.store.lookup(path)[0]
                    self.metrics.observe_cache(cache_before)
                    try:
                        body = await self.isr.get_or_render(
                            path, self.isr_policies[pattern],
                            lambda: self.render_page(pattern, None, params, csp_nonce=False),
                        )
                    except CacheBusy:
                        self.metrics.observe_cache("busy")
                        self.metrics.observe_regeneration("failure")
                        return Response("Server Busy", status=503,
                                        headers=[(b"retry-after", b"1")])
                    except TimeoutError:
                        self.metrics.observe_regeneration("failure")
                        return Response("Gateway Timeout", status=504)
                    if cache_before == "miss":
                        self.metrics.observe_regeneration("success")
                    elif cache_before == "stale":
                        self.metrics.observe_regeneration("started")
                    response = Response(body, media_type="text/html; charset=utf-8",
                                        headers=[
                                            (b"cache-control", b"public, max-age=0, must-revalidate"),
                                            (b"vary", b"Cookie, Authorization"),
                                        ])
            else:
                response = await self.render_page(pattern, request, params)
        else:
            method = "GET" if request.method == "HEAD" and "HEAD" not in entry else request.method
            if (method not in SAFE_METHODS
                    and (self.security.csrf_enabled
                         or getattr(entry[method], "__pynext_csrf__", False))
                    and not validate_csrf(request, config=self.security)):
                return Response("Forbidden", status=403)
            response = await _invoke(entry[method], request=request, params=params)
            if not isinstance(response, Response):
                raise TypeError("API handlers must return a Response")
        if request.method == "HEAD":
            if isinstance(response, StreamingResponse):
                await response.aclose()
                response._head_only = True
            response.body = b""
        return response

    @property
    def websocket_routes(self):
        """URL patterns that export a WebSocket handler (for the Axum adapter)."""
        return frozenset(self.websocket_entries)

    async def _handle_websocket(self, scope, receive, send):
        try:
            path = _decode_path(scope["path"], scope.get("raw_path"))
        except (ValueError, UnicodeError):
            await send({"type": "websocket.close", "code": 1008})
            return
        match = self.router.matches(path)
        handler = None
        params = {}
        if match is not None:
            pattern, raw_params = match
            handler = self.websocket_entries.get(pattern)
            if handler is not None:
                params = {k: v if f"[...{k}]" in pattern else v[0] for k, v in raw_params.items()}
        if handler is None:
            # No route, or a route with no WebSocket handler: refuse the
            # handshake with a policy-violation close instead of hanging.
            await send({"type": "websocket.close", "code": 1008})
            return
        try:
            first = await receive()
        except Exception:
            return
        if first.get("type") == "websocket.disconnect":
            return
        if first.get("type") != "websocket.connect":
            await send({"type": "websocket.close", "code": 1008})
            return
        state = {"accepted": False, "closed": False, "peer_gone": False}

        async def asgi_receive():
            while True:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    state["peer_gone"] = True
                    raise WebSocketDisconnect(message.get("code", 1000), message.get("reason", ""))
                if message["type"] != "websocket.receive":
                    continue
                if message.get("text") is not None:
                    data = message["text"]
                    if len(data.encode("utf-8")) > MAX_WS_MESSAGE:
                        await asgi_send(("close", 1009, "message too large"))
                        raise WebSocketDisconnect(1009, "message too large")
                    return ("text", data)
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    if len(data) > MAX_WS_MESSAGE:
                        await asgi_send(("close", 1009, "message too large"))
                        raise WebSocketDisconnect(1009, "message too large")
                    return ("bytes", bytes(data))

        async def asgi_send(item):
            kind = item[0]
            if kind == "accept":
                if state["accepted"]:
                    raise RuntimeError("WebSocket is already accepted")
                state["accepted"] = True
                await send({"type": "websocket.accept"})
            elif kind == "text":
                await send({"type": "websocket.send", "text": item[1]})
            elif kind == "bytes":
                await send({"type": "websocket.send", "bytes": item[1]})
            elif kind == "close":
                if state["closed"]:
                    return
                state["closed"] = True
                await send({"type": "websocket.close", "code": item[1], "reason": item[2]})

        ws = WebSocket(receive=asgi_receive, send=asgi_send, path=path, params=params,
                       headers=tuple(scope.get("headers", [])),
                       query_string=scope.get("query_string", b""))
        try:
            await _invoke(handler, websocket=ws, params=params)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("WebSocket handler failed")
            if state["accepted"] and not state["closed"] and not state["peer_gone"]:
                try:
                    await ws.close(1011)
                except Exception:
                    pass
        finally:
            if not state["closed"] and not state["peer_gone"]:
                try:
                    await send({"type": "websocket.close", "code": 1000})
                except Exception:
                    pass

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    try:
                        self.isr.open()
                    except Exception as exc:
                        # ASGI requires startup.failed instead of a bare
                        # exception so the server can shut the app down.
                        await send({"type": "lifespan.startup.failed",
                                    "message": str(exc) or exc.__class__.__name__})
                        return
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    try:
                        await self.isr.close()
                    except Exception as exc:
                        await send({"type": "lifespan.shutdown.failed",
                                    "message": str(exc) or exc.__class__.__name__})
                        return
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            if "path" not in scope:
                # A websocket scope without a path cannot be routed; reject
                # it the same way as an unknown scope type, before receiving.
                raise RuntimeError(f"Unsupported ASGI scope: {scope['type']}")
            await self._handle_websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"Unsupported ASGI scope: {scope['type']}")
        _, _reset_request_context = begin_request_context(scope.get("headers", []))
        try:
            await self._handle_http(scope, receive, send)
        finally:
            _reset_request_context()

    async def _handle_http(self, scope, receive, send):
        if self.rate_limit is not None:
            # Check before reading the body: an over-limit request is
            # rejected without handler execution or body intake.
            client = scope.get("client")
            client_ip = client[0] if client else None
            key = self.rate_limit.key_for(client_ip, scope.get("headers", []))
            allowed, retry_after = self.rate_limit.allow(key)
            if not allowed:
                response = rate_limited_response(retry_after)
                if scope["method"].upper() == "HEAD":
                    response.body = b""
                await _send_response(response, send)
                return
        request = None
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
                        break
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
                response = Response("Bad Request", status=400)
            except Exception:
                log.exception("Request failed")
                response = Response("Internal Server Error", status=500)
        if request is not None:
            async def dispatch_and_send():
                try:
                    result = await self.dispatch(request)
                except BadRequest as exc:
                    log.info("Bad request: %s", exc)
                    result = Response("Bad Request", status=400)
                except Exception:
                    log.exception("Request failed")
                    result = Response("Internal Server Error", status=500)
                if scope["method"].upper() == "HEAD":
                    result.body = b""
                await _send_response(result, send)

            await _run_while_connected(dispatch_and_send(), receive)
            return
        if scope["method"].upper() == "HEAD":
            response.body = b""
        request_id = get_request_id()
        if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
            response.headers.append((b"x-request-id", request_id.encode("ascii")))
        await _send_response(response, send)


def create_app(root=".", *, production=False, security=None, cache_backend="memory", cache_path=None,
               observability=None, rate_limit=None):
    root = Path(root).resolve()
    return App(root, load_manifest(root) if production else discover(root), production=production,
               security=security, cache_backend=cache_backend, cache_path=cache_path,
               observability=observability, rate_limit=rate_limit)
