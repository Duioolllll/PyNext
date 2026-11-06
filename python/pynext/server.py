"""Axum server adapter; Python handlers run on the owning asyncio loop."""
import asyncio
import concurrent.futures
import logging

from . import _native
from .app import _decode_path
from .http import BadRequest, Request, Response

log = logging.getLogger("pynext")
REQUEST_TIMEOUT = 30


def start_native(app, loop, host="127.0.0.1", port=3000, max_concurrent=64, acquire_timeout_ms=1000):
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
        return response

    def bridge(method, raw_path, query, headers, body):
        future = asyncio.run_coroutine_threadsafe(call(method, raw_path, query, headers, body), loop)
        try:
            response = future.result(timeout=REQUEST_TIMEOUT)
        except concurrent.futures.TimeoutError:
            future.cancel()
            response = Response("Gateway Timeout", status=504)
        return response.status, response.headers, response.body

    return _native.NativeServer(bridge, host, port, app.static_pages, app.public_assets, app.client_assets, app.isr.store,
                                max_concurrent=max_concurrent, acquire_timeout_ms=acquire_timeout_ms)


async def serve(app, host="127.0.0.1", port=3000, max_concurrent=64, acquire_timeout_ms=1000):
    server = start_native(app, asyncio.get_running_loop(), host, port, max_concurrent, acquire_timeout_ms)
    print(f"PyNext listening on http://{server.address}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(server.stop)
        await app.isr.close()
