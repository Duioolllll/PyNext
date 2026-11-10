"""Axum server adapter; Python handlers run on the owning asyncio loop."""
import asyncio
import concurrent.futures
import contextvars
import logging
import math
import signal
import threading
import time
from dataclasses import replace

from . import _native
from .app import _decode_path, _invoke, _split_request_path, _typed_params
from .config import DEFAULT_CONFIG, effective_allowed_hosts
from .errors import SignalError, digest_for_error
from .http import (MAX_WS_MESSAGE, BadRequest, Request, Response, StreamingResponse, WebSocket,
                   WebSocketDisconnect, _FALLBACK_CSRF_SIGNING_KEY, _RequestCancellation,
                   apply_path_headers, apply_security_headers, ensure_shared_csrf_signing_key,
                   filter_internal_headers, security_headers)
from .mgmtplane import ManagementPlaneConfig
from .observability import begin_request_context, get_request_id, queue_wait_var
from .ratelimit import _is_exempt_path
from .standalone import log_bundle_info
from .ws_origin import websocket_origin_allowed

log = logging.getLogger("pynext")
REQUEST_TIMEOUT = 30
STREAM_CHUNK_TIMEOUT = 30
# Connection limits (A4). These are the native server defaults; the Rust
# accept loop enforces them, so a client that never completes a request
# cannot hold a Python worker thread or an execution slot. The values live
# in config.DEFAULT_CONFIG (the single default source); these names stay
# as the public aliases other modules import.
DEFAULT_MAX_CONNECTIONS = DEFAULT_CONFIG.max_connections
DEFAULT_MAX_CONNECTIONS_PER_IP = DEFAULT_CONFIG.max_connections_per_ip
DEFAULT_HEADER_READ_TIMEOUT_MS = DEFAULT_CONFIG.header_read_timeout_ms
# Minimum average body receive rate in bytes/second, enforced after a
# 2-second grace window (see read_limited_body in the native crate). 0
# disables the rate check; the whole-body timeout still applies.
DEFAULT_BODY_MIN_RATE_BYTES_PER_SEC = DEFAULT_CONFIG.body_min_rate_bytes_per_sec
# S-P1-14: a streaming response is cut off after this many seconds in
# total, even if every chunk arrives inside the 30s per-chunk timeout
# (see streaming_body in the native crate). 0 disables the cap.
DEFAULT_STREAMING_MAX_LIFETIME_SECS = DEFAULT_CONFIG.streaming_max_lifetime_secs
# S-P1-15: at most this many concurrent body slots per client IP, inside
# the global body_capacity (16 by default).
DEFAULT_BODY_CAPACITY_PER_IP = DEFAULT_CONFIG.body_capacity_per_ip
# S-P1-16: an idle WebSocket session is closed after this long, a session
# never lives longer than ws_max_lifetime_secs, and the server pings an
# idle session every ws_ping_interval_secs (unanswered for 10s: close).
# 0 disables each timeout/ping.
DEFAULT_WS_IDLE_TIMEOUT_SECS = DEFAULT_CONFIG.ws_idle_timeout_secs
DEFAULT_WS_MAX_LIFETIME_SECS = DEFAULT_CONFIG.ws_max_lifetime_secs
DEFAULT_WS_PING_INTERVAL_SECS = DEFAULT_CONFIG.ws_ping_interval_secs
# Upper bounds for the server limits (review #4 #5). The CLI enforces
# these at parse time; start_native enforces them again so a direct
# Python caller cannot smuggle a value past the CLI that overflows the
# native usize/u64/f64 conversion (OverflowError traceback) or exhausts
# resources (a million execution slots, a day-long shutdown drain).
MAX_CONCURRENT = 1_000_000
MAX_WAITING = 1_000_000
MAX_BODY_CAPACITY = 1_000_000
MAX_CONNECTIONS = 1_000_000
MAX_CONNECTIONS_PER_IP = 1_000_000
MAX_TIMEOUT_MS = 86_400_000  # 24 hours in milliseconds
MAX_BODY_MIN_RATE_BYTES_PER_SEC = 1_000_000_000  # 1 GiB/s
MAX_STREAMING_LIFETIME_SECS = 86400  # 24 hours in seconds
MAX_BODY_CAPACITY_PER_IP = 1_000_000
MAX_WS_TIMEOUT_SECS = 86400  # 24 hours in seconds
MAX_SHUTDOWN_TIMEOUT = 3600  # seconds
_CANCEL_POLL_SECONDS = 0.05
_WS_QUEUE_SIZE = 32
_WS_INBOX_TIMEOUT = 1


class _WsSession:
    """One Axum WebSocket connection, seen from the Python handler.

    The Rust side owns the socket: it pushes incoming frames into ``_inbox``
    and drains ``_outbox`` as frames. The Python handler talks to a
    :class:`pynext.http.WebSocket` whose transport is this session. All
    methods here run on Rust blocking threads (any thread may hold the GIL),
    so they only touch the event loop through ``call_soon_threadsafe`` or
    ``run_coroutine_threadsafe``.
    """

    def __init__(self, loop, max_message_bytes=MAX_WS_MESSAGE):
        self._loop = loop
        self._max_message_bytes = max_message_bytes
        self._inbox = asyncio.Queue(maxsize=_WS_QUEUE_SIZE)
        self._outbox = asyncio.Queue(maxsize=_WS_QUEUE_SIZE)
        self._future = None
        # The WebSocket object, set by _websocket_callback once the
        # handler's transport exists; serve() uses it to send a clean
        # 1001 close when the server drains (B6).
        self._ws = None

    def _push(self, item):
        # Apply backpressure at the socket boundary. If the handler cannot
        # keep up, Rust closes the connection rather than buffering forever.
        try:
            future = _submit(self._loop, self._inbox.put(item))
        except RuntimeError:
            return False
        try:
            future.result(timeout=_WS_INBOX_TIMEOUT)
            return True
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError, RuntimeError):
            future.cancel()
            return False

    def _push_text(self, data):
        if len(data.encode("utf-8")) > self._max_message_bytes:
            self._push_disconnect()
            return False
        return self._push(("text", data))

    def _push_bytes(self, data):
        if len(data) > self._max_message_bytes:
            self._push_disconnect()
            return False
        return self._push(("bytes", bytes(data)))

    def _push_disconnect(self):
        def disconnect():
            while not self._inbox.empty():
                self._inbox.get_nowait()
            self._inbox.put_nowait(("disconnect",))
        self._loop.call_soon_threadsafe(disconnect)

    def _take_outgoing(self, timeout_ms):
        try:
            future = _submit(self._loop, self._outbox.get())
        except RuntimeError:
            return None
        try:
            item = future.result(timeout=timeout_ms / 1000)
        except concurrent.futures.TimeoutError:
            # Cancel the waiter instead of leaving it queued: Rust polls
            # roughly every 50 ms, and an uncancelled getter would pile up
            # one waiter per poll on an idle connection.
            future.cancel()
            return None
        except (asyncio.CancelledError, RuntimeError):
            return None
        if item[0] == "text":
            return ("text", item[1], None, None, None)
        if item[0] == "bytes":
            return ("bytes", None, bytes(item[1]), None, None)
        if item[0] == "close":
            return ("close", None, None, item[1], item[2])
        if item[0] == "done":
            return ("done", None, None, None, None)
        return ("accept", None, None, None, None)

    def _is_done(self):
        return self._future is not None and self._future.done()

    def _cancel(self):
        if self._future is not None and not self._future.done():
            self._future.cancel()

    def _set_future(self, future):
        self._future = future


def _websocket_callback(app, loop, path, query, headers, subprotocol=None):
    """Create a session and schedule the Python handler (called from Rust).

    Runs on a Rust blocking thread, so it must not await: it schedules the
    handler on the app's event loop with ``run_coroutine_threadsafe`` and
    returns the session immediately.
    """
    match = app.router.matches(path)
    if match is None:
        raise RuntimeError(f"No WebSocket route for {path}")
    pattern, raw_params, kinds = match
    handler = app.websocket_entries.get(pattern)
    if handler is None:
        raise RuntimeError(f"No WebSocket handler for {path}")
    params = _typed_params(raw_params, kinds)
    session = _WsSession(loop, app.websocket_max_message)

    async def receive():
        kind, *rest = await session._inbox.get()
        if kind == "disconnect":
            raise WebSocketDisconnect(1000, "")
        return (kind, rest[0])

    async def send(item):
        # "accept" is a no-op here: the Rust side already completed the
        # handshake before this session was created.
        await session._outbox.put(item)

    ws = WebSocket(receive=receive, send=send, path=path, params=params,
                   headers=tuple((bytes(k), bytes(v)) for k, v in headers),
                   query_string=bytes(query), max_message_bytes=app.websocket_max_message,
                   subprotocol=subprotocol, offered_subprotocols=[
                       part.strip().decode("ascii", "ignore")
                       for name, value in headers if bytes(name).lower() == b"sec-websocket-protocol"
                       for part in bytes(value).split(b",") if part.strip()])
    session._ws = ws
    app._register_ws_session(session)

    async def run_handler():
        try:
            await _invoke(handler, websocket=ws, params=params)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("WebSocket handler failed")
            if ws.accepted and not ws.closed:
                try:
                    await ws.close(1011)
                except Exception:
                    pass
        finally:
            if ws.accepted and not ws.closed:
                try:
                    await ws.close(1000)
                except Exception:
                    pass
            # Tell the Rust side the handler is finished, so it can stop
            # waiting on the outbox without relying on the poll timeout.
            await session._outbox.put(("done",))
            app._unregister_ws_session(session)

    future = asyncio.run_coroutine_threadsafe(run_handler(), loop)
    session._set_future(future)
    return session


def _submit(loop, coroutine):
    try:
        if not loop.is_running():
            raise RuntimeError("Python event loop is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, loop)
    except RuntimeError:
        coroutine.close()
        raise


class _NativeStream:
    """Pull one chunk at a time on the owning Python event loop."""

    def __init__(self, response, loop, app=None, chunk_timeout=None):
        self._response = response
        self._loop = loop
        self._lock = threading.Lock()
        self._iterator_lock = asyncio.Lock()
        self._pending = None
        self._closed = False
        self._context = response._native_context
        # Response-send stage budget (C-P1-7), seconds per chunk. None
        # keeps STREAM_CHUNK_TIMEOUT (30 s); 0 disables the chunk
        # deadline. Resolved here, not at import time, so tests can
        # still monkeypatch the module constant.
        self._chunk_timeout_s = STREAM_CHUNK_TIMEOUT if chunk_timeout is None else chunk_timeout
        # Counted on the app so serve() can wait out streaming bodies
        # during a graceful drain (B6), not just handler execution.
        self._app = app
        if app is not None:
            with app._inflight_lock:
                app._inflight_streams += 1

    async def _pull(self):
        async with self._iterator_lock:
            chunk = await self._response._next_chunk()
            if chunk is None:
                # Complete asynchronous iterator cleanup before signaling
                # end-of-stream to the client.
                await self._response.aclose()
            return chunk, contextvars.copy_context()

    async def _finish(self):
        async with self._iterator_lock:
            await self._response.aclose()

    def next_chunk(self):
        with self._lock:
            if self._closed:
                return None
            future = self._context.run(_submit, self._loop, self._pull())
            self._pending = future
        try:
            # Response-send stage (C-P1-7): each chunk gets its own
            # budget. A stalled chunk aborts the stream — the status
            # line is already out, so no 504 can be sent; the stage is
            # logged and counted instead of dropping silently.
            deadline = None if self._chunk_timeout_s == 0 else time.monotonic() + self._chunk_timeout_s
            while True:
                try:
                    poll = (_CANCEL_POLL_SECONDS if deadline is None
                            else min(_CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                    chunk, context = future.result(timeout=poll)
                    break
                except concurrent.futures.TimeoutError:
                    if future.done():
                        chunk, context = future.result()
                        break
                    if not self._loop.is_running():
                        self.close()
                        raise RuntimeError("Python event loop stopped during response streaming") from None
                    if deadline is not None and time.monotonic() >= deadline:
                        self.close()
                        if self._app is not None:
                            self._app.metrics.observe_stage_timeout("response")
                        log.warning("Streaming response chunk timed out")
                        raise TimeoutError("Streaming response chunk timed out") from None
            with self._lock:
                self._context = context
            return chunk
        finally:
            with self._lock:
                if self._pending is future:
                    self._pending = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = self._pending
        if self._app is not None:
            with self._app._inflight_lock:
                self._app._inflight_streams -= 1
        if pending is not None:
            pending.cancel()
        try:
            future = self._context.run(_submit, self._loop, self._finish())
        except RuntimeError:
            log.warning("Could not close streaming response: event loop is unavailable")
            return

        def report_failure(result):
            if not result.cancelled() and result.exception() is not None:
                log.error("Streaming response cleanup failed", exc_info=result.exception())

        future.add_done_callback(report_failure)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _validate_server_limits(max_concurrent, acquire_timeout_ms, max_waiting,
                            body_capacity, body_timeout_ms, max_connections,
                            max_connections_per_ip, header_read_timeout_ms,
                            body_min_rate_bytes_per_sec, shutdown_timeout,
                            max_concurrent_per_ip=DEFAULT_CONFIG.max_concurrent_per_ip,
                            acquire_timeout_max_ms=None, handler_timeout_ms=None,
                            response_timeout_ms=None,
                            fast_path_max_concurrent=None,
                            streaming_max_lifetime_secs=300, body_capacity_per_ip=4,
                            ws_idle_timeout_secs=300, ws_max_lifetime_secs=3600,
                            ws_ping_interval_secs=60):
    """Reject server limits that cannot reach the native server safely.

    Counts and timeouts cross into Rust as usize/u64/f64: a value past
    the PyO3 conversion range dies with an OverflowError traceback, and
    an absurd-but-fitting value (a million slots, a day-long drain)
    exhausts resources before serving anything. ``max_waiting`` may be
    None (the native default min(max_concurrent, 64)).
    ``acquire_timeout_max_ms`` may be None (fixed acquire timeout);
    when set it must be at least ``acquire_timeout_ms``, or the
    adaptive ceiling could never apply.
    """
    counts = (
        ("max_concurrent", max_concurrent, 1, MAX_CONCURRENT),
        ("max_waiting", max_waiting, 0, MAX_WAITING),
        ("body_capacity", body_capacity, 1, MAX_BODY_CAPACITY),
        ("max_connections", max_connections, 1, MAX_CONNECTIONS),
        ("max_connections_per_ip", max_connections_per_ip, 1, MAX_CONNECTIONS_PER_IP),
        ("max_concurrent_per_ip", max_concurrent_per_ip, 1, MAX_CONCURRENT),
        ("acquire_timeout_ms", acquire_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("acquire_timeout_max_ms", acquire_timeout_max_ms, 0, MAX_TIMEOUT_MS),
        ("handler_timeout_ms", handler_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("response_timeout_ms", response_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("body_timeout_ms", body_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("header_read_timeout_ms", header_read_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("body_min_rate_bytes_per_sec", body_min_rate_bytes_per_sec, 0,
         MAX_BODY_MIN_RATE_BYTES_PER_SEC),
        ("fast_path_max_concurrent", fast_path_max_concurrent, 1, MAX_CONCURRENT),
        ("streaming_max_lifetime_secs", streaming_max_lifetime_secs, 0,
         MAX_STREAMING_LIFETIME_SECS),
        ("body_capacity_per_ip", body_capacity_per_ip, 1,
         MAX_BODY_CAPACITY_PER_IP),
        ("ws_idle_timeout_secs", ws_idle_timeout_secs, 0, MAX_WS_TIMEOUT_SECS),
        ("ws_max_lifetime_secs", ws_max_lifetime_secs, 0, MAX_WS_TIMEOUT_SECS),
        ("ws_ping_interval_secs", ws_ping_interval_secs, 0, MAX_WS_TIMEOUT_SECS),
    )
    for name, value, minimum, maximum in counts:
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    if (acquire_timeout_max_ms is not None
            and acquire_timeout_max_ms < acquire_timeout_ms):
        raise ValueError("acquire_timeout_max_ms must be at least acquire_timeout_ms")
    if (isinstance(shutdown_timeout, bool) or not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(shutdown_timeout)
            or not 0 <= shutdown_timeout <= MAX_SHUTDOWN_TIMEOUT):
        raise ValueError(f"shutdown_timeout must be a number between 0 and {MAX_SHUTDOWN_TIMEOUT} seconds")


def _ensure_shared_csrf_key(app):
    """Share one CSRF signing key for this server process (M-10).

    ``start_native``/``serve`` are the non-supervisor entry points: a
    process started here without an explicit key (and without
    ``PYNEXT_CSRF_SIGNING_KEY``) used to sign with the per-process
    fallback key, so a cookie issued by one such process failed HMAC
    verification on the next (finding #23, intermittent 403). Resolve
    the shared key exactly like the supervisor does — an explicitly
    configured key wins, then the environment, otherwise a fresh key
    that is published back into the environment so processes spawned
    afterwards (``multiprocessing`` ``"spawn"``, a forked worker, or a
    dev-server restart) inherit it — and point the app's own config at
    it, so this process validates with the same key its children will.
    The key itself is never logged.
    """
    config = app.security
    explicit = (None if config.csrf_signing_key is _FALLBACK_CSRF_SIGNING_KEY
                else config.csrf_signing_key)
    shared_key = ensure_shared_csrf_signing_key(explicit)
    if shared_key != config.csrf_signing_key:
        app.security = replace(config, csrf_signing_key=shared_key)
    return shared_key


def _resolve_management_plane(value):
    """Normalize the ``management_plane`` argument to a validated config.

    ``None`` reads the environment (``PYNEXT_MGMT_ENABLED`` /
    ``PYNEXT_MGMT_KEY`` / ``PYNEXT_MGMT_GATE_READY``), which is how the
    CLI enables the plane without a new flag; an explicit config wins.
    Anything else is a programming error.
    """
    if value is None:
        return ManagementPlaneConfig.from_env()
    if isinstance(value, ManagementPlaneConfig):
        return value
    raise TypeError("management_plane must be a ManagementPlaneConfig or None")


def _management_response(mgmt, app, method, path, headers):
    """Answer a management-plane request, or return None to dispatch normally.

    * ``/metrics`` (``mgmt.metrics_path``) is served only when the plane
      is enabled, and only with a valid key (constant-time check in
      :meth:`ManagementPlaneConfig.check_key`). Disabled means "not
      exposed": the request falls through to the app router, so an app
      that defines its own ``/metrics`` route still serves it, and
      anything else gets the router's 404. The framework itself never
      serves metrics unauthenticated.
    * ``/readyz`` is gated only when ``readyz_requires_key`` is set: an
      unauthorized probe gets 401, an authorized one falls through to
      ``App.dispatch`` so the readiness verdict (200/503) still comes
      from the app. By default ``/readyz`` stays an open readiness
      probe, like ``/healthz``.
    """
    if path == mgmt.metrics_path:
        if not mgmt.enabled:
            return None
        if method.upper() not in ("GET", "HEAD"):
            return Response("Method Not Allowed", status=405,
                            headers=[(b"allow", b"GET, HEAD")])
        if not mgmt.check_key(headers):
            return Response("Unauthorized", status=401,
                            headers=[(b"www-authenticate", b"Bearer")])
        return Response(app.metrics.prometheus_text(),
                        media_type="text/plain; charset=utf-8")
    if path == "/readyz" and mgmt.readyz_requires_key:
        if method.upper() not in ("GET", "HEAD"):
            return None  # let App.dispatch answer 405, as it does today
        if not mgmt.check_key(headers):
            return Response("Unauthorized", status=401,
                            headers=[(b"www-authenticate", b"Bearer")])
    return None
def _trusted_proxy_strings(app):
    """Trusted-proxy networks for the native server, as strings (S-P1-18).

    The app's :class:`pynext.proxy.ProxyTrust` is the single source of
    truth; the native accept loop gets the same list so its per-IP
    connection cap can exempt trusted peers. An app without a
    ``proxy_trust`` (e.g. a test double) contributes no trusted proxies,
    which is the fail-closed default.
    """
    trust = getattr(app, "proxy_trust", None)
    if trust is None:
        return ()
    strings = getattr(trust, "trusted_proxy_strings", None)
    if callable(strings):
        return tuple(strings())
    return tuple(str(network) for network in getattr(trust, "networks", ()))


def start_native(app, loop, host="127.0.0.1", port=3000, max_concurrent=DEFAULT_CONFIG.max_concurrent,
                 acquire_timeout_ms=DEFAULT_CONFIG.acquire_timeout_ms,
                 body_capacity=16, body_timeout_ms=10000, observability=None, *, reuse_port=False, listener=None,
                 max_connections=DEFAULT_MAX_CONNECTIONS,
                 max_connections_per_ip=DEFAULT_MAX_CONNECTIONS_PER_IP,
                 max_concurrent_per_ip=DEFAULT_CONFIG.max_concurrent_per_ip,
                 header_read_timeout_ms=DEFAULT_HEADER_READ_TIMEOUT_MS,
                 body_min_rate_bytes_per_sec=DEFAULT_BODY_MIN_RATE_BYTES_PER_SEC,
                 shutdown_timeout=30.0, max_waiting=None,
                 acquire_timeout_max_ms=None, handler_timeout_ms=None, response_timeout_ms=None,
                 management_plane=None,
                 fast_path_max_concurrent=None,
                 streaming_max_lifetime_secs=300, body_capacity_per_ip=4,
                 ws_idle_timeout_secs=300, ws_max_lifetime_secs=3600,
                 ws_ping_interval_secs=60,
                 allowed_hosts=None):
    """Start the native server and return its handle.

    Overload policy (roadmap C7): bounded wait, then reject. At most
    ``max_concurrent`` requests run Python handlers at once; further
    requests wait up to ``acquire_timeout_ms`` for a slot, then get
    503 with ``Retry-After``. ``max_concurrent_per_ip`` (H-5) caps how
    many of those slots one client IP may hold at once (default 16, one
    eighth of the 128-slot pool), so one IP cannot take every slot with
    slow requests and 503 all other users. The wait queue holds at most
    ``max_waiting`` requests (``None`` keeps the default
    ``min(max_concurrent, 64)``; 0 means a slot must be free
    immediately). Each waiter can hold a fully buffered request body,
    so the queue stays bounded: raising ``max_waiting`` without raising
    ``acquire_timeout_ms`` does nothing, and waiting indefinitely would
    trade 503s for memory exhaustion. A 503 after the full wait carries
    ``Retry-After`` of ``ceil(acquire_timeout_ms / 1000)`` seconds (at
    least 1); a 503 for a full wait queue carries ``Retry-After: 1``.

    Fairness and adaptivity (C-P1-5): the Rust wait queue is FIFO —
    tokio's semaphore grants permits in request order — so a waiter
    never starves behind later arrivals. When
    ``acquire_timeout_max_ms`` is set, the wait is adaptive instead of
    fixed: the effective timeout is ``clamp(2 * EWMA(slot hold time),
    acquire_timeout_ms, acquire_timeout_max_ms)``, so a deployment
    whose handlers typically take 500 ms waits longer than the 3 s
    default instead of converting latency into 503s. ``None`` (the
    default) keeps the fixed ``acquire_timeout_ms``.

    Stage timeouts (C-P1-7): a handler that runs past
    ``handler_timeout_ms`` (``None``: the ``REQUEST_TIMEOUT`` module
    constant, 30 s; 0: no deadline) answers 504, and each streaming
    response chunk gets ``response_timeout_ms`` (``None``: the
    ``STREAM_CHUNK_TIMEOUT`` module constant, 30 s; 0: no deadline).
    Admission overload answers 503 + ``Retry-After``; handler
    overrun answers 504 — the two stages are never mixed, and each
    timeout is counted under its stage (see
    ``MetricsCollector.observe_stage_timeout``).

    Saturation metrics (roadmap C-P1-6): the bridge samples the in-flight
    depth on every entry (queue-depth histogram) and the sync-worker gauge
    (``app._sync_active`` / ``app.sync_workers``, read under the app's
    in-flight lock) on entry and exit; the slot-wait histogram is recorded
    once per request by dispatch (``observe_request`` with
    ``queue_wait_ms``). Together they separate the 503 causes — Rust slot
    saturation (503s, short Python queue waits), GIL/loop serialisation
    (long queue waits, sync workers idle), sync-worker saturation
    (``pynext_sync_workers_active`` pinned at capacity).

    DoS defences (S-P1-14/15/16): ``streaming_max_lifetime_secs`` caps
    the total lifetime of one streaming response (0 disables);
    ``body_capacity_per_ip`` caps concurrent body slots per client IP
    inside the global ``body_capacity``; ``ws_idle_timeout_secs`` closes
    an idle WebSocket session, ``ws_max_lifetime_secs`` caps a session's
    total lifetime, and ``ws_ping_interval_secs`` pings idle sessions
    (unanswered for 10s: close) — 0 disables each.

    Host validation (S-P1-19, M-9): the native server rejects requests
    whose Host header is missing or not on ``allowed_hosts`` with 400,
    before any handler runs. ``allowed_hosts`` is a Django
    ``ALLOWED_HOSTS`` list (``*``, ``.example.com``,
    ``*.example.com``, exact hosts); ``None`` (the default) means the
    loopback default plus the bind host
    (``config.effective_allowed_hosts``).
    """
    _validate_server_limits(max_concurrent, acquire_timeout_ms, max_waiting,
                            body_capacity, body_timeout_ms, max_connections,
                            max_connections_per_ip, header_read_timeout_ms,
                            body_min_rate_bytes_per_sec, shutdown_timeout,
                            max_concurrent_per_ip,
                            acquire_timeout_max_ms=acquire_timeout_max_ms,
                            handler_timeout_ms=handler_timeout_ms,
                            response_timeout_ms=response_timeout_ms,
                            fast_path_max_concurrent=fast_path_max_concurrent,
                            streaming_max_lifetime_secs=streaming_max_lifetime_secs,
                            body_capacity_per_ip=body_capacity_per_ip,
                            ws_idle_timeout_secs=ws_idle_timeout_secs,
                            ws_max_lifetime_secs=ws_max_lifetime_secs,
                            ws_ping_interval_secs=ws_ping_interval_secs)
    # M-10: non-supervisor servers share one CSRF signing key too, so a
    # cookie issued by this process validates on any process spawned
    # from it (and survives a dev-server restart).
    _ensure_shared_csrf_key(app)
    # Management plane, fail-closed (S-P1-32): enabling it without a key
    # refuses to start -- before the socket is bound -- instead of
    # serving the plane unauthenticated with a warning (Sanic's trap).
    mgmt = _resolve_management_plane(management_plane)
    mgmt.validate()
    if host in ("0.0.0.0", "::"):
        # The default bind is 127.0.0.1; a wildcard bind makes every
        # exposed endpoint -- including the management plane, when
        # enabled -- reachable from the network.
        log.warning("PyNext is binding to %s (all interfaces): prefer 127.0.0.1 "
                    "unless this server is behind a trusted reverse proxy", host)
    if mgmt.enabled:
        log.info("Management plane enabled: %s requires a key", mgmt.metrics_path)
    if observability is not None:
        app.configure_observability(observability)
    app.metrics.observe_worker_start()
    limiter = getattr(app, "rate_limit", None)

    async def call(method, raw_path, query, headers, body, submitted_at=None, client_ip=None, cancel_event=None):
        started = time.monotonic()
        # Boundary hygiene, same as the ASGI entry point: internal-only
        # headers never reach the request context, the middleware chain,
        # or any security decision.
        headers = filter_internal_headers(headers)
        request_id, reset_context = begin_request_context(headers)
        if submitted_at is not None:
            queue_wait_var.set((started - submitted_at) * 1000)
        try:
            response = await _call_inner(method, raw_path, query, headers, body, client_ip, cancel_event)
        finally:
            reset_context()
        # dispatch() already applied the headers to routed responses; this
        # covers the 400/error-page responses _call_inner builds itself.
        # Idempotent, so routed responses are unchanged.
        apply_security_headers(response, config=app.security, production=app.production)
        if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
            response.headers.append((b"x-request-id", request_id.encode("ascii")))
        return response

    async def _call_inner(method, raw_path, query, headers, body, client_ip=None, cancel_event=None):
        try:
            path, encoded_path = _split_request_path(raw_path, raw_path.encode("ascii"))
        except (ValueError, UnicodeError):
            response = Response("Bad Request", status=400)
        else:
            try:
                request = Request(method.upper(), path, bytes(query),
                                  tuple((bytes(k), bytes(v)) for k, v in headers), bytes(body), _app=app,
                                  _cancel_event=cancel_event if cancel_event is not None else _RequestCancellation(),
                                  raw_path=encoded_path)
                # Trust-resolved client identity (A6, pynext.proxy), same
                # values as on ASGI: without trusted proxies these are just
                # the direct connection values. The native listener is plain
                # HTTP, so the default scheme is "http".
                host = next((value.decode("latin-1") for key, value in headers
                             if key.lower() == b"host"), None)
                info = app.proxy_trust.resolve(
                    client_ip, tuple((bytes(k), bytes(v)) for k, v in headers),
                    default_scheme="http", default_host=host)
                request.state["client_ip"] = info.client_ip
                request.state["client_scheme"] = info.scheme
                request.state["client_host"] = info.host
                # Management plane (S-P1-32, fail-closed): intercept before
                # App.dispatch so /metrics is never served by the router
                # unauthenticated, and a gated /readyz never reaches the
                # app without a valid key. A None result means "not a
                # management request (or plane disabled)": dispatch normally.
                mgmt_response = _management_response(mgmt, app, method, path, headers)
                if mgmt_response is not None:
                    response = mgmt_response
                else:
                    response = await app.dispatch(request)
            except SignalError as exc:
                # Defensive: the timed dispatcher converts signals first.
                # A signal escaping dispatch still answers without an error log.
                response = exc.to_response()
                request_id = get_request_id()
                if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
                    response.headers.append((b"x-request-id", request_id.encode("ascii")))
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
                response = Response("Bad Request", status=400)
            except Exception as exc:
                digest, _count = digest_for_error(exc)
                log.exception("Request failed", extra={"error_digest": digest})
                response = app.error_response(exc, digest=digest)
                # error_response results bypass dispatch, so path headers
                # are applied here; routed responses already have them.
                apply_path_headers(response, path, path_headers=app.path_headers)
        if method.upper() == "HEAD":
            response.body = b""
        if isinstance(response, StreamingResponse) and not response._head_only:
            response._native_context = contextvars.copy_context()
        return response

    def _sample_sync_workers():
        """Best-effort sample of the sync-worker gauge (C-P1-6).

        Reads ``app._sync_active`` / ``app.sync_workers`` under the app's
        in-flight lock — the same lock ``_invoke_sync`` uses — so the
        sample is consistent. Never raises: observability must not break
        request handling. Called on bridge entry and exit, so the recorded
        maximum is a lower bound on the true peak.
        """
        try:
            metrics = getattr(app, "metrics", None)
            if metrics is None:
                return
            lock = getattr(app, "_inflight_lock", None)
            if lock is None:
                return
            with lock:
                active = getattr(app, "_sync_active", None)
                capacity = getattr(app, "sync_workers", None)
            if isinstance(active, int) and isinstance(capacity, int):
                metrics.observe_sync_workers(active, capacity)
        except Exception:
            pass

    def bridge(method, raw_path, query, headers, body, cancellation, client_ip=None):
        # The native header-stage callback has already charged this
        # request, before acquiring body or Python execution capacity.
        app.metrics.observe_queue_enter()
        _sample_sync_workers()
        with app._inflight_lock:
            app._inflight += 1
        try:
            return _bridge_inner(method, raw_path, query, headers, body, cancellation, client_ip)
        finally:
            with app._inflight_lock:
                app._inflight -= 1
            app.metrics.observe_queue_exit()
            _sample_sync_workers()

    def _bridge_inner(method, raw_path, query, headers, body, cancellation, client_ip=None):
        submitted_at = time.monotonic()
        cancel_event = _RequestCancellation()
        try:
            future = _submit(loop, call(method, raw_path, query, headers, body, submitted_at, client_ip, cancel_event))
        except RuntimeError:
            app.metrics.observe_worker_timeout("unavailable")
            response = Response("Service Unavailable", status=503)
        else:
            # Handler stage (C-P1-7): this deadline bounds handler
            # execution + response build only — streaming bodies get
            # their own per-chunk budget in _NativeStream, and the body
            # read is bounded in Rust by body_timeout_ms. A handler
            # that runs past it answers 504 (counted under stage
            # "handler"); admission overload answers 503 in Rust before
            # the bridge is ever called.
            handler_timeout_s = (REQUEST_TIMEOUT if handler_timeout_ms is None
                                 else handler_timeout_ms / 1000)
            deadline = None if handler_timeout_s == 0 else time.monotonic() + handler_timeout_s
            while True:
                if cancellation.cancelled() or not loop.is_running():
                    cancel_event.cancel(499 if cancellation.cancelled() else 503)
                    future.cancel()
                    app.metrics.observe_worker_timeout("unavailable")
                    response = Response("Service Unavailable", status=503)
                    break
                try:
                    poll = (_CANCEL_POLL_SECONDS if deadline is None
                            else min(_CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                    response = future.result(timeout=poll)
                    break
                except concurrent.futures.TimeoutError:
                    if deadline is not None and time.monotonic() >= deadline:
                        cancel_event.cancel(504)
                        future.cancel()
                        app.metrics.observe_worker_timeout("timeout")
                        app.metrics.observe_stage_timeout("handler")
                        response = Response("Gateway Timeout", status=504)
                        break
                except concurrent.futures.CancelledError:
                    app.metrics.observe_worker_timeout("unavailable")
                    response = Response("Service Unavailable", status=503)
                    break
        # Timeout/unavailable responses built here bypass dispatch, so apply
        # the headers here; routed responses already have them (idempotent).
        # The raw path may not decode; then no rule can match it anyway.
        try:
            bridge_path = _decode_path(raw_path, raw_path.encode("ascii"))
        except (ValueError, UnicodeError):
            bridge_path = None
        if bridge_path is not None:
            apply_path_headers(response, bridge_path, path_headers=app.path_headers)
        apply_security_headers(response, config=app.security, production=app.production)
        body = (_NativeStream(response, loop, app,
                              chunk_timeout=(STREAM_CHUNK_TIMEOUT if response_timeout_ms is None
                                            else response_timeout_ms / 1000))
                if isinstance(response, StreamingResponse) and not response._head_only
                else response.body)
        return response.status, response.headers, body

    def ws_callback(path, query, headers, subprotocol):
        return _websocket_callback(app, loop, path, query, filter_internal_headers(headers), subprotocol)

    def ws_origin_callback(headers, peer_ip):
        # Log denials here (the native branch answers 403 itself): a
        # rejected handshake must be observable in the server log on
        # this transport too, exactly like the ASGI handler logs its
        # own 403. Exceptions propagate so Rust still fails closed
        # with 503.
        filtered = filter_internal_headers(headers)
        allowed = websocket_origin_allowed(filtered, peer_ip, "http",
                                           app.proxy_trust, app.security)
        if not allowed:
            origin = None
            for name, value in filtered:
                if (isinstance(name, (bytes, bytearray))
                        and isinstance(value, (bytes, bytearray))
                        and bytes(name).lower() == b"origin"):
                    origin = bytes(value).decode("ascii", "replace")
                    break
            log.warning("WebSocket handshake denied by origin policy: origin=%r peer=%s",
                        origin, peer_ip)
        return allowed

    # One charge per request, including WebSocket handshakes. Rust runs
    # this before polling HTTP bodies or accepting upgrades. Health
    # probes are skipped by the HTTP transport; configured exemptions
    # apply to both transports using their strictly decoded paths.
    rate_limit_callback = None
    if limiter is not None:
        def rate_limit_callback(path, headers, peer_ip):
            headers = filter_internal_headers(headers)
            if _is_exempt_path(limiter, path):
                return True, None
            key = limiter.key_for(peer_ip, headers)
            return limiter.allow(key)

    async def _ws_auth_decision(path, query, headers):
        # True allows the upgrade, False denies it (403), None is a
        # pre-flight error (503, fail closed). A deliberate denial from
        # middleware or @require_auth is 401/403; anything else the
        # pre-flight returns (unknown route, middleware failure) is an
        # error rather than an authorization verdict.
        denied = await app._authorize_websocket(path, bytes(query), headers)
        if denied is None:
            return True
        if denied.status in (401, 403):
            return False
        return None

    def ws_auth_callback(path, query, headers, peer_ip):
        # Runs on a Rust blocking thread, so it must not await: schedule
        # the async pre-flight on the app's event loop and wait with a
        # timeout, mirroring the Origin callback above.
        headers = filter_internal_headers(headers)
        try:
            future = _submit(loop, _ws_auth_decision(path, query, headers))
        except RuntimeError:
            return None
        try:
            return future.result(timeout=5)
        except Exception:
            future.cancel()
            return None

    return _native.NativeServer(bridge, host, port, app.static_pages, app.public_assets, app.client_assets, app.isr.store, app.asset_etags,
                                max_concurrent=max_concurrent, acquire_timeout_ms=acquire_timeout_ms,
                                acquire_timeout_max_ms=acquire_timeout_max_ms,
                                max_waiting=max_waiting,
                                body_capacity=body_capacity, body_timeout_ms=body_timeout_ms,
                                max_connections=max_connections,
                                max_connections_per_ip=max_connections_per_ip,
                                max_concurrent_per_ip=max_concurrent_per_ip,
                                header_read_timeout_ms=header_read_timeout_ms,
                                body_min_rate_bytes_per_sec=body_min_rate_bytes_per_sec,
                                streaming_max_lifetime_secs=streaming_max_lifetime_secs,
                                body_capacity_per_ip=body_capacity_per_ip,
                                ws_idle_timeout_secs=ws_idle_timeout_secs,
                                ws_max_lifetime_secs=ws_max_lifetime_secs,
                                ws_ping_interval_secs=ws_ping_interval_secs,
                                websocket_paths=sorted(app.websocket_routes),
                                websocket_callback=ws_callback,
                                websocket_origin_callback=ws_origin_callback,
                                websocket_rate_limit_callback=rate_limit_callback,
                                http_rate_limit_callback=rate_limit_callback,
                                websocket_auth_callback=ws_auth_callback,
                                reuse_port=reuse_port,
                                route_patterns=app.router.ordered_routes(),
                                middleware_enabled=bool(app.middleware) or app.hot_reload,
                                shared_cache_enabled=app.isr.shared is not None,
                                websocket_protocols=app.websocket_protocols,
                                websocket_max_message=app.websocket_max_message, listener=listener,
                                security_headers=security_headers(config=app.security,
                                                                  production=app.production),
                                path_headers=app.path_headers.for_native(),
                                drain_timeout_secs=shutdown_timeout,
                                fast_path_max_concurrent=fast_path_max_concurrent,
                                # S-P1-18: the Rust accept loop enforces the
                                # per-IP connection cap on the TCP peer only
                                # (headers are not read yet), so it needs the
                                # same trust list Python resolves XFF with.
                                # A trusted peer is exempt from the per-IP cap
                                # (total cap still applies); per-client limits
                                # behind it are enforced at request time by
                                # the trust-resolved rate limiter.
                                trusted_proxies=_trusted_proxy_strings(app),
                                allowed_hosts=list(effective_allowed_hosts(allowed_hosts, host)))


async def serve(app, host=DEFAULT_CONFIG.host, port=DEFAULT_CONFIG.port,
                max_concurrent=DEFAULT_CONFIG.max_concurrent,
                acquire_timeout_ms=DEFAULT_CONFIG.acquire_timeout_ms,
                body_capacity=DEFAULT_CONFIG.body_capacity,
                body_timeout_ms=DEFAULT_CONFIG.body_timeout_ms, stop_event=None, observability=None, *,
                reuse_port=False, on_ready=None, listener=None,
                max_connections=DEFAULT_MAX_CONNECTIONS,
                max_connections_per_ip=DEFAULT_MAX_CONNECTIONS_PER_IP,
                max_concurrent_per_ip=DEFAULT_CONFIG.max_concurrent_per_ip,
                header_read_timeout_ms=DEFAULT_HEADER_READ_TIMEOUT_MS,
                body_min_rate_bytes_per_sec=DEFAULT_BODY_MIN_RATE_BYTES_PER_SEC,
                shutdown_timeout=DEFAULT_CONFIG.shutdown_timeout, max_waiting=None,
                acquire_timeout_max_ms=DEFAULT_CONFIG.acquire_timeout_max_ms,
                handler_timeout_ms=DEFAULT_CONFIG.handler_timeout_ms,
                response_timeout_ms=DEFAULT_CONFIG.response_timeout_ms,
                management_plane=None,
                fast_path_max_concurrent=None,
                streaming_max_lifetime_secs=DEFAULT_CONFIG.streaming_max_lifetime_secs,
                body_capacity_per_ip=DEFAULT_CONFIG.body_capacity_per_ip,
                ws_idle_timeout_secs=DEFAULT_CONFIG.ws_idle_timeout_secs,
                ws_max_lifetime_secs=DEFAULT_CONFIG.ws_max_lifetime_secs,
                ws_ping_interval_secs=DEFAULT_CONFIG.ws_ping_interval_secs,
                allowed_hosts=None):
    """Run the native server until cancelled, or until ``stop_event`` is set.

    ``stop_event`` lets ``pynext dev`` stop the server cleanly and restart
    it on the same port after a file change. When no ``stop_event`` is
    given (the single-process ``pynext start`` path), SIGTERM/SIGINT on
    the main thread set one, so a bare ``kill`` also drains gracefully.
    ``max_waiting`` is the bounded execution wait-queue size; see :func:`start_native`.

    Connection limits (A4): ``max_connections`` caps concurrent TCP
    connections (excess connections are dropped at accept time, before any
    request is read); ``max_connections_per_ip`` caps them per client IP;
    ``header_read_timeout_ms`` bounds both slow request headers and idle
    keep-alive connections (hyper applies it to every header read on the
    connection); ``body_min_rate_bytes_per_sec`` rejects bodies arriving
    slower than that average after a 2-second grace window with a 408.
    The fast-path budget (``fast_path_max_concurrent``, default 4x
    ``max_concurrent``) caps concurrent static/ISR-Fresh responses served
    without an execution slot; excess fast-path requests get 503 with
    ``Retry-After``. The ASGI adapter enforces the same body timeout and
    minimum receive rate as the native server; the other connection
    limits apply to the native server only.

    DoS defences (S-P1-14/15/16): ``streaming_max_lifetime_secs`` caps
    the total lifetime of one streaming response; ``body_capacity_per_ip``
    caps concurrent body slots per client IP; ``ws_idle_timeout_secs``,
    ``ws_max_lifetime_secs`` and ``ws_ping_interval_secs`` bound idle
    WebSocket sessions (see :func:`start_native`).

    Graceful shutdown (B6), in order: the app stops advertising
    readiness (``/readyz`` starts returning 503), live WebSocket
    sessions get a clean 1001 close, in-flight HTTP requests and
    streaming bodies are waited out for up to ``shutdown_timeout``
    seconds, background ISR regenerations get whatever of that same
    budget is left to finish, the native server stops and drains its
    connections (bounded by the same timeout), and finally the app
    shuts its resources down. New connections are still accepted
    during the drain, so a load balancer keeps reaching ``/healthz``
    (200) and ``/readyz`` (503) while in-flight requests finish; the
    listener closes only when the server stops. A request that is
    still running when the timeout expires is abandoned: the server
    stops anyway and the timeout is logged, rather than hanging the
    shutdown forever. A background regeneration still running at that
    point is cancelled the same way.

    Keep-alive policy: an idle keep-alive connection is closed after
    ``header_read_timeout_ms`` (default 10s) without a complete request
    header — the same bound that limits slow request headers — and idle
    connections count toward ``max_connections`` (default 1024) and
    ``max_connections_per_ip`` (default 128), so idle clients cannot
    exhaust the connection budget. Set the idle timeout longer than the
    fronting proxy's idle timeout (e.g. Cloudflare Tunnel) so the proxy,
    not this server, initiates the close of an idle connection; a server
    that closes first can reset a connection the proxy is about to reuse.
    """
    await app.startup()
    server = None
    if stop_event is None:
        stop_event = asyncio.Event()
        if threading.current_thread() is threading.main_thread():
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except (NotImplementedError, RuntimeError, ValueError):
                    # Windows loops and non-main threads cannot install
                    # handlers; the caller's stop_event (or Ctrl-C via
                    # asyncio) still stops the server.
                    pass
    try:
        server = start_native(app, asyncio.get_running_loop(), host, port, max_concurrent, acquire_timeout_ms,
                              body_capacity, body_timeout_ms, observability=observability, reuse_port=reuse_port,
                              listener=listener, max_connections=max_connections,
                              max_connections_per_ip=max_connections_per_ip,
                              max_concurrent_per_ip=max_concurrent_per_ip,
                              header_read_timeout_ms=header_read_timeout_ms,
                              body_min_rate_bytes_per_sec=body_min_rate_bytes_per_sec,
                              shutdown_timeout=shutdown_timeout, max_waiting=max_waiting,
                              acquire_timeout_max_ms=acquire_timeout_max_ms,
                              handler_timeout_ms=handler_timeout_ms,
                              response_timeout_ms=response_timeout_ms,
                              management_plane=management_plane,
                              fast_path_max_concurrent=fast_path_max_concurrent,
                              streaming_max_lifetime_secs=streaming_max_lifetime_secs,
                              body_capacity_per_ip=body_capacity_per_ip,
                              ws_idle_timeout_secs=ws_idle_timeout_secs,
                              ws_max_lifetime_secs=ws_max_lifetime_secs,
                              ws_ping_interval_secs=ws_ping_interval_secs,
                              allowed_hosts=allowed_hosts)
        print(f"PyNext listening on http://{server.address}", flush=True)
        # S-P1-28 (M-19): a debug server bound to a non-loopback address
        # exposes detailed dev error pages to the network; warn at startup
        # and let the app suppress those pages while externally bound.
        note_bind = getattr(app, "note_dev_bind_host", None)
        if note_bind is not None:
            note_bind(host)
        # A standalone bundle records its provenance in
        # bundle-manifest.json next to the app dir; log it (and warn on
        # a Python/PyNext version mismatch) after the listening line so
        # the first line stays the address.
        log_bundle_info(getattr(app, "root", None))
        if on_ready is not None:
            on_ready(server.address)
        await stop_event.wait()
    finally:
        # Two-phase drain (C-P2-5): one shared shutdown budget starting
        # here. First stop advertising readiness, then wait out what is
        # already in flight, then let background work finish with what
        # is left; whatever is still running is cancelled in
        # app.shutdown(), so shutdown stays bounded instead of hanging
        # on a stuck request or regeneration. New connections are still
        # accepted during the drain (the listener closes only in
        # server.stop() below): a load balancer must still reach
        # /healthz (200) and /readyz (503) while in-flight requests
        # finish, and a client without an idle keep-alive connection
        # would otherwise be refused outright.
        drain_deadline = time.monotonic() + shutdown_timeout
        app.begin_drain()
        try:
            await app.close_websockets(timeout=min(5.0, shutdown_timeout))
        except Exception:
            log.exception("WebSocket drain failed")
        try:
            if not await app.wait_for_inflight(shutdown_timeout):
                log.warning("Proceeding with shutdown after %.1fs with requests still in flight",
                            shutdown_timeout)
        except Exception:
            log.exception("Inflight drain wait failed")
        try:
            # Background work (C-P2-5): an ISR regeneration that started
            # while serving stale gets the budget that is left to finish
            # and publish its result; a regeneration still running when
            # the budget runs out is cancelled by app.shutdown() below.
            remaining = drain_deadline - time.monotonic()
            if remaining > 0 and not await app.isr.wait_for_background(remaining):
                log.warning("Proceeding with shutdown with ISR regenerations still running")
        except Exception:
            log.exception("Background drain wait failed")
        try:
            if server is not None:
                await asyncio.to_thread(server.stop)
        finally:
            await app.shutdown()
