import asyncio
import contextvars

import h2.config
import h2.connection
import h2.events
import httpx
import pytest

from pynext import Response, StreamingResponse, create_app
from pynext.server import start_native


def make_app(root, handler):
    for name in ("stream", "quick"):
        directory = root / "app" / name
        directory.mkdir(parents=True)
        (directory / "route.py").write_text(
            'from pynext import Response, route\n'
            '@route("GET")\ndef get(): return Response("ok")\n', encoding="utf-8"
        )
    public = root / "public"
    public.mkdir()
    (public / "health.txt").write_text("healthy", encoding="utf-8")
    app = create_app(root)
    app.entries["/stream"]["GET"] = handler
    return app


async def connect(server):
    host, port = server.address.rsplit(":", 1)
    return await asyncio.open_connection(host, int(port))


async def get_first_http1_chunk(reader, writer):
    writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert headers.startswith(b"HTTP/1.1 200 ")
    assert b"transfer-encoding: chunked\r\n" in headers.lower()
    assert b"content-length:" not in headers.lower()
    size = int((await asyncio.wait_for(reader.readline(), 5)).strip(), 16)
    chunk = await asyncio.wait_for(reader.readexactly(size + 2), 5)
    assert chunk.endswith(b"\r\n")
    return chunk[:-2]


def test_native_stream_disconnect_releases_handler_capacity(tmp_path):
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()

        async def chunks():
            try:
                started.set()
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks(), media_type="text/plain")

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=1, acquire_timeout_ms=100)
        reader, writer = await connect(server)
        try:
            assert await get_first_http1_chunk(reader, writer) == b"first"
            await asyncio.wait_for(started.wait(), 5)
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.get("/quick")).status_code == 503
                assert (await client.get("/health.txt")).text == "healthy"
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(closed.wait(), 5)
                deadline = asyncio.get_running_loop().time() + 5
                while True:
                    response = await client.get("/quick")
                    if response.status_code == 200:
                        break
                    assert response.status_code == 503 and asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.05)
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_disconnect_cancels_pending_async_handler(tmp_path):
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(started.wait(), 5)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(cancelled.wait(), 5)
            assert reader.at_eof() or await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_stream_timeout_closes_iterator(tmp_path, monkeypatch):
    monkeypatch.setattr("pynext.server.STREAM_CHUNK_TIMEOUT", 0.2)

    async def run():
        closed = asyncio.Event()

        async def chunks():
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks())

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            assert await get_first_http1_chunk(reader, writer) == b"first"
            await asyncio.wait_for(closed.wait(), 5)
            remaining = await asyncio.wait_for(reader.read(), 5)
            assert not remaining.endswith(b"0\r\n\r\n")
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_http2_stream_uses_http2_framing_and_cleans_up_on_reset(tmp_path):
    async def run():
        closed = asyncio.Event()

        async def chunks():
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks())

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            connection = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
            connection.initiate_connection()
            connection.send_headers(1, [(":method", "GET"), (":scheme", "http"),
                                        (":authority", server.address), (":path", "/stream")], end_stream=True)
            writer.write(connection.data_to_send())
            await writer.drain()
            headers, data = None, b""
            async with asyncio.timeout(5):
                while not data:
                    packet = await reader.read(65535)
                    assert packet
                    for event in connection.receive_data(packet):
                        if isinstance(event, h2.events.ResponseReceived):
                            headers = dict(event.headers)
                        elif isinstance(event, h2.events.DataReceived):
                            data += event.data
                            connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    writer.write(connection.data_to_send())
                    await writer.drain()
            assert headers[":status"] == "200" and "transfer-encoding" not in headers
            assert data == b"first"
            connection.reset_stream(1)
            writer.write(connection.data_to_send())
            await writer.drain()
            await asyncio.wait_for(closed.wait(), 5)
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_wait_queue_is_bounded_and_releases_waiters(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def handler():
            started.set()
            await release.wait()
            return Response("finished")

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=1, acquire_timeout_ms=5000)
        tasks = []
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=7) as client:
                active = asyncio.create_task(client.get("/stream"))
                tasks.append(active)
                await asyncio.wait_for(started.wait(), 5)
                waiting = asyncio.create_task(client.get("/quick"))
                tasks.append(waiting)
                await asyncio.sleep(0.25)
                assert not waiting.done()
                rejected = await asyncio.wait_for(client.get("/quick"), 2)
                assert rejected.status_code == 503 and rejected.headers["retry-after"] == "1"
                assert not waiting.done()
                release.set()
                assert (await active).status_code == 200
                assert (await waiting).status_code == 200
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_stopped_python_loop_returns_503(tmp_path):
    async def run():
        app = make_app(tmp_path, lambda: Response("unused"))
        stopped_loop = asyncio.new_event_loop()
        server = start_native(app, stopped_loop, port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.get("/stream")).status_code == 503
        finally:
            await asyncio.to_thread(server.stop)
            stopped_loop.close()
            await app.isr.close()
    asyncio.run(run())


def test_native_head_does_not_iterate_stream(tmp_path):
    async def run():
        started = asyncio.Event()

        async def chunks():
            started.set()
            yield b"content"

        async def handler():
            return StreamingResponse(chunks())

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                response = await client.head("/stream")
                assert response.status_code == 200 and response.content == b""
                assert not started.is_set()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_stream_preserves_handler_context_between_chunks(tmp_path):
    marker = contextvars.ContextVar("stream_marker", default="missing")

    async def run():
        async def handler():
            marker.set("handler")

            async def chunks():
                yield marker.get() + ":"
                marker.set("next")
                yield marker.get()

            return StreamingResponse(chunks(), media_type="text/plain")

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.get("/stream")).text == "handler:next"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_http2_stream_completes_incrementally(tmp_path):
    async def run():
        release = asyncio.Event()

        async def chunks():
            yield b"first"
            await release.wait()
            yield b"second"

        async def handler():
            return StreamingResponse(chunks())

        app = make_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            connection = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
            connection.initiate_connection()
            connection.send_headers(1, [(":method", "GET"), (":scheme", "http"),
                                        (":authority", server.address), (":path", "/stream")], end_stream=True)
            writer.write(connection.data_to_send())
            await writer.drain()
            headers, data, ended = None, b"", False
            async with asyncio.timeout(5):
                while not ended:
                    packet = await reader.read(65535)
                    assert packet
                    for event in connection.receive_data(packet):
                        if isinstance(event, h2.events.ResponseReceived):
                            headers = dict(event.headers)
                        elif isinstance(event, h2.events.DataReceived):
                            data += event.data
                            connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        elif isinstance(event, h2.events.StreamEnded):
                            ended = True
                    if data == b"first":
                        assert not ended
                        release.set()
                    writer.write(connection.data_to_send())
                    await writer.drain()
            assert headers[":status"] == "200" and "transfer-encoding" not in headers
            assert data == b"firstsecond"
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
