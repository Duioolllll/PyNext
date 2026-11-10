"""Reject over-limit HTTP requests before polling their request bodies."""
import asyncio
import os
import time
import threading
import uuid

import h2.config
import h2.connection
import h2.events
import httpx
import pytest

from pynext import RateLimiter, RedisRateLimiter, Response, create_app
from pynext.ratelimit import RateLimitUnavailable
from pynext.server import start_native


def _project(tmp_path):
    route = tmp_path / "app" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import Response, route\n'
        'calls = []\n'
        '@route("GET")\n'
        'async def get(request):\n'
        '    calls.append(request.body)\n'
        '    return Response("ok")\n'
        '@route("POST")\n'
        'async def post(request):\n'
        '    calls.append(request.body)\n'
        '    return Response("ok")\n', encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_configured_limiter_preserves_native_static_fast_path(tmp_path, monkeypatch, backend):
    _project(tmp_path)
    public = tmp_path / "public"
    public.mkdir()
    (public / "asset.txt").write_bytes(b"asset")
    if backend == "redis":
        pytest.importorskip("redis")
        url = os.environ.get("PYNEXT_TEST_REDIS_URL")
        if not url:
            pytest.skip("PYNEXT_TEST_REDIS_URL is not set")
        limiter = RedisRateLimiter(4, 60, url=url, namespace=uuid.uuid4().hex)
    else:
        limiter = RateLimiter(4, 60)

    async def run():
        app = create_app(tmp_path, rate_limit=limiter)
        python_dispatches = []
        dispatch = app.dispatch

        async def observed_dispatch(request):
            python_dispatches.append(request.path)
            return await dispatch(request)

        monkeypatch.setattr(app, "dispatch", observed_dispatch)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                response = await client.get("/asset.txt")
                assert response.status_code == 200 and response.content == b"asset"
                etag = response.headers["etag"]
                response = await client.head("/asset.txt")
                assert response.status_code == 200 and response.content == b""
                response = await client.get("/asset.txt", headers={"If-None-Match": etag})
                assert response.status_code == 304 and response.content == b""
                response = await client.get("/asset.txt", headers={"Range": "bytes=0-1"})
                assert response.status_code == 206 and response.content == b"as"
                assert (await client.get("/asset.txt")).status_code == 429
                assert python_dispatches == []
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()
            if backend == "redis":
                limiter.close()

    asyncio.run(run())


@pytest.mark.parametrize("expect_continue", [False, True])
@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_native_rejects_exhausted_client_before_body_intake(tmp_path, expect_continue, backend):
    if backend == "redis":
        pytest.importorskip("redis")
        url = os.environ.get("PYNEXT_TEST_REDIS_URL")
        if not url:
            pytest.skip("PYNEXT_TEST_REDIS_URL is not set")
        limiter = RedisRateLimiter(1, 60, url=url, namespace=uuid.uuid4().hex, trusted_proxies=("127.0.0.1",))
    else:
        limiter = RateLimiter(1, 60, trusted_proxies=("127.0.0.1",))
    async def run():
        app = create_app(_project(tmp_path), rate_limit=limiter)
        module = app._module("app/input/route.py")
        server = start_native(app, asyncio.get_running_loop(), port=0, body_capacity=1,
                              body_timeout_ms=15000, body_min_rate_bytes_per_sec=0)
        host, port = server.address.rsplit(":", 1)
        writer = None
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.get("/input", headers={"X-Forwarded-For": "192.0.2.1"})).status_code == 200
                reader, writer = await asyncio.open_connection(host, int(port))
                expect = "Expect: 100-continue\r\n" if expect_continue else ""
                writer.write((f"POST /input HTTP/1.1\r\nHost: {server.address}\r\n"
                              f"X-Forwarded-For: 192.0.2.1\r\nContent-Length: 100\r\n{expect}\r\n").encode("ascii"))
                await writer.drain()
                # Send no body. A denial must arrive before its 15s body
                # deadline and before any interim 100 Continue response.
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                assert head.startswith(b"HTTP/1.1 429 "), head
                assert b"retry-after:" in head.lower()
                assert b"connection: close\r\n" in head.lower()
                assert b"x-content-type-options: nosniff\r\n" in head.lower()
                # The refused upload never occupies the one body slot.
                assert (await client.post("/input", content=b"allowed",
                                          headers={"X-Forwarded-For": "192.0.2.2"})).status_code == 200
                assert module.calls == [b"", b"allowed"]
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
            if backend == "redis":
                limiter.close()
    asyncio.run(run())


def test_asgi_rejects_exhausted_client_without_receiving_body(tmp_path):
    async def run():
        app = create_app(_project(tmp_path), rate_limit=RateLimiter(1, 60))
        scope = {"type": "http", "method": "POST", "path": "/input", "scheme": "http",
                 "headers": [(b"host", b"localhost"), (b"content-length", b"100")],
                 "client": ("127.0.0.1", 1234)}
        app.rate_limit.allow("127.0.0.1")
        sent = []
        async def receive():
            raise AssertionError("A rejected request must not receive its body")
        async def send(message):
            sent.append(message)
        try:
            await app(scope, receive, send)
            assert sent[0]["status"] == 429
            assert app._module("app/input/route.py").calls == []
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_native_limiter_failure_refuses_before_continue(tmp_path):
    class UnavailableLimiter(RateLimiter):
        def allow(self, key):
            raise RateLimitUnavailable("test outage")
    async def run():
        app = create_app(_project(tmp_path), rate_limit=UnavailableLimiter(1, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /input HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 503 "), head
            assert b"retry-after: 1\r\n" in head.lower()
            assert app._module("app/input/route.py").calls == []
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_over_limit_is_checked_before_execution_capacity(tmp_path):
    async def run():
        app = create_app(_project(tmp_path), rate_limit=RateLimiter(1, 60))
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return Response("done")
        app.entries["/input"]["GET"] = slow
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=1, max_waiting=0)
        task = None
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False, timeout=10) as client:
                task = asyncio.create_task(client.get("/input"))
                await asyncio.wait_for(entered.wait(), 5)
                denied = await client.get("/input")
                assert denied.status_code == 429
                release.set()
                assert (await task).status_code == 200
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_partial_upload_is_charged_when_headers_arrive(tmp_path):
    async def run():
        app = create_app(_project(tmp_path), rate_limit=RateLimiter(1, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /input HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 100 "), head
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.get("/input")).status_code == 429
                writer.close()
                await writer.wait_closed()
                # Abandoning an admitted upload does not refund its token.
                assert (await client.get("/input")).status_code == 429
                assert (await client.get("/%68ealthz")).status_code == 200
                response = await client.head("/input")
                assert response.status_code == 429 and response.content == b""
            assert app._module("app/input/route.py").calls == []
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_decoded_exempt_path_is_not_charged(tmp_path):
    async def run():
        limiter = RateLimiter(1, 60, exempt_paths=("/input",))
        app = create_app(_project(tmp_path), rate_limit=limiter)
        limiter.allow("127.0.0.1")
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.post("/in%70ut", content=b"exempt")).status_code == 200
                assert (await client.get("/other")).status_code == 429
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_encoded_question_mark_cannot_bypass_exempt_path(tmp_path, mode):
    _project(tmp_path)
    catchall = tmp_path / "app" / "[...path]"
    catchall.mkdir()
    (catchall / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'def get(): return Response("catchall")\n', encoding="utf-8")
    async def run():
        limiter = RateLimiter(1, 60, exempt_paths=("/input",))
        limiter.allow("127.0.0.1")
        app = create_app(tmp_path, rate_limit=limiter)
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                # A real query is outside the path; an encoded question
                # mark remains part of the routed path after decoding.
                assert (await client.get("/input?query=1")).status_code == 200
                assert (await client.get("/input%3Fother")).status_code == 429
                assert (await client.get("/input/child")).status_code == 200
                # The encoded question mark is now a matchable parameter
                # value (two-stage param decoding re-escapes it), so the
                # catch-all matches instead of the old 404. The path is
                # still under the exempt "/input" prefix, so it is not
                # charged — the bypass check above (429) is unaffected.
                assert (await client.get("/input/child%3Fvalue")).status_code == 200
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_custom_exemption_override_is_preserved(tmp_path, mode):
    class CustomLimiter(RateLimiter):
        def is_exempt(self, path):
            return path == "/input"
    async def run():
        limiter = CustomLimiter(1, 60)
        limiter.allow("127.0.0.1")
        app = create_app(_project(tmp_path), rate_limit=limiter)
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/input")).status_code == 200
                assert (await client.get("/other")).status_code == 429
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_limiter_timeout_fails_open_before_body(tmp_path):
    release = threading.Event()
    class SlowLimiter(RateLimiter):
        def allow(self, key):
            release.wait(10)
            return True, None
    async def run():
        app = create_app(_project(tmp_path), rate_limit=SlowLimiter(1, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /input HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
            await writer.drain()
            # H-2: no decision within 2s, so the request is admitted
            # (100) and answered (200) instead of 503ing.
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 100 "), head
            writer.write(b"x" * 100)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 200 "), head
            assert server.rate_limit_fail_open_count() >= 1
            assert app._module("app/input/route.py").calls == [b"x" * 100]
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_http_and_websocket_share_64_slots_and_timeouts_fail_open(tmp_path):
    release = threading.Event()
    lock = threading.Lock()
    entered = 0
    class BlockedLimiter(RateLimiter):
        def allow(self, key):
            nonlocal entered
            with lock:
                entered += 1
            release.wait(15)
            return True, None

    async def run():
        root = _project(tmp_path)
        folder = root / "app/socket"
        folder.mkdir()
        (folder / "route.py").write_text(
            'from pynext import websocket\n@websocket\nasync def socket(websocket):\n'
            '    await websocket.accept()\n    await websocket.close()\n', encoding="utf-8")
        app = create_app(root, rate_limit=BlockedLimiter(1000, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              # The decision gate is what this test fills;
                              # lift the per-IP execution quota (default
                              # 16) so it cannot 503 the burst first.
                              max_concurrent_per_ip=64)
        host, port = server.address.rsplit(":", 1)
        connections = []
        async def open_request(websocket=False):
            reader, writer = await asyncio.open_connection(host, int(port))
            connections.append((reader, writer))
            upgrade = ("Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                       "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                       f"Origin: http://{server.address}\r\n") if websocket else ""
            path = "/socket" if websocket else "/input"
            writer.write((f"GET {path} HTTP/1.1\r\nHost: {server.address}\r\n{upgrade}\r\n").encode())
            await writer.drain()
            return reader
        try:
            # Mixed transports must occupy the SAME budget. H-2: when no
            # decision can be produced within 2s, the requests fail
            # open instead of 503ing.
            readers = await asyncio.gather(*(open_request(i % 2 == 0) for i in range(64)))
            async with asyncio.timeout(10):
                while entered != 64:
                    await asyncio.sleep(0.01)
            # The gate is still full (the 64 decisions are pending), so
            # the 65th request of either transport is admitted without
            # its callback ever being invoked.
            for websocket in (False, True):
                reader = await open_request(websocket)
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                assert head.startswith(b"HTTP/1.1 200 " if not websocket else b"HTTP/1.1 101 "), head
                assert entered == 64  # the 65th callback was never invoked
            heads = await asyncio.gather(*(asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5) for r in readers))
            for i, head in enumerate(heads):
                assert head.startswith(b"HTTP/1.1 101 " if i % 2 == 0 else b"HTTP/1.1 200 "), head
            assert server.rate_limit_fail_open_count() >= 66
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.get("/healthz")).status_code == 200
                release.set()
                async with asyncio.timeout(5):
                    while (await client.get("/input")).status_code == 503:
                        await asyncio.sleep(0.01)
                assert entered == 65
        finally:
            release.set()
            for _, writer in connections:
                writer.close()
            await asyncio.gather(*(w.wait_closed() for _, w in connections), return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "redis"])
@pytest.mark.parametrize("failure", ["body-size", "body-rate", "execution-capacity"])
def test_admitted_failures_never_refund_a_token(tmp_path, backend, failure):
    if backend == "redis":
        pytest.importorskip("redis")
        url = os.environ.get("PYNEXT_TEST_REDIS_URL")
        if not url:
            pytest.skip("PYNEXT_TEST_REDIS_URL is not set")
        limiter = RedisRateLimiter(1, 60, url=url, namespace=uuid.uuid4().hex, trusted_proxies=("127.0.0.1",))
    else:
        limiter = RateLimiter(1, 60, trusted_proxies=("127.0.0.1",))

    async def run():
        app = create_app(_project(tmp_path), rate_limit=limiter)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return Response("done")
        if failure == "execution-capacity":
            app.entries["/input"]["GET"] = slow
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=1, max_waiting=0,
                              body_min_rate_bytes_per_sec=1000000, body_timeout_ms=10000)
        host, port = server.address.rsplit(":", 1)
        writer = holder = None
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False, timeout=10) as client:
                if failure == "execution-capacity":
                    holder = asyncio.create_task(client.get("/input", headers={"X-Forwarded-For": "192.0.2.1"}))
                    await asyncio.wait_for(entered.wait(), 5)
                reader, writer = await asyncio.open_connection(host, int(port))
                size = 1048577 if failure == "body-size" else 100
                writer.write((f"POST /input HTTP/1.1\r\nHost: {server.address}\r\n"
                              f"X-Forwarded-For: 192.0.2.2\r\nContent-Length: {size}\r\n\r\n").encode())
                if failure in {"body-size", "execution-capacity"}:
                    writer.write(b"x" * size)
                elif failure == "body-rate":
                    writer.write(b"x")
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 7)
                status = {"body-size": 413, "body-rate": 408, "execution-capacity": 503}[failure]
                assert head.startswith(f"HTTP/1.1 {status} ".encode()), head
                if holder is not None:
                    release.set()
                    assert (await holder).status_code == 200
                retry = await client.get("/input", headers={"X-Forwarded-For": "192.0.2.2"})
                assert retry.status_code == 429
                assert "retry-after" in retry.headers
                assert app._module("app/input/route.py").calls == []
        finally:
            release.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if holder is not None:
                await asyncio.gather(holder, return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
            if backend == "redis":
                limiter.close()
    asyncio.run(run())


def test_h2_rejects_unfinished_body_without_closing_other_streams(tmp_path):
    async def run():
        app = create_app(_project(tmp_path), rate_limit=RateLimiter(1, 60))
        app.rate_limit.allow("127.0.0.1")
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        connection = h2.connection.H2Connection(config=h2.config.H2Configuration(header_encoding="utf-8"))
        connection.initiate_connection()
        async def response(stream_id):
            writer.write(connection.data_to_send())
            await writer.drain()
            headers = None
            async with asyncio.timeout(5):
                while True:
                    packet = await reader.read(65535)
                    assert packet
                    for event in connection.receive_data(packet):
                        if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream_id:
                            headers = dict(event.headers)
                        elif isinstance(event, h2.events.DataReceived):
                            connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        elif isinstance(event, h2.events.StreamEnded) and event.stream_id == stream_id:
                            return headers
                    writer.write(connection.data_to_send())
                    await writer.drain()
        try:
            connection.send_headers(1, [(":method", "POST"), (":scheme", "http"),
                                        (":authority", server.address), (":path", "/input"),
                                        ("content-length", "100")], end_stream=False)
            denied = await response(1)
            assert denied[":status"] == "429" and "connection" not in denied
            assert 1 <= int(denied["retry-after"]) <= 60
            connection.send_headers(3, [(":method", "GET"), (":scheme", "http"),
                                        (":authority", server.address), (":path", "/healthz")], end_stream=True)
            assert (await response(3))[":status"] == "200"
            assert app._module("app/input/route.py").calls == []
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("policy", ["fail_closed", "fail_open"])
def test_native_redis_outage_policy_is_decided_before_continue(tmp_path, monkeypatch, policy):
    redis = pytest.importorskip("redis")
    limiter = RedisRateLimiter(1, 60, url="redis://127.0.0.1:1", failure_policy=policy)
    calls = []
    def unavailable(*args):
        calls.append(True)
        raise redis.ConnectionError("test outage")
    monkeypatch.setattr(limiter.client, "eval", unavailable)
    async def run():
        app = create_app(_project(tmp_path), rate_limit=limiter)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /input HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 4\r\nExpect: 100-continue\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if policy == "fail_closed":
                assert head.startswith(b"HTTP/1.1 503 "), head
                assert app._module("app/input/route.py").calls == []
            else:
                assert head.startswith(b"HTTP/1.1 100 "), head
                writer.write(b"body")
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                assert head.startswith(b"HTTP/1.1 200 "), head
                assert app._module("app/input/route.py").calls == [b"body"]
            assert calls == [True]
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
            limiter.close()
    asyncio.run(run())
