"""WebSocket origin policy, including the actual native HTTP upgrade."""
import asyncio
import base64
import os
import socket
import time

import pytest

from pynext import SecurityConfig, create_app
from pynext.proxy import ProxyTrust
from pynext.server import start_native
from pynext.ws_origin import canonical_origin, websocket_origin_allowed


@pytest.mark.parametrize(("value", "expected"), [
    ("HTTP://Example.COM:80", ("http", "example.com", 80)),
    ("https://example.com", ("https", "example.com", 443)),
    ("https://[2001:db8::1]:8443", ("https", "2001:db8::1", 8443)),
    ("null", None), ("*", None), ("https://example.com/", None),
    ("https://user@example.com", None), ("https://example.com?", None),
    ("https://example.com#", None), ("https://example.com:0", None),
    ("https://example.com:", None), ("https://example.com:65536", None),
    ("https://example.com\\@other.com", None), ("https://example.com\n", None),
    ("https://é.example", None), ("https://[fe80::1%25eth0]", None),
    ("https://[::1]attacker", None),
    ("https://example.com https://other.com", None),
])
def test_canonical_origin(value, expected):
    assert canonical_origin(value) == expected


@pytest.mark.parametrize("origins", [("*",), ("null",), ("https://example.com/",),
                                     ("https://user:secret@example.com",), (None,)])
def test_invalid_allowlist_is_rejected(origins):
    with pytest.raises(ValueError, match="valid HTTP"):
        SecurityConfig(websocket_allowed_origins=origins)


def test_origin_config_requires_explicit_valid_values():
    with pytest.raises(TypeError):
        SecurityConfig(websocket_allowed_origins="https://example.com")
    with pytest.raises(TypeError):
        SecurityConfig(websocket_allow_missing_origin=1)
    config = SecurityConfig(None, "pynext_csrf", "X-CSRF-Token", "csrf_token", True, "Lax",
                            (), ("GET",), ("Content-Type",), False, 600, True)
    assert config.csp_nonce_enabled is True
    assert not config.websocket_allowed_origins and not config.websocket_allow_missing_origin


def test_origin_policy_normalization_and_proxy_trust():
    config = SecurityConfig(websocket_allowed_origins=("https://CLIENT.example:443",))
    direct = ProxyTrust()
    trusted = ProxyTrust(["127.0.0.1"])
    check = lambda headers, trust=direct: websocket_origin_allowed(headers, "127.0.0.1", "ws", trust, config)
    assert check([(b"host", b"EXAMPLE.com:80"), (b"origin", b"http://example.com")])
    assert check([(b"origin", b"https://client.example")])
    assert not check([(b"host", b"example.com"), (b"host", b"other.example"),
                      (b"origin", b"https://client.example")])
    config.websocket_allowed_origins = ("https://new.example",)
    assert not check([(b"origin", b"https://client.example")])
    assert check([(b"origin", b"https://new.example")])
    assert not check([(b"host", b"example.com"), (b"origin", b"https://example.com")])
    assert not check([(b"host", b"example.com"), (b"origin", b"http://example.com"),
                      (b"origin", b"http://example.com")])
    assert not check([(b"host", b"example.com"), (b"host", b"example.com"),
                      (b"origin", b"http://example.com")])
    assert not check([(b"host", b"example.com"), (b"origin", b"null")])
    assert not check([(b"host", b"example.com"), (b"origin", 123)])
    forwarded = [(b"host", b"internal"), (b"origin", b"https://public.example"),
                 (b"x-forwarded-host", b"public.example"), (b"x-forwarded-proto", b"https")]
    assert not check(forwarded)
    assert check(forwarded, trusted)
    assert not check(forwarded + [(b"x-forwarded-host", b"attacker.example")], trusted)


def _app(tmp_path, *, security=None, trusted_proxies=()):
    directory = tmp_path / "app" / "ws"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "route.py").write_text(
        "from pynext import websocket\n"
        "calls = []\n"
        "@websocket\n"
        "async def handler(websocket):\n"
        "    calls.append(True)\n"
        "    await websocket.accept()\n"
        "    await websocket.close()\n"
    )
    return create_app(tmp_path, security=security, trusted_proxies=trusted_proxies)


def test_asgi_policy_rejects_before_handler_and_honors_opt_in(tmp_path):
    async def run():
        config = SecurityConfig(websocket_allowed_origins=("https://client.example",),
                                websocket_allow_missing_origin=True)
        app = _app(tmp_path, security=config, trusted_proxies=("127.0.0.1",))
        module = app._module("app/ws/route.py")
        async def attempt(headers, scheme="ws", peer="127.0.0.1"):
            sent = []
            async def receive():
                return {"type": "websocket.connect"}
            async def send(message):
                sent.append(message)
            scope = {"type": "websocket", "path": "/ws", "scheme": scheme,
                     "headers": headers, "client": (peer, 1234), "query_string": b""}
            before = len(module.calls)
            await app(scope, receive, send)
            accepted = sent[0]["type"] == "websocket.accept"
            assert len(module.calls) == before + accepted
            return accepted
        try:
            assert await attempt([(b"host", b"app.example"), (b"origin", b"http://app.example")])
            assert await attempt([(b"origin", b"https://client.example")])
            assert await attempt([(b"host", b"app.example")])
            app.security.websocket_allow_missing_origin = False
            assert not await attempt([(b"host", b"app.example")])
            assert not await attempt([(b"host", b"app.example"), (b"origin", b"null")])
            assert not await attempt([(b"host", b"app.example"), (b"origin", b"http://evil.example")])
            forwarded = [(b"host", b"internal"), (b"origin", b"https://public.example"),
                         (b"x-forwarded-host", b"public.example"), (b"x-forwarded-proto", b"https")]
            assert await attempt(forwarded)
            assert not await attempt(forwarded, peer="192.0.2.1")
            assert await attempt([(b"host", b"app.example"), (b"origin", b"https://app.example")], "wss")
        finally:
            await app.isr.close()
    asyncio.run(run())


def _handshake(address, headers):
    host, port = address.rsplit(":", 1)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = ["GET /ws HTTP/1.1", f"Host: {address}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", *headers, "", ""]
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        conn.settimeout(5)
        conn.sendall("\r\n".join(lines).encode("ascii"))
        result = b""
        while b"\r\n\r\n" not in result:
            chunk = conn.recv(4096)
            assert chunk
            result += chunk
        return int(result.split(b" ", 2)[1])


def test_axum_rejects_before_upgrade_and_honors_proxy_policy(tmp_path):
    async def run():
        config = SecurityConfig(websocket_allowed_origins=("https://client.example",))
        app = _app(tmp_path, security=config, trusted_proxies=("127.0.0.1",))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        async def attempt(*headers):
            return await asyncio.to_thread(_handshake, server.address, headers)
        try:
            assert await attempt(f"Origin: http://{server.address}") == 101
            assert await attempt("Origin: https://client.example") == 101
            assert await attempt() == 403
            assert await attempt("Origin: null") == 403
            assert await attempt("Origin: http://evil.example") == 403
            assert await attempt(f"Origin: http://{server.address}",
                                 f"Origin: http://{server.address}") == 403
            assert await attempt("Origin: https://public.example", "X-Forwarded-Host: public.example",
                                 "X-Forwarded-Proto: https") == 101
            assert await attempt("Origin: https://evil.example", "X-Forwarded-Host: public.example",
                                 "X-Forwarded-Proto: https") == 403
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
        untrusted = _app(tmp_path, security=SecurityConfig(websocket_allow_missing_origin=True))
        server = start_native(untrusted, asyncio.get_running_loop(), port=0)
        try:
            assert await asyncio.to_thread(_handshake, server.address, []) == 101
            assert await asyncio.to_thread(_handshake, server.address, ["Origin: null"]) == 403
            assert await asyncio.to_thread(_handshake, server.address,
                                           ["Origin: https://evil.example", "X-Forwarded-Host: evil.example",
                                            "X-Forwarded-Proto: https"]) == 403
        finally:
            await asyncio.to_thread(server.stop)
            await untrusted.isr.close()
    asyncio.run(run())


def test_axum_origin_callback_errors_fail_closed(tmp_path, monkeypatch):
    import pynext.server
    def fail(*args):
        raise RuntimeError("validator unavailable")
    monkeypatch.setattr(pynext.server, "websocket_origin_allowed", fail)
    async def run():
        app = _app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            assert await asyncio.to_thread(_handshake, server.address,
                                           [f"Origin: http://{server.address}"]) == 503
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_axum_origin_callback_timeout_fails_closed(tmp_path, monkeypatch):
    import pynext.server
    def slow(*args):
        time.sleep(2.4)
        return True
    monkeypatch.setattr(pynext.server, "websocket_origin_allowed", slow)
    async def run():
        app = _app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            assert await asyncio.to_thread(_handshake, server.address,
                                           [f"Origin: http://{server.address}"]) == 503
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
