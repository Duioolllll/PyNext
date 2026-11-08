"""Axum server adapter; Python handlers run on the owning asyncio loop."""
import asyncio
import concurrent.futures
import contextvars
import logging
import threading
import time

from . import _native
from .app import _decode_path, _invoke
from .http import MAX_WS_MESSAGE, BadRequest, Request, Response, StreamingResponse, WebSocket, WebSocketDisconnect
from .observability import begin_request_context, queue_wait_var
from .ratelimit import RateLimitUnavailable, rate_limited_response

log = logging.getLogger("pynext")
REQUEST_TIMEOUT = 30
STREAM_CHUNK_TIMEOUT = 30
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
    pattern, raw_params = match
    handler = app.websocket_entries.get(pattern)
    if handler is None:
        raise RuntimeError(f"No WebSocket handler for {path}")
    params = {k: v if f"[...{k}]" in pattern else v[0] for k, v in raw_params.items()}
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

    def __init__(self, response, loop):
        self._response = response
        self._loop = loop
        self._lock = threading.Lock()
        self._iterator_lock = asyncio.Lock()
        self._pending = None
        self._closed = False
        self._context = response._native_context

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


def start_native(app, loop, host="127.0.0.1", port=3000, max_concurrent=64, acquire_timeout_ms=1000,
                 body_capacity=16, body_timeout_ms=10000, observability=None, *, reuse_port=False, listener=None,
                 max_waiting=None):
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
    if observability is not None:
        app.configure_observability(observability)
    app.metrics.observe_worker_start()
    limiter = getattr(app, "rate_limit", None)

    async def call(method, raw_path, query, headers, body, submitted_at=None):
        started = time.monotonic()
        request_id, reset_context = begin_request_context(headers)
        if submitted_at is not None:
            queue_wait_var.set((started - submitted_at) * 1000)
        try:
            response = await _call_inner(method, raw_path, query, headers, body)
        finally:
            reset_context()
        if request_id and not any(k == b"x-request-id" for k, _ in response.headers):
            response.headers.append((b"x-request-id", request_id.encode("ascii")))
        return response

    async def _call_inner(method, raw_path, query, headers, body):
        try:
            path = _decode_path(raw_path, raw_path.encode("ascii"))
        except (ValueError, UnicodeError):
            response = Response("Bad Request", status=400)
        else:
            try:
                request = Request(method.upper(), path, bytes(query),
                                  tuple((bytes(k), bytes(v)) for k, v in headers), bytes(body), _app=app)
                response = await app.dispatch(request)
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
                response = Response("Bad Request", status=400)
            except Exception as exc:
                log.exception("Request failed")
                response = app.error_response(exc)
        if method.upper() == "HEAD":
            response.body = b""
        if isinstance(response, StreamingResponse) and not response._head_only:
            response._native_context = contextvars.copy_context()
        return response

    def bridge(method, raw_path, query, headers, body, cancellation, client_ip=None):
        # Rate limiting runs before any handler executes. Static files and
        # fresh ISR pages never reach the bridge (the Rust layer serves
        # them), so they are not rate-limited; see ratelimit.py.
        if limiter is not None:
            key = limiter.key_for(client_ip, headers)
            try:
                allowed, retry_after = limiter.allow(key)
            except RateLimitUnavailable:
                response = Response("Service Unavailable", status=503, headers=[(b"retry-after", b"1")])
                if method.upper() == "HEAD":
                    response.body = b""
                return response.status, response.headers, response.body
            if not allowed:
                response = rate_limited_response(retry_after)
                if method.upper() == "HEAD":
                    response.body = b""
                return response.status, response.headers, response.body
        app.metrics.observe_queue_enter()
        try:
            return _bridge_inner(method, raw_path, query, headers, body, cancellation)
        finally:
            app.metrics.observe_queue_exit()

    def _bridge_inner(method, raw_path, query, headers, body, cancellation):
        submitted_at = time.monotonic()
        try:
            future = _submit(loop, call(method, raw_path, query, headers, body, submitted_at))
        except RuntimeError:
            app.metrics.observe_worker_timeout("unavailable")
            response = Response("Service Unavailable", status=503)
        else:
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while True:
                if cancellation.cancelled() or not loop.is_running():
                    future.cancel()
                    app.metrics.observe_worker_timeout("unavailable")
                    response = Response("Service Unavailable", status=503)
                    break
                try:
                    response = future.result(timeout=min(_CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                    break
                except concurrent.futures.TimeoutError:
                    if time.monotonic() >= deadline:
                        future.cancel()
                        app.metrics.observe_worker_timeout("timeout")
                        response = Response("Gateway Timeout", status=504)
                        break
                except concurrent.futures.CancelledError:
                    app.metrics.observe_worker_timeout("unavailable")
                    response = Response("Service Unavailable", status=503)
                    break
        body = (_NativeStream(response, loop) if isinstance(response, StreamingResponse) and not response._head_only
                else response.body)
        return response.status, response.headers, body

    def ws_callback(path, query, headers, subprotocol):
        return _websocket_callback(app, loop, path, query, headers, subprotocol)

    return _native.NativeServer(bridge, host, port, app.static_pages, app.public_assets, app.client_assets, app.isr.store, app.asset_etags,
                                max_concurrent=max_concurrent, acquire_timeout_ms=acquire_timeout_ms,
                                max_waiting=max_waiting,
                                body_capacity=body_capacity, body_timeout_ms=body_timeout_ms,
                                websocket_paths=sorted(app.websocket_routes),
                                websocket_callback=ws_callback,
                                reuse_port=reuse_port,
                                route_patterns=app.router.ordered_routes(),
                                middleware_enabled=bool(app.middleware) or limiter is not None or app.hot_reload,
                                shared_cache_enabled=app.isr.shared is not None,
                                websocket_protocols=app.websocket_protocols,
                                websocket_max_message=app.websocket_max_message, listener=listener)


async def serve(app, host="127.0.0.1", port=3000, max_concurrent=64, acquire_timeout_ms=1000,
                body_capacity=16, body_timeout_ms=10000, stop_event=None, observability=None, *,
                reuse_port=False, on_ready=None, listener=None, max_waiting=None):
    """Run the native server until cancelled, or until ``stop_event`` is set.

    ``stop_event`` lets ``pynext dev`` stop the server cleanly and restart
    it on the same port after a file change. ``max_waiting`` is the
    bounded execution wait-queue size; see :func:`start_native`.
    """
    await app.startup()
    server = None
    try:
        server = start_native(app, asyncio.get_running_loop(), host, port, max_concurrent, acquire_timeout_ms,
                              body_capacity, body_timeout_ms, observability=observability, reuse_port=reuse_port,
                              listener=listener, max_waiting=max_waiting)
        print(f"PyNext listening on http://{server.address}", flush=True)
        if on_ready is not None:
            on_ready(server.address)
        wait_event = stop_event if stop_event is not None else asyncio.Event()
        await wait_event.wait()
    finally:
        try:
            if server is not None:
                await asyncio.to_thread(server.stop)
        finally:
            await app.shutdown()
