from conftest import page_html
import asyncio

import httpx

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


def test_shutdown_closes_idle_keepalive_after_inflight_request_finishes(tmp_path):
    for name in ("quick", "slow"):
        directory = tmp_path / "app" / name
        directory.mkdir(parents=True)
        (directory / "route.py").write_text(
            'from pynext import Response, route\n@route("GET")\n'
            'async def get(): return Response("ok")\n', encoding="utf-8")

    async def run():
        from pynext import Response
        app = create_app(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow():
            entered.set()
            await release.wait()
            return Response("finished")

        app.entries["/slow"]["GET"] = slow
        server = start_native(app, asyncio.get_running_loop(), port=0)
        first = httpx.AsyncClient(base_url=f"http://{server.address}")
        second = httpx.AsyncClient(base_url=f"http://{server.address}")
        stop = None
        try:
            assert (await first.get("/quick")).text == "ok"  # Keep this connection idle and open.
            request = asyncio.create_task(second.get("/slow"))
            await asyncio.wait_for(entered.wait(), 5)
            stop = asyncio.create_task(asyncio.to_thread(server.stop))
            await asyncio.sleep(0.1)
            assert not stop.done()
            release.set()
            assert (await asyncio.wait_for(request, 5)).text == "finished"
            await asyncio.wait_for(stop, 5)
        finally:
            release.set()
            await first.aclose()
            await second.aclose()
            if stop is None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_slow_request_bodies_do_not_hold_execution_or_static_capacity(tmp_path):
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    public = tmp_path / "public"
    public.mkdir()
    (public / "health.txt").write_text("healthy", encoding="utf-8")

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_capacity=2, body_capacity_per_ip=3,
                              body_timeout_ms=5000, body_min_rate_bytes_per_sec=0)
        host, port = server.address.rsplit(":", 1)
        connections = []
        try:
            async def open_partial():
                reader, writer = await asyncio.open_connection(host, int(port))
                connections.append((reader, writer))
                writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
                await writer.drain()
                # Continue is sent only after dispatch acquires a body slot
                # and polls the body. Each connection is a confirmed holder.
                interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                assert interim.startswith(b"HTTP/1.1 100 Continue\r\n"), interim

            await open_partial()
            await open_partial()
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         trust_env=False, timeout=5) as client:
                assert (await client.post("/api/input", content=b"x")).status_code == 503
                assert (await client.get("/health.txt")).text == "healthy"
                response = await client.get("/api/input")
                assert response.status_code == 200 and response.json() == {"ok": True}
                # Closing a partial upload must promptly return its intake slot.
                connections[0][1].close()
                await connections[0][1].wait_closed()
                deadline = asyncio.get_running_loop().time() + 3
                while True:
                    response = await client.post("/api/input", content=b"x")
                    if response.status_code == 200:
                        break
                    assert response.status_code == 503 and asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.05)
                first_line = await asyncio.wait_for(connections[1][0].readline(), timeout=10)
                assert first_line.startswith(b"HTTP/1.1 408 "), first_line
                headers = await asyncio.wait_for(connections[1][0].readuntil(b"\r\n\r\n"), timeout=2)
                assert b"connection: close\r\n" in headers.lower()
                assert (await client.post("/api/input", content=b"x" * 1_048_577)).status_code == 413
                assert (await client.request("GET", "/health.txt", content=b"x" * 1_048_577)).status_code == 413
        finally:
            for _, writer in connections:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in connections), return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_static_files_bypass_saturated_python_execution(tmp_path):
    route = tmp_path / "app" / "api" / "wait"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        'started = 0\nrelease = None\n'
        '@route("GET")\n'
        'async def get():\n'
        '    global started\n'
        '    started += 1\n'
        '    await release.wait()\n'
        '    return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    public = tmp_path / "public"
    public.mkdir()
    (public / "health.txt").write_text("healthy", encoding="utf-8")
    cached = tmp_path / "app" / "cached"
    cached.mkdir()
    (cached / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=30)\n'
        'def Page(): return h("p", {}, "cached")\n', encoding="utf-8"
    )

    async def run():
        app = create_app(tmp_path)
        module = app._module("app/api/wait/route.py")
        module.release = asyncio.Event()
        assert app.isr.store.insert("/cached", b"<p>cached</p>", 30, 1)
        capacity = 4
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=capacity)
        tasks = []
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=20) as client:
                # Admit each request before starting the next. A burst can
                # legitimately time out in the bounded wait queue on a
                # loaded runner, leaving fewer than capacity handlers active.
                for expected in range(1, capacity + 1):
                    tasks.append(asyncio.create_task(client.get("/api/wait")))
                    async with asyncio.timeout(15):
                        while module.started < expected:
                            await asyncio.sleep(0.02)
                assert (await client.get("/api/wait")).status_code == 503
                assert (await client.get("/health.txt")).text == "healthy"
                assert (await client.get("/cached")).text == "<p>cached</p>"
                module.release.set()
                assert all(response.status_code == 200 for response in await asyncio.gather(*tasks))
        finally:
            module.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_main_capacity_waits_for_slot_then_503_with_retry_after(tmp_path):
    route = tmp_path / "app" / "api" / "wait"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        'started = 0\nrelease = None\n'
        '@route("GET")\n'
        'async def get():\n'
        '    global started\n'
        '    started += 1\n'
        '    await release.wait()\n'
        '    return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path)
        module = app._module("app/api/wait/route.py")
        module.release = asyncio.Event()

        async def run_case(timeout_ms, expect_status, release_after=None):
            module.release.clear()
            started_before = module.started
            server = start_native(app, asyncio.get_running_loop(), port=0,
                                  max_concurrent=1, acquire_timeout_ms=timeout_ms)
            try:
                async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                    task = asyncio.create_task(client.get("/api/wait"))
                    async with asyncio.timeout(10):
                        while module.started <= started_before:
                            await asyncio.sleep(0.02)
                    if release_after is not None:
                        await asyncio.sleep(release_after)
                        module.release.set()
                    response = await client.get("/api/wait")
                    assert response.status_code == expect_status
                    if expect_status == 503:
                        assert response.headers["retry-after"] == "1"
                    module.release.set()
                    first = await task
                    assert first.status_code == 200
            finally:
                module.release.set()
                await asyncio.to_thread(server.stop)

        await run_case(100, 503)
        await run_case(5000, 200, release_after=0.3)
        await app.isr.close()

    asyncio.run(run())


def test_body_stage_503_and_408_carry_retry_after(tmp_path):
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_capacity=1, body_capacity_per_ip=2,
                              body_timeout_ms=1500)
        host, port = server.address.rsplit(":", 1)
        writer = None
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         trust_env=False, timeout=5) as client:
                reader, writer = await asyncio.open_connection(host, int(port))
                writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
                await writer.drain()
                # Hyper sends Continue when dispatch polls the body, after
                # acquiring its slot. This is a confirmed global slot holder;
                # the per-IP limit still has room for the next request.
                interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                assert interim.startswith(b"HTTP/1.1 100 Continue\r\n"), interim
                response = await client.post("/api/input", content=b"x")
                assert response.status_code == 503
                assert response.headers["retry-after"] == "1"
                first_line = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first_line.startswith(b"HTTP/1.1 408 "), first_line
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
                assert b"retry-after: 1\r\n" in headers.lower()
                assert b"connection: close\r\n" in headers.lower()
                # The timed-out body returned its slot.
                assert (await client.post("/api/input", content=b"x")).status_code == 200
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_static_pages_carry_cache_control(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                response = await client.get("/")
                assert response.status_code == 200 and response.text == page_html("<p>built</p>")
                assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


# NOTE: this plain conflicting-Content-Length case is rejected by hyper's
# parser with an automatic 400 before dispatch ever runs, so it verifies
# hyper's behaviour, not the dispatch check. The dispatch check
# (conflicting_content_length) is a backstop for the case where conflicting
# values ever reach dispatch; when Transfer-Encoding is present hyper drops
# Content-Length entirely, so the check never sees them there either — the
# CL+TE combination is instead caught by the first-request sniff (S-P1-31,
# see the next test).
def test_conflicting_content_length_is_bad_request(tmp_path):
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello"
                )
                await writer.drain()
                first_line = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first_line.startswith(b"HTTP/1.1 400 "), first_line
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_transfer_encoding_and_content_length_is_bad_request(tmp_path):
    # S-P1-31 (a): Content-Length + Transfer-Encoding together is
    # ambiguous framing and is rejected with 400, even though RFC 7230
    # section 3.3.3 says Transfer-Encoding overrides Content-Length and
    # hyper implements that by dropping Content-Length and framing the
    # request as chunked. The lenient override is exactly the smuggling
    # primitive (a proxy framing by Content-Length disagrees with the
    # backend framing by Transfer-Encoding), so PyNext rejects the
    # combination at the first-request sniff instead of running the
    # handler. This replaces the old expectation that the handler runs
    # with 200 here.
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Content-Length: 5\r\nContent-Length: 6\r\n\r\n"
                    b"0\r\n\r\n"
                )
                await writer.drain()
                first_line = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first_line.startswith(b"HTTP/1.1 400 "), first_line
                headers = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=5
                )
                assert b"connection: close" in headers.lower(), headers
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_http2_prior_knowledge_preface_gets_settings_frame(tmp_path):
    # h2c: a client that opens with the HTTP/2 prior-knowledge preface must
    # get an HTTP/2 SETTINGS frame back, not an HTTP/1.1 400 (the old
    # Http1Builder-only loop failed the preface with a VersionH2 error).
    # This is a handshake-only check, not a full h2 client: the 9-byte
    # frame header is enough to tell a SETTINGS frame (type 0x4, stream 0)
    # apart from an HTTP/1.1 error line, which starts with ASCII "HTTP".
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
                    b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"  # empty SETTINGS frame
                )
                await writer.drain()
                header = await asyncio.wait_for(reader.readexactly(9), timeout=5)
                frame_type = header[3]
                stream_id = int.from_bytes(header[5:9], "big") & 0x7FFF_FFFF
                assert frame_type == 0x4, header  # SETTINGS
                assert stream_id == 0, header
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_native_body_timeout_zero_disables_timeout(tmp_path):
    # R5-M1: body_timeout_ms=0 must mean "no timeout" on the native
    # path, like the ASGI path (body_timeout_ms / 1000 or None) and the
    # other native timeouts. Before the fix, a body arriving after the
    # headers was answered 408 immediately.
    route = tmp_path / "app" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8")

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_timeout_ms=0, body_min_rate_bytes_per_sec=0)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"POST /input HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Length: 100\r\n\r\n")
                await writer.drain()
                # The body arrives well after the headers; with the
                # timeout disabled the request must still succeed.
                await asyncio.sleep(0.3)
                writer.write(b"x" * 100)
                await writer.drain()
                first_line = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first_line.startswith(b"HTTP/1.1 200 "), first_line
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
