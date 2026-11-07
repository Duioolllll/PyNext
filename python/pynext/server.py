"""Axum server adapter; Python handlers run on the owning asyncio loop."""
import asyncio
import concurrent.futures
import contextvars
import logging
import threading
import time

from . import _native
from .app import _decode_path
from .http import BadRequest, Request, Response, StreamingResponse

log = logging.getLogger("pynext")
REQUEST_TIMEOUT = 30
STREAM_CHUNK_TIMEOUT = 30
_CANCEL_POLL_SECONDS = 0.05


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
                 body_capacity=16, body_timeout_ms=10000):
    async def call(method, raw_path, query, headers, body):
        try:
            path = _decode_path(raw_path, raw_path.encode("ascii"))
        except (ValueError, UnicodeError):
            response = Response("Bad Request", status=400)
        else:
            try:
                request = Request(method.upper(), path, bytes(query),
                                  tuple((bytes(k), bytes(v)) for k, v in headers), bytes(body))
                response = await app.dispatch(request)
            except BadRequest as exc:
                log.info("Bad request: %s", exc)
                response = Response("Bad Request", status=400)
            except Exception:
                log.exception("Request failed")
                response = Response("Internal Server Error", status=500)
        if method.upper() == "HEAD":
            response.body = b""
        if isinstance(response, StreamingResponse) and not response._head_only:
            response._native_context = contextvars.copy_context()
        return response

    def bridge(method, raw_path, query, headers, body, cancellation):
        try:
            future = _submit(loop, call(method, raw_path, query, headers, body))
        except RuntimeError:
            response = Response("Service Unavailable", status=503)
        else:
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while True:
                if cancellation.cancelled() or not loop.is_running():
                    future.cancel()
                    response = Response("Service Unavailable", status=503)
                    break
                try:
                    response = future.result(timeout=min(_CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                    break
                except concurrent.futures.TimeoutError:
                    if time.monotonic() >= deadline:
                        future.cancel()
                        response = Response("Gateway Timeout", status=504)
                        break
                except concurrent.futures.CancelledError:
                    response = Response("Service Unavailable", status=503)
                    break
        body = (_NativeStream(response, loop) if isinstance(response, StreamingResponse) and not response._head_only
                else response.body)
        return response.status, response.headers, body

    return _native.NativeServer(bridge, host, port, app.static_pages, app.public_assets, app.client_assets, app.isr.store, app.asset_etags,
                                max_concurrent=max_concurrent, acquire_timeout_ms=acquire_timeout_ms,
                                body_capacity=body_capacity, body_timeout_ms=body_timeout_ms)


async def serve(app, host="127.0.0.1", port=3000, max_concurrent=64, acquire_timeout_ms=1000,
                body_capacity=16, body_timeout_ms=10000):
    server = start_native(app, asyncio.get_running_loop(), host, port, max_concurrent, acquire_timeout_ms,
                          body_capacity, body_timeout_ms)
    print(f"PyNext listening on http://{server.address}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(server.stop)
        await app.isr.close()
