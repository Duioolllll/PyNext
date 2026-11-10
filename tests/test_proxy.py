"""Trusted-proxy header resolution (A6).

Unit tests for :mod:`pynext.proxy` plus ASGI/native integration tests
proving the resolved client identity a handler actually sees — including
the spoofing cases: multi-hop chains, malicious ``Forwarded`` headers,
and forwarded headers from peers that are not trusted.
"""
import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.proxy import ProxyTrust, parse_trusted_proxies
from pynext.ratelimit import RateLimiter
from pynext.server import start_native


def _h(*pairs):
    return tuple((k.encode("latin-1"), v.encode("latin-1")) for k, v in pairs)


def test_no_trusted_proxies_means_no_forwarded_header_is_interpreted():
    trust = ProxyTrust()
    headers = _h(("X-Forwarded-For", "203.0.113.7"),
                 ("Forwarded", "for=203.0.113.7;proto=https;host=evil.example"),
                 ("X-Forwarded-Proto", "https"),
                 ("X-Forwarded-Host", "evil.example"))
    assert trust.resolve_client_ip("192.0.2.9", headers) == "192.0.2.9"
    assert trust.resolve_scheme("192.0.2.9", headers) is None
    assert trust.resolve_host("192.0.2.9", headers) is None
    assert trust.resolve_client_ip(None, headers) is None


def test_untrusted_peer_forwarded_headers_are_ignored():
    trust = ProxyTrust(["10.0.0.0/8"])
    headers = _h(("X-Forwarded-For", "203.0.113.7"),
                 ("Forwarded", "for=203.0.113.7;proto=https"),
                 ("X-Forwarded-Proto", "https"))
    # 192.0.2.9 is not in the trusted list: peer address wins everywhere.
    assert trust.resolve_client_ip("192.0.2.9", headers) == "192.0.2.9"
    assert trust.resolve_scheme("192.0.2.9", headers) is None
    assert trust.resolve_host("192.0.2.9", headers) is None


def test_xff_multi_hop_stops_at_nearest_untrusted_hop():
    trust = ProxyTrust(["127.0.0.1", "10.0.0.0/8"])
    headers = _h(("X-Forwarded-For", "192.0.2.1, 198.51.100.7, 10.1.2.3"))
    # Right-to-left: 10.1.2.3 trusted, 198.51.100.7 is not -> key is that hop.
    assert trust.resolve_client_ip("127.0.0.1", headers) == "198.51.100.7"
    # A spoofed leftmost prefix cannot move the resolved address.
    spoofed = _h(("X-Forwarded-For", "192.0.2.99, 198.51.100.7, 10.1.2.3"))
    assert trust.resolve_client_ip("127.0.0.1", spoofed) == "198.51.100.7"


def test_xff_malformed_entry_stops_the_walk():
    trust = ProxyTrust(["127.0.0.1", "10.0.0.0/8"])
    headers = _h(("X-Forwarded-For", "192.0.2.1, not-an-ip, 10.1.2.3"))
    assert trust.resolve_client_ip("127.0.0.1", headers) == "10.1.2.3"
    # Split across two headers, same result as one combined header.
    split = _h(("X-Forwarded-For", "192.0.2.1, not-an-ip"),
               ("X-Forwarded-For", "10.1.2.3"))
    assert trust.resolve_client_ip("127.0.0.1", split) == "10.1.2.3"
    # Bracketed IPv6 is accepted.
    assert trust.resolve_client_ip("127.0.0.1",
                                   _h(("X-Forwarded-For", "[2001:0DB8::1]"))) == "2001:db8::1"


def test_forwarded_for_variants():
    trust = ProxyTrust(["127.0.0.1"])
    cases = [
        ("for=203.0.113.7", "203.0.113.7"),
        ("for=203.0.113.7:1234", "203.0.113.7"),
        ('for="[2001:db8::1]"', "2001:db8::1"),
        ('for="[2001:db8::1]:1234"', "2001:db8::1"),
        ("for=2001:db8::1", "2001:db8::1"),
        ('for="203.0.113.7"', "203.0.113.7"),
    ]
    for value, expected in cases:
        assert trust.resolve_client_ip("127.0.0.1", _h(("Forwarded", value))) == expected
    # Obfuscated / unknown values hide the client: the walk stops at the peer.
    for value in ("for=_hidden", "for=_secret", "for=unknown", "for=not-an-ip"):
        assert trust.resolve_client_ip("127.0.0.1", _h(("Forwarded", value))) == "127.0.0.1"


def test_forwarded_multi_element_chain_and_precedence_over_xff():
    trust = ProxyTrust(["127.0.0.1", "10.0.0.0/8"])
    # Elements are client-first; walk from the rightmost trusted hop.
    headers = _h(("Forwarded", "for=192.0.2.1, for=198.51.100.7, for=10.1.2.3"),
                 ("X-Forwarded-For", "203.0.113.99"))
    assert trust.resolve_client_ip("127.0.0.1", headers) == "198.51.100.7"
    # When Forwarded carries no `for`, X-Forwarded-For is the fallback.
    headers = _h(("Forwarded", "proto=https"),
                 ("X-Forwarded-For", "203.0.113.7"))
    assert trust.resolve_client_ip("127.0.0.1", headers) == "203.0.113.7"


def test_forwarded_proto_and_host_need_a_fully_trusted_chain():
    trust = ProxyTrust(["127.0.0.1", "10.0.0.0/8"])
    # Single trusted proxy: the whole chain is consumed, proto/host apply.
    headers = _h(("Forwarded", "for=203.0.113.7;proto=https;host=app.example:8443"))
    assert trust.resolve_scheme("127.0.0.1", headers) == "https"
    assert trust.resolve_host("127.0.0.1", headers) == "app.example:8443"
    # Spoofed left elements plus an untrusted hop: the chain is not fully
    # consumed, so the attacker-supplied proto/host must not leak through.
    spoofed = _h(("Forwarded", "for=203.0.113.7;proto=https;host=evil.example, "
                               "for=198.51.100.7, for=10.1.2.3"))
    assert trust.resolve_scheme("127.0.0.1", spoofed) is None
    assert trust.resolve_host("127.0.0.1", spoofed) is None
    # ... but the resolved client IP is still the nearest untrusted hop.
    assert trust.resolve_client_ip("127.0.0.1", spoofed) == "198.51.100.7"


def test_x_forwarded_proto_and_host_need_a_trusted_peer():
    trust = ProxyTrust(["127.0.0.1"])
    headers = _h(("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", "app.example"))
    assert trust.resolve_scheme("127.0.0.1", headers) == "https"
    assert trust.resolve_host("127.0.0.1", headers) == "app.example"
    assert trust.resolve_scheme("192.0.2.9", headers) is None
    assert trust.resolve_host("192.0.2.9", headers) is None
    # Garbage, non-HTTP schemes, multiple values and invalid hosts are ignored.
    for proto in ("ftp", "https, http", "", "https "):
        if proto.strip() in ("https",):
            continue
        assert trust.resolve_scheme("127.0.0.1",
                                    _h(("X-Forwarded-Proto", proto))) is None
    for host in ("evil.example/path", "evil.example, other.example", "a b", "http://x"):
        assert trust.resolve_host("127.0.0.1",
                                  _h(("X-Forwarded-Host", host))) is None
    # Uppercase scheme and IPv6 host with a port are fine.
    assert trust.resolve_scheme("127.0.0.1", _h(("X-Forwarded-Proto", "HTTPS"))) == "https"
    assert trust.resolve_host("127.0.0.1",
                              _h(("X-Forwarded-Host", "[2001:DB8::1]:8443"))) == "[2001:db8::1]:8443"


def test_ipv4_mapped_ipv6_normalizes_to_ipv4():
    trust = ProxyTrust(["127.0.0.1"])
    assert trust.resolve_client_ip("::ffff:127.0.0.1",
                                   _h(("X-Forwarded-For", "203.0.113.7"))) == "203.0.113.7"
    assert trust.resolve_client_ip("::ffff:192.0.2.9", ()) == "192.0.2.9"
    assert trust.is_trusted("::ffff:10.1.2.3") is False
    assert ProxyTrust(["10.0.0.0/8"]).is_trusted("::ffff:10.1.2.3") is True


def test_parse_trusted_proxies_accepts_cidr_and_networks_and_rejects_bad_entries():
    trust = ProxyTrust(["10.0.0.0/8"])
    assert len(trust.networks) == 1
    assert trust.is_trusted("10.1.2.3") is True
    assert trust.is_trusted("192.0.2.9") is False
    assert trust.is_trusted(None) is False
    assert trust.is_trusted("not-an-ip") is False
    with pytest.raises(ValueError):
        parse_trusted_proxies(["not-an-ip"])
    with pytest.raises(ValueError):
        ProxyTrust(["10.0.0.0/33"])


def test_resolve_applies_defaults_when_nothing_is_forwarded():
    trust = ProxyTrust(["127.0.0.1"])
    info = trust.resolve("127.0.0.1", (), default_scheme="http", default_host="test")
    assert (info.client_ip, info.scheme, info.host) == ("127.0.0.1", "http", "test")
    info = trust.resolve(None, (), default_scheme="http", default_host="test")
    assert (info.client_ip, info.scheme, info.host) == (None, "http", "test")
    info = trust.resolve("127.0.0.1", _h(("Forwarded", "for=203.0.113.7;proto=https")),
                         default_scheme="http", default_host="test")
    assert (info.client_ip, info.scheme, info.host) == ("203.0.113.7", "https", "test")


def test_rate_limiter_key_for_delegates_to_proxy_trust():
    limiter = RateLimiter(10, 60, trusted_proxies=["127.0.0.1"])
    assert limiter.key_for("127.0.0.1",
                           _h(("Forwarded", "for=203.0.113.7;proto=https"))) == "203.0.113.7"
    assert limiter.key_for("192.0.2.9",
                           _h(("Forwarded", "for=203.0.113.7"))) == "192.0.2.9"
    assert limiter.key_for(None, _h(("X-Forwarded-For", "203.0.113.7"))) == "unknown"
    assert len(limiter.trusted_proxies) == 1


def _write_echo_app(tmp_path):
    route = tmp_path / "app" / "api" / "who"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\n'
        'def get(request):\n'
        '    return JSONResponse({"ip": request.state.get("client_ip"),\n'
        '                       "scheme": request.state.get("client_scheme"),\n'
        '                       "host": request.state.get("client_host")})\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_handler_sees_trusted_forwarded_identity(tmp_path, mode):
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path, trusted_proxies=["127.0.0.1"])
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                       trust_env=False)
        try:
            async with client:
                # No forwarded headers: peer identity (127.0.0.1 over the
                # loopback connection, http scheme, test Host header).
                body = (await client.get("/api/who")).json()
                assert body["ip"] == "127.0.0.1"
                assert body["scheme"] == "http"
                # Trusted loopback peer: forwarded identity is honoured.
                body = (await client.get("/api/who", headers={
                    "Forwarded": "for=203.0.113.7;proto=https;host=app.example"})).json()
                assert body == {"ip": "203.0.113.7", "scheme": "https",
                                "host": "app.example"}
                body = (await client.get("/api/who", headers={
                    "X-Forwarded-For": "203.0.113.8",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "other.example"})).json()
                assert body == {"ip": "203.0.113.8", "scheme": "https",
                                "host": "other.example"}
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_handler_ignores_forwarded_headers_without_trust(tmp_path):
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            body = (await client.get("/api/who", headers={
                "X-Forwarded-For": "203.0.113.7",
                "Forwarded": "for=203.0.113.7;proto=https;host=evil.example",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "evil.example"})).json()
            # ASGI test client peer is 127.0.0.1; with no trusted proxies
            # every forwarded header is ignored.
            assert body["ip"] == "127.0.0.1"
            assert body["scheme"] == "http"
            assert body["host"] == "test"
        await app.isr.close()
    asyncio.run(run())


def test_forwarded_header_changes_rate_limit_bucket(tmp_path):
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(1, 60, trusted_proxies=["127.0.0.1"]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            first = await client.get("/api/who", headers={"Forwarded": "for=203.0.113.7"})
            assert first.status_code == 200
            # Same forwarded client, second request: bucket exhausted.
            assert (await client.get("/api/who",
                                     headers={"Forwarded": "for=203.0.113.7"})).status_code == 429
            # A different forwarded client has its own bucket.
            assert (await client.get("/api/who",
                                     headers={"Forwarded": "for=203.0.113.8"})).status_code == 200
            # ... while a spoofed header from an untrusted peer cannot mint
            # new buckets: covered by test_ratelimit's untrusted-peer tests.
        await app.isr.close()
    asyncio.run(run())


async def _read_one_response(reader):
    """Read one HTTP/1.1 response from a raw connection."""
    status = await asyncio.wait_for(reader.readline(), timeout=5)
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    length = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
    return status, headers, body


async def _open_idle_who(host, port, extra_headers=b""):
    """Open a keep-alive connection, complete one request, leave it idle."""
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"GET /api/who HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n"
                 + extra_headers + b"\r\n")
    await writer.drain()
    status, _, _ = await _read_one_response(reader)
    assert status.startswith(b"HTTP/1.1 200 "), status
    return reader, writer


async def _close_all(idle):
    for _, writer in idle:
        writer.close()
    await asyncio.gather(*(w.wait_closed() for _, w in idle), return_exceptions=True)


def test_trusted_proxy_peer_exempt_from_native_per_ip_connection_limit(tmp_path):
    """S-P1-18 (M-8): behind a trusted proxy, all users arrive on the
    proxy's one TCP peer IP. The native per-IP connection cap must not
    count those connections against the proxy IP, or every user behind
    the proxy would share a single 128-connection budget."""
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path, trusted_proxies=["127.0.0.1"])
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_connections=1024, max_connections_per_ip=2)
        host, port = server.address.rsplit(":", 1)
        idle = []
        try:
            # Three idle connections from the trusted loopback proxy,
            # each for a different forwarded client: over the per-IP cap
            # of 2 if the proxy IP itself were counted.
            for client in ("203.0.113.7", "203.0.113.8", "203.0.113.9"):
                idle.append(await _open_idle_who(
                    host, port,
                    f"X-Forwarded-For: {client}\r\n".encode("latin-1")))
            assert len(idle) == 3
        finally:
            await _close_all(idle)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_untrusted_peer_native_per_ip_connection_limit_still_applies(tmp_path):
    """S-P1-18: with no trusted proxies configured, the per-IP cap still
    keys on the TCP peer — the trusted-peer exemption must not leak to
    unlisted peers, or any client could mint unlimited connections."""
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_connections=1024, max_connections_per_ip=2)
        host, port = server.address.rsplit(":", 1)
        idle = []
        try:
            for _ in range(2):
                idle.append(await _open_idle_who(host, port))
            # Third connection from the same untrusted peer is over the
            # per-IP cap: the server drops it at accept time (EOF/reset).
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"GET /api/who HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                first = await asyncio.wait_for(reader.readline(), timeout=5)
                assert first == b"", first
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError):
                    pass
        finally:
            await _close_all(idle)
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_rate_limit_uses_trusted_forwarded_client_ip(tmp_path):
    """S-P1-18 (H-5): on the native server, the per-IP rate-limit bucket
    must key on the trust-resolved client IP, not the proxy IP. With a
    limit of 1 request, a second request from the same forwarded client
    is 429 while a different forwarded client behind the same proxy
    still gets its own bucket."""
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path, trusted_proxies=["127.0.0.1"],
                         rate_limit=RateLimiter(1, 60, trusted_proxies=["127.0.0.1"]))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                         trust_env=False) as client:
                first = await client.get("/api/who",
                                         headers={"X-Forwarded-For": "203.0.113.7"})
                assert first.status_code == 200
                assert first.json()["ip"] == "203.0.113.7"
                # Same forwarded client: bucket exhausted.
                assert (await client.get("/api/who", headers={
                    "X-Forwarded-For": "203.0.113.7"})).status_code == 429
                # Different forwarded client behind the same proxy: own bucket.
                other = await client.get("/api/who",
                                         headers={"X-Forwarded-For": "203.0.113.8"})
                assert other.status_code == 200
                assert other.json()["ip"] == "203.0.113.8"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_rate_limit_ignores_xff_from_untrusted_peer(tmp_path):
    """S-P1-18: without trusted proxies, a spoofed X-Forwarded-For must
    not mint a fresh rate-limit bucket on the native server — every
    request keys on the direct peer, so the second request is 429."""
    _write_echo_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(1, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                         trust_env=False) as client:
                first = await client.get("/api/who",
                                         headers={"X-Forwarded-For": "203.0.113.7"})
                assert first.status_code == 200
                assert first.json()["ip"] == "127.0.0.1"
                spoofed = await client.get("/api/who",
                                           headers={"X-Forwarded-For": "203.0.113.99"})
                assert spoofed.status_code == 429
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_proxy_trust_strings_round_trip_for_native_server():
    """S-P1-18: the strings handed to the native server serialize the
    parsed networks (CIDR kept as CIDR, single host as bare IP)."""
    trust = ProxyTrust(["127.0.0.1", "10.0.0.0/8"])
    assert trust.trusted_proxy_strings() == ("127.0.0.1", "10.0.0.0/8")
    assert ProxyTrust().trusted_proxy_strings() == ()
