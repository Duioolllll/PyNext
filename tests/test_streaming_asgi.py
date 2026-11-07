import asyncio

import pytest

from pynext import StreamingResponse, create_app


def make_app(root, handler):
    route = root / "app" / "stream"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import Response, route\n'
        '@route("GET")\ndef get(): return Response("placeholder")\n', encoding="utf-8"
    )
    app = create_app(root)
    app.entries["/stream"]["GET"] = handler
    return app


def scope(method="GET"):
    return {"type": "http", "method": method, "path": "/stream", "headers": []}


async def queue_receive(queue):
    return await queue.get()


def test_asgi_streams_incrementally_and_closes_the_iterator(tmp_path):
    async def run():
        release, closed = asyncio.Event(), asyncio.Event()
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})

        async def chunks():
            try:
                yield b"first"
                await release.wait()
                yield "한글"
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")

        app = make_app(tmp_path, handler)
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), outgoing.put))
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["status"] == 200
        assert b"content-length" not in dict(start["headers"])
        first = await asyncio.wait_for(outgoing.get(), 3)
        assert first == {"type": "http.response.body", "body": b"first", "more_body": True}
        assert not task.done() and outgoing.empty()
        release.set()
        assert (await asyncio.wait_for(outgoing.get(), 3))["body"] == "한글".encode()
        assert await asyncio.wait_for(outgoing.get(), 3) == {
            "type": "http.response.body", "body": b"", "more_body": False
        }
        await asyncio.wait_for(task, 3)
        assert closed.is_set()
        await app.isr.close()
    asyncio.run(run())


def test_asgi_disconnect_cancels_handler_and_stream(tmp_path):
    async def run():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        started, cleaned = asyncio.Event(), asyncio.Event()
        await incoming.put({"type": "http.request", "body": b""})

        async def handler():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        app = make_app(tmp_path, handler)
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), outgoing.put))
        await asyncio.wait_for(started.wait(), 3)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 3)
        assert cleaned.is_set() and outgoing.empty()

        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        cleaned.clear()
        await incoming.put({"type": "http.request", "body": b""})

        async def chunks():
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def stream_handler():
            return StreamingResponse(chunks())

        app.entries["/stream"]["GET"] = stream_handler
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), outgoing.put))
        assert (await asyncio.wait_for(outgoing.get(), 3))["status"] == 200
        assert (await asyncio.wait_for(outgoing.get(), 3))["body"] == b"first"
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 3)
        assert cleaned.is_set() and outgoing.empty()
        await app.isr.close()
    asyncio.run(run())


def test_asgi_backpressure_and_head_do_not_consume_extra_chunks(tmp_path):
    async def run():
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b""})
        unblock, sending, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        produced = []

        async def chunks():
            try:
                for item in (b"one", b"two"):
                    produced.append(item)
                    yield item
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks())

        async def send(message):
            if message.get("body") == b"one":
                sending.set()
                await unblock.wait()

        app = make_app(tmp_path, handler)
        task = asyncio.create_task(app(scope(), lambda: queue_receive(incoming), send))
        await asyncio.wait_for(sending.wait(), 3)
        await asyncio.sleep(0.05)
        assert produced == [b"one"]
        unblock.set()
        await asyncio.wait_for(task, 3)
        assert produced == [b"one", b"two"] and closed.is_set()

        produced.clear()
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b""})
        await app(scope("HEAD"), lambda: queue_receive(incoming), outgoing.put)
        start, body = await outgoing.get(), await outgoing.get()
        assert start["status"] == 200 and b"content-length" not in dict(start["headers"])
        assert body["body"] == b"" and produced == []
        await app.isr.close()
    asyncio.run(run())


def test_streaming_response_rejects_invalid_framing_and_chunk_types(tmp_path):
    async def chunks():
        yield 5

    with pytest.raises(ValueError, match="framing"):
        StreamingResponse(chunks(), headers=[(b"content-length", b"5")])
    with pytest.raises(ValueError, match="permits a body"):
        StreamingResponse(chunks(), status=204)

    async def run():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b""})
        app = make_app(tmp_path, lambda: StreamingResponse(chunks()))
        with pytest.raises(TypeError, match="chunks must be"):
            await app(scope(), lambda: queue_receive(incoming), outgoing.put)
        assert (await outgoing.get())["status"] == 200
        assert outgoing.empty()
        await app.isr.close()
    asyncio.run(run())


def test_asgi_stream_closes_if_send_raises(tmp_path):
    async def run():
        incoming, closed = asyncio.Queue(), asyncio.Event()
        await incoming.put({"type": "http.request", "body": b""})

        async def chunks():
            try:
                yield b"first"
                yield b"second"
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks())

        async def send(message):
            if message["type"] == "http.response.body":
                raise BrokenPipeError("client closed")

        app = make_app(tmp_path, handler)
        with pytest.raises(BrokenPipeError):
            await app(scope(), lambda: queue_receive(incoming), send)
        assert closed.is_set()
        await app.isr.close()
    asyncio.run(run())
