import asyncio
import sys

import httpx
import pytest

from pynext import create_app
from pynext.ratelimit import RateLimiter, rate_limited_response
from pynext.server import start_native


def test_limiter_allows_up_to_the_limit_then_denies_with_retry_after():
    limiter = RateLimiter(3, 60)
    assert limiter.allow("a") == (True, None)
    assert limiter.allow("a") == (True, None)
    assert limiter.allow("a") == (True, None)
    allowed, retry_after = limiter.allow("a")
    assert allowed is False
    assert isinstance(retry_after, int) and retry_after >= 1


def test_limiter_refills_over_time():
    limiter = RateLimiter(2, 10)
    assert limiter.allow("a", now=1000.0) == (True, None)
    assert limiter.allow("a", now=1000.0) == (True, None)
    assert limiter.allow("a", now=1000.0)[0] is False
    # After 5s half the window, one token has refilled.
    assert limiter.allow("a", now=1005.0) == (True, None)
    assert limiter.allow("a", now=1005.0)[0] is False
    # After a full window of idleness the bucket is full again.
    assert limiter.allow("a", now=1015.0) == (True, None)
    assert limiter.allow("a", now=1015.0) == (True, None)


def test_limiter_isolates_keys():
    limiter = RateLimiter(1, 60)
    assert limiter.allow("a") == (True, None)
    assert limiter.allow("a")[0] is False
    assert limiter.allow("b") == (True, None)
    assert limiter.allow("b")[0] is False


@pytest.mark.parametrize("limit,window", [(0, 60), (-1, 60), (1.5, 60), ("3", 60),
                                          (3, 0), (3, -1), (3, float("inf")), (3, float("nan"))])
def test_limiter_rejects_invalid_config(limit, window):
    with pytest.raises(ValueError):
        RateLimiter(limit, window)


def test_key_for_ignores_forwarded_headers_without_a_trusted_proxy():
    limiter = RateLimiter(10, 60)
    headers = ((b"x-forwarded-for", b"203.0.113.7"),)
    assert limiter.key_for("127.0.0.1", headers) == "127.0.0.1"
    assert limiter.key_for(None, headers) == "unknown"


def test_key_for_honours_forwarded_headers_from_a_trusted_proxy():
    limiter = RateLimiter(10, 60, trusted_proxies=["127.0.0.1"])
    headers = ((b"x-forwarded-for", b"203.0.113.7, 198.51.100.2"),)
    assert limiter.key_for("127.0.0.1", headers) == "198.51.100.2"
    # An untrusted peer's header is still ignored.
    assert limiter.key_for("192.0.2.9", headers) == "192.0.2.9"
    # A malformed hop stops the walk and falls back to the trusted peer.
    assert limiter.key_for("127.0.0.1", ((b"x-forwarded-for", b"not-an-ip"),)) == "127.0.0.1"


def test_key_for_supports_cidr_trusted_proxies_and_rejects_bad_entries():
    limiter = RateLimiter(10, 60, trusted_proxies=["10.0.0.0/8"])
    assert limiter.key_for("10.1.2.3", ((b"x-forwarded-for", b"203.0.113.7"),)) == "203.0.113.7"
    assert limiter.key_for("192.0.2.9", ((b"x-forwarded-for", b"203.0.113.7"),)) == "192.0.2.9"
    with pytest.raises(ValueError, match="Invalid trusted proxy"):
        RateLimiter(10, 60, trusted_proxies=["not-an-ip"])


def test_forwarded_chain_stops_at_nearest_untrusted_hop():
    limiter = RateLimiter(1, 60, trusted_proxies=["127.0.0.1", "10.0.0.0/8"])
    # The client controls the left side of an appended X-Forwarded-For.
    first = ((b"x-forwarded-for", b"192.0.2.1, 198.51.100.7, 10.1.2.3"),)
    second = ((b"x-forwarded-for", b"192.0.2.2, 198.51.100.7, 10.1.2.3"),)
    assert limiter.key_for("127.0.0.1", first) == "198.51.100.7"
    assert limiter.key_for("127.0.0.1", second) == "198.51.100.7"
    assert limiter.allow(limiter.key_for("127.0.0.1", first))[0]
    assert not limiter.allow(limiter.key_for("127.0.0.1", second))[0]
    split = ((b"x-forwarded-for", b"192.0.2.1, 198.51.100.7"),
             (b"x-forwarded-for", b"10.1.2.3"))
    assert limiter.key_for("127.0.0.1", split) == "198.51.100.7"
    malformed = ((b"x-forwarded-for", b"192.0.2.1, not-an-ip, 10.1.2.3"),)
    assert limiter.key_for("127.0.0.1", malformed) == "10.1.2.3"
    assert limiter.key_for("127.0.0.1", ((b"x-forwarded-for", b"[2001:0DB8::1]"),)) == "2001:db8::1"


def test_limiter_bounds_new_keys_without_evicting_active_buckets(monkeypatch):
    import pynext.ratelimit as ratelimit

    monkeypatch.setattr(ratelimit, "_MAX_BUCKETS", 2)
    limiter = RateLimiter(1, 60)
    assert limiter.allow("a", now=1000)[0]
    assert limiter.allow("b", now=1000)[0]
    assert not limiter.allow("c", now=1000)[0]
    assert len(limiter._buckets) <= 2
    assert not limiter.allow("a", now=1000)[0]
    # Once the oldest buckets have fully refilled, new keys can enter.
    assert limiter.allow("c", now=1061)[0]
    assert len(limiter._buckets) <= 2


def test_rate_limited_response_carries_429_and_retry_after():
    response = rate_limited_response(7)
    assert response.status == 429
    assert (b"retry-after", b"7") in response.headers
    assert response.body == b"Too Many Requests"


def _write_api_app(tmp_path):
    route = tmp_path / "app" / "api" / "ok"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    public = tmp_path / "public"
    public.mkdir()
    (public / "health.txt").write_text("healthy", encoding="utf-8")


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_forwarded_client_cannot_rotate_spoofed_prefix_to_bypass_limit(tmp_path, mode):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(1, 60, trusted_proxies=["127.0.0.1"]))
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5, trust_env=False)
        try:
            async with client:
                first = await client.get("/api/ok", headers={"X-Forwarded-For": "192.0.2.1, 198.51.100.7"})
                second = await client.get("/api/ok", headers={"X-Forwarded-For": "192.0.2.2, 198.51.100.7"})
                assert first.status_code == 200
                assert second.status_code == 429
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_rate_limit_returns_429_with_retry_after(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(3, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                for _ in range(3):
                    response = await client.get("/api/ok")
                    assert response.status_code == 200
                response = await client.get("/api/ok")
                assert response.status_code == 429
                # Bucket empty after 3 rapid requests: one token refills
                # every window/limit = 20 seconds.
                assert response.headers["retry-after"] == "20"
                assert response.text == "Too Many Requests"
                # Static files are served by the Rust layer before the
                # bridge, so they are not rate-limited (documented scope).
                assert (await client.get("/health.txt")).text == "healthy"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_rate_limit_isolates_keys_via_trusted_forwarded_for(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(2, 60, trusted_proxies=["127.0.0.1"]))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                headers_a = {"X-Forwarded-For": "203.0.113.7"}
                headers_b = {"X-Forwarded-For": "203.0.113.8"}
                for _ in range(2):
                    assert (await client.get("/api/ok", headers=headers_a)).status_code == 200
                assert (await client.get("/api/ok", headers=headers_a)).status_code == 429
                # A different forwarded client still has its own bucket.
                assert (await client.get("/api/ok", headers=headers_b)).status_code == 200
                assert (await client.get("/api/ok", headers=headers_b)).status_code == 200
                assert (await client.get("/api/ok", headers=headers_b)).status_code == 429
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_native_rate_limit_ignores_forwarded_for_without_trust(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(2, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                # Distinct X-Forwarded-For values from an untrusted peer all
                # share the peer-IP bucket.
                assert (await client.get("/api/ok", headers={"X-Forwarded-For": "203.0.113.7"})).status_code == 200
                assert (await client.get("/api/ok", headers={"X-Forwarded-For": "203.0.113.8"})).status_code == 200
                assert (await client.get("/api/ok", headers={"X-Forwarded-For": "203.0.113.9"})).status_code == 429
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_asgi_rate_limit_returns_429_with_retry_after(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(2, 60))
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                assert (await client.get("/api/ok")).status_code == 200
                assert (await client.get("/api/ok")).status_code == 200
                response = await client.get("/api/ok")
                assert response.status_code == 429
                # One token refills every window/limit = 30 seconds.
                assert response.headers["retry-after"] == "30"
        finally:
            await app.isr.close()
    asyncio.run(run())


# --- CLI wiring for --rate-limit / --rate-limit-window / --trusted-proxy ---

def test_cli_rejects_invalid_rate_limit_flags(monkeypatch, capsys):
    import sys

    from pynext import cli

    for argv, message in [
        (["pynext", "start", "--rate-limit", "-1"], "--rate-limit must be an integer >= 0"),
        (["pynext", "start", "--rate-limit", "not-a-number"], "--rate-limit must be an integer >= 0"),
        (["pynext", "start", "--rate-limit-window", "0"], "--rate-limit-window must be a number > 0"),
        (["pynext", "start", "--rate-limit-window", "-5"], "--rate-limit-window must be a number > 0"),
        (["pynext", "start", "--rate-limit-window", "not-a-number"], "--rate-limit-window must be a number > 0"),
        (["pynext", "start", "--rate-limit", "10", "--trusted-proxy", "not-an-ip"],
         "Invalid trusted proxy"),
    ]:
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        assert message in capsys.readouterr().err


def test_cli_passes_rate_limit_to_app(tmp_path, monkeypatch):
    import sys

    from pynext import cli

    root = tmp_path.resolve()
    seen = []

    def fake_create_app(path, production, rate_limit=None, **kwargs):
        seen.append(("app", path, production, rate_limit))
        return object()

    monkeypatch.setattr(cli, "create_app", fake_create_app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--rate-limit", "100", "--rate-limit-window", "30",
                                      "--trusted-proxy", "10.0.0.0/8"])
    cli.main()
    assert len(seen) == 2
    _, app_path, production, limiter = seen[0]
    assert (app_path, production) == (root, True)
    assert limiter is not None
    assert limiter.limit == 100
    assert limiter.window_seconds == 30.0
    assert len(limiter.trusted_proxies) == 1
    assert seen[1][0] == "serve"
