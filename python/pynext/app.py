import asyncio
import copy
import dataclasses
import hashlib
import hmac
import html
import importlib.machinery
import importlib.util
import inspect
import json
import keyword
import logging
import math
import os
import re
import sys
import threading
import time
import traceback
import uuid
import warnings
import weakref
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, unquote, unquote_to_bytes

from . import _native
from .admission import DecisionGate
from .assets import read_public_artifacts, read_public_sources
from .auth import check_authorization
from .cache import (MAX_CACHE_SECONDS, CacheBusy, ISRCache, SharedCacheUnavailable,
                    dev_retention_policy, validate_cache_tag)
from .client import compile_client, read_client_artifacts
from .components import (_isr_policy, _isr_query_keys, _node, _partial_isr_policy, _validate_tags,
                        bind_islands, reduce_segment_config)
from .env import load_env
from .build import _materialize, discover, load_manifest
from .config import DEFAULT_CONFIG
from .dynamic import (DYNAMIC_MODES, MAX_DYNAMIC_ACCESSES, format_dynamic_accesses,
                      suppress_dynamic_tracking, track_prerender)
from .errors import SignalError, digest_for_error, error_count
from .http import (SAFE_METHODS, BadRequest, JSONResponse, Request, RequestCancelled, Response, SecurityConfig,
                   StreamingResponse, WebSocket, WebSocketDisconnect, apply_cors_headers,
                   apply_path_headers, apply_security_headers, compile_path_headers,
                   cors_preflight_response, filter_internal_headers, new_csp_nonce, validate_csrf)
from .images import (MAX_IMAGE_QUALITY, MAX_IMAGE_WIDTH, ImageBusy, ImageUnavailable, optimize_image)
from .middleware import ScopedMiddleware
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
from .ratelimit import _is_exempt_path, rate_limited_response
from .sessions import SessionMiddleware
from .static import asset_response
from .ws_origin import websocket_origin_allowed

log = logging.getLogger("pynext")
MAX_BODY = 1_048_576
ASGI_DECISION_CAPACITY = 64
ASGI_DECISION_TIMEOUT = 2.0

# Development error page (P1-3): how much of a traceback the dev page keeps.
# The tail is kept so the frames nearest the failure survive; anything cut
# off the front is announced on the page instead of silently disappearing.
_DEV_TRACEBACK_MAX_CHARS = 16384
_DEV_EDITOR_URL_TEMPLATE = "vscode://file/{path}:{line}"
_DEV_ERROR_COUNT_LIMIT = 512
_FRAME_RE = re.compile(r'^(?P<indent>[ \t]*)File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>.*)$')


def _editor_url_template():
    """Editor link template for dev traceback frames.

    ``PYNEXT_EDITOR_URL`` overrides the default ``vscode://`` template; it
    must contain ``{path}`` and ``{line}`` placeholders (e.g.
    ``idea://open?file={path}&line={line}``). A malformed value falls back
    to the default instead of breaking the error page.
    """
    template = os.environ.get("PYNEXT_EDITOR_URL") or _DEV_EDITOR_URL_TEMPLATE
    if "{path}" not in template or "{line}" not in template:
        return _DEV_EDITOR_URL_TEMPLATE
    return template


def _dev_error_digest(exc):
    """Short stable digest for a dev error page: type + message + origin frame."""
    origin = ""
    tb = exc.__traceback__
    while tb is not None:
        origin = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"
        tb = tb.tb_next
    raw = f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}\n{origin}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _render_dev_traceback(text):
    """HTML-escape traceback text, linking ``File "...", line N`` frames.

    The link opens the frame in the editor (see :func:`_editor_url_template`).
    Lines that do not match a frame are escaped verbatim, so chained
    exceptions (``During handling of the above exception ...``) survive.
    """
    template = _editor_url_template()
    rendered = []
    for line in text.splitlines():
        match = _FRAME_RE.match(line)
        if match is None:
            rendered.append(html.escape(line))
            continue
        href = template.format(path=quote(match.group("path"), safe="/:"),
                               line=match.group("line"))
        rendered.append(
            f"{html.escape(match.group('indent'))}File &quot;<a href=\"{html.escape(href, quote=True)}\">"
            f"{html.escape(match.group('path'))}</a>&quot;, line {match.group('line')}, "
            f"in {html.escape(match.group('func'))}")
    return "\n".join(rendered)


class _SyncHandlerBusy(Exception):
    """All bounded synchronous handler slots are still occupied."""


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
    request = values.get("request")
    if request is not None and request._app is not None:
        result = await request._app._invoke_sync(fn, kwargs, request)
    else:
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


def _substitute_shell_marker(node, marker, page_tree):
    """Return ``(tree, count)`` with the marker text node swapped for ``page_tree``.

    The input tree is never mutated: every dict and list on the path is
    rebuilt, so a cached shell template stays pristine for the next
    request. ``count`` is the number of marker nodes replaced; a valid
    shell renders its children exactly once, so callers require 1.
    """
    if isinstance(node, dict):
        if node.get("kind") == "text" and node.get("value") == marker:
            return page_tree, 1
        result = {}
        count = 0
        for key, value in node.items():
            new_value, found = _substitute_shell_marker(value, marker, page_tree)
            result[key] = new_value
            count += found
        return result, count
    if isinstance(node, list):
        result = []
        count = 0
        for child in node:
            new_child, found = _substitute_shell_marker(child, marker, page_tree)
            result.append(new_child)
            count += found
        return result, count
    return node, 0


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


def _typed_params(raw_params, kinds):
    """Collapse matched parameter values to the handler's types.

    ``raw_params`` maps each name to its list of matched segments and
    ``kinds`` maps the same names to the kind the router recorded for
    them (``"dynamic"``, ``"catch_all"`` or ``"optional"`` — see
    ``Router.matches``). Catch-all and optional catch-all parameters stay
    lists; a dynamic parameter becomes its single segment. The kinds
    must come from the router, never from a substring test on the
    pattern: ``"[...a]"`` is a substring of ``"/[a]/[...a2]"`` and would
    misclassify the dynamic ``a`` as a list.
    """
    return {k: v if kinds.get(k) in ("catch_all", "optional") else v[0]
            for k, v in raw_params.items()}


# Matches Next.js `escapePathDelimiters(segment, true)`: re-encode any path
# delimiter a decoded value produced, plus encoded delimiters that were
# already present, so a decoded value can never be mistaken for a
# separator or decoded a second time.
_DELIMITER_RE = re.compile(r"([/#?\\]|%(?:2f|23|3f|5c))", re.IGNORECASE)


def _escape_path_delimiters(segment):
    """Re-encode path delimiters in a decoded parameter value."""
    return _DELIMITER_RE.sub(lambda match: quote(match.group(0), safe=""), segment)


def _validate_path(path):
    """Reject a path that is not a normalized absolute path."""
    if (not path.startswith("/") or "//" in path or "\\" in path or
            any(ord(c) < 32 or ord(c) == 127 for c in path) or
            any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("Invalid request path")


def _validate_encoded_path(raw):
    """Validate a still-encoded request path; return it as a string.

    Only the encoded form is checked here: ``%00`` and malformed ``%``
    sequences are rejected, as are literal ``//``, backslashes, control
    characters and ``.``/``..`` segments. Encoded delimiters (``%2F``,
    ``%5C``, ``%23``, ``%3F``) are allowed through — they are decoded
    per segment before matching, with delimiters re-encoded, so they
    never act as separators.
    """
    if isinstance(raw, str):
        raw = raw.encode("ascii")
    if re.search(br"%(?![0-9a-fA-F]{2})|%00", raw):
        raise ValueError("Invalid or NUL-encoded path")
    encoded = raw.decode("ascii")
    _validate_path(encoded)
    return encoded


def _split_request_path(path, raw_path=None):
    """Return ``(decoded_path, encoded_path)`` for a request.

    Route matching runs on the per-segment decoded form produced by
    :func:`_normalize_path_for_matching` (``/files/a%2Fb`` matches
    ``[name]`` with the value ``a%2Fb``; ``/in%70ut`` matches the
    static segment ``input``). The decoded path is kept for
    asset/static/ISR lookups and handler visibility. When no raw path
    is available (for example ``revalidate()``), both are the validated
    path. The decoded form is validated too, so a ``%2F`` that would
    create an empty segment (``/%2finput`` → ``//input``) or a ``..``
    segment is still rejected before any rate-limit charge.
    """
    if raw_path is None:
        _validate_path(path)
        return path, path
    encoded = _validate_encoded_path(raw_path)
    decoded = unquote_to_bytes(encoded).decode("utf-8", errors="strict")
    _validate_path(decoded)
    return decoded, encoded


def _decode_path(path, raw_path=None):
    """Return the decoded request path (see :func:`_split_request_path`)."""
    return _split_request_path(path, raw_path)[0]


def _decode_param(name, raw):
    """Decode one matched parameter value (Next.js ``decodePathParams``).

    Raises :class:`BadRequest` naming the parameter when the value
    cannot be decoded. Delimiters produced by decoding are re-encoded
    so the value cannot be mistaken for a separator.
    """
    try:
        value = unquote(raw, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadRequest(f"Failed to decode path param '{name}'") from exc
    return _escape_path_delimiters(value)


def _normalize_path_for_matching(encoded_path):
    """Decode each segment and re-escape delimiters (Next.js ``decodePathParams``).

    Matching runs on this form: a static segment compares decoded (so
    ``/in%70ut`` matches ``/input``), while an encoded delimiter stays
    one segment (so ``/files/a%2Fb`` matches ``[name]`` with the value
    ``a%2Fb``). The values the router returns are already the decoded,
    delimiter-safe values the handler receives, so no second decode is
    needed. Raises :class:`BadRequest` naming the offending segment when
    a value cannot be decoded.
    """
    segments = encoded_path.split("/")
    return "/".join(
        _decode_param(segment, segment) for segment in segments
    )


def _is_dynamic_pattern(path):
    """True when ``path`` is a route pattern (``[slug]``, ``[...rest]``)
    rather than a concrete path."""
    return any(segment.startswith("[") for segment in path.split("/"))


def _posix_path(path):
    """A project-relative path with forward slashes.

    The build manifest records paths with the OS separator, so on
    Windows a layout arrives as ``app\\blog\\layout.py`` while the
    URL-derived lookups build ``app/blog/layout.py``. Normalise to
    forward slashes so comparisons, ``source`` fields and log lines
    are identical on every platform. (``Path.as_posix()`` alone would
    not do this for a Windows-style string on POSIX, where a
    backslash is a legal filename character, so the separator is
    replaced directly.)
    """
    return str(path).replace("\\", "/")


def _layout_url_dir(module):
    """The URL directory a layout module applies to.

    ``app/news/layout.py`` applies to ``/news``; a layout inside a route
    group (``app/(marketing)/layout.py``) applies to ``/`` because groups
    do not appear in URLs.
    """
    parts = _posix_path(module).split("/")[1:-1]
    url_parts = [part for part in parts if not (part.startswith("(") and part.endswith(")"))]
    return "/" + "/".join(url_parts)


_ROUTE_GROUP_RE = re.compile(r"\([A-Za-z0-9_-]+\)")


def _layout_scope(relative):
    """Map a layout file (``app/blog/layout.py``) to its URL scope (``/blog``).

    Route groups do not appear in the URL, so a layout inside ``(group)``
    takes the scope of the directory the group sits in; the root layout
    scopes to ``/``.
    """
    parts = Path(relative).parts[1:-1]
    visible = [part for part in parts if not _ROUTE_GROUP_RE.fullmatch(part)]
    return "/" + "/".join(visible) if visible else "/"


def _layout_tag(scope):
    return "_pynext_layout_" + scope


def _implicit_path_tags(path):
    """The implicit tag(s) an ISR entry for a concrete path carries.

    ``revalidate(path)`` is tag invalidation for these tags. ``/`` and
    ``/index`` are aliases of each other (Next.js ``revalidatePath``),
    so invalidating either invalidates both.
    """
    tags = ["_pynext_path_" + path]
    if path == "/":
        tags.append("_pynext_path_/index")
    elif path == "/index":
        tags.append("_pynext_path_/")
    return tags


def _module_key(root, relative):
    # Casefold so the same file reached through a different case (e.g.
    # App/Page.py vs app/page.py on a case-insensitive filesystem) maps to
    # one cached module instead of being loaded twice.
    return str((root / relative).resolve()).casefold()


class App:
    def __init__(self, root, manifest, *, production=False, security=None,
                 cache_backend=DEFAULT_CONFIG.cache_backend,
                 cache_path=None, observability=None, rate_limit=None, middleware=None, debug=False,
                 resources=None, cache_url=None, cache_failure_policy=None,
                 websocket_max_message=DEFAULT_CONFIG.websocket_max_message, hot_reload=False,
                 trusted_proxies=None, sync_workers=32, strict_ssg=False, headers=None,
                 revalidate_secret=None):
        if isinstance(sync_workers, bool) or not isinstance(sync_workers, int) or not 1 <= sync_workers <= 1024:
            raise ValueError("sync_workers must be an integer from 1 to 1024")
        self.sync_workers = sync_workers
        # Secret for the POST /_pynext/revalidate webhook. None disables
        # the endpoint (it answers 404). create_app() also reads
        # PYNEXT_REVALIDATE_SECRET when no explicit secret is given.
        if revalidate_secret is not None and (
                not isinstance(revalidate_secret, str) or not revalidate_secret):
            raise ValueError("revalidate_secret must be a non-empty string")
        self.revalidate_secret = revalidate_secret
        if not isinstance(debug, bool):
            raise TypeError("debug must be a bool")
        if not isinstance(strict_ssg, bool):
            raise TypeError("strict_ssg must be a bool")
        # Dev-only: with strict_ssg, an SSG path that generate_static_params
        # would not produce is a 404 in development too, instead of merely
        # warning that it will 404 in production (see _dispatch).
        self.strict_ssg = strict_ssg
        if production and debug:
            raise ValueError("debug cannot be enabled in production")
        if not isinstance(hot_reload, bool) or (hot_reload and (production or not debug)):
            raise ValueError("hot_reload requires development debug mode")
        self.debug = debug
        self.hot_reload = hot_reload
        # Dev error page occurrence counts, keyed by _dev_error_digest().
        # Bounded so a crash loop cannot grow memory without limit.
        self._dev_error_counts = {}
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
        # Per-path response headers (Next.js headers() equivalent): compiled
        # and validated here, so a bad rule fails at startup. See
        # http.compile_path_headers for the accepted shapes.
        self.path_headers = compile_path_headers(headers)
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
                # A scoped SessionMiddleware still counts: unwrap the scope
                # so CSRF auto-enable sees the middleware inside.
                security = dataclasses.replace(
                    security,
                    csrf_enabled=any(isinstance(item.middleware if isinstance(item, ScopedMiddleware)
                                                 else item, SessionMiddleware)
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
            self._sync_active = 0
            self._inflight_lock = threading.Lock()
            self._asgi_admission = DecisionGate(ASGI_DECISION_CAPACITY, ASGI_DECISION_TIMEOUT)
        except BaseException:
            self._release_modules()
            raise

    async def _invoke_sync(self, fn, kwargs, request):
        # Keep capacity occupied until the actual thread finishes, even if
        # the awaiter timed out. Cancellation cannot stop a Python thread.
        request.check_cancelled()
        with self._inflight_lock:
            if self._sync_active >= self.sync_workers:
                raise _SyncHandlerBusy()
            self._sync_active += 1

        def run():
            request.check_cancelled()  # a cancelled queued job never starts
            return fn(**kwargs)

        task = asyncio.create_task(asyncio.to_thread(run))

        def finished(done):
            with self._inflight_lock:
                self._sync_active -= 1
            # A timed-out awaiter cannot retrieve a late exception/result.
            if not done.cancelled():
                error = done.exception()
                if error is None and request.cancelled and inspect.iscoroutine(done.result()):
                    done.result().close()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            request._cancel_event.set()
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
        self._layout_shells = {}
        self._layout_shell_locks = {}
        self._layout_shell_dynamic = {}
        self.websocket_entries = {}
        self.render_modes = {}
        self.dynamic_modes = {}
        # Per-pattern dynamic accesses recorded during SSG/ISR prerenders
        # (see _merge_dynamic_accesses); written to the build manifest.
        self.dynamic_accesses = {}
        self._dev_static_paths = {}
        self.isr_policies = {}
        self.layout_policies = {}
        self.isr_policy_overrides = {}
        self.isr_queries = {}
        self.isr_tags = {}
        self.entry_layouts = {}
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
                # The manifest records paths with the OS separator, so on
                # Windows ``spec["layouts"]`` holds ``app\\blog\\layout.py``.
                # Normalise once here: layout lookups, ``layout_policies``
                # sources and the override warning all compare against
                # slash-built paths (see _posix_path).
                entry = _posix_path(spec["entry"])
                layout_relatives = [_posix_path(relative) for relative in spec["layouts"]]
                layouts = []
                layout_policies = []
                for relative in layout_relatives:
                    layout_fn = getattr(self._module(relative), "Layout", None)
                    if not callable(layout_fn) or not getattr(layout_fn, "__pynext_layout__", False):
                        raise ValueError(f"{relative} must export a @layout Layout function")
                    _check_signature(layout_fn, {"children"}, relative)
                    if "children" not in inspect.signature(layout_fn).parameters:
                        raise ValueError(f"{relative} must accept children")
                    layouts.append(layout_fn)
                    raw_layout_policy = getattr(layout_fn, "__pynext_layout_policy__", None)
                    if raw_layout_policy is not None:
                        if not isinstance(raw_layout_policy, dict) or set(raw_layout_policy) != {"revalidate", "stale", "expire"}:
                            raise ValueError(f"Invalid layout policy in {relative}")
                        try:
                            layout_policy = _partial_isr_policy(raw_layout_policy["revalidate"],
                                                                raw_layout_policy["stale"],
                                                                raw_layout_policy["expire"])
                        except ValueError as exc:
                            raise ValueError(f"Invalid layout policy in {relative}: {exc}") from exc
                        if (layout_policy["revalidate"] is not None or layout_policy["stale"] is not None
                                or layout_policy["expire"] is not None):
                            layout_policies.append((relative, layout_policy))
                dynamic = getattr(fn, "__pynext_dynamic__", "auto")
                if dynamic not in DYNAMIC_MODES:
                    raise ValueError(f"Unsupported dynamic mode in {spec['entry']}")
                if mode == "ssr" and dynamic != "auto":
                    raise ValueError(f"SSR page {spec['entry']} cannot set dynamic={dynamic!r}")
                if dynamic == "force-dynamic" and mode != "ssg":
                    raise ValueError(f"Only SSG pages can use dynamic='force-dynamic' in {spec['entry']}")
                self.dynamic_modes[pattern] = dynamic
                if mode == "isr":
                    raw_policy = getattr(fn, "__pynext_cache_policy__", None)
                    if not isinstance(raw_policy, dict) or set(raw_policy) != {"revalidate", "stale", "expire"}:
                        raise ValueError(f"Invalid ISR policy in {spec['entry']}")
                    try:
                        page_policy = _partial_isr_policy(raw_policy["revalidate"],
                                                          raw_policy["stale"],
                                                          raw_policy["expire"])
                    except ValueError as exc:
                        raise ValueError(f"Invalid ISR policy in {spec['entry']}: {exc}") from exc
                    if layout_policies:
                        self.layout_policies[pattern] = [
                            {"source": source, "revalidate": policy["revalidate"],
                             "stale": policy["stale"], "expire": policy["expire"]}
                            for source, policy in layout_policies]
                    # Segment config inheritance (Next.js reduceAppConfig):
                    # layouts set the section policy, a page only wins with a
                    # shorter revalidate / smaller stale / smaller expire. Warn
                    # when the page explicitly asked for a value it does not
                    # get, naming both files so the override is never silent.
                    effective, winners = reduce_segment_config(
                        [*layout_policies, (entry, page_policy)])
                    for key in ("revalidate", "stale", "expire"):
                        page_value = page_policy[key]
                        if (page_value is not None and page_value is not False and
                                effective[key] != page_value):
                            winner_source, winner_value = winners[key]
                            log.warning(
                                "ISR %s=%s in %s is overridden by %s=%s from %s for route %s; "
                                "a child segment only wins with a shorter %s",
                                key, page_value, entry, key, winner_value,
                                winner_source, pattern, key)
                    if effective["revalidate"] is None:
                        raise ValueError(
                            f"ISR page {spec['entry']} must set revalidate or inherit it from a layout")
                    if effective["revalidate"] is False:
                        # On-demand-only ISR: never regenerates on a timer,
                        # only explicit revalidate()/revalidate_tag() does.
                        # _isr_policy normalizes it to the longest lifetime
                        # (expire ignored, stale window 0).
                        stale = 60.0 if effective["stale"] is None else effective["stale"]
                        try:
                            policy = _isr_policy(False, stale, None)
                        except ValueError as exc:
                            raise ValueError(f"Invalid ISR policy in {spec['entry']}: {exc}") from exc
                    else:
                        # Reconcile stale/expire: they are two spellings of the
                        # same window. A set expire is the hard cap and wins;
                        # stale is derived from it. Without an expire, stale
                        # (default 60) defines the window and expire follows.
                        revalidate = effective["revalidate"]
                        if effective["expire"] is not None:
                            expire = effective["expire"]
                            stale = expire - revalidate
                        else:
                            stale = 60.0 if effective["stale"] is None else effective["stale"]
                            expire = revalidate + stale
                        try:
                            policy = _isr_policy(revalidate, stale, expire)
                        except ValueError as exc:
                            raise ValueError(f"Invalid ISR policy in {spec['entry']}: {exc}") from exc
                    self.isr_policies[pattern] = policy
                    if getattr(module, "generate_static_params", None) is not None:
                        raise ValueError(f"ISR page {spec['entry']} cannot export generate_static_params yet")
                    try:
                        query_keys = _isr_query_keys(getattr(fn, "__pynext_cache_query__", ()))
                    except ValueError as exc:
                        raise ValueError(f"Invalid ISR query keys in {spec['entry']}: {exc}") from exc
                    self.isr_queries[pattern] = query_keys
                    try:
                        # Lists, not tuples: the manifest round-trips
                        # through JSON, like isr_queries.
                        self.isr_tags[pattern] = list(_validate_tags(
                            getattr(fn, "__pynext_cache_tags__", ())))
                    except ValueError as exc:
                        raise ValueError(f"Invalid ISR tags in {spec['entry']}: {exc}") from exc
                    # Layout tags: an ISR page also carries one tag per
                    # ancestor layout (scopes recorded in entry_layouts),
                    # so invalidating a layout tag evicts the whole
                    # subtree (webhook type="layout" via _layout_tag).
                    self.isr_tags[pattern] = sorted(set(self.isr_tags[pattern]) |
                        {_layout_tag(_layout_scope(relative)) for relative in layout_relatives})
                else:
                    query_keys = []
                if "query" in inspect.signature(fn).parameters and not query_keys:
                    raise ValueError(f"Page {spec['entry']} needs ISR query_keys to accept query")
                if mode == "ssg":
                    is_dynamic = any(segment.startswith("[") for segment in pattern.split("/"))
                    generator = getattr(module, "generate_static_params", None)
                    if dynamic == "force-dynamic":
                        # Served dynamically in production, so no generator
                        # is needed and any exported one is ignored at build.
                        pass
                    elif is_dynamic and (not callable(generator) or inspect.signature(generator).parameters):
                        raise ValueError(f"Dynamic SSG page {spec['entry']} must export generate_static_params() with no arguments")
                    elif not is_dynamic and generator is not None:
                        raise ValueError(f"Static SSG page {spec['entry']} cannot export generate_static_params")
                    self.static_generators[pattern] = generator
                self._layout_shell_dynamic[pattern] = any(
                    getattr(layout_fn, "__pynext_layout_dynamic__", False) for layout_fn in layouts
                )
                self.entries[pattern] = (fn, layouts)
                self.entry_layouts[pattern] = tuple(layout_relatives)
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
            if manifest.get("layout_policies") != self.layout_policies:
                raise ValueError("Layout policies changed since build; run `pynext build` again")
            if manifest.get("isr_queries") != self.isr_queries:
                raise ValueError("ISR query keys changed since build; run `pynext build` again")
            if manifest.get("dynamic_modes") != self.dynamic_modes:
                raise ValueError("Dynamic modes changed since build; run `pynext build` again")
            if manifest.get("isr_tags") != self.isr_tags:
                raise ValueError("ISR tags changed since build; run `pynext build` again")
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
        # Cached shells embed island module URLs from the old manifest.
        self.invalidate_layout_shell()

    def invalidate_layout_shell(self, pattern=None):
        """Drop cached layout shells for one ``pattern`` or for all routes.

        Layout source changes create a new app, so the cache is normally
        fresh; this is for the cases where a running app outlives its
        inputs — :meth:`update_dev_client` calls it when hot reload swaps
        the client manifest. Dynamic layouts are never cached.
        """
        if pattern is None:
            self._layout_shells.clear()
        else:
            self._layout_shells.pop(pattern, None)

    def _module(self, path):
        key = _module_key(self.root, path)
        if key not in self.modules:
            self.modules[key] = _load(self.root, path, self._namespace)
        return self.modules[key]

    def error_response(self, exc, digest=None):
        """Render a 500 for an unexpected failure.

        The production body never leaks the exception — it carries the
        ``request_id`` and the error ``digest`` instead, so a user reporting
        "500" can be matched to the log line (which records the same
        digest) and the registry entry :func:`errors.lookup_error` restores.
        The ``x-request-id`` response header carries the same id. In
        development the page also shows the digest, how many times this
        error has been seen, and whether the traceback was truncated.
        Signal errors never reach this method: the dispatcher converts them
        to their response before an error log is written.
        """
        if digest is None:
            digest, count = digest_for_error(exc)
        else:
            count = error_count(digest)
        request_id = get_request_id()
        if not self.debug:
            detail = "Internal Server Error"
            parts = []
            if request_id:
                parts.append(f"request_id: {request_id}")
            if digest:
                parts.append(f"digest: {digest}")
            if parts:
                detail += " (" + ", ".join(parts) + ")"
            return Response(detail, status=500)
        digest = _dev_error_digest(exc)
        if digest not in self._dev_error_counts and len(self._dev_error_counts) >= _DEV_ERROR_COUNT_LIMIT:
            self._dev_error_counts.clear()
        count = self._dev_error_counts.get(digest, 0) + 1
        self._dev_error_counts[digest] = count
        full = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        truncated = len(full) > _DEV_TRACEBACK_MAX_CHARS
        omitted = len(full) - _DEV_TRACEBACK_MAX_CHARS if truncated else 0
        details = full[-_DEV_TRACEBACK_MAX_CHARS:] if truncated else full
        request_id = get_request_id() or "unknown"
        route = route_var.get() or "unknown"
        occurred = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        header = (
            "<dl>"
            f"<dt>Exception</dt><dd>{html.escape(type(exc).__qualname__ + ': ' + str(exc))}</dd>"
            f"<dt>Request ID</dt><dd>{html.escape(request_id)}</dd>"
            f"<dt>Route</dt><dd>{html.escape(route)}</dd>"
            f"<dt>Digest</dt><dd>{digest}</dd>"
            f"<dt>Time</dt><dd>{occurred}</dd>"
            f"<dt>Occurrences</dt><dd>Seen {count} time{'s' if count != 1 else ''} in this process</dd>"
            "</dl>"
        )
        notice = ""
        if truncated:
            notice = (
                f"<p>Traceback truncated: showing the last {_DEV_TRACEBACK_MAX_CHARS:,} of "
                f"{len(full):,} characters ({omitted:,} characters omitted from the start).</p>"
            )
        body = ("<!DOCTYPE html><html><head><title>PyNext development error</title></head>"
                "<body><h1>Development error</h1>"
                + header + notice + "<pre>" + _render_dev_traceback(details) + "</pre></body></html>")
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
        Includes native requests/streams, unfinished synchronous handlers,
        and ASGI admission callbacks, even after their caller timed out.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._inflight_lock:
                remaining = self._inflight + self._inflight_streams + self._sync_active
            remaining += self._asgi_admission.inflight
            if remaining <= 0:
                return True
            if time.monotonic() >= deadline:
                log.warning("Shutdown timed out with %d requests still in flight", remaining)
                return False
            await asyncio.sleep(0.05)

    async def revalidate(self, path, type=None):
        """Invalidate ISR entries for ``path``, optionally scoped by ``type``.

        With no ``type`` this invalidates one concrete ISR path, as
        before. ``type="page"`` with a dynamic route pattern (``"/news/[slug]"``)
        invalidates every cached entry for that pattern, and ``type="layout"``
        invalidates every ISR page that uses the layout defined at ``path``
        (``"/"`` is the root layout, so it covers all pages). A dynamic
        pattern without ``type`` only warns and invalidates nothing, like
        Next.js ``revalidatePath``. ``"/"`` and ``"/index"`` are aliases.
        Returns True when at least one entry (or query-variant generation)
        was invalidated.
        """
        if type is not None and type not in ("page", "layout"):
            raise ValueError("revalidate type must be 'page' or 'layout'")
        if not isinstance(path, str):
            raise TypeError("ISR path must be a string")
        path = _decode_path(path)
        if path != "/" and path.endswith("/"):
            raise ValueError("ISR path must not have a trailing slash")
        if _is_dynamic_pattern(path):
            if type is None:
                warnings.warn(
                    f"revalidate({path!r}) is a dynamic route pattern; pass "
                    "type='page' to invalidate its cached entries or "
                    "type='layout' to invalidate its layout's pages",
                    UserWarning, stacklevel=2)
                return False
            if type == "page":
                return await self._revalidate_pattern(path)
            return await self._revalidate_layout(path)
        if type == "layout":
            return await self._revalidate_layout(path)
        # Concrete page scope. "/" and "/index" name the same page, unless
        # the app defines a real /index route of its own.
        if self.router.matches("/index") is None:
            primary = "/" if path == "/index" else path
            removed = await self._revalidate_one(primary)
            if primary == "/":
                removed = await self.isr.invalidate("/index") or removed
            return removed
        return await self._revalidate_one(path)

    async def _revalidate_one(self, path):
        """Invalidate one concrete ISR path. Raises ValueError when the
        path does not name an ISR page (or is shadowed by an asset)."""
        match = self.router.matches(path)
        if (match is None or self.render_modes.get(match[0]) != "isr" or
                path in self.public_assets or path in self.static_pages or path in self.client_assets):
            raise ValueError("Path does not match an ISR page")
        # _revalidate_decoded also invalidates the implicit path tags,
        # so "/" reaches its "/index" alias (and vice versa).
        return await self._revalidate_decoded(path, "page")

    async def _invalidate_cached_path(self, path, pattern):
        """Invalidate one concrete cached path of ``pattern``.

        Pages with ISR query keys store entries under a hashed key, so
        rotating their variant generation is what makes the old entries
        unreachable; other pages are removed directly.
        """
        if self.isr_queries.get(pattern):
            _, existed = await self.isr.variant_generation(path, invalidate=True)
            return existed
        return await self.isr.invalidate(path)

    def _check_revalidate_path(self, path, type):
        """Validate a revalidation target without changing any cache.

        Returns the decoded path. Raises ``TypeError``/``ValueError`` for
        a bad path, a non-ISR page (``type="page"``), or an unknown layout
        (``type="layout"``).
        """
        if type not in ("page", "layout"):
            raise ValueError('revalidate type must be "page" or "layout"')
        if not isinstance(path, str):
            raise TypeError("ISR path must be a string")
        path = _decode_path(path)
        if path != "/" and path.endswith("/"):
            raise ValueError("ISR path must not have a trailing slash")
        if type == "layout":
            try:
                self._layout_module_for_path(path)
            except ValueError:
                raise ValueError("Path does not match a layout") from None
            return path
        match = self.router.matches(path)
        if (match is None or self.render_modes.get(match[0]) != "isr" or
                path in self.public_assets or path in self.static_pages or path in self.client_assets):
            raise ValueError("Path does not match an ISR page")
        return path

    async def _revalidate_decoded(self, path, type):
        """Invalidate an already-decoded, already-validated path."""
        if type == "layout":
            return await self.isr.invalidate_tag(_layout_tag(path)) > 0
        match = self.router.matches(path)
        if self.isr_queries.get(match[0]):
            _, existed = await self.isr.variant_generation(path, invalidate=True)
            return existed
        evicted = 0
        for tag in _implicit_path_tags(path):
            evicted += await self.isr.invalidate_tag(tag)
        removed = await self.isr.invalidate(path)
        return removed or evicted > 0

    async def _revalidate_webhook(self, request):
        """Serve ``POST /_pynext/revalidate`` (on-demand revalidation).

        Disabled (404) while no ``revalidate_secret`` is configured.
        Otherwise the caller must present it as ``Authorization: Bearer
        <secret>`` (constant-time comparison). The JSON body takes
        ``paths`` (list of ISR paths, ``type`` ``"page"`` or ``"layout"``)
        and/or ``tags`` (list of cache tags, with optional ``expire``
        seconds for deferred tag expiry). Every target is validated
        before anything is invalidated, so a bad request changes nothing.
        """
        if request.method != "POST":
            return Response("Method Not Allowed", status=405, headers=[(b"allow", b"POST")])
        if self.revalidate_secret is None:
            return Response("Not Found", status=404)
        auth = next((value for name, value in request.headers
                     if name.lower() == b"authorization"), None)
        token = auth.decode("ascii", errors="ignore") if auth is not None else ""
        if not token.startswith("Bearer ") or not hmac.compare_digest(
                token[len("Bearer "):], self.revalidate_secret):
            return Response("Unauthorized", status=401,
                            headers=[(b"www-authenticate", b"Bearer")])
        try:
            payload = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return Response("Bad Request", status=400)
        if not isinstance(payload, dict) or set(payload) - {"paths", "tags", "type", "expire"}:
            return Response("Bad Request", status=400)
        paths = payload.get("paths", [])
        tags = payload.get("tags", [])
        revalidate_type = payload.get("type", "page")
        expire = payload.get("expire")
        if (not isinstance(paths, list) or not all(isinstance(item, str) for item in paths) or
                not isinstance(tags, list) or not all(isinstance(item, str) for item in tags) or
                revalidate_type not in ("page", "layout") or
                (expire is not None and
                 (isinstance(expire, bool) or not isinstance(expire, (int, float)))) or
                (not paths and not tags) or (expire is not None and not tags)):
            return Response("Bad Request", status=400)
        try:
            decoded_paths = [self._check_revalidate_path(path, revalidate_type) for path in paths]
            for tag in tags:
                validate_cache_tag(tag)
            if expire is not None and (
                    not math.isfinite(expire) or expire < 0 or expire > MAX_CACHE_SECONDS):
                raise ValueError(
                    "expire must be a finite number of seconds from 0 to "
                    f"{int(MAX_CACHE_SECONDS)}")
        except (TypeError, ValueError) as exc:
            return Response(f"Bad Request: {exc}", status=400)
        results = {}
        evicted = 0
        for path in decoded_paths:
            invalidated = await self._revalidate_decoded(path, revalidate_type)
            results[path] = invalidated
            if invalidated:
                evicted += 1
        tag_counts = {}
        for tag in tags:
            count = await self.isr.invalidate_tag(tag, expire=expire)
            tag_counts[tag] = count
            evicted += count
        return JSONResponse({"paths": results, "tags": tag_counts, "evicted_entries": evicted})

    async def revalidate_tag(self, tag, expire=None):
        """Invalidate every ISR entry carrying ``tag`` for this app instance.

        ``expire=None`` expires matching entries immediately; ``expire``
        seconds keeps them merely stale until that delay passes. With a
        shared cache backend the invalidation is visible to other
        workers; with the memory backend it is process-local. Returns
        True when the invalidation was recorded.
        """
        return await self.isr.revalidate_tag(tag, expire)

    def _merge_dynamic_accesses(self, pattern, state):
        """Fold one prerender's recorded accesses into the app report.

        Only newly seen expressions are logged, so a dev SSG page rendered
        on every request (or a regenerating ISR page) warns once per
        expression instead of once per render. The report is written to
        the build manifest as ``dynamic_accesses``.
        """
        if not state.accesses:
            return
        known = self.dynamic_accesses.setdefault(pattern, [])
        seen = {entry["expression"] for entry in known}
        fresh = [access for access in state.accesses if access.expression not in seen]
        for access in fresh:
            seen.add(access.expression)
            known.append(access.to_dict())
        if len(known) > MAX_DYNAMIC_ACCESSES:
            del known[MAX_DYNAMIC_ACCESSES:]
        if fresh:
            log.warning("Static page %s read dynamic data during render:\n%s",
                        pattern, format_dynamic_accesses(fresh))

    async def _dev_generated_paths(self, pattern):
        """Paths a dynamic SSG pattern would generate, cached per process.

        Returns None when the generator cannot run in development, in
        which case the dev/prod consistency check is skipped rather than
        guessed at.
        """
        if pattern in self._dev_static_paths:
            return self._dev_static_paths[pattern]
        generator = self.static_generators.get(pattern)
        if generator is None:
            paths = {pattern}
        else:
            try:
                params_list = await _invoke(generator)
                paths = {_materialize(pattern, params) for params in params_list}
            except Exception:
                log.warning("Cannot determine generated SSG paths for %s; "
                            "skipping the dev/prod consistency check", pattern, exc_info=True)
                paths = None
        self._dev_static_paths[pattern] = paths
        return paths
    async def _revalidate_pattern(self, pattern):
        """Invalidate every cached entry for one ISR route pattern."""
        if self.render_modes.get(pattern) != "isr":
            raise ValueError("Path does not match an ISR page")
        if not _is_dynamic_pattern(pattern):
            return await self._revalidate_one(pattern)
        removed = False
        for key in await self.isr.cached_keys():
            match = self.router.matches(key)
            if match is not None and match[0] == pattern:
                removed = await self._invalidate_cached_path(key, pattern) or removed
        return removed

    def _layout_module_for_path(self, path):
        """The layout module defined at ``path`` (``app/<path>/layout.py``).

        Route groups do not appear in URLs, so a layout inside a group is
        also found by its URL directory. Raises ValueError when no layout
        is defined there, or when several group layouts share the URL.
        """
        known = {_posix_path(module) for layouts in self.entry_layouts.values() for module in layouts}
        direct = "app/layout.py" if path == "/" else f"app{path}/layout.py"
        if direct in known:
            return direct
        matches = sorted(module for module in known if _layout_url_dir(module) == path)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous layout for {path}: {', '.join(matches)}")
        raise ValueError(f"No layout at {path}")

    async def _revalidate_layout(self, path):
        """Invalidate every ISR page that uses the layout at ``path``."""
        module = self._layout_module_for_path(path)
        targets = [pattern for pattern, layouts in self.entry_layouts.items()
                   if module in layouts and self.render_modes.get(pattern) == "isr"]
        removed = False
        cached = None
        for pattern in targets:
            if _is_dynamic_pattern(pattern):
                if cached is None:
                    cached = await self.isr.cached_keys()
                for key in cached:
                    match = self.router.matches(key)
                    if match is not None and match[0] == pattern:
                        removed = await self._invalidate_cached_path(key, pattern) or removed
            else:
                removed = await self._revalidate_one(pattern) or removed
        return removed


    def get_isr_policy(self, pattern):
        """Return the effective ISR policy for a route pattern.

        Lookup order is "runtime override, then the build manifest"
        (copy-on-write, mirroring Next.js ``SharedCacheControls``):
        an override set with :meth:`set_isr_policy` wins, otherwise the
        policy the page declared at build time is returned. The result
        is a copy, so mutating it cannot change the stored policy.
        Raises :class:`ValueError` when ``pattern`` is not an ISR route.
        """
        if not isinstance(pattern, str):
            raise TypeError("ISR pattern must be a string")
        override = self.isr_policy_overrides.get(pattern)
        if override is not None:
            return dict(override)
        policy = self.isr_policies.get(pattern)
        if policy is None:
            raise ValueError(f"No ISR policy for route pattern {pattern!r}")
        return dict(policy)

    def set_isr_policy(self, pattern, *, revalidate=None, stale=None):
        """Override an ISR policy at runtime, without rewriting the manifest.

        ``pattern`` is a route pattern such as ``"/blog/[slug]"``. At
        least one of ``revalidate``/``stale`` must be given; an omitted
        value inherits the currently effective policy. Both values are
        validated exactly like :func:`pynext.components.page` enforces
        them (``revalidate`` positive, ``stale`` non-negative, both at
        most 31536000 seconds). Raises :class:`ValueError` for a
        non-ISR pattern or an invalid value.

        The override is process-local copy-on-write state: the build
        manifest file and the base :attr:`isr_policies` are untouched,
        so no rebuild is needed and production startup still passes
        its manifest check. Each worker process needs its own call.
        The override applies to generations started after the call;
        entries already cached keep the lifetimes they were stored
        with, and in-flight generations keep the policy snapshot they
        started with. Pair with :meth:`revalidate` when an already
        cached entry must pick the new policy up immediately. Returns
        a copy of the new effective policy.
        """
        if not isinstance(pattern, str):
            raise TypeError("ISR pattern must be a string")
        if revalidate is None and stale is None:
            raise ValueError("set_isr_policy requires revalidate or stale")
        current = self.get_isr_policy(pattern)
        policy = _isr_policy(
            current["revalidate"] if revalidate is None else revalidate,
            current["stale"] if stale is None else stale,
        )
        self.isr_policy_overrides[pattern] = policy
        return dict(policy)

    def clear_isr_policy(self, pattern):
        """Drop the runtime override for one ISR route pattern.

        Returns True when an override existed, False otherwise. The
        route then falls back to its build-time policy. Raises
        :class:`ValueError` for a non-ISR pattern.
        """
        if not isinstance(pattern, str):
            raise TypeError("ISR pattern must be a string")
        if pattern not in self.isr_policies:
            raise ValueError(f"No ISR policy for route pattern {pattern!r}")
        return self.isr_policy_overrides.pop(pattern, None) is not None

    def clear_isr_policies(self):
        """Drop every runtime ISR policy override. Returns how many were removed."""
        count = len(self.isr_policy_overrides)
        self.isr_policy_overrides.clear()
        return count

    async def _render_stream_chunk(self, node):
        """Serialize one streamed UI node to HTML on a worker thread."""
        bound = bind_islands(_node(node), self.client_manifest, include_runtime=False)
        payload = json.dumps(bound, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return await asyncio.to_thread(_native.render, payload)


    async def _layout_shell(self, pattern, layouts, request=None):
        """Return the cached layout shell entry for ``pattern``.

        The entry holds the layouts applied once to a marker text node
        (the unbound shell tree, plus a rendered prefix/suffix split
        once a streaming page needs it). Layouts only receive
        ``children``, so the shell cannot depend on the request or the
        route params; a layout that reads other mutable state must opt
        out with ``@layout(dynamic=True)``, in which case this returns
        None and the caller renders the shell on every request. Returns
        the entry dict, or None for dynamic layouts.

        ``request`` is threaded through to the layout invocations so a
        sync layout runs under the sync-worker limit (``_invoke_sync``)
        exactly as it did before shell caching; without it the layout
        would run on a bare worker thread, bypassing the limit and its
        tracking counter. Prerenders pass None and keep the old bypass,
        as they did before caching existed.
        """
        if self._layout_shell_dynamic.get(pattern, False):
            return None
        entry = self._layout_shells.get(pattern)
        if entry is not None:
            return entry
        lock = self._layout_shell_locks.get(pattern)
        if lock is None:
            lock = asyncio.Lock()
            self._layout_shell_locks[pattern] = lock
        async with lock:
            entry = self._layout_shells.get(pattern)
            if entry is not None:
                return entry
            # The marker is framework-generated, not a page read: suppress
            # it so it is never misreported as the page being dynamic.
            with suppress_dynamic_tracking():
                marker = "__pynext_shell_" + uuid.uuid4().hex + "__"
            shell = {"kind": "text", "value": marker}
            for layout_fn in reversed(layouts):
                shell = await _invoke(layout_fn, children=shell, request=request)
                if not isinstance(shell, dict):
                    raise TypeError(
                        f"Layout for {pattern} must return a render node dict, "
                        f"got {type(shell).__name__}"
                    )
            entry = {"tree": shell, "marker": marker, "prefix": None, "suffix": None}
            self._layout_shells[pattern] = entry
            return entry

    async def _render_shell_split(self, pattern, shell, marker):
        """Bind and render a shell tree, splitting it around its marker.

        Returns ``(prefix, suffix)``. Raises ValueError unless the
        marker renders exactly once — the same "children exactly once"
        invariant the streaming path has always enforced.
        """
        bound = bind_islands(shell, self.client_manifest, dev_id=self._dev_id)
        payload = json.dumps(bound, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        html = await asyncio.to_thread(_native.render, payload)
        parts = html.split(marker)
        if len(parts) != 2:
            raise ValueError(
                f"Layout for {pattern} must render its children exactly once"
            )
        prefix, suffix = parts
        if shell.get("tag", "").lower() == "html":
            prefix = "<!DOCTYPE html>" + prefix
        return prefix, suffix

    async def _layout_shell_html(self, pattern, entry):
        """Return the cached ``(prefix, suffix)`` HTML split for a shell entry.

        The split is derived lazily: binding the shell alone can fail
        for shapes that only become valid once the page is substituted
        (for example a layout without a ``<body>`` whose page provides
        one), so buffered pages never pay for it — they substitute into
        the tree and bind the composed tree instead.
        """
        if entry["prefix"] is not None:
            return entry["prefix"], entry["suffix"]
        lock = self._layout_shell_locks.get(pattern)
        if lock is None:
            lock = asyncio.Lock()
            self._layout_shell_locks[pattern] = lock
        async with lock:
            if entry["prefix"] is not None:
                return entry["prefix"], entry["suffix"]
            prefix, suffix = await self._render_shell_split(
                pattern, copy.deepcopy(entry["tree"]), entry["marker"])
            entry["prefix"], entry["suffix"] = prefix, suffix
            return prefix, suffix

    async def _streaming_page_response(self, pattern, chunks, layouts, tracker=None, request=None):
        """Wrap an async iterable of UI nodes in a StreamingResponse.

        The layout shell is rendered once per route and cached; the
        cached prefix is sent first, each page chunk is serialized and
        sent as it arrives, and the cached suffix closes the document.
        Layouts that opted out with ``@layout(dynamic=True)`` are still
        rendered on every request, exactly as before. Layouts must render
        their children exactly once; anything else fails before headers
        are sent. SSG consumes the stream during the build. ISR streams
        its first generation while retaining a bounded copy for the cache.

        When ``tracker`` is given, the stream owns it: ``page_stream()``
        exits it when the body is fully consumed or the stream is closed,
        so dynamic accesses in later chunks are still recorded.

        ``request`` is threaded through to the layout invocations (see
        :meth:`_layout_shell`) so sync layouts stay under the
        sync-worker limit; prerenders pass None.
        """
        entry = await self._layout_shell(pattern, layouts, request)
        if entry is None:
            # The marker is framework-generated, not a page read: suppress
            # it so it is never misreported as the page being dynamic.
            with suppress_dynamic_tracking():
                marker = "__pynext_stream_" + uuid.uuid4().hex + "__"
            shell = {"kind": "text", "value": marker}
            for layout_fn in reversed(layouts):
                shell = await _invoke(layout_fn, children=shell, request=request)
                if not isinstance(shell, dict):
                    raise TypeError(
                        f"Layout for {pattern} must return a render node dict, "
                        f"got {type(shell).__name__}"
                    )
            prefix, suffix = await self._render_shell_split(pattern, shell, marker)
        else:
            prefix, suffix = await self._layout_shell_html(pattern, entry)

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
                if tracker is not None:
                    tracker.__exit__(None, None, None)

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

    async def _render_isr_cached(self, pattern, params, query, key, policy=None, tags=None):
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

        if policy is None:
            policy = self.get_isr_policy(pattern)
        if tags is None:
            tags = tuple(self.isr_tags.get(pattern, ()))
        task = asyncio.create_task(self.isr.get_or_render(
            key, policy, render, tags=tags))

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
        """Render one page, tracking dynamic accesses for SSG/ISR pages.

        Static renders run under a :class:`PrerenderTracker`: dynamic reads
        are recorded, and in ``dynamic="error"`` mode the first one raises
        :class:`StaticGenBailoutError`. A streamed body keeps the tracker
        alive until the stream ends, so accesses in later chunks count too.
        ``force-dynamic`` SSG pages render untracked: they are dynamic by
        design, so tracking them would only produce false warnings.
        """
        fn, layouts = self.entries[pattern]
        if query is None:
            query = self._page_query(pattern, request)
        mode = self.render_modes.get(pattern)
        dynamic_mode = self.dynamic_modes.get(pattern, "auto")
        if mode not in {"ssg", "isr"} or dynamic_mode == "force-dynamic":
            # SSR pages and force-dynamic pages are dynamic by design: there
            # is nothing static to protect, so no tracker is installed and
            # no "static page read dynamic data" warning is produced.
            return await self._render_page_inner(pattern, fn, layouts, request, params,
                                                 query, csp_nonce)
        tracker = track_prerender(pattern, dynamic_mode,
                                  on_finish=lambda state: self._merge_dynamic_accesses(pattern, state))
        tracker.__enter__()
        try:
            response = await self._render_page_inner(pattern, fn, layouts, request, params,
                                                     query, csp_nonce, tracker=tracker)
        except BaseException:
            tracker.__exit__(None, None, None)
            raise
        if isinstance(response, StreamingResponse):
            # The stream owns the tracker now: page_stream() exits it when
            # the body is fully consumed or the stream is closed.
            return response
        tracker.__exit__(None, None, None)
        return response

    async def _render_page_inner(self, pattern, fn, layouts, request, params, query, csp_nonce,
                                 tracker=None):
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
            return await self._streaming_page_response(pattern, result, layouts, tracker=tracker,
                                                       request=request)
        tree = result
        if not isinstance(tree, dict):
            raise TypeError(f"Page {pattern} must return a render node dict, got {type(tree).__name__}")
        entry = await self._layout_shell(pattern, layouts, request)
        if entry is None:
            # Dynamic layouts: render the shell on every request.
            for layout_fn in reversed(layouts):
                tree = await _invoke(layout_fn, children=tree, request=request)
                if not isinstance(tree, dict):
                    raise TypeError(f"Layout for {pattern} must return a render node dict, got {type(tree).__name__}")
        else:
            # Substitute the page into the cached shell instead of
            # re-invoking the layouts, then bind the composed tree as
            # before so island binding and the client runtime land in
            # exactly the same places (and the per-request CSP nonce
            # still covers the runtime script).
            tree, replaced = _substitute_shell_marker(entry["tree"], entry["marker"], tree)
            if replaced != 1:
                raise ValueError(
                    f"Layout for {pattern} must render its children exactly once"
                )
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
        try:
            return await self._dispatch_response(request)
        except _SyncHandlerBusy:
            response = Response("Service Unavailable", status=503, headers=[(b"retry-after", b"1")])
            apply_path_headers(response, request.path, path_headers=self.path_headers)
            apply_cors_headers(request, response, config=self.security)
            apply_security_headers(response, config=self.security, production=self.production)
            if request.method == "HEAD":
                response.body = b""
            return response
        except RequestCancelled:
            if request.cancelled:
                # A cooperative worker may observe the signal before its
                # awaiter is cancelled. Keep that normal exit out of the
                # transport's application-error/500 conversion.
                raise asyncio.CancelledError from None
            raise
        except asyncio.CancelledError:
            request._cancel_event.set()
            raise

    async def _dispatch_response(self, request):
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
                response = Response("Method Not Allowed", status=405,
                                    headers=[(b"allow", b"GET, HEAD")])
            elif request.path == "/healthz":
                body = b"" if request.method == "HEAD" else b"ok"
                response = Response(body, media_type="text/plain; charset=utf-8")
            elif self.is_ready:
                body = b"" if request.method == "HEAD" else b"ready"
                response = Response(body, media_type="text/plain; charset=utf-8")
            else:
                response = Response(b"" if request.method == "HEAD" else b"not ready", status=503,
                                    media_type="text/plain; charset=utf-8",
                                    headers=[(b"retry-after", b"1")])
            # Health responses skip the security-header step here (the
            # transport applies it); path headers still apply, before it.
            return apply_path_headers(response, request.path, path_headers=self.path_headers)
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
            apply_path_headers(response, request.path, path_headers=self.path_headers)
            apply_security_headers(response, config=self.security, production=self.production)
            if request.method == "HEAD":
                if isinstance(response, StreamingResponse):
                    await response.aclose()
                    response._head_only = True
                response.body = b""
            return response
        if preflight is not None:
            apply_path_headers(preflight, request.path, path_headers=self.path_headers)
            apply_security_headers(preflight, config=self.security, production=self.production)
            return preflight
        response = await self._dispatch_timed(request)
        apply_cors_headers(request, response, config=self.security)
        apply_path_headers(response, request.path, path_headers=self.path_headers)
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
            except SignalError as exc:
                # Control-flow signal (notFound/forbidden/redirect): answer
                # with its response and count it, but write no error log.
                # The access log line in the finally block still records it.
                status = exc.status
                response = exc.to_response()
                self.metrics.observe_error(route_var.get() or "__unmatched__",
                                           type(exc).__name__, exc.label_digest)
                request_id = get_request_id()
                if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
                    response.headers.append((b"x-request-id", request_id.encode("ascii")))
                return response
            except BadRequest as exc:
                status = 400
                self.metrics.observe_error(route_var.get() or "__unmatched__",
                                           type(exc).__name__)
                raise
            except _SyncHandlerBusy:
                status = 503
                raise
            except (asyncio.CancelledError, RequestCancelled) as exc:
                if isinstance(exc, asyncio.CancelledError) or request.cancelled:
                    status = getattr(request._cancel_event, "status", 499)
                    request_span.set_attribute("pynext.cancelled", True)
                raise
            except Exception as exc:
                status = 500
                digest, _count = digest_for_error(exc)
                self.metrics.observe_error(route_var.get() or "__unmatched__",
                                           type(exc).__name__, digest)
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
        # Matching runs on the per-segment decoded form (Next.js
        # decodePathParams): static segments compare decoded, while an
        # encoded delimiter stays one segment. The encoded path is kept
        # for the trailing-slash redirect so the encoding is preserved.
        encoded_path = request.raw_path or request.path
        match_path = _normalize_path_for_matching(encoded_path)
        if self.hot_reload and path == "/_pynext/dev/client":
            route_var.set("__dev__")
            if request.method not in {"GET", "HEAD"}:
                return Response("Method Not Allowed", status=405, headers=[(b"allow", b"GET, HEAD")])
            response = JSONResponse({"id": self._dev_id, "bundles": self.client_manifest["bundles"]},
                                    headers=[(b"cache-control", b"no-store")])
            if request.method == "HEAD":
                response.body = b""
            return response
        if match_path != "/" and match_path.endswith("/"):
            route_var.set("__redirect__")
            # Redirect to the encoded form so an encoded delimiter in a
            # parameter value is preserved instead of becoming a separator.
            if request.raw_path:
                location = encoded_path.rstrip("/").encode("ascii")
            else:
                location = quote(encoded_path.rstrip("/"), safe="/!$&'()*+,;=:@-._~").encode("ascii")
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
        if path == "/_pynext/revalidate":
            route_var.set("__revalidate__")
            return await self._revalidate_webhook(request)
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
        match = self.router.matches(match_path)
        if match is None:
            route_var.set("__not_found__")
            return Response("Not Found", status=404)
        pattern, raw_params, kinds = match
        route_var.set(pattern)
        # The matched values are already decoded and delimiter-safe
        # (see _normalize_path_for_matching); collapse them by the
        # router-reported kinds, never by a substring test on the pattern.
        params = _typed_params(raw_params, kinds)
        request = Request(request.method, request.path, request.query_string, request.headers, request.body,
                          params, self, request.state, request._cancel_event, raw_path=request.raw_path)
        spec = self.manifest["routes"][pattern]
        entry = self.entries[pattern]
        if (self.production and self.render_modes.get(pattern) == "ssg"
                and self.dynamic_modes.get(pattern) != "force-dynamic"
                and path not in self.static_pages):
            return Response("Not Found", status=404)
        if (not self.production and self.render_modes.get(pattern) == "ssg"
                and self.dynamic_modes.get(pattern) != "force-dynamic"):
            # Dev/prod consistency (§3.2 #4): development renders SSG pages
            # on request, so a path generate_static_params would never
            # produce silently works in dev and 404s in production. Warn
            # here; --strict-ssg makes dev answer 404 as well.
            generated = await self._dev_generated_paths(pattern)
            if generated is not None and path not in generated:
                log.warning("SSG path %s for %s is not generated by generate_static_params; "
                            "it will 404 in production", path, pattern)
                if self.strict_ssg:
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
            if (self.production and self.render_modes[pattern] == "ssg"
                    and self.dynamic_modes.get(pattern) != "force-dynamic"):
                response = asset_response(request, self.static_pages[path], "text/html; charset=utf-8",
                                          self.asset_etags[path], cache_control=b"public, max-age=0, must-revalidate",
                                          extra_headers=[(b"vary", b"Cookie, Authorization")])
            elif self.render_modes[pattern] == "isr" and (self.production or self.debug):
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
                    # The cache key is the routing-normalized path, not
                    # the fully decoded one: "/files/a%2Fb" and
                    # "/files/a/b" are different routes (different
                    # patterns and params) and must not share an entry.
                    # The normalized form is also what revalidate() sees
                    # for an encoded URL, so invalidation still matches.
                    cache_key = match_path
                    if self.isr_queries[pattern]:
                        try:
                            generation, _ = await self.isr.variant_generation(match_path)
                        except SharedCacheUnavailable:
                            self.metrics.observe_regeneration("failure")
                            return Response("Service Unavailable", status=503,
                                            headers=[(b"retry-after", b"1")])
                        encoded = json.dumps(query, ensure_ascii=True, separators=(",", ":")).encode("ascii")
                        cache_key = ("\0isr:" + hashlib.sha256(match_path.encode("utf-8")).hexdigest() + ":" +
                                     generation + ":" + hashlib.sha256(encoded).hexdigest())
                    cache_before, retained_body = self.isr.store.lookup(cache_key)
                    self.metrics.observe_cache(cache_before)
                    policy = self.get_isr_policy(pattern)
                    if self.debug:
                        # Dev relaxation: serve stale entries until expire
                        # and retain them for at least the floor, so a
                        # reload hits the cache. Production keeps the
                        # declared policy.
                        policy = dev_retention_policy(policy)
                    try:
                        # Implicit path tags stay on the decoded path:
                        # the tag charset (letters, digits, "_", ".",
                        # "/", ":", "-") rejects "%", so a normalized
                        # tag such as "_pynext_path_/files/a%2Fb" would
                        # fail validation and 500 the request. Key
                        # invalidation above still reaches the entry
                        # because the key itself is the normalized path.
                        tags = (tuple(self.isr_tags.get(pattern, ())) +
                                tuple(_implicit_path_tags(path)))
                        body = await self._render_isr_cached(pattern, params, query, cache_key, policy, tags)
                    except CacheBusy:
                        self.metrics.observe_cache("busy")
                        self.metrics.observe_regeneration("failure")
                        if retained_body is None:
                            # C-2 retention: the store lookup above has
                            # no body for an expired entry (the native
                            # cache drops it on lookup), so ask the ISR
                            # cache for the retained previous value.
                            retained_body = await self.isr.retained_body(cache_key)
                        if retained_body is not None:
                            # C-2 retention: the server is too busy to
                            # regenerate, not the page broken — serve
                            # the previous value instead of failing the
                            # request (stale-while-revalidate).
                            return Response(
                                retained_body, media_type="text/html; charset=utf-8", headers=[
                                    (b"cache-control", b"public, max-age=0, must-revalidate"),
                                    (b"vary", b"Cookie, Authorization"),
                                ])
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

    async def _rate_limit_denied(self, scope, path):
        """One bounded admission check for both ASGI HTTP and WebSocket."""
        limiter = self.rate_limit
        if limiter is None:
            return None
        client = scope.get("client")
        client_ip = client[0] if client else None
        headers = tuple(scope.get("headers", ()))

        def decide():
            if _is_exempt_path(limiter, path):
                return True, None
            key = limiter.key_for(client_ip, headers)
            allowed, retry_after = limiter.allow(key)
            if type(allowed) is not bool or (retry_after is not None and (
                    not isinstance(retry_after, int) or not 0 <= retry_after <= 2**64 - 1)):
                raise ValueError("Invalid rate-limit decision")
            return allowed, retry_after

        try:
            allowed, retry_after = await self._asgi_admission.check(decide)
        except Exception:
            # Match native admission: callback failures and overload fail
            # closed regardless of a backend's own failure-policy choice.
            return Response("Service Unavailable", status=503, headers=[(b"retry-after", b"1")])
        return None if allowed else rate_limited_response(max(1, retry_after or 1))

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
        return await self._rate_limit_denied(scope, path)

    async def _authorize_websocket(self, path, query_string=b"", headers=(), raw_path=None):
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
        # Match on the per-segment decoded form (Next.js
        # decodePathParams) so the pre-flight checks the same route the
        # handler will run; when no encoded path is available (native
        # transport, where Rust already decoded it) the decoded path
        # normalizes to the same form.
        try:
            match_path = _normalize_path_for_matching(raw_path or path)
        except BadRequest:
            return Response("Bad Request", status=400)
        match = self.router.matches(match_path)
        if match is None:
            return Response("Not Found", status=404)
        pattern, raw_params, kinds = match
        handler = self.websocket_entries.get(pattern)
        if handler is None:
            return Response("Not Found", status=404)
        # The matched values are already decoded and delimiter-safe.
        params = _typed_params(raw_params, kinds)
        request = Request("GET", path, bytes(query_string),
                          tuple((bytes(name), bytes(value)) for name, value in headers),
                          b"", params, self, {}, raw_path=raw_path or "")
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
            path, encoded_path = _split_request_path(scope["path"], scope.get("raw_path"))
        except (ValueError, UnicodeError):
            await send({"type": "websocket.close", "code": 1008})
            return
        # Route lookup runs on the decoded path (unlike HTTP dispatch,
        # which matches on the normalized encoded form): the router
        # rejects a literal '?' in the path, so an encoded '%3F' that
        # decodes to '?' is refused here with a policy-violation close
        # before any rate-limit charge (B-1 / review #7). Matching on
        # the normalized form would hide the '?' and let the handshake
        # reach the rate limiter instead.
        match = self.router.matches(path)
        handler = None
        params = {}
        if match is not None:
            pattern, raw_params, kinds = match
            handler = self.websocket_entries.get(pattern)
            if handler is not None:
                # The matched values are already decoded and delimiter-safe.
                params = _typed_params(raw_params, kinds)
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
            apply_path_headers(denied, path, path_headers=self.path_headers)
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
                                                 scope.get("headers", []), raw_path=encoded_path)
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
        # Boundary hygiene (Next.js `filterInternalHeaders` at the
        # router-server entry point): strip internal-only headers before
        # any handler sees them — the request context, rate-limit and
        # proxy-trust decisions, the middleware chain, and the WebSocket
        # pre-flight all read from this same scope.
        if scope["type"] in ("http", "websocket"):
            scope["headers"] = filter_internal_headers(scope.get("headers", []))
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
        # Normalize before admission, as the native adapter does: malformed
        # paths never consume a token, even when the client's bucket is empty.
        request = None
        try:
            path, encoded_path = _split_request_path(scope["path"], scope.get("raw_path"))
            # Admission uses the routing-normalized path (Next.js
            # decodePathParams): an encoded delimiter stays one segment,
            # so "/free%2Fblocked" is not under "/free" even though its
            # fully decoded form "/free/blocked" is. Exemption must agree
            # with the handler routing actually serves, or an encoded
            # path escapes the limiter while reaching another handler.
            match_path = _normalize_path_for_matching(encoded_path)
        except (ValueError, UnicodeError, BadRequest):
            response = Response("Bad Request", status=400)
        else:
            # Liveness/readiness remain available when decision slots are full.
            denied = (None if match_path in ("/healthz", "/readyz") else
                      await self._rate_limit_denied(scope, match_path))
            if denied is not None:
                if scope["method"].upper() == "HEAD":
                    denied.body = b""
                apply_path_headers(denied, path, path_headers=self.path_headers)
                await _send_response(denied, send, config=self.security, production=self.production)
                return
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
                                          tuple(scope.get("headers", [])), b"".join(chunks), _app=self,
                                          raw_path=encoded_path)
                        break
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
                response = Response("Bad Request", status=400)
            except Exception as exc:
                digest, _count = digest_for_error(exc)
                self.metrics.observe_error("__unmatched__", type(exc).__name__, digest)
                log.exception("Request failed", extra={"error_digest": digest})
                response = self.error_response(exc, digest=digest)
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
                except SignalError as exc:
                    # A signal that escaped dispatch (defensive: the timed
                    # dispatcher converts these first) still answers without
                    # an error log.
                    result = exc.to_response()
                    request_id = get_request_id()
                    if request_id and not any(k == b"x-request-id" for k, _ in result.headers):
                        result.headers.append((b"x-request-id", request_id.encode("ascii")))
                except BadRequest as exc:
                    log.info("Bad request: %s", exc)
                    result = Response("Bad Request", status=400)
                except Exception as exc:
                    digest, _count = digest_for_error(exc)
                    log.exception("Request failed", extra={"error_digest": digest})
                    result = self.error_response(exc, digest=digest)
                    request_id = get_request_id()
                    if request_id and not any(k == b"x-request-id" for k, _ in result.headers):
                        result.headers.append((b"x-request-id", request_id.encode("ascii")))
                # error_response results bypass dispatch, so path headers
                # are applied here; routed responses already have them
                # (apply_path_headers is idempotent).
                apply_path_headers(result, path, path_headers=self.path_headers)
                if scope["method"].upper() == "HEAD":
                    result.body = b""
                try:
                    await _send_response(result, send, config=self.security, production=self.production)
                except asyncio.CancelledError:
                    request._cancel_event.set()
                    raise

            await _run_while_connected(dispatch_and_send(), receive)
            return
        if scope["method"].upper() == "HEAD":
            response.body = b""
        request_id = get_request_id()
        if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
            response.headers.append((b"x-request-id", request_id.encode("ascii")))
        await _send_response(response, send, config=self.security, production=self.production)


def create_app(root=".", *, production=False, security=None, cache_backend=DEFAULT_CONFIG.cache_backend,
               cache_path=None,
               observability=None, rate_limit=None, middleware=None, debug=False, resources=None, cache_url=None,
               cache_failure_policy=None, websocket_max_message=DEFAULT_CONFIG.websocket_max_message,
               hot_reload=False,
               trusted_proxies=None, sync_workers=32, strict_ssg=False, headers=None,
               revalidate_secret=None):
    root = Path(root).resolve()
    # .env files are loaded before the manifest is read or user code runs,
    # so server code, middleware and island bundling all see them.
    load_env(root, mode="production" if production else "development")
    if revalidate_secret is None:
        revalidate_secret = os.environ.get("PYNEXT_REVALIDATE_SECRET") or None
    return App(root, load_manifest(root) if production else discover(root), production=production,
               security=security, cache_backend=cache_backend, cache_path=cache_path,
               observability=observability, rate_limit=rate_limit, middleware=middleware, debug=debug,
               resources=resources, cache_url=cache_url, cache_failure_policy=cache_failure_policy,
               websocket_max_message=websocket_max_message, hot_reload=hot_reload,
               trusted_proxies=trusted_proxies, sync_workers=sync_workers, strict_ssg=strict_ssg,
               headers=headers, revalidate_secret=revalidate_secret)
