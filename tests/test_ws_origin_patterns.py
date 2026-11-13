"""Wildcard origin patterns for ``websocket_allowed_origins`` (D P2-3).

Next.js Server Actions ``allowedOrigins`` semantics, applied to the one
allowlist PyNext already has: ``*`` matches exactly one DNS label,
``**`` (leftmost label only) matches one or more leading labels, scheme
and effective port must match exactly, and a pattern must keep at least
two literal labels so ``*.com`` can never match whole domains. The
strict defaults are unchanged: the allowlist is empty, and the CSRF
Origin check itself has no allowlist.
"""
import asyncio

import pytest

from pynext import SecurityConfig, create_app
from pynext.proxy import ProxyTrust
from pynext.ws_origin import (
    origin_matches_allowlist,
    origin_pattern_matches,
    parse_origin_pattern,
    websocket_origin_allowed,
)


@pytest.mark.parametrize(("value", "expected"), [
    ("https://example.com", ("https", ("example", "com"), 443)),
    ("HTTP://Example.COM:80", ("http", ("example", "com"), 80)),
    ("https://[2001:db8::1]:8443", ("https", ("2001:db8::1",), 8443)),
    ("https://*.example.com", ("https", ("*", "example", "com"), 443)),
    ("https://**.example.com", ("https", ("**", "example", "com"), 443)),
    ("https://a.*.example.com", ("https", ("a", "*", "example", "com"), 443)),
    ("https://*.EXAMPLE.COM", ("https", ("*", "example", "com"), 443)),
    ("https://*.example.com:8443", ("https", ("*", "example", "com"), 8443)),
    ("https://*.example.com.", ("https", ("*", "example", "com"), 443)),
    ("https://[::1]", ("https", ("::1",), 443)),
])
def test_parse_origin_pattern_valid(value, expected):
    assert parse_origin_pattern(value) == expected


@pytest.mark.parametrize("value", [
    # Whole-domain matching is forbidden: a wildcard directly under a TLD.
    "https://*.com", "https://**.com", "https://*.example",
    # Bare wildcards and wildcards as the whole host.
    "*", "**", "https://*", "https://**",
    # "**" anywhere but the leftmost label.
    "https://a.**.example.com", "https://*.**.example.com",
    # Partial-label wildcards are literal labels, never patterns.
    "https://foo*.example.com", "https://*foo.example.com", "https://***.example.com",
    # Shape errors, same as exact origins.
    "null", "https://example.com/", "https://user@*.example.com",
    "https://*.example.com?", "https://*.example.com#", "https://*.example.com:0",
    "https://*.example.com:65536", "https://*.example.com:", "https://é.example",
    "https://*.example.com\\@other.com", None, 123,
])
def test_parse_origin_pattern_invalid(value):
    assert parse_origin_pattern(value) is None


@pytest.mark.parametrize(("pattern", "origin", "expected"), [
    ("https://*.example.com", ("https", "a.example.com", 443), True),
    ("https://*.example.com", ("https", "a.b.example.com", 443), False),
    ("https://*.example.com", ("https", "example.com", 443), False),
    ("https://*.example.com", ("http", "a.example.com", 80), False),
    ("https://*.example.com", ("https", "a.example.com", 8443), False),
    ("https://*.example.com:8443", ("https", "a.example.com", 8443), True),
    ("https://*.example.com", ("https", "a.example.com.", 443), True),
    ("https://**.example.com", ("https", "a.example.com", 443), True),
    ("https://**.example.com", ("https", "a.b.c.example.com", 443), True),
    ("https://**.example.com", ("https", "example.com", 443), False),
    ("https://**.example.com", ("https", "evil.com", 443), False),
    ("https://a.*.example.com", ("https", "a.b.example.com", 443), True),
    ("https://a.*.example.com", ("https", "a.b.c.example.com", 443), False),
    ("https://a.*.example.com", ("https", "x.b.example.com", 443), False),
    ("https://example.com", ("https", "example.com", 443), True),
    ("https://example.com", ("https", "other.com", 443), False),
    ("https://example.com", ("https", "example.com", 8443), False),
    ("https://[2001:db8::1]:8443", ("https", "2001:db8::1", 8443), True),
    ("https://[2001:db8::1]:8443", ("https", "2001:db8::2", 8443), False),
    # Numeric-looking labels are still DNS labels: "*.192.0.2.1" matches
    # the DNS name x.192.0.2.1, never the IP address 192.0.2.1 itself.
    ("https://*.192.0.2.1", ("https", "x.192.0.2.1", 443), True),
    ("https://*.192.0.2.1", ("https", "192.0.2.1", 443), False),
])
def test_origin_pattern_matches(pattern, origin, expected):
    assert origin_pattern_matches(parse_origin_pattern(pattern), origin) is expected


def test_origin_matches_allowlist_mixes_exact_and_wildcard():
    entries = ("https://exact.example", "https://*.example.com")
    assert origin_matches_allowlist(("https", "exact.example", 443), entries)
    assert origin_matches_allowlist(("https", "a.example.com", 443), entries)
    assert not origin_matches_allowlist(("https", "other.example", 443), entries)
    assert not origin_matches_allowlist(("https", "a.example.com", 443), ("https://*.other.com",))


def test_wildcard_config_accepted_and_bad_patterns_rejected_at_startup():
    config = SecurityConfig(websocket_allowed_origins=("https://*.example.com", "https://**.other.example"))
    assert config.websocket_allowed_origins == ("https://*.example.com", "https://**.other.example")
    for bad in ("https://*.com", "https://a.**.example.com", "https://foo*.example.com"):
        with pytest.raises(ValueError, match="valid HTTP"):
            SecurityConfig(websocket_allowed_origins=(bad,))


def test_websocket_origin_allowed_with_wildcards():
    config = SecurityConfig(websocket_allowed_origins=("https://*.example.com", "https://**.other.example"))
    check = lambda headers: websocket_origin_allowed(headers, "127.0.0.1", "ws", ProxyTrust(), config)
    assert check([(b"host", b"app.example"), (b"origin", b"https://a.example.com")])
    assert not check([(b"host", b"app.example"), (b"origin", b"https://a.b.example.com")])
    assert not check([(b"host", b"app.example"), (b"origin", b"https://example.com")])
    assert check([(b"host", b"app.example"), (b"origin", b"https://a.other.example")])
    assert check([(b"host", b"app.example"), (b"origin", b"https://a.b.other.example")])
    assert not check([(b"host", b"app.example"), (b"origin", b"https://other.example")])
    assert not check([(b"host", b"app.example"), (b"origin", b"http://a.example.com")])
    assert not check([(b"host", b"app.example"), (b"origin", b"https://a.example.com:8443")])
    assert not check([(b"host", b"app.example"), (b"origin", b"https://evil.com")])


def test_asgi_wildcard_allowlist_before_handler(tmp_path):
    async def run():
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
        config = SecurityConfig(websocket_allowed_origins=("https://*.client.example",))
        app = create_app(tmp_path, security=config)
        module = app._module("app/ws/route.py")
        try:
            async def attempt(origin):
                sent = []
                async def receive():
                    return {"type": "websocket.connect"}
                async def send(message):
                    sent.append(message)
                scope = {"type": "websocket", "path": "/ws", "scheme": "ws",
                         "headers": [(b"host", b"app.example"), (b"origin", origin)],
                         "client": ("127.0.0.1", 1234), "query_string": b""}
                before = len(module.calls)
                await app(scope, receive, send)
                accepted = sent[0]["type"] == "websocket.accept"
                assert len(module.calls) == before + accepted
                return accepted
            assert await attempt(b"https://one.client.example")
            assert not await attempt(b"https://one.two.client.example")
            assert not await attempt(b"https://client.example")
            assert not await attempt(b"https://evil.example")
        finally:
            await app.isr.close()
    asyncio.run(run())
