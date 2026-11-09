import asyncio
import dataclasses
import hashlib
import html
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
import traceback
import uuid
import weakref
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, unquote_to_bytes

from . import _native
from .assets import read_public_artifacts, read_public_sources
from .auth import check_authorization
from .cache import CacheBusy, ISRCache, SharedCacheUnavailable
from .client import compile_client, read_client_artifacts
from .components import _isr_policy, _isr_query_keys, _node, bind_islands
from .build import discover, load_manifest
from .http import (MAX_WS_MESSAGE, SAFE_METHODS, BadRequest, JSONResponse, Request, Response, SecurityConfig,
                   StreamingResponse, WebSocket, WebSocketDisconnect, apply_cors_headers,
                   apply_security_headers, cors_preflight_response, new_csp_nonce, validate_csrf)
from .images import (MAX_IMAGE_QUALITY, MAX_IMAGE_WIDTH, ImageBusy, ImageUnavailable, optimize_image)
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
from .proxy import ProxyTrust
from .ratelimit import RateLimitUnavailable, _is_exempt_path, rate_limited_response
from .sessions import SessionMiddleware
from .static import asset_response
from .ws_origin import websocket_origin_allowed

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


_MAX_ISR_STREAM_BYTES = 8 * 1024 * 1024


class _ISRStreamFeed:
    """One bounded stream tap for the request that starts regeneration."""

    def __init__(self):
        self.ready = asyncio.get_running_loop().create_future()
        self._queue = asyncio.Queue(maxsize=4)
        self._attached = True

    def begin(self):
        if self._attached and not self.ready.done():
            self.ready.set_result(None)

    async def send(self, chunk):
        if self._attached:
            await self._queue.put(("chunk", chunk))

    async def finish(self):
        if self._attached:
            await self._queue.put(("end", None))

    def fail(self, error):
        if not self._attached:
            return
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(("error", error))

    def detach(self):
        self._attached = False
        while not self._queue.empty():
            self._queue.get_nowait()

    async def chunks(self):
        try:
            while True:
                kind, value = await self._queue.get()
                if kind == "end":
                    return
                if kind == "error":
                    if isinstance(value, Exception):
                        raise value
                    raise RuntimeError("ISR stream interrupted")
                yield value
        finally:
            self.detach()


class _ISRStreamingResponse(StreamingResponse):
    def __init__(self, feed):
        self._feed = feed
        super().__init__(feed.chunks(), media_type="text/html; charset=utf-8", headers=[
            (b"cache-control", b"public, max-age=0, must-revalidate"),
            (b"vary", b"Cookie, Authorization"),
        ])

    async def aclose(self):
        # aclose() on an async generator that has never been iterated does
        # not execute its finally block (notably on HEAD and early disconnect).
        self._feed.detach()
        await super().aclose()


async def _send_response(response, send, *, config=None, production=True):
    """Send ``response`` over ASGI, adding the A1 security headers first.

    ``config`` is the app's :class:`SecurityConfig`; when it is ``None`` the
    response is sent unchanged (callers that already went through
    :meth:`App.dispatch` applied the headers there). Applying here as well
    covers the responses that bypass dispatch: rate-limit 429/503, bad
    request 400, oversized 413 and the 500 error page.
    """
    if config is not None:
        apply_security_headers(response, config=config, production=production)
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
                 cache_path=None, observability=None, rate_limit=None, middleware=None, debug=False,
                 resources=None, cache_url=None, cache_failure_policy=None,
                 websocket_max_message=MAX_WS_MESSAGE, hot_reload=False,
                 trusted_proxies=None):
        if not isinstance(debug, bool):
            raise TypeError("debug must be a bool")
        if production and debug:
            raise ValueError("debug cannot be enabled in production")
        if not isinstance(hot_reload, bool) or (hot_reload and (production or not debug)):
            raise ValueError("hot_reload requires development debug mode")
        self.debug = debug
        self.hot_reload = hot_reload
        self._dev_id = uuid.uuid4().hex if hot_reload else None
        if (isinstance(websocket_max_message, bool) or not isinstance(websocket_max_message, int) or
                not 1 <= websocket_max_message <= 16 * 1024 * 1024):
            raise ValueError("websocket_max_message must be from 1 to 16777216 bytes")
        self.websocket_max_message = websocket_max_message
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
        # Trusted proxies for forwarded-header resolution (A6, pynext.proxy).
        # An explicit list wins; otherwise the rate limiter's list is reused
        # so the bucket key and the handler-visible client IP agree; with
        # neither configured, no forwarded header is interpreted at all.
        if trusted_proxies is None:
            trusted_proxies = (rate_limit.trusted_proxies
                               if rate_limit is not None else ())
        self.proxy_trust = ProxyTrust(trusted_proxies)
        try:
            _register_project(self._namespace)
            _package(self._namespace, Path(root).resolve(), load_initializer=False)
            # Resources may initialize libraries whose models are imported by
            # route modules. Load the declarations first, but start their
            # connections only during the async application lifespan.
            self.root = Path(root).resolve()
            self.modules = {}
            configured_resources = {}
            if "resources.py" in manifest["sources"]:
                configured_resources = getattr(self._module("resources.py"), "RESOURCES", None)
                if not isinstance(configured_resources, dict):
                    raise TypeError("resources.py must export a RESOURCES dict")
            self._initialize(root, manifest, production=production,
                             cache_backend=cache_backend, cache_path=cache_path, cache_url=cache_url,
                             cache_failure_policy=cache_failure_policy)
            configured = ()
            if "middleware.py" in manifest["sources"]:
                configured = getattr(self._module("middleware.py"), "MIDDLEWARE", None)
                if not isinstance(configured, (list, tuple)):
                    raise TypeError("middleware.py must export a MIDDLEWARE list or tuple")
            if middleware is None:
                middleware = ()
            if not isinstance(middleware, (list, tuple)):
                raise TypeError("middleware must be a list or tuple")
            self.middleware = tuple(configured) + tuple(middleware)
            for item in self.middleware:
                if not (inspect.iscoroutinefunction(item) or
                        inspect.iscoroutinefunction(getattr(item, "__call__", None))):
                    raise TypeError("middleware entries must be async callables")
            if security.csrf_enabled is None:
                # Auto CSRF: a session middleware means the app keeps
                # server-side state, so every state-changing API route is
                # protected unless the app explicitly opts out with
                # SecurityConfig(csrf_enabled=False). Without sessions the
                # default stays off (opt in with csrf_enabled=True or
                # @csrf_protect) so token-less API clients keep working.
                security = dataclasses.replace(
                    security,
                    csrf_enabled=any(isinstance(item, SessionMiddleware)
                                     for item in self.middleware))
                self.security = security
            if resources is None:
                resources = {}
            if not isinstance(resources, dict):
                raise TypeError("resources must be a dict")
            if set(configured_resources) & set(resources):
                raise ValueError("resource names must be unique")
            combined = {**configured_resources, **resources}
            for name, resource in combined.items():
                if not isinstance(name, str) or not name.isidentifier():
                    raise ValueError("resource names must be Python identifiers")
                if not all(inspect.iscoroutinefunction(getattr(resource, method, None))
                           for method in ("startup", "shutdown")):
                    raise TypeError("resources must provide async startup and shutdown methods")
            self.resources = MappingProxyType(combined)
            self._resources_started = []
            self._started = False
            self._lifecycle_lock = asyncio.Lock()
            # Graceful-shutdown state (B6): set by begin_drain() when the
            # server stops accepting new traffic. In-flight requests and
            # WebSocket sessions are tracked here so serve() can wait for
            # them; the counters are touched from Rust worker threads, so
            # they are guarded by a plain lock, never the event loop.
            self._draining = False
            self._ws_sessions = set()
            self._ws_lock = threading.Lock()
            self._inflight = 0
            self._inflight_streams = 0
            self._inflight_lock = threading.Lock()
        except BaseException:
            self._release_modules()
            raise

    def configure_observability(self, config):
        """Apply a new observability config to this app (logging + metrics)."""
        self.observability_config = config
        self.metrics.enabled = config.metrics
        configure_logging(config)
        return self

    def _initialize(self, root, manifest, *, production=False, cache_backend="memory", cache_path=None,
                    cache_url=None, cache_failure_policy=None):
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.production = production
        self.router = _native.Router(list(manifest["routes"]))
        self.entries = {}
        self.websocket_entries = {}
        self.render_modes = {}
        self.isr_policies = {}
        self.isr_queries = {}
        if cache_backend == "sqlite" and cache_path is None:
            cache_path = self.root / ".pynext" / "isr-cache.sqlite3"
        build_id = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=True).encode("ascii")).hexdigest()
        self.isr = ISRCache(cache_backend=cache_backend, cache_path=cache_path,
                            namespace=(build_id + ":") if cache_backend in {"sqlite", "redis"} else "",
                            cache_url=cache_url, failure_policy=cache_failure_policy)
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
                _check_signature(fn, {"request", "params", "query"}, spec["entry"])
                mode = getattr(fn, "__pynext_render__", "ssr")
                if mode not in {"ssr", "ssg", "isr"}:
                    raise ValueError(f"Unsupported render mode in {spec['entry']}")
                if mode in {"ssg", "isr"} and "request" in inspect.signature(fn).parameters:
                    raise ValueError(f"{mode.upper()} page {spec['entry']} cannot accept request")
                if mode in {"ssg", "isr"} and getattr(fn, "__pynext_auth__", False):
                    raise ValueError(f"{mode.upper()} page {spec['entry']} cannot require authentication")
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
                    try:
                        query_keys = _isr_query_keys(getattr(fn, "__pynext_cache_query__", ()))
                    except ValueError as exc:
                        raise ValueError(f"Invalid ISR query keys in {spec['entry']}: {exc}") from exc
                    self.isr_queries[pattern] = query_keys
                else:
                    query_keys = []
                if "query" in inspect.signature(fn).parameters and not query_keys:
                    raise ValueError(f"Page {spec['entry']} needs ISR query_keys to accept query")
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
            if manifest.get("isr_queries") != self.isr_queries:
                raise ValueError("ISR query keys changed since build; run `pynext build` again")
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
        self._dev_client_history = [self.client_assets] if self.hot_reload else []

    def update_dev_client(self, manifest, assets):
        """Atomically replace development bundles, keeping two prior versions."""
        if not self.hot_reload:
            raise RuntimeError("Hot reload is unavailable for this app")
        self._dev_client_history = (self._dev_client_history + [assets])[-3:]
        retained = {}
        for generation in self._dev_client_history:
            retained.update(generation)
        for path in self.client_assets.keys() - retained.keys():
            self.asset_etags.pop(path, None)
        self.client_manifest = manifest
        self.client_assets = retained
        for path, body in retained.items():
            self.asset_etags[path] = f'"{hashlib.sha256(body).hexdigest()}"'
        for path, body in self.client_assets.items():
            self.metrics.observe_island_bundle(path, len(body))

    def _module(self, path):
        key = _module_key(self.root, path)
        if key not in self.modules:
            self.modules[key] = _load(self.root, path, self._namespace)
        return self.modules[key]

    def error_response(self, exc):
        if not self.debug:
            return Response("Internal Server Error", status=500)
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-16384:]
        body = ("<!DOCTYPE html><html><head><title>PyNext development error</title></head>"
                "<body><h1>Development error</h1><pre>" + html.escape(details) + "</pre></body></html>")
        return Response(body, status=500, media_type="text/html; charset=utf-8",
                        headers=[(b"cache-control", b"no-store")])

    async def startup(self):
        """Start the cache and registered resources in registration order."""
        async with self._lifecycle_lock:
            await self._startup_locked()

    async def _startup_locked(self):
        if self._started:
            return
        self._draining = False
        self.isr.open()
        try:
            for resource in self.resources.values():
                self._resources_started.append(resource)
                await resource.startup()
        except BaseException:
            for resource in reversed(self._resources_started):
                try:
                    await resource.shutdown()
                except Exception:
                    log.exception("Resource cleanup after startup failure failed")
            self._resources_started.clear()
            await self.isr.close()
            raise
        self._started = True

    async def shutdown(self):
        """Close resources in reverse order even when one shutdown fails."""
        self._draining = True
        async with self._lifecycle_lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self):
        error = None
        self._draining = True
        for resource in reversed(self._resources_started):
            try:
                await resource.shutdown()
            except Exception as exc:
                if error is None:
                    error = exc
                else:
                    log.exception("Resource shutdown failed")
        self._resources_started.clear()
        self._started = False
        try:
            await self.isr.close()
        except Exception as exc:
            if error is None:
                error = exc
            else:
                log.exception("Cache shutdown failed")
        if error is not None:
            raise error

    @property
    def is_ready(self):
        """True once startup finished and the app is not draining.

        ``/readyz`` answers from this: a load balancer must stop sending
        new traffic as soon as draining starts, while in-flight requests
        still finish.
        """
        return self._started and not self._draining

    def begin_drain(self):
        """Stop advertising readiness without stopping in-flight work.

        Idempotent. Called by ``server.serve()`` the moment a shutdown is
        requested, before the native server stops accepting connections.
        """
        self._draining = True

    def _register_ws_session(self, session):
        with self._ws_lock:
            self._ws_sessions.add(session)

    def _unregister_ws_session(self, session):
        with self._ws_lock:
            self._ws_sessions.discard(session)

    async def close_websockets(self, code=1001, reason="server shutting down", timeout=5.0):
        """End every live native WebSocket session with a clean close.

        Each accepted session first gets a close frame (``code`` 1001 by
        default, "going away"), so peers see an orderly shutdown instead
        of a dropped connection. Handlers that do not exit on their own
        are cancelled once ``timeout`` seconds have passed. Sessions that
        never accepted a connection have nothing to close and are only
        waited on. Returns True when every handler finished in time.
        """
        with self._ws_lock:
            sessions = list(self._ws_sessions)
        for session in sessions:
            ws = getattr(session, "_ws", None)
            if ws is not None and ws.accepted and not ws.closed:
                try:
                    await ws.close(code, reason)
                except Exception:
                    log.debug("WebSocket close during drain failed", exc_info=True)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            pending = [s for s in sessions
                       if (future := getattr(s, "_future", None)) is not None and not future.done()]
            if not pending:
                return True
            if time.monotonic() >= deadline:
                for session in pending:
                    future = getattr(session, "_future", None)
                    if future is not None and not future.done():
                        future.cancel()
                return False
            await asyncio.sleep(0.05)

    async def wait_for_inflight(self, timeout):
        """Wait until in-flight HTTP requests and streams drain.

        Returns True when nothing is in flight, False on timeout. The
        counters are updated by the native bridge (HTTP) and by
        ``_NativeStream`` (streaming bodies), both from Rust threads.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._inflight_lock:
                remaining = self._inflight + self._inflight_streams
            if remaining <= 0:
                return True
            if time.monotonic() >= deadline:
                log.warning("Shutdown timed out with %d requests still in flight", remaining)
                return False
            await asyncio.sleep(0.05)

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
        if self.isr_queries.get(match[0]):
            _, existed = await self.isr.variant_generation(path, invalidate=True)
            return existed
        return await self.isr.invalidate(path)

    async def _render_stream_chunk(self, node):
        """Serialize one streamed UI node to HTML on a worker thread."""
        bound = bind_islands(_node(node), self.client_manifest, include_runtime=False)
        payload = json.dumps(bound, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return await asyncio.to_thread(_native.render, payload)

    async def _streaming_page_response(self, pattern, chunks, layouts):
        """Wrap an async iterable of UI nodes in a StreamingResponse.

        The layout shell is rendered once with a marker in place of the page
        children, then split into a prefix and a suffix around the marker.
        The prefix is sent first, each page chunk is serialized and sent as
        it arrives, and the suffix closes the document. Layouts must render
        their children exactly once; anything else fails before headers are
        sent. SSG consumes the stream during the build. ISR streams its first
        generation while retaining a bounded copy for the cache.
        """
        marker = "__pynext_stream_" + uuid.uuid4().hex + "__"
        shell = {"kind": "text", "value": marker}
        for layout_fn in reversed(layouts):
            shell = await _invoke(layout_fn, children=shell)
            if not isinstance(shell, dict):
                raise TypeError(
                    f"Layout for {pattern} must return a render node dict, "
                    f"got {type(shell).__name__}"
                )
        shell = bind_islands(shell, self.client_manifest, dev_id=self._dev_id)
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

    def _page_query(self, pattern, request):
        keys = self.isr_queries.get(pattern, ())
        if not keys or request is None:
            return {}
        parsed = request.query
        selected = {key: parsed[key] for key in keys if key in parsed}
        if (any(len(values) > 8 for values in selected.values()) or
                len(json.dumps(selected, ensure_ascii=True).encode("ascii")) > 2048):
            raise BadRequest("ISR query values are too large")
        return selected

    async def _render_isr_cached(self, pattern, params, query, key):
        feed = _ISRStreamFeed()

        async def render():
            response = await self.render_page(pattern, None, params, csp_nonce=False, query=query)
            if not isinstance(response, StreamingResponse):
                return response
            feed.begin()
            body = bytearray()
            try:
                while (chunk := await response._next_chunk()) is not None:
                    if len(body) + len(chunk) > _MAX_ISR_STREAM_BYTES:
                        raise ValueError("Streamed ISR page exceeds the size limit")
                    body.extend(chunk)
                    await feed.send(chunk)
                await feed.finish()
                return Response(bytes(body), status=response.status, headers=response.headers)
            except BaseException as exc:
                feed.fail(exc)
                raise
            finally:
                await response.aclose()

        task = asyncio.create_task(self.isr.get_or_render(key, self.isr_policies[pattern], render))

        def observe(done):
            # A streamed request stops awaiting this task before generation
            # finishes. Retrieve failures while ISR's own task logs them.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(observe)
        try:
            await asyncio.wait((task, feed.ready), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                feed.detach()
                return task.result()
            return _ISRStreamingResponse(feed)
        except BaseException:
            # Do not cancel a shared generation when the initiating client
            # disconnects; other requests can still use its eventual result.
            feed.detach()
            raise

    async def render_page(self, pattern, request, params, *, csp_nonce=True, query=None):
        fn, layouts = self.entries[pattern]
        if query is None:
            query = self._page_query(pattern, request)
        if inspect.isasyncgenfunction(fn):
            # Calling an async generator function only creates the iterator;
            # its body runs when the stream is first pulled. Call it directly
            # instead of _invoke, which would run it on a worker thread.
            kwargs = {key: value for key, value in (("request", request), ("params", params), ("query", query))
                      if key in inspect.signature(fn).parameters}
            result = fn(**kwargs)
        else:
            result = await _invoke(fn, request=request, params=params, query=query)
        if hasattr(result, "__aiter__"):
            return await self._streaming_page_response(pattern, result, layouts)
        tree = result
        if not isinstance(tree, dict):
            raise TypeError(f"Page {pattern} must return a render node dict, got {type(tree).__name__}")
        for layout_fn in reversed(layouts):
            tree = await _invoke(layout_fn, children=tree)
            if not isinstance(tree, dict):
                raise TypeError(f"Layout for {pattern} must return a render node dict, got {type(tree).__name__}")
        tree = bind_islands(tree, self.client_manifest, dev_id=self._dev_id)
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

    async def _run_middleware(self, request, terminal):
        """Run the global middleware chain around ``terminal(request)``.

        ``terminal`` is the final step (the HTTP dispatcher, or the
        WebSocket pre-flight sentinel): it only runs when every middleware
        called ``call_next``. A middleware that returns its own response
        short-circuits the chain, and that response is the result.
        """
        async def invoke(index, current):
            if not isinstance(current, Request):
                raise TypeError("call_next requires a Request")
            if index == len(self.middleware):
                return await terminal(current)
            called = False

            async def call_next(next_request):
                nonlocal called
                if called:
                    raise RuntimeError("call_next may be called only once")
                called = True
                return await invoke(index + 1, next_request)

            result = await self.middleware[index](current, call_next)
            if not isinstance(result, Response):
                raise TypeError("middleware must return a Response")
            return result

        return await invoke(0, request)

    async def dispatch(self, request):
        """Return a response. Exceptions are converted at the transport boundary."""
        # Health probes are reserved framework paths (B6): they bypass
        # middleware, CORS preflight and CSRF so a load balancer can probe
        # a half-broken app, and an app route or public asset with the
        # same path never shadows them. The native transport also skips
        # rate limiting and static serving for these paths; see
        # server.start_native and crates/pynext-native/src/lib.rs.
        # /healthz is liveness (the process answers at all); /readyz is
        # readiness (startup finished and not draining).
        if request.path in ("/healthz", "/readyz"):
            route_var.set("__health__")
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405,
                                headers=[(b"allow", b"GET, HEAD")])
            if request.path == "/healthz":
                body = b"" if request.method == "HEAD" else b"ok"
                return Response(body, media_type="text/plain; charset=utf-8")
            if self.is_ready:
                body = b"" if request.method == "HEAD" else b"ready"
                return Response(body, media_type="text/plain; charset=utf-8")
            return Response(b"" if request.method == "HEAD" else b"not ready", status=503,
                            media_type="text/plain; charset=utf-8",
                            headers=[(b"retry-after", b"1")])
        preflight = cors_preflight_response(request, config=self.security)
        if self.middleware:
            async def terminal(current):
                if preflight is not None:
                    route_var.set("__preflight__")
                    return preflight
                return await self._dispatch(current)

            response = await self._dispatch_timed(request, lambda: self._run_middleware(request, terminal))
            if preflight is None:
                apply_cors_headers(request, response, config=self.security)
            apply_security_headers(response, config=self.security, production=self.production)
            if request.method == "HEAD":
                if isinstance(response, StreamingResponse):
                    await response.aclose()
                    response._head_only = True
                response.body = b""
            return response
        if preflight is not None:
            apply_security_headers(preflight, config=self.security, production=self.production)
            return preflight
        response = await self._dispatch_timed(request)
        apply_cors_headers(request, response, config=self.security)
        apply_security_headers(response, config=self.security, production=self.production)
        return response

    async def _dispatch_timed(self, request, handler=None):
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
                response = await (self._dispatch(request) if handler is None else handler())
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
        if self.hot_reload and path == "/_pynext/dev/client":
            route_var.set("__dev__")
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", b"GET, HEAD")])
            response = JSONResponse({"id": self._dev_id, "bundles": self.client_manifest["bundles"]},
                                    headers=[(b"cache-control", b"no-store")])
            if request.method == "HEAD":
                response.body = b""
            return response
        if path != "/" and path.endswith("/"):
            route_var.set("__redirect__")
            location = quote(path.rstrip("/"), safe="/!$&'()*+,;=:@-._~").encode("ascii")
            if request.query_string:
                if any(c < 32 or c == 127 for c in request.query_string):
                    return Response("Bad Request", status=400)
                location += b"?" + request.query_string
            return Response("", status=308, headers=[(b"location", location)])
        if path == "/_pynext/image":
            route_var.set("__image__")
            if request.method == "OPTIONS":
                return Response(b"", status=204, headers=[(b"allow", b"GET, HEAD, OPTIONS")], media_type=None)
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", b"GET, HEAD, OPTIONS")])
            query = request.query
            if (set(query) - {"src", "w", "q"} or any(len(values) != 1 for values in query.values()) or
                    "src" not in query or "w" not in query):
                raise BadRequest("Invalid image parameters")
            source = query["src"][0]
            width, quality = query["w"][0], query.get("q", ["75"])[0]
            if (not width.isascii() or not width.isdecimal() or not quality.isascii() or not quality.isdecimal() or
                    len(width) > 4 or len(quality) > 2 or not 1 <= int(width) <= MAX_IMAGE_WIDTH or
                    not 1 <= int(quality) <= MAX_IMAGE_QUALITY):
                raise BadRequest("Invalid image dimensions or quality")
            asset = self.public_assets.get(source)
            if asset is None:
                return Response("Not Found", status=404)
            try:
                body = await asyncio.to_thread(optimize_image, asset[0], int(width), int(quality))
            except (ImageBusy, ImageUnavailable):
                return Response("Image optimizer unavailable", status=503, headers=[(b"retry-after", b"1")])
            except ValueError as exc:
                raise BadRequest("Invalid image") from exc
            etag = f'"{hashlib.sha256(body).hexdigest()}"'
            return asset_response(request, body, "image/webp", etag,
                                  cache_control=b"public, max-age=0, must-revalidate",
                                  extra_headers=[(b"x-content-type-options", b"nosniff")])
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
        request = Request(request.method, request.path, request.query_string, request.headers, request.body,
                          params, self, request.state)
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
        handler = entry[0] if spec["kind"] == "page" else entry.get(
            "GET" if request.method == "HEAD" and "HEAD" not in entry else request.method)
        if handler is not None:
            denied = check_authorization(request, handler)
            if denied is not None:
                return denied
        if spec["kind"] == "page":
            if self.production and self.render_modes[pattern] == "ssg":
                response = asset_response(request, self.static_pages[path], "text/html; charset=utf-8",
                                          self.asset_etags[path], cache_control=b"public, max-age=0, must-revalidate",
                                          extra_headers=[(b"vary", b"Cookie, Authorization")])
            elif self.production and self.render_modes[pattern] == "isr":
                query = self._page_query(pattern, request)
                private = any(name.lower() in {b"authorization", b"cookie"} for name, _ in request.headers)
                if private:
                    response = await self.render_page(pattern, None, params, query=query)
                    response.headers.extend([
                        (b"cache-control", b"private, no-store"),
                        (b"vary", b"Cookie, Authorization"),
                    ])
                else:
                    # Classify the outcome before rendering: a fresh entry is
                    # a hit, a stale entry is served while one regeneration
                    # runs in the background, a miss renders synchronously.
                    cache_key = path
                    if self.isr_queries[pattern]:
                        try:
                            generation, _ = await self.isr.variant_generation(path)
                        except SharedCacheUnavailable:
                            self.metrics.observe_regeneration("failure")
                            return Response("Service Unavailable", status=503,
                                            headers=[(b"retry-after", b"1")])
                        encoded = json.dumps(query, ensure_ascii=True, separators=(",", ":")).encode("ascii")
                        cache_key = ("\0isr:" + hashlib.sha256(path.encode("utf-8")).hexdigest() + ":" +
                                     generation + ":" + hashlib.sha256(encoded).hexdigest())
                    cache_before = self.isr.store.lookup(cache_key)[0]
                    self.metrics.observe_cache(cache_before)
                    try:
                        body = await self._render_isr_cached(pattern, params, query, cache_key)
                    except CacheBusy:
                        self.metrics.observe_cache("busy")
                        self.metrics.observe_regeneration("failure")
                        return Response("Server Busy", status=503,
                                        headers=[(b"retry-after", b"1")])
                    except SharedCacheUnavailable:
                        # fail_closed cache policy: the shared store could
                        # not be consulted, so refuse rather than serve
                        # from degraded state.
                        self.metrics.observe_regeneration("failure")
                        return Response("Service Unavailable", status=503,
                                        headers=[(b"retry-after", b"1")])
                    except TimeoutError:
                        self.metrics.observe_regeneration("failure")
                        return Response("Gateway Timeout", status=504)
                    if cache_before == "miss":
                        self.metrics.observe_regeneration("started" if isinstance(body, StreamingResponse)
                                                          else "success")
                    elif cache_before == "stale":
                        self.metrics.observe_regeneration("started")
                    response = body if isinstance(body, StreamingResponse) else Response(
                        body, media_type="text/html; charset=utf-8", headers=[
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

    @property
    def websocket_protocols(self):
        return {path: list(getattr(handler, "__pynext_websocket_protocols__", ()))
                for path, handler in self.websocket_entries.items()}

    async def _websocket_rate_limit_denied(self, scope, path):
        """Return the denial response for an over-limit WS handshake, else ``None``.

        Mirrors the native upgrade branch (review #4 #21): WebSocket
        handshakes share the HTTP limiter's bucket per client IP, so one
        token is charged per handshake attempt, exempt paths skip the
        check, no configured limiter means no change, and a limiter that
        cannot decide fails closed with 503, like ``_handle_http``. The
        caller answers with the returned response before the accept, so a
        denied handshake is an HTTP 429 with ``Retry-After``, never an
        accepted socket closed afterwards.

        Composition with ``_authorize_websocket`` (Track A, separate
        branch): the pre-flight runs a synthetic GET through the
        middleware chain only — it never passes through ``_handle_http``,
        where the HTTP limiter lives — so it consumes no token. This
        check is therefore the single charge per handshake attempt. Do
        not add a second limiter call inside ``_authorize_websocket`` or
        the pre-flight, or each handshake would cost two tokens; if a
        middleware-based limiter is ever introduced, the pre-flight must
        be exempted from it instead.
        """
        if self.rate_limit is None:
            return None
        if _is_exempt_path(self.rate_limit, path):
            return None
        client = scope.get("client")
        client_ip = client[0] if client else None
        key = self.rate_limit.key_for(client_ip, scope.get("headers", []))
        try:
            if getattr(self.rate_limit, "blocking", False):
                allowed, retry_after = await asyncio.to_thread(self.rate_limit.allow, key)
            else:
                allowed, retry_after = self.rate_limit.allow(key)
        except RateLimitUnavailable:
            return Response("Service Unavailable", status=503, headers=[(b"retry-after", b"1")])
        if not allowed:
            return rate_limited_response(retry_after)
        return None

    async def _authorize_websocket(self, path, query_string=b"", headers=()):
        """Pre-handshake authorization for a WebSocket upgrade.

        Returns ``None`` when the upgrade may proceed, else a ``Response``
        describing the denial. Runs the global middleware chain (including
        ``SessionMiddleware``, which loads the session into
        ``request.state``) around a synthetic ``GET`` for the upgrade path,
        then the handler's ``@require_auth``/``@require_permission`` policy
        — all before the handshake completes, so a denial is a rejected
        upgrade, never an accepted socket closed afterwards.

        The upgrade is allowed only when the chain reaches the terminal:
        a middleware that short-circuits with its own response (even a
        200) denies the upgrade, because the request never reached the
        handler. A middleware failure fails closed with 503. The
        authorization state is not passed to the WebSocket handler: the
        handler receives a ``WebSocket``, not a ``Request``, and the native
        transport cannot carry the pre-flight ``request.state`` through
        the Rust upgrade — so handlers that need the identity must re-derive
        it from the socket (for example from a session cookie the
        middleware already validated, or from a token checked in
        middleware). The check itself is identical on ASGI and native.
        Middleware must not rotate or invalidate the session during this
        pre-flight: the pre-flight response is discarded, so a rotated
        token's ``set-cookie`` would never reach the client while the
        old token is already gone from the store.
        """
        match = self.router.matches(path)
        if match is None:
            return Response("Not Found", status=404)
        pattern, raw_params = match
        handler = self.websocket_entries.get(pattern)
        if handler is None:
            return Response("Not Found", status=404)
        params = {k: v if f"[...{k}]" in pattern else v[0] for k, v in raw_params.items()}
        request = Request("GET", path, bytes(query_string),
                          tuple((bytes(name), bytes(value)) for name, value in headers),
                          b"", params, self, {})
        terminal_reached = False
        seen = {}

        async def terminal(current):
            nonlocal terminal_reached
            terminal_reached = True
            seen["request"] = current
            return Response(b"", status=200)

        try:
            response = await (self._run_middleware(request, terminal) if self.middleware
                              else terminal(request))
        except Exception:
            log.exception("WebSocket authorization failed")
            return Response("Service Unavailable", status=503)
        if not terminal_reached:
            # A middleware answered the upgrade itself: its response is
            # the denial (for example 403 from an auth middleware).
            return response
        denied = check_authorization(seen.get("request", request), handler)
        if denied is not None:
            return denied
        return None

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
            # Unknown paths consume no token: the native upgrade branch
            # answers 404 before its rate-limit check, and this mirrors it.
            await send({"type": "websocket.close", "code": 1008})
            return
        # Rate limiting runs before the origin check and the accept, in
        # the same relative order as the native upgrade branch (route
        # lookup, then rate limit, then origin, then 101): a denied
        # handshake is answered as an HTTP 429 with Retry-After via
        # websocket.http.response.start, before any websocket.connect is
        # read, so the socket is never accepted and then closed.
        denied = await self._websocket_rate_limit_denied(scope, path)
        if denied is not None:
            apply_security_headers(denied, config=self.security, production=self.production)
            await send({"type": "websocket.http.response.start", "status": denied.status,
                        "headers": denied.headers})
            await send({"type": "websocket.http.response.body", "body": denied.body})
            return
        client = scope.get("client")
        peer_ip = client[0] if client else None
        if not websocket_origin_allowed(scope.get("headers", []), peer_ip, scope.get("scheme") or "ws",
                                        self.proxy_trust, self.security):
            await send({"type": "websocket.close", "code": 1008})
            return
        offered = scope.get("subprotocols", [])
        if not isinstance(offered, (list, tuple)) or any(not isinstance(value, str) for value in offered):
            await send({"type": "websocket.close", "code": 1008})
            return
        supported = getattr(handler, "__pynext_websocket_protocols__", ())
        chosen = next((protocol for protocol in supported if protocol in offered), None)
        if supported and offered and chosen is None:
            await send({"type": "websocket.close", "code": 1008})
            return
        # Authorization runs before the handshake completes: a denied
        # upgrade is refused with a close instead of an accept, so the
        # handler never runs and no data is sent.
        denied = await self._authorize_websocket(path, scope.get("query_string", b""),
                                                 scope.get("headers", []))
        if denied is not None:
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
                    if len(data.encode("utf-8")) > self.websocket_max_message:
                        await asgi_send(("close", 1009, "message too large"))
                        raise WebSocketDisconnect(1009, "message too large")
                    return ("text", data)
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    if len(data) > self.websocket_max_message:
                        await asgi_send(("close", 1009, "message too large"))
                        raise WebSocketDisconnect(1009, "message too large")
                    return ("bytes", bytes(data))

        async def asgi_send(item):
            kind = item[0]
            if kind == "accept":
                if state["accepted"]:
                    raise RuntimeError("WebSocket is already accepted")
                state["accepted"] = True
                message = {"type": "websocket.accept"}
                if chosen is not None:
                    message["subprotocol"] = chosen
                await send(message)
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
                       query_string=scope.get("query_string", b""), max_message_bytes=self.websocket_max_message,
                       subprotocol=chosen, offered_subprotocols=offered)
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
                        await self.startup()
                    except Exception as exc:
                        # ASGI requires startup.failed instead of a bare
                        # exception so the server can shut the app down.
                        await send({"type": "lifespan.startup.failed",
                                    "message": str(exc) or exc.__class__.__name__})
                        return
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    try:
                        await self.shutdown()
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
        # Health probes bypass rate limiting (B6): a saturated limiter
        # must never make the process look dead to a load balancer.
        is_health = scope.get("path") in ("/healthz", "/readyz")
        if self.rate_limit is not None and not (is_health or _is_exempt_path(self.rate_limit, scope.get("path", ""))):
            # Check before reading the body: an over-limit request is
            # rejected without handler execution or body intake.
            client = scope.get("client")
            client_ip = client[0] if client else None
            key = self.rate_limit.key_for(client_ip, scope.get("headers", []))
            try:
                if getattr(self.rate_limit, "blocking", False):
                    allowed, retry_after = await asyncio.to_thread(self.rate_limit.allow, key)
                else:
                    allowed, retry_after = self.rate_limit.allow(key)
            except RateLimitUnavailable:
                response = Response("Service Unavailable", status=503, headers=[(b"retry-after", b"1")])
                if scope["method"].upper() == "HEAD":
                    response.body = b""
                await _send_response(response, send, config=self.security, production=self.production)
                return
            if not allowed:
                response = rate_limited_response(retry_after)
                if scope["method"].upper() == "HEAD":
                    response.body = b""
                await _send_response(response, send, config=self.security, production=self.production)
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
                                          tuple(scope.get("headers", [])), b"".join(chunks), _app=self)
                        break
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
                response = Response("Bad Request", status=400)
            except Exception as exc:
                log.exception("Request failed")
                response = self.error_response(exc)
        if request is not None:
            # Trust-resolved client identity (A6, pynext.proxy): the peer
            # address, scheme and Host header, unless a trusted proxy
            # supplied forwarded values. Handlers read these from
            # ``request.state``; without trusted proxies they are just the
            # direct connection values, never a spoofed header.
            client = scope.get("client")
            peer_ip = client[0] if client else None
            headers = tuple(scope.get("headers", []))
            host = next((value.decode("latin-1") for key, value in headers
                         if key.lower() == b"host"), None)
            info = self.proxy_trust.resolve(
                peer_ip, headers,
                default_scheme=scope.get("scheme") or "http",
                default_host=host)
            request.state["client_ip"] = info.client_ip
            request.state["client_scheme"] = info.scheme
            request.state["client_host"] = info.host

            async def dispatch_and_send():
                try:
                    result = await self.dispatch(request)
                except BadRequest as exc:
                    log.info("Bad request: %s", exc)
                    result = Response("Bad Request", status=400)
                except Exception as exc:
                    log.exception("Request failed")
                    result = self.error_response(exc)
                if scope["method"].upper() == "HEAD":
                    result.body = b""
                await _send_response(result, send, config=self.security, production=self.production)

            await _run_while_connected(dispatch_and_send(), receive)
            return
        if scope["method"].upper() == "HEAD":
            response.body = b""
        request_id = get_request_id()
        if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
            response.headers.append((b"x-request-id", request_id.encode("ascii")))
        await _send_response(response, send, config=self.security, production=self.production)


def create_app(root=".", *, production=False, security=None, cache_backend="memory", cache_path=None,
               observability=None, rate_limit=None, middleware=None, debug=False, resources=None, cache_url=None,
               cache_failure_policy=None, websocket_max_message=MAX_WS_MESSAGE, hot_reload=False,
               trusted_proxies=None):
    root = Path(root).resolve()
    return App(root, load_manifest(root) if production else discover(root), production=production,
               security=security, cache_backend=cache_backend, cache_path=cache_path,
               observability=observability, rate_limit=rate_limit, middleware=middleware, debug=debug,
               resources=resources, cache_url=cache_url, cache_failure_policy=cache_failure_policy,
               websocket_max_message=websocket_max_message, hot_reload=hot_reload,
               trusted_proxies=trusted_proxies)
