"""Connection limits (A4): total / per-IP connection caps, idle keep-alive
timeout, slow-header timeout and a minimum body receive rate.

The limits live in the Rust accept loop / body intake, not in the Python
bridge: the bridge only ever sees requests whose headers and body already
arrived. Slowloris-style clients therefore cannot hold a Python worker
thread or an execution slot at all.
"""
import asyncio
import sys

import httpx
import pytest

from pynext import create_app
from pynext.server import start_native


def _app_dir(tmp_path):
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    (tmp_path / "app" / "quick").mkdir(parents=True)
    (tmp_path / "app" / "quick" / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    return tmp_path


async def _read_response(reader):
    """Read one HTTP/1.1 response from a raw connection."""
    status = await asyncio.wait_for(reader.readline(), timeout=5)
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    length = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
    return status, headers, body


async def _open_idle(host, port):
    """Open a keep-alive connection, complete one request, leave it idle."""
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n")
    await writer.drain()
    status, _, _ = await _read_response(reader)
    assert status.startswith(b"HTTP/1.1 200 "), status
    return reader, writer


def test_total_connection_limit_closes_excess_connections(tmp_path):
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_connections=3, max_connections_per_ip=128)
        host, port = server.address.rsplit(":", 1)
        idle = []
        try:
            for _ in range(3):
                idle.append(await _open_idle(host, port))
            # Fourth connection is over the total cap: the server drops it
            # at accept time, so the client sees EOF instead of a response.
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                first = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first == b"", first
            except (ConnectionResetError, BrokenPipeError):
                # A connection rejected at accept can be reset rather than
                # closed with EOF when unread request bytes are in flight.
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError):
                    pass
            # Closing one idle connection frees its slot for a new client.
            # The server notices the close asynchronously, so retry for a
            # couple of seconds (a loaded runner may schedule the
            # connection task's cleanup late).
            idle[0][1].close()
            await idle[0][1].wait_closed()
            await asyncio.sleep(0.3)
            status = None
            for _ in range(10):
                reader, writer = await asyncio.open_connection(host, int(port))
                try:
                    writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    await writer.drain()
                    status, _, _ = await _read_response(reader)
                    break
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    status = None
                finally:
                    writer.close()
                    await writer.wait_closed()
                await asyncio.sleep(0.2)
            assert status is not None and status.startswith(b"HTTP/1.1 200 "), status
        finally:
            for _, writer in idle:
                writer.close()
            await asyncio.gather(*(w.wait_closed() for _, w in idle), return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_per_ip_connection_limit_closes_excess_from_same_ip(tmp_path):
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_connections=1024, max_connections_per_ip=2)
        host, port = server.address.rsplit(":", 1)
        idle = []
        try:
            for _ in range(2):
                idle.append(await _open_idle(host, port))
            # Third connection from the same IP is over the per-IP cap even
            # though the total cap still has room.
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                first = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first == b"", first
            except (ConnectionResetError, BrokenPipeError):
                # Rejection may reset a socket with unread request bytes.
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError):
                    pass
        finally:
            for _, writer in idle:
                writer.close()
            await asyncio.gather(*(w.wait_closed() for _, w in idle), return_exceptions=True)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_idle_keepalive_connection_closes_after_timeout(tmp_path):
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              header_read_timeout_ms=600)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n")
                await writer.drain()
                status, _, _ = await _read_response(reader)
                assert status.startswith(b"HTTP/1.1 200 "), status
                # Idle past the header-read timeout: the server closes the
                # keep-alive connection, so the next request sees EOF.
                await asyncio.sleep(1.5)
                try:
                    writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                first = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first == b"", first
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_slow_headers_are_closed(tmp_path):
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              header_read_timeout_ms=600)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"GET /api/input HTTP/1.1\r\nHost: localhost\r\n")
                await writer.drain()
                # Headers never complete: the connection is closed instead
                # of holding an accept-loop slot indefinitely.
                first = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first == b"", first
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_body_below_min_receive_rate_gets_408_and_frees_its_slot(tmp_path):
    async def run():
        app = create_app(_app_dir(tmp_path))
        # 1024 B/s with a 2s grace window: a body that stalls after a few
        # bytes is rejected long before the 10s body timeout.
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_min_rate_bytes_per_sec=1024)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Length: 100000\r\n\r\n"
                             b"0123456789")
                await writer.drain()
                first = await asyncio.wait_for(reader.readline(), timeout=8)
                assert first.startswith(b"HTTP/1.1 408 "), first
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                assert b"retry-after: 1\r\n" in headers.lower()
                assert b"connection: close\r\n" in headers.lower()
            finally:
                writer.close()
                await writer.wait_closed()
            # The rejected upload freed its body slot: a fast upload works.
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.post("/api/input", content=b"x" * 1000)).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_connection_limits_do_not_slow_normal_traffic(tmp_path):
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_connections=64, max_connections_per_ip=64,
                              header_read_timeout_ms=10000,
                              body_min_rate_bytes_per_sec=1024)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                responses = await asyncio.gather(
                    *(client.get("/api/input") for _ in range(20)))
                assert all(r.status_code == 200 for r in responses)
                assert (await client.post("/api/input", content=b"x" * 10_000)).status_code == 200
                assert (await client.get("/quick")).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_cli_connection_limit_flags_reach_serve(tmp_path, monkeypatch):
    from pynext import cli

    seen = {}

    async def serve(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--max-connections", "7",
                                      "--max-connections-per-ip", "3",
                                      "--header-read-timeout-ms", "500",
                                      "--body-min-rate-bytes-per-sec", "2048"])
    cli.main()
    assert seen["kwargs"]["max_connections"] == 7
    assert seen["kwargs"]["max_connections_per_ip"] == 3
    assert seen["kwargs"]["header_read_timeout_ms"] == 500
    assert seen["kwargs"]["body_min_rate_bytes_per_sec"] == 2048


@pytest.mark.parametrize("flag,value,message", [
    ("--max-connections", "0", "--max-connections must be an integer >= 1"),
    ("--max-connections", "-1", "--max-connections must be an integer >= 1"),
    ("--max-connections", "nope", "--max-connections must be an integer in [1, 1000000]"),
    ("--max-connections-per-ip", "0", "--max-connections-per-ip must be an integer >= 1"),
    ("--header-read-timeout-ms", "-1", "--header-read-timeout-ms must be an integer >= 0"),
    ("--body-min-rate-bytes-per-sec", "-1",
     "--body-min-rate-bytes-per-sec must be an integer >= 0"),
])
def test_cli_rejects_out_of_range_connection_limits(flag, value, message, monkeypatch, capsys):
    from pynext import cli

    monkeypatch.setattr(sys, "argv", ["pynext", "dev", flag, value])
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_worker_config_carries_connection_limits(tmp_path, monkeypatch):
    from pynext import cli

    seen = {}

    def run_supervisor(config, workers):
        seen["config"] = config
        seen["workers"] = workers

    monkeypatch.setattr(cli, "run_supervisor", run_supervisor)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--workers", "2",
                                      "--max-connections", "9",
                                      "--max-connections-per-ip", "4",
                                      "--header-read-timeout-ms", "700",
                                      "--body-min-rate-bytes-per-sec", "4096"])
    cli.main()
    config = seen["config"]
    assert seen["workers"] == 2
    assert config.max_connections == 9
    assert config.max_connections_per_ip == 4
    assert config.header_read_timeout_ms == 700
    assert config.body_min_rate_bytes_per_sec == 4096
