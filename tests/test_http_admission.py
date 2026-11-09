"""Reject over-limit HTTP requests before polling their request bodies."""
import asyncio
import os
import time
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


def test_native_limiter_timeout_fails_closed_before_body(tmp_path):
    class SlowLimiter(RateLimiter):
        def allow(self, key):
            time.sleep(2.4)
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
