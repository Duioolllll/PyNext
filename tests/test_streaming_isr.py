import asyncio
import os
import uuid

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


def make_project(root):
    directory = root / "app" / "stream"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\ngate = None\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    number = calls\n'
        '    yield h("p", {}, f"first-{number}")\n'
        '    if gate is not None: await gate.wait()\n'
        '    yield h("p", {}, f"last-{number}")\n'
        f'# test namespace {uuid.uuid4().hex}\n', encoding="utf-8")
    write_manifest(root)


async def start_asgi_stream(app):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    scope = {"type": "http", "method": "GET", "path": "/stream", "headers": []}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    start = await asyncio.wait_for(outgoing.get(), 5)
    assert start["status"] == 200
    assert b"content-length" not in dict(start["headers"])
    body = bytearray()
    while b"first-" not in body:
        message = await asyncio.wait_for(outgoing.get(), 5)
        body.extend(message.get("body", b""))
    return task, incoming, outgoing, body


async def finish_asgi_stream(task, outgoing, body):
    while True:
        message = await asyncio.wait_for(outgoing.get(), 5)
        body.extend(message.get("body", b""))
        if not message.get("more_body"):
            break
    await asyncio.wait_for(task, 5)
    return bytes(body)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "redis"])
def test_first_isr_request_streams_and_concurrent_request_shares_result(tmp_path, backend):
    if backend == "redis" and not os.environ.get("PYNEXT_TEST_REDIS_URL"):
        pytest.skip("PYNEXT_TEST_REDIS_URL is not configured")
    make_project(tmp_path)
    options = {"production": True, "cache_backend": backend}
    if backend == "sqlite":
        options["cache_path"] = tmp_path / "cache.sqlite3"
    elif backend == "redis":
        options["cache_url"] = os.environ["PYNEXT_TEST_REDIS_URL"]

    async def run():
        app = create_app(tmp_path, **options)
        module = app._module("app/stream/page.py")
        module.gate = asyncio.Event()
        try:
            task, _, outgoing, body = await start_asgi_stream(app)
            assert b"last-1" not in body
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                follower = asyncio.create_task(client.get("/stream"))
                await asyncio.sleep(0.05)
                assert not follower.done()
                module.gate.set()
                body = await finish_asgi_stream(task, outgoing, body)
                assert (await follower).content == body
                assert (await client.get("/stream")).content == body
                assert module.calls == 1
                assert b"last-1" in body
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_isr_stream_invalidation_fences_the_previous_generation(tmp_path):
    make_project(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/stream/page.py")
        module.gate = asyncio.Event()
        try:
            old_task, _, old_out, old_body = await start_asgi_stream(app)
            assert await app.revalidate("/stream")
            new_task, _, new_out, new_body = await start_asgi_stream(app)
            assert b"first-1" in old_body and b"first-2" in new_body
            module.gate.set()
            old_body, new_body = await asyncio.gather(
                finish_asgi_stream(old_task, old_out, old_body),
                finish_asgi_stream(new_task, new_out, new_body))
            assert b"last-1" in old_body and b"last-2" in new_body
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/stream")).content == new_body
                assert module.calls == 2
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_isr_head_detaches_stream_without_blocking_generation(tmp_path):
    make_project(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/stream/page.py")
        module.gate = asyncio.Event()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                head = await asyncio.wait_for(client.head("/stream"), 5)
                assert head.status_code == 200 and not head.content
                module.gate.set()
                response = await asyncio.wait_for(client.get("/stream"), 5)
                assert b"last-1" in response.content
                assert module.calls == 1
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_isr_generation_survives_initiating_client_disconnect(tmp_path):
    make_project(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/stream/page.py")
        module.gate = asyncio.Event()
        try:
            task, incoming, _, _ = await start_asgi_stream(app)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 5)
            module.gate.set()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/stream")
                assert b"last-1" in response.content
                assert module.calls == 1
        finally:
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["sqlite", "redis"])
def test_shared_isr_stream_is_generated_once_across_apps(tmp_path, backend):
    if backend == "redis" and not os.environ.get("PYNEXT_TEST_REDIS_URL"):
        pytest.skip("PYNEXT_TEST_REDIS_URL is not configured")
    make_project(tmp_path)
    options = {"production": True, "cache_backend": backend}
    if backend == "sqlite":
        options["cache_path"] = tmp_path / "shared.sqlite3"
    else:
        options["cache_url"] = os.environ["PYNEXT_TEST_REDIS_URL"]

    async def run():
        first, second = create_app(tmp_path, **options), create_app(tmp_path, **options)
        module = first._module("app/stream/page.py")
        other_module = second._module("app/stream/page.py")
        module.gate = asyncio.Event()
        try:
            task, _, outgoing, body = await start_asgi_stream(first)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as client:
                follower = asyncio.create_task(client.get("/stream"))
                await asyncio.sleep(0.1)
                assert not follower.done()
                module.gate.set()
                body = await finish_asgi_stream(task, outgoing, body)
                assert (await asyncio.wait_for(follower, 5)).content == body
                assert module.calls == 1 and other_module.calls == 0
        finally:
            await first.isr.close()
            await second.isr.close()
    asyncio.run(run())


def test_isr_stream_applies_bounded_backpressure(tmp_path):
    make_project(tmp_path)

    async def run():
        from pynext import h
        app = create_app(tmp_path, production=True)
        produced, sending, unblock = [], asyncio.Event(), asyncio.Event()

        async def page():
            for number in range(20):
                produced.append(number)
                yield h("p", {}, str(number))

        _, layouts = app.entries["/stream"]
        app.entries["/stream"] = (page, layouts)
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})

        async def send(message):
            if message["type"] == "http.response.body" and message.get("more_body"):
                sending.set()
                await unblock.wait()

        task = asyncio.create_task(app({"type": "http", "method": "GET", "path": "/stream", "headers": []},
                                       incoming.get, send))
        try:
            await asyncio.wait_for(sending.wait(), 5)
            await asyncio.sleep(0.05)
            assert len(produced) <= 6
            unblock.set()
            await asyncio.wait_for(task, 5)
            assert len(produced) == 20
        finally:
            unblock.set()
            await app.isr.close()
    asyncio.run(run())


def test_isr_first_request_streams_over_native_http(tmp_path):
    make_project(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/stream/page.py")
        module.gate = asyncio.Event()
        server = start_native(app, asyncio.get_running_loop(), port=0)
        writer = None
        try:
            host, port = server.address.rsplit(":", 1)
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert header.startswith(b"HTTP/1.1 200")
            assert b"transfer-encoding: chunked" in header.lower()
            body = bytearray()
            while b"first-1" not in body:
                size = int((await asyncio.wait_for(reader.readline(), 5)).strip(), 16)
                body.extend((await reader.readexactly(size + 2))[:-2])
            assert b"last-1" not in body
            module.gate.set()
            while True:
                size = int((await asyncio.wait_for(reader.readline(), 5)).strip(), 16)
                if not size:
                    break
                body.extend((await reader.readexactly(size + 2))[:-2])
            assert b"last-1" in body
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/stream")).content == body
                assert module.calls == 1
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
