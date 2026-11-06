import asyncio

import httpx

from pynext import create_app
from pynext.server import start_native


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
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        connections = []
        try:
            for _ in range(64):
                reader, writer = await asyncio.open_connection(host, int(port))
                writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\nx")
                await writer.drain()
                connections.append((reader, writer))
            await asyncio.sleep(0.5)
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.post("/api/input", content=b"x")).status_code == 503
                assert (await client.get("/health.txt")).text == "healthy"
                response = await client.get("/api/input")
                assert response.status_code == 200 and response.json() == {"ok": True}
                # Closing a partial upload must promptly return its intake slot.
                connections[-1][1].close()
                await connections[-1][1].wait_closed()
                deadline = asyncio.get_running_loop().time() + 3
                while True:
                    response = await client.post("/api/input", content=b"x")
                    if response.status_code == 200:
                        break
                    assert response.status_code == 503 and asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.05)
                first_line = await asyncio.wait_for(connections[0][0].readline(), timeout=15)
                assert first_line.startswith(b"HTTP/1.1 408 "), first_line
                headers = await asyncio.wait_for(connections[0][0].readuntil(b"\r\n\r\n"), timeout=2)
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
        server = start_native(app, asyncio.get_running_loop(), port=0)
        tasks = []
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=20) as client:
                tasks = [asyncio.create_task(client.get("/api/wait")) for _ in range(64)]
                async with asyncio.timeout(15):
                    while module.started < 64:
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
