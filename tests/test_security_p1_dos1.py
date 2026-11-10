"""P1 DoS defenses (S-P1-11/12/13): fast-path cap, ASGI body timeout, h2c idle timeout."""
import asyncio

import h2.config
import h2.connection
import h2.events
import h2.settings

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
    """An HTTP/2 flow-control barrier holds the sole fast-path permit."""
    size = 128 * 1024
    _project_with_big_public_file(tmp_path, size=size)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              fast_path_max_concurrent=1)
        host, port = server.address.rsplit(":", 1)
        writer = None
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            connection = h2.connection.H2Connection(
                config=h2.config.H2Configuration(header_encoding="utf-8"))
            connection.initiate_connection()
            # A zero stream window prevents DATA transmission on every OS.
            # Receiving the response headers confirms dispatch acquired the
            # permit; only our explicit window update can release the body.
            connection.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 0})
            connection.send_headers(1, [
                (":method", "GET"), (":scheme", "http"),
                (":authority", server.address), (":path", "/big.bin"),
            ], end_stream=True)
            writer.write(connection.data_to_send())
            await writer.drain()
            headers = None
            async with asyncio.timeout(5):
                while headers is None:
                    packet = await reader.read(65536)
                    assert packet, "holder connection closed before response headers"
                    for event in connection.receive_data(packet):
                        assert not isinstance(event, (h2.events.DataReceived, h2.events.StreamEnded)), event
                        if isinstance(event, h2.events.ResponseReceived):
                            headers = dict(event.headers)
                    writer.write(connection.data_to_send())
                    await writer.drain()
            assert headers[":status"] == "200", headers
            assert headers["content-length"] == str(size), headers
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         trust_env=False, timeout=5) as client:
                response = await client.get("/big.bin")
                assert response.status_code == 503
                assert response.headers.get("retry-after") == "1"
                # The separate execution budget remains available.
                assert (await client.get("/")).text == "ok"

                # Release the barrier and drain to END_STREAM before checking
                # reuse. No kernel buffer sizes or scheduling sleeps are used.
                connection.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 65535})
                writer.write(connection.data_to_send())
                await writer.drain()
                received, ended = bytearray(), False
                async with asyncio.timeout(5):
                    while not ended:
                        packet = await reader.read(65536)
                        assert packet, "holder connection closed before END_STREAM"
                        for event in connection.receive_data(packet):
                            if isinstance(event, h2.events.DataReceived):
                                received.extend(event.data)
                                connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                            elif isinstance(event, h2.events.StreamEnded):
                                assert event.stream_id == 1
                                ended = True
                        writer.write(connection.data_to_send())
                        await writer.drain()
                assert received == b"x" * size
                response = await client.get("/big.bin")
                assert response.status_code == 200
                assert response.content == b"x" * size
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
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
