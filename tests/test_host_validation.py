"""Host header validation (S-P1-19, M-9) on the native server.

The Rust layer rejects requests whose Host header is missing or not
on the allowlist with 400, before any handler runs — including the
WebSocket branch, the fast path and the health endpoints. The Python
ASGI transport is out of scope: the ASGI server there owns Host
parsing. Matching follows Django ``ALLOWED_HOSTS``: ``*`` allows any
host, ``.example.com`` allows the domain and all its subdomains,
``*.example.com`` allows subdomains only, anything else is an exact
(case-insensitive) match, and the request Host's port is stripped
before matching.
"""
import asyncio
import base64
import os
import socket
import sys

import h2.config
import h2.connection
import h2.events
import httpx

from pynext import JSONResponse, cli, create_app
from pynext.server import start_native


def make_app(root):
    directory = root / "app" / "api" / "hello"
    directory.mkdir(parents=True)
    (directory / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\n'
        'def get(): return JSONResponse({"ok": True})\n'
    )
    return create_app(root)


def raw_status(address, request, timeout=15):
    """Send one raw request and return its HTTP status code."""
    host, port = address.rsplit(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        return int(response.split(b" ", 2)[1])
    finally:
        sock.close()


def run_with_server(app, check, **server_kwargs):
    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0, **server_kwargs)
        try:
            await check(server)
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_default_allowlist_accepts_loopback(tmp_path):
    async def check(server):
        async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
            response = await client.get("/api/hello")
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            response = await client.get("/healthz")
            assert response.status_code == 200
    run_with_server(make_app(tmp_path), check)


def test_disallowed_host_is_400(tmp_path):
    async def check(server):
        async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
            response = await client.get("/api/hello", headers={"Host": "evil.com"})
            assert response.status_code == 400
            response = await client.get("/api/hello", headers={"Host": "example.com"})
            assert response.status_code == 200
            # The port is stripped before matching, and matching is
            # case-insensitive.
            response = await client.get("/api/hello", headers={"Host": "EXAMPLE.com:8443"})
            assert response.status_code == 200
            # An exact entry does not cover subdomains.
            response = await client.get("/api/hello", headers={"Host": "sub.example.com"})
            assert response.status_code == 400
            # The health endpoint is checked too.
            response = await client.get("/healthz", headers={"Host": "evil.com"})
            assert response.status_code == 400
    run_with_server(make_app(tmp_path), check, allowed_hosts=("example.com",))


def test_dot_pattern_allows_domain_and_subdomains(tmp_path):
    async def check(server):
        async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
            for host in ("example.com", "www.example.com", "deep.sub.example.com"):
                response = await client.get("/api/hello", headers={"Host": host})
                assert response.status_code == 200, host
            response = await client.get("/api/hello", headers={"Host": "example.com.evil.com"})
            assert response.status_code == 400
            response = await client.get("/api/hello", headers={"Host": "evil.com"})
            assert response.status_code == 400
    run_with_server(make_app(tmp_path), check, allowed_hosts=(".example.com",))


def test_star_subdomain_pattern_allows_subdomains_only(tmp_path):
    async def check(server):
        async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
            response = await client.get("/api/hello", headers={"Host": "www.example.com"})
            assert response.status_code == 200
            response = await client.get("/api/hello", headers={"Host": "example.com"})
            assert response.status_code == 400
    run_with_server(make_app(tmp_path), check, allowed_hosts=("*.example.com",))


def test_missing_host_is_400(tmp_path):
    async def check(server):
        # HTTP/1.0 carries no Host requirement in hyper, so a request
        # without Host reaches dispatch — and the check answers 400.
        # Raw socket I/O runs in a thread: a blocking recv here would
        # stall the event loop the Python bridge runs on.
        assert await asyncio.to_thread(
            raw_status, server.address, b"GET /api/hello HTTP/1.0\r\n\r\n") == 400
        assert await asyncio.to_thread(
            raw_status, server.address,
            b"GET /api/hello HTTP/1.0\r\nHost: evil.com\r\n\r\n") == 400
        assert await asyncio.to_thread(
            raw_status, server.address,
            b"GET /api/hello HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n") == 200
    run_with_server(make_app(tmp_path), check)


def test_host_check_runs_before_websocket_upgrade(tmp_path):
    async def check(server):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            "GET /api/hello HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        # A disallowed Host is 400, not a 101 or a 404: the check runs
        # before the upgrade branch.
        assert await asyncio.to_thread(
            raw_status, server.address,
            handshake.format(host="evil.com", key=key).encode()) == 400
        # The same handshake with an allowed Host reaches the upgrade
        # branch, which answers 404 because /api/hello is no WS route.
        assert await asyncio.to_thread(
            raw_status, server.address,
            handshake.format(host="127.0.0.1", key=key).encode()) == 404
    run_with_server(make_app(tmp_path), check)


def test_cli_forwards_config_allowed_hosts_to_serve(tmp_path, monkeypatch):
    # A configured allowlist in pynext.config.py must reach serve();
    # with no list configured, no allowed_hosts kwarg leaks through
    # (the old start call shape is preserved).
    root = tmp_path.resolve()
    (root / "pynext.config.py").write_text(
        "config = {'allowed_hosts': ['Example.COM', '.example.org']}\n")
    seen = {}
    app = object()
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: app)

    async def serve(received_app, *args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root)])
    cli.main()
    assert seen.get("allowed_hosts") == ("example.com", ".example.org")


def test_cli_start_without_allowed_hosts_keeps_old_call_shape(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    seen = {}
    app = object()
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: app)

    async def serve(received_app, *args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root)])
    cli.main()
    assert "allowed_hosts" not in seen


def test_h2_authority_is_validated_as_host(tmp_path):
    """h2c carries :authority instead of a Host header: a disallowed
    authority is 400, an allowed one reaches the handler."""
    async def check(server):
        async def h2_status(authority):
            host, port = server.address.rsplit(":", 1)
            reader, writer = await asyncio.open_connection(host, int(port))
            connection = h2.connection.H2Connection(
                config=h2.config.H2Configuration(header_encoding="utf-8"))
            connection.initiate_connection()
            writer.write(connection.data_to_send())
            await writer.drain()
            connection.send_headers(1, [(":method", "GET"), (":scheme", "http"),
                                        (":authority", authority), (":path", "/api/hello")],
                                    end_stream=True)
            writer.write(connection.data_to_send())
            await writer.drain()
            try:
                async with asyncio.timeout(10):
                    while True:
                        packet = await reader.read(65535)
                        assert packet
                        for event in connection.receive_data(packet):
                            if isinstance(event, h2.events.ResponseReceived):
                                return int(dict(event.headers)[":status"])
                        writer.write(connection.data_to_send())
                        await writer.drain()
            finally:
                writer.close()
        assert await h2_status("evil.com") == 400
        assert await h2_status("example.com") == 200
    run_with_server(make_app(tmp_path), check, allowed_hosts=("example.com",))
