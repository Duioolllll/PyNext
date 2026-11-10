"""P1 DoS defenses (S-P1-11/12/13): fast-path cap, ASGI body timeout, h2c idle timeout."""
import asyncio
import socket

import httpx
import pytest

from pynext import create_app
from pynext.server import start_native


def _project_with_big_public_file(root, size=12 * 1024 * 1024):
    (root / "app").mkdir(parents=True)
    (root / "app" / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("ok")\n', encoding="utf-8")
    public = root / "public"
    public.mkdir()
    (public / "big.bin").write_bytes(b"x" * size)


def test_fast_path_cap_sheds_when_budget_exhausted(tmp_path):
    """S-P1-11 (M-1): with fast_path_max_concurrent=1, a slow reader
    holding the only fast-path permit makes the next static request
    503 (with Retry-After); once the holder goes away the same request
    succeeds again."""
    _project_with_big_public_file(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              fast_path_max_concurrent=1)
        host, port = server.address.rsplit(":", 1)
        reader, writer = None, None
        try:
            # Holder: request the 12 MiB asset and stop reading after the
            # headers, so hyper cannot flush the body and the fast-path
            # permit stays held. 12 MiB deliberately exceeds the loopback
            # send buffer (tcp_wmem max 4 MiB here): a smaller asset would
            # fit in the kernel buffer, hyper would finish the body, and
            # the permit would be released before the next request.
            reader, writer = await asyncio.open_connection(host, int(port))
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            writer.write(b"GET /big.bin HTTP/1.1\r\nHost: localhost\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
            headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=5)
            assert b"200" in headers.split(b"\r\n", 1)[0], headers
            assert b"content-length: 12582912" in headers.lower(), headers
            # The permit is held while the body is being flushed; give the
            # server a moment to fill the socket buffer and stall.
            await asyncio.sleep(0.5)
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         timeout=5) as client:
                deadline = asyncio.get_running_loop().time() + 5
                seen_503 = False
                while asyncio.get_running_loop().time() < deadline:
                    response = await client.get("/big.bin")
                    # Drain the shed response so the connection is reusable.
                    await response.aread()
                    if response.status_code == 503:
                        seen_503 = True
                        assert response.headers.get("retry-after") == "1"
                        break
                    await asyncio.sleep(0.1)
                assert seen_503, "fast-path cap never shed while permit held"
            # Release the holder: the budget frees and the asset serves.
            writer.close()
            await writer.wait_closed()
            writer = None
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         timeout=5) as client:
                response = await client.get("/big.bin")
                assert response.status_code == 200
                assert len(response.content) == 12 * 1024 * 1024
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def _h2_frame_header(payload, frame_type, flags=0, stream_id=0):
    return (len(payload).to_bytes(3, "big") + bytes([frame_type, flags])
            + stream_id.to_bytes(4, "big"))


async def _read_h2_frame(reader):
    header = await reader.readexactly(9)
    length = int.from_bytes(header[0:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return header[3], header[4], int.from_bytes(header[5:9], "big") & 0x7FFF_FFFF, payload


async def _read_until_eof(reader, timeout=5):
    async with asyncio.timeout(timeout):
        while await reader.read(65536):
            pass


def test_h2c_idle_connection_is_closed(tmp_path):
    """S-P1-13 (M-3): an idle h2c connection is closed after the header
    read timeout, freeing its connection-budget slot."""
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "app" / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("ok")\n', encoding="utf-8")

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              header_read_timeout_ms=1000)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
                    + _h2_frame_header(b"", 0x4))  # empty SETTINGS
                await writer.drain()
                frame_type, _, _, _ = await asyncio.wait_for(
                    _read_h2_frame(reader), timeout=5)
                assert frame_type == 0x4  # server SETTINGS
                # Idle: no frames for longer than the timeout.
                await _read_until_eof(reader, timeout=5)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_h2c_active_connection_survives_idle_timeout(tmp_path):
    """S-P1-13 (M-3): an h2c connection that keeps exchanging frames
    (PINGs here) is not cut by the idle timeout; it is only closed
    once it actually goes idle."""
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "app" / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("ok")\n', encoding="utf-8")

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              header_read_timeout_ms=1000)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
                    + _h2_frame_header(b"", 0x4))
                await writer.drain()
                frame_type, _, _, _ = await asyncio.wait_for(
                    _read_h2_frame(reader), timeout=5)
                assert frame_type == 0x4

                async def ping_and_wait_ack(payload=b"12345678"):
                    writer.write(_h2_frame_header(payload, 0x6) + payload)
                    await writer.drain()
                    async with asyncio.timeout(2):
                        while True:
                            ftype, flags, _, data = await _read_h2_frame(reader)
                            if ftype == 0x6 and flags & 0x1:
                                assert data == payload
                                return
                            # SETTINGS ACKs / WINDOW_UPDATEs are skipped.

                # Ping every 400 ms for 2.5 s: well past the 1 s idle
                # timeout, the connection must still answer.
                deadline = asyncio.get_running_loop().time() + 2.5
                while asyncio.get_running_loop().time() < deadline:
                    await ping_and_wait_ack()
                    await asyncio.sleep(0.4)
                # Now go idle: the connection must be closed.
                await _read_until_eof(reader, timeout=5)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def _asgi_project(root):
    (root / "app" / "input").mkdir(parents=True)
    (root / "app" / "input" / "route.py").write_text(
        'from pynext import Response, route\n@route("POST")\n'
        'async def post(request): return Response("ok")\n', encoding="utf-8")


async def _call_asgi(app, receive):
    scope = {"type": "http", "path": "/input", "method": "POST", "scheme": "http",
             "client": ("127.0.0.1", 1234),
             "headers": [(b"host", b"localhost")]}
    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent):
    assert sent and sent[0]["type"] == "http.response.start", sent
    return sent[0]["status"]


def test_asgi_slow_body_times_out(tmp_path, monkeypatch):
    """S-P1-12 (M-2): an ASGI body that never arrives gets 408 instead
    of holding the connection forever (native body-timeout parity)."""
    _asgi_project(tmp_path)
    monkeypatch.setattr("pynext.app.ASGI_BODY_TIMEOUT_SECS", 0.5)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_GRACE_SECS", 0.1)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_CHECK_INTERVAL_SECS", 0.05)

    async def receive():
        await asyncio.sleep(3600)

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asyncio.wait_for(_call_asgi(app, receive), timeout=5)
            assert _status(sent) == 408
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_asgi_stalled_body_times_out_on_min_rate(tmp_path, monkeypatch):
    """S-P1-12 (M-2): a body that stalls after one byte is cut by the
    minimum receive rate once the grace window passes (native parity)."""
    _asgi_project(tmp_path)
    monkeypatch.setattr("pynext.app.ASGI_BODY_TIMEOUT_SECS", 5.0)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_BYTES_PER_SEC", 1024)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_GRACE_SECS", 0.1)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_CHECK_INTERVAL_SECS", 0.05)

    state = {"calls": 0}

    async def receive():
        state["calls"] += 1
        if state["calls"] == 1:
            return {"type": "http.request", "body": b"x", "more_body": True}
        await asyncio.sleep(3600)

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asyncio.wait_for(_call_asgi(app, receive), timeout=5)
            assert _status(sent) == 408
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_asgi_steady_body_times_out_on_whole_body_timeout(tmp_path, monkeypatch):
    """S-P1-12 (M-2): a body that keeps arriving fast enough but never
    finishes is cut by the whole-body timeout (native parity)."""
    _asgi_project(tmp_path)
    monkeypatch.setattr("pynext.app.ASGI_BODY_TIMEOUT_SECS", 0.5)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_BYTES_PER_SEC", 1024)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_GRACE_SECS", 10.0)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_CHECK_INTERVAL_SECS", 0.05)

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"x" * 2048, "more_body": True}

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asyncio.wait_for(_call_asgi(app, receive), timeout=5)
            assert _status(sent) == 408
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_asgi_fast_body_still_succeeds(tmp_path, monkeypatch):
    """S-P1-12 (M-2): a normal fast body is unaffected by the timeout."""
    _asgi_project(tmp_path)
    monkeypatch.setattr("pynext.app.ASGI_BODY_TIMEOUT_SECS", 0.5)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_GRACE_SECS", 0.1)
    monkeypatch.setattr("pynext.app.ASGI_BODY_MIN_RATE_CHECK_INTERVAL_SECS", 0.05)

    async def receive():
        return {"type": "http.request", "body": b"hello", "more_body": False}

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asyncio.wait_for(_call_asgi(app, receive), timeout=5)
            assert _status(sent) == 200
        finally:
            await app.isr.close()

    asyncio.run(run())
