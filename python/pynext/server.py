"""Axum server adapter; Python handlers run on the owning asyncio loop."""
import asyncio
import concurrent.futures
import contextvars
import logging
import math
import signal
import threading
import time

from . import _native
from .app import _decode_path, _invoke, _split_request_path, _typed_params
from .config import DEFAULT_CONFIG
from .errors import SignalError, digest_for_error
from .http import (MAX_WS_MESSAGE, BadRequest, Request, Response, StreamingResponse, WebSocket,
                   WebSocketDisconnect, _RequestCancellation, apply_path_headers,
                   apply_security_headers, filter_internal_headers, security_headers)
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

    def __init__(self, response, loop, app=None):
        self._response = response
        self._loop = loop
        self._lock = threading.Lock()
        self._iterator_lock = asyncio.Lock()
        self._pending = None
        self._closed = False
        self._context = response._native_context
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
            deadline = time.monotonic() + STREAM_CHUNK_TIMEOUT
            while True:
                try:
                    chunk, context = future.result(timeout=min(_CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                    break
                except concurrent.futures.TimeoutError:
                    if future.done():
                        chunk, context = future.result()
                        break
                    if not self._loop.is_running():
                        self.close()
                        raise RuntimeError("Python event loop stopped during response streaming") from None
                    if time.monotonic() >= deadline:
                        self.close()
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
                            body_min_rate_bytes_per_sec, shutdown_timeout):
    """Reject server limits that cannot reach the native server safely.

    Counts and timeouts cross into Rust as usize/u64/f64: a value past
    the PyO3 conversion range dies with an OverflowError traceback, and
    an absurd-but-fitting value (a million slots, a day-long drain)
    exhausts resources before serving anything. ``max_waiting`` may be
    None (the native default min(max_concurrent, 64)).
    """
    counts = (
        ("max_concurrent", max_concurrent, 1, MAX_CONCURRENT),
        ("max_waiting", max_waiting, 0, MAX_WAITING),
        ("body_capacity", body_capacity, 1, MAX_BODY_CAPACITY),
        ("max_connections", max_connections, 1, MAX_CONNECTIONS),
        ("max_connections_per_ip", max_connections_per_ip, 1, MAX_CONNECTIONS_PER_IP),
        ("acquire_timeout_ms", acquire_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("body_timeout_ms", body_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("header_read_timeout_ms", header_read_timeout_ms, 0, MAX_TIMEOUT_MS),
        ("body_min_rate_bytes_per_sec", body_min_rate_bytes_per_sec, 0,
         MAX_BODY_MIN_RATE_BYTES_PER_SEC),
    )
    for name, value, minimum, maximum in counts:
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    if (isinstance(shutdown_timeout, bool) or not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(shutdown_timeout)
            or not 0 <= shutdown_timeout <= MAX_SHUTDOWN_TIMEOUT):
        raise ValueError(f"shutdown_timeout must be a number between 0 and {MAX_SHUTDOWN_TIMEOUT} seconds")


def start_native(app, loop, host="127.0.0.1", port=3000, max_concurrent=64, acquire_timeout_ms=1000,
                 body_capacity=16, body_timeout_ms=10000, observability=None, *, reuse_port=False, listener=None,
                 max_connections=DEFAULT_MAX_CONNECTIONS,
                 max_connections_per_ip=DEFAULT_MAX_CONNECTIONS_PER_IP,
                 header_read_timeout_ms=DEFAULT_HEADER_READ_TIMEOUT_MS,
                 body_min_rate_bytes_per_sec=DEFAULT_BODY_MIN_RATE_BYTES_PER_SEC,
                 shutdown_timeout=30.0, max_waiting=None):
    """Start the native server and return its handle.

    Overload policy (roadmap C7): bounded wait, then reject. At most
    ``max_concurrent`` requests run Python handlers at once; further
    requests wait up to ``acquire_timeout_ms`` for a slot, then get
    503 with ``Retry-After``. The wait queue holds at most ``max_waiting``
    requests (``None`` keeps the default ``min(max_concurrent, 64)``;
    0 means a slot must be free immediately). Each waiter can hold a
    fully buffered request body, so the queue stays bounded: raising
    ``max_waiting`` without raising ``acquire_timeout_ms`` does nothing,
    and waiting indefinitely would trade 503s for memory exhaustion.
    A 503 after the full wait carries ``Retry-After`` of
    ``ceil(acquire_timeout_ms / 1000)`` seconds (at least 1); a 503 for
    a full wait queue carries ``Retry-After: 1``.
    """
    _validate_server_limits(max_concurrent, acquire_timeout_ms, max_waiting,
                            body_capacity, body_timeout_ms, max_connections,
                            max_connections_per_ip, header_read_timeout_ms,
                            body_min_rate_bytes_per_sec, shutdown_timeout)
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

    def bridge(method, raw_path, query, headers, body, cancellation, client_ip=None):
        # The native header-stage callback has already charged this
        # request, before acquiring body or Python execution capacity.
        app.metrics.observe_queue_enter()
        with app._inflight_lock:
            app._inflight += 1
        try:
            return _bridge_inner(method, raw_path, query, headers, body, cancellation, client_ip)
        finally:
            with app._inflight_lock:
                app._inflight -= 1
            app.metrics.observe_queue_exit()

    def _bridge_inner(method, raw_path, query, headers, body, cancellation, client_ip=None):
        submitted_at = time.monotonic()
        cancel_event = _RequestCancellation()
        try:
            future = _submit(loop, call(method, raw_path, query, headers, body, submitted_at, client_ip, cancel_event))
        except RuntimeError:
            app.metrics.observe_worker_timeout("unavailable")
            response = Response("Service Unavailable", status=503)
        else:
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while True:
                if cancellation.cancelled() or not loop.is_running():
                    cancel_event.cancel(499 if cancellation.cancelled() else 503)
                    future.cancel()
                    app.metrics.observe_worker_timeout("unavailable")
                    response = Response("Service Unavailable", status=503)
                    break
                try:
                    response = future.result(timeout=min(_CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                    break
                except concurrent.futures.TimeoutError:
                    if time.monotonic() >= deadline:
                        cancel_event.cancel(504)
                        future.cancel()
                        app.metrics.observe_worker_timeout("timeout")
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
        body = (_NativeStream(response, loop, app) if isinstance(response, StreamingResponse) and not response._head_only
                else response.body)
        return response.status, response.headers, body

    def ws_callback(path, query, headers, subprotocol):
        return _websocket_callback(app, loop, path, query, filter_internal_headers(headers), subprotocol)

    def ws_origin_callback(headers, peer_ip):
        return websocket_origin_allowed(filter_internal_headers(headers), peer_ip, "http",
                                        app.proxy_trust, app.security)

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
                                max_waiting=max_waiting,
                                body_capacity=body_capacity, body_timeout_ms=body_timeout_ms,
                                max_connections=max_connections,
                                max_connections_per_ip=max_connections_per_ip,
                                header_read_timeout_ms=header_read_timeout_ms,
                                body_min_rate_bytes_per_sec=body_min_rate_bytes_per_sec,
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
                                drain_timeout_secs=shutdown_timeout)


async def serve(app, host=DEFAULT_CONFIG.host, port=DEFAULT_CONFIG.port,
                max_concurrent=DEFAULT_CONFIG.max_concurrent,
                acquire_timeout_ms=DEFAULT_CONFIG.acquire_timeout_ms,
                body_capacity=DEFAULT_CONFIG.body_capacity,
                body_timeout_ms=DEFAULT_CONFIG.body_timeout_ms, stop_event=None, observability=None, *,
                reuse_port=False, on_ready=None, listener=None,
                max_connections=DEFAULT_MAX_CONNECTIONS,
                max_connections_per_ip=DEFAULT_MAX_CONNECTIONS_PER_IP,
                header_read_timeout_ms=DEFAULT_HEADER_READ_TIMEOUT_MS,
                body_min_rate_bytes_per_sec=DEFAULT_BODY_MIN_RATE_BYTES_PER_SEC,
                shutdown_timeout=DEFAULT_CONFIG.shutdown_timeout, max_waiting=None):
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
    These limits apply to the native (Axum) server only; the ASGI adapter
    relies on the ASGI server's own limits.

    Graceful shutdown (B6), in order: the app stops advertising
    readiness (``/readyz`` starts returning 503), live WebSocket
    sessions get a clean 1001 close, in-flight HTTP requests and
    streaming bodies are waited out for up to ``shutdown_timeout``
    seconds, the native server stops accepting and drains its
    connections (bounded by the same timeout), and finally the app
    shuts its resources down. A request that is still running when the
    timeout expires is abandoned: the server stops anyway and the
    timeout is logged, rather than hanging the shutdown forever.
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
                              header_read_timeout_ms=header_read_timeout_ms,
                              body_min_rate_bytes_per_sec=body_min_rate_bytes_per_sec,
                              shutdown_timeout=shutdown_timeout, max_waiting=max_waiting)
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
            if server is not None:
                await asyncio.to_thread(server.stop)
        finally:
            await app.shutdown()
