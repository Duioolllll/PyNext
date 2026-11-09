"""Decoded paths must stay decoded through a custom super().is_exempt hook."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import uuid

import httpx
import pytest

from pynext import RateLimiter, RedisRateLimiter, create_app
from pynext.ratelimit import _is_exempt_path
from pynext.server import start_native


@pytest.mark.parametrize("rewrite", [lambda p: p, str, lambda p: p + "", str.lower])
def test_delegating_hook_preserves_decoded_path_and_public_query_contract(rewrite):
    class Delegating(RateLimiter):
        def is_exempt(self, path):
            return path == "/custom" or super().is_exempt(rewrite(path))
    limiter = Delegating(1, 60, exempt_paths=("/free",))
    assert not _is_exempt_path(limiter, "/free?protected")
    assert _is_exempt_path(limiter, "/free")
    assert _is_exempt_path(limiter, "/free/child")
    assert _is_exempt_path(limiter, "/custom")
    assert limiter.is_exempt("/free?real=query")  # public API still accepts a raw query


def test_decoded_context_resets_after_hook_failure():
    class Delegating(RateLimiter):
        def is_exempt(self, path):
            if path == "/boom":
                raise ValueError("test failure")
            return super().is_exempt(path)
    limiter = Delegating(1, 60, exempt_paths=("/free",))
    with pytest.raises(ValueError, match="test failure"):
        _is_exempt_path(limiter, "/boom")
    assert limiter.is_exempt("/free?real=query")


def test_decoded_context_does_not_leak_to_other_threads():
    barrier = threading.Barrier(2)
    class Delegating(RateLimiter):
        def is_exempt(self, path):
            barrier.wait(timeout=5)
            return super().is_exempt(path)
    limiter = Delegating(1, 60, exempt_paths=("/free",))
    with ThreadPoolExecutor(max_workers=2) as executor:
        decoded = executor.submit(_is_exempt_path, limiter, "/free?protected")
        public = executor.submit(limiter.is_exempt, "/free?real=query")
        assert decoded.result(timeout=5) is False
        assert public.result(timeout=5) is True


def project(root):
    (root / "app/[...path]").mkdir(parents=True)
    (root / "app/[...path]/route.py").write_text(
        'from pynext import route, websocket, Response\n@route("GET")\n'
        'async def get(): return Response("ok")\n'
        '@websocket\nasync def socket(websocket):\n'
        '    await websocket.accept()\n    await websocket.close()\n')


def limiter_for(backend, coerce):
    base = RedisRateLimiter if backend == "redis" else RateLimiter
    class Delegating(base):
        def is_exempt(self, path):
            return path == "/custom" or super().is_exempt(str(path) if coerce else path)
    options = {"exempt_paths": ("/free",)}
    if backend == "redis":
        pytest.importorskip("redis")
        url = os.environ.get("PYNEXT_TEST_REDIS_URL")
        if not url:
            pytest.skip("PYNEXT_TEST_REDIS_URL is not set")
        options.update(url=url, namespace=uuid.uuid4().hex)
    limiter = Delegating(1, 60, **options)
    assert limiter.allow("127.0.0.1")[0]
    return limiter


@pytest.mark.parametrize("backend", ["memory", "redis"])
@pytest.mark.parametrize("mode", ["asgi", "axum"])
@pytest.mark.parametrize("coerce", [False, True])
def test_encoded_question_mark_cannot_bypass_delegating_http_hook(tmp_path, backend, mode, coerce):
    project(tmp_path)
    limiter = limiter_for(backend, coerce)
    async def exercise():
        app = create_app(tmp_path, rate_limit=limiter)
        server = None
        if mode == "axum":
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        else:
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        try:
            async with client:
                for path in ("/free", "/free/child", "/free?real=query", "/custom"):
                    assert (await client.get(path)).status_code == 200
                assert (await client.get("/protected")).status_code == 429
                assert (await client.get("/free%3Fprotected")).status_code == 429
                assert (await client.get("/free%3fprotected/child")).status_code == 429
                assert limiter.is_exempt("/free?real=query")
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.shutdown()
            if backend == "redis":
                limiter.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ["memory", "redis"])
@pytest.mark.parametrize("mode", ["asgi", "axum"])
@pytest.mark.parametrize("coerce", [False, True])
def test_delegating_websocket_hook_keeps_exemptions_and_rejects_encoded_paths(tmp_path, backend, mode, coerce):
    project(tmp_path)
    limiter = limiter_for(backend, coerce)
    async def exercise():
        app = create_app(tmp_path, rate_limit=limiter)
        server = writer = None
        try:
            if mode == "axum":
                server = start_native(app, asyncio.get_running_loop(), port=0)
                host, port = server.address.rsplit(":", 1)
                for path, status in ((b"/protected", 429), (b"/free%3Fprotected", 404), (b"/free", 101)):
                    reader, writer = await asyncio.open_connection(host, int(port))
                    writer.write(b"GET " + path + b" HTTP/1.1\r\nHost: localhost\r\n"
                                 b"Origin: http://localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                 b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
                    await writer.drain()
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                    assert head.startswith(f"HTTP/1.1 {status} ".encode()), head
                    writer.close()
                    await writer.wait_closed()
                    writer = None
            else:
                for path, raw_path in (("/protected", b"/protected"),
                                       ("/free?protected", b"/free%3Fprotected"), ("/free", b"/free")):
                    messages = asyncio.Queue()
                    await messages.put({"type": "websocket.connect"})
                    sent = []
                    async def send(message):
                        sent.append(message)
                    await app({"type": "websocket", "path": path, "raw_path": raw_path,
                               "scheme": "http", "client": ("127.0.0.1", 1234),
                               "headers": [(b"host", b"localhost"), (b"origin", b"http://localhost")]},
                              messages.get, send)
                    if path == "/protected":
                        assert sent[0].get("status") == 429, sent
                    elif path == "/free":
                        assert sent[0]["type"] == "websocket.accept", sent
                    else:
                        # The router rejects literal '?' before WebSocket admission.
                        assert sent == [{"type": "websocket.close", "code": 1008}]
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.shutdown()
            if backend == "redis":
                limiter.close()
    asyncio.run(exercise())
