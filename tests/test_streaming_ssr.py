"""Streaming SSR: a page may return an async iterable of UI nodes.

Buffered rendering stays the default; a page function that returns an
async iterator streams instead. The layout shell is sent first, each UI
chunk is serialized by Rust and sent as it is produced, and the layout
suffix closes the document. The existing StreamingResponse transport
provides backpressure and disconnect cancellation; a mid-stream error
ends the stream without changing the already-sent status.
"""
import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.server import start_native

LAYOUT = (
    "from pynext import h, layout\n"
    "@layout\n"
    "def Layout(children):\n"
    "    return h(\"html\", {\"lang\": \"en\"},\n"
    "             h(\"head\", {}, h(\"title\", {}, \"Stream test\")),\n"
    "             h(\"body\", {}, h(\"header\", {}, \"shell\"), children))\n"
)

STREAM_PAGE = (
    "import asyncio\n"
    "from pynext import h, page\n"
    "@page()\n"
    "async def Page():\n"
    "    yield h(\"p\", {}, \"chunk-one\")\n"
    "    await asyncio.sleep(0.05)\n"
    "    yield h(\"p\", {}, \"chunk-two\")\n"
)

PLAIN_PAGE = (
    "from pynext import h, page\n"
    "@page()\n"
    "async def Page():\n"
    "    return h(\"main\", {}, h(\"p\", {}, \"buffered\"))\n"
)


def make_project(root, stream_page=STREAM_PAGE):
    app_dir = root / "app"
    (app_dir / "stream").mkdir(parents=True)
    (app_dir / "stream" / "page.py").write_text(stream_page, encoding="utf-8")
    (app_dir / "plain").mkdir(parents=True)
    (app_dir / "plain" / "page.py").write_text(PLAIN_PAGE, encoding="utf-8")
    (app_dir / "layout.py").write_text(LAYOUT, encoding="utf-8")
    return create_app(root)


async def connect(server):
    host, port = server.address.rsplit(":", 1)
    return await asyncio.open_connection(host, int(port))


async def read_chunk_frame(reader):
    line = await asyncio.wait_for(reader.readline(), 5)
    size = int(line.strip(), 16)
    data = await asyncio.wait_for(reader.readexactly(size + 2), 5)
    assert data.endswith(b"\r\n")
    return size, data[:-2]


async def read_frames_until(reader, marker, limit=20):
    """Read chunked frames until the accumulated body contains marker."""
    body = b""
    for _ in range(limit):
        size, data = await read_chunk_frame(reader)
        if size == 0:
            break
        body += data
        if marker in body:
            break
    return body


def test_native_streaming_page_sends_shell_and_chunks_in_order(tmp_path):
    async def run():
        release = asyncio.Event()

        async def page():
            from pynext import h
            yield h("p", {}, "chunk-one")
            await release.wait()
            yield h("p", {}, "chunk-two")

        app = make_project(tmp_path)
        _, layouts = app.entries["/stream"]
        app.entries["/stream"] = (page, layouts)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert headers.startswith(b"HTTP/1.1 200 ")
            assert b"transfer-encoding: chunked\r\n" in headers.lower()
            assert b"content-length:" not in headers.lower()
            body = await read_frames_until(reader, b"chunk-one")
            assert b"chunk-two" not in body
            assert body.index(b"<!DOCTYPE html><html") < body.index(b"chunk-one")
            assert b"<header>shell</header>" in body
            release.set()
            rest = b""
            while True:
                size, data = await read_chunk_frame(reader)
                if size == 0:
                    break
                rest += data
            assert b"chunk-two" in rest
            assert b"</body></html>" in rest
            assert (body + rest).index(b"chunk-one") < (body + rest).index(b"chunk-two")
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_streaming_page_disconnect_cancels_generator(tmp_path):
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()

        async def page():
            from pynext import h
            try:
                started.set()
                yield h("p", {}, "chunk-one")
                await asyncio.Event().wait()
            finally:
                closed.set()

        app = make_project(tmp_path)
        _, layouts = app.entries["/stream"]
        app.entries["/stream"] = (page, layouts)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert headers.startswith(b"HTTP/1.1 200 ")
            await read_frames_until(reader, b"chunk-one")
            await asyncio.wait_for(started.wait(), 5)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(closed.wait(), 5)
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_streaming_page_midstream_error_keeps_status(tmp_path):
    async def run():
        async def page():
            from pynext import h
            yield h("p", {}, "chunk-one")
            raise ValueError("boom")

        app = make_project(tmp_path)
        _, layouts = app.entries["/stream"]
        app.entries["/stream"] = (page, layouts)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        reader, writer = await connect(server)
        try:
            writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert headers.startswith(b"HTTP/1.1 200 ")
            await read_frames_until(reader, b"chunk-one")
            remaining = await asyncio.wait_for(reader.read(), 5)
            assert b"chunk-two" not in remaining
            assert not remaining.endswith(b"0\r\n\r\n")
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_buffered_page_still_sends_content_length(tmp_path):
    async def run():
        app = make_project(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                response = await client.get("/plain")
                assert response.status_code == 200
                assert response.headers["content-length"] == str(len(response.content))
                assert b"<p>buffered</p>" in response.content
                streamed = await client.get("/stream")
                assert streamed.status_code == 200
                assert "content-length" not in streamed.headers
                assert streamed.content.index(b"chunk-one") < streamed.content.index(b"chunk-two")
                assert streamed.content.startswith(b"<!DOCTYPE html><html")
                assert streamed.content.endswith(b"</body></html>")
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def scope(path="/stream", method="GET"):
    return {"type": "http", "method": method, "path": path, "headers": []}


async def queue_receive(queue):
    return await queue.get()


def test_asgi_streaming_page_chunks_in_order_and_closes(tmp_path):
    async def run():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        app = make_project(tmp_path)
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), outgoing.put))
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["status"] == 200
        assert b"content-length" not in dict(start["headers"])
        bodies = []
        while True:
            message = await asyncio.wait_for(outgoing.get(), 3)
            assert message["type"] == "http.response.body"
            if not message.get("more_body", False):
                assert message["body"] == b""
                break
            bodies.append(message["body"])
        await asyncio.wait_for(task, 3)
        full = b"".join(bodies)
        assert full.startswith(b"<!DOCTYPE html><html")
        assert full.endswith(b"</body></html>")
        assert full.index(b"chunk-one") < full.index(b"chunk-two")
        assert b"<header>shell</header>" in full
        await app.isr.close()
    asyncio.run(run())


def test_asgi_streaming_page_backpressure_does_not_advance_generator(tmp_path):
    async def run():
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        produced, unblock, sending = [], asyncio.Event(), asyncio.Event()

        async def page():
            from pynext import h
            for label in ("chunk-one", "chunk-two"):
                produced.append(label)
                yield h("p", {}, label)

        app = make_project(tmp_path)
        _, layouts = app.entries["/stream"]
        app.entries["/stream"] = (page, layouts)

        async def send(message):
            if message["type"] == "http.response.body" and message.get("more_body"):
                sending.set()
                await unblock.wait()

        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), send))
        await asyncio.wait_for(sending.wait(), 3)
        await asyncio.sleep(0.05)
        # The layout prefix is stuck in send, so the page generator has not
        # been asked for its first chunk yet.
        assert produced == []
        unblock.set()
        await asyncio.wait_for(task, 3)
        assert produced == ["chunk-one", "chunk-two"]
        await app.isr.close()
    asyncio.run(run())


def test_asgi_async_function_returning_async_iterable_streams(tmp_path):
    async def run():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        returned_page = (
            "from pynext import h, page\n"
            "@page()\n"
            "async def Page():\n"
            "    async def gen():\n"
            "        yield h(\"p\", {}, \"returned\")\n"
            "    return gen()\n"
        )
        app = make_project(tmp_path, stream_page=returned_page)
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), outgoing.put))
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["status"] == 200
        bodies = []
        while True:
            message = await asyncio.wait_for(outgoing.get(), 3)
            if not message.get("more_body", False):
                assert message["body"] == b""
                break
            bodies.append(message["body"])
        await asyncio.wait_for(task, 3)
        full = b"".join(bodies)
        assert full.startswith(b"<!DOCTYPE html><html")
        assert b"returned" in full
        assert full.endswith(b"</body></html>")
        await app.isr.close()
    asyncio.run(run())


def test_isr_streaming_page_also_streams_in_development(tmp_path):
    async def run():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        isr_page = (
            "from pynext import h, page\n"
            "@page(render=\"isr\", revalidate=60)\n"
            "async def Page():\n"
            "    yield h(\"p\", {}, \"chunk-one\")\n"
        )
        app = make_project(tmp_path, stream_page=isr_page)
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), outgoing.put))
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["status"] == 200
        await asyncio.wait_for(task, 3)
        messages = []
        while not outgoing.empty():
            messages.append(await outgoing.get())
        assert b"chunk-one" in b"".join(message.get("body", b"") for message in messages)
        await app.isr.close()
    asyncio.run(run())
