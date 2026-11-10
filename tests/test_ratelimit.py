import asyncio
import sys
import threading
import time

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
    assert limiter.key_for("127.0.0.1", ((b"x-forwarded-for", b"[2001:0DB8::1]"),)) == "2001:db8::/64"


def test_limiter_admits_new_keys_by_evicting_the_stalest_bucket(monkeypatch):
    import pynext.ratelimit as ratelimit

    monkeypatch.setattr(ratelimit, "_MAX_BUCKETS", 2)
    # A generous new-key window isolates table eviction from the global
    # new-key window in this test.
    limiter = RateLimiter(1, 60, max_new_keys_per_window=100)
    assert limiter.allow("a", now=1000)[0]
    assert limiter.allow("b", now=1000)[0]
    # Table full, but a new key is admitted by evicting the stalest
    # bucket ("a", least recently used) instead of getting 429.
    assert limiter.allow("c", now=1000)[0]
    assert len(limiter._buckets) <= 2
    assert limiter.stats()["evicted"] == 1
    # The evicted owner gets a fresh full bucket on its next request.
    assert limiter.allow("a", now=1000)[0]
    assert len(limiter._buckets) <= 2


def test_limiter_prefers_evicting_a_fully_refilled_bucket(monkeypatch):
    import pynext.ratelimit as ratelimit

    monkeypatch.setattr(ratelimit, "_MAX_BUCKETS", 2)
    limiter = RateLimiter(1, 60, max_new_keys_per_window=100)
    assert limiter.allow("a", now=1000)[0]
    assert limiter.allow("b", now=1000)[0]
    # After a full window both buckets have refilled; admitting "c"
    # evicts one of them (the stalest, "a") and "b" keeps its bucket.
    assert limiter.allow("c", now=1061)[0]
    assert "b" in limiter._buckets
    assert "a" not in limiter._buckets


def test_global_new_key_window_denies_only_brand_new_keys():
    # At most 2 new keys per 60s window; existing keys are unaffected.
    limiter = RateLimiter(10, 60, max_new_keys_per_window=2)
    assert limiter.allow("a", now=1000.0)[0]
    assert limiter.allow("b", now=1000.0)[0]
    allowed, retry_after = limiter.allow("c", now=1000.0)
    assert allowed is False
    assert isinstance(retry_after, int) and retry_after >= 1
    # An existing key still has tokens and is not blocked by the window.
    assert limiter.allow("a", now=1000.0)[0]
    # Once the window slides, the new key is admitted.
    assert limiter.allow("c", now=1061.0)[0]


def test_evicted_existing_key_is_not_denied_by_new_key_window():
    # Review #4 #3: at most 2 buckets and 3 new keys per 60s window.
    # "c" is admitted by evicting "a"; when "a" returns within the
    # window it is an existing key, not a brand-new one, so the full
    # new-key window must not deny it with 429.
    limiter = RateLimiter(10, 60, max_buckets=2, max_new_keys_per_window=3)
    assert limiter.allow("a", now=1000.0)[0]
    assert limiter.allow("b", now=1000.0)[0]
    assert limiter.allow("c", now=1000.0)[0]
    # Only 2 buckets: admitting "c" evicted the stalest one, "a".
    # (stats() is not used here: it prunes with real monotonic time,
    # which would wipe these fake-now admissions.)
    assert "a" not in limiter._buckets and limiter._evicted == 1
    assert limiter.allow("a", now=1000.0)[0]
    # A genuinely brand-new key is still denied while the window is full.
    allowed, retry_after = limiter.allow("d", now=1000.0)
    assert allowed is False
    assert isinstance(retry_after, int) and retry_after >= 1
    # And an existing key that kept its bucket is unaffected either way.
    assert limiter.allow("b", now=1000.0)[0]


def test_limiter_rejects_invalid_new_key_and_bucket_bounds():
    with pytest.raises(ValueError):
        RateLimiter(10, 60, max_buckets=0)
    with pytest.raises(ValueError):
        RateLimiter(10, 60, max_new_keys_per_window=0)
    with pytest.raises(ValueError):
        RateLimiter(10, 60, exempt_paths=["relative"])
    with pytest.raises(ValueError):
        RateLimiter(10, 60, exempt_paths=[123])


def test_key_for_groups_ipv6_by_configured_prefix():
    # Review #4 #3 follow-up (/48 residual risk): the grouping prefix is
    # configurable. Default 64 keeps the old behaviour; 48 groups a
    # whole /48 into one bucket, which stops a /48 holder minting a fresh
    # bucket per /64 (at the cost of throttling every host in that /48
    # together). IPv4 and IPv4-mapped addresses are unaffected.
    default = RateLimiter(10, 60)
    assert default.key_for("2001:db8:0:1::1") == "2001:db8:0:1::/64"
    assert default.key_for("2001:db8:0:1::1") != default.key_for("2001:db8:0:2::1")
    wide = RateLimiter(10, 60, ipv6_prefix_len=48)
    assert wide.key_for("2001:db8:0:1::1") == "2001:db8::/48"
    assert wide.key_for("2001:db8:0:1::1") == wide.key_for("2001:db8:0:2::1")
    assert wide.key_for("2001:db8:1::1") == "2001:db8:1::/48"
    assert wide.key_for("2001:db8:0:1::1") != wide.key_for("2001:db8:1::1")
    assert wide.key_for("203.0.113.7") == "203.0.113.7"
    assert wide.key_for("::ffff:203.0.113.7") == "203.0.113.7"
    narrow = RateLimiter(10, 60, ipv6_prefix_len=128)
    assert narrow.key_for("2001:db8:0:1::1") == "2001:db8:0:1::1/128"
    assert narrow.key_for("2001:db8:0:1::1") != narrow.key_for("2001:db8:0:1::2")
    for bad in (-1, 129, 64.5, True, "64"):
        with pytest.raises(ValueError, match="ipv6_prefix_len must be an integer between 0 and 128"):
            RateLimiter(10, 60, ipv6_prefix_len=bad)


def test_key_for_groups_ipv6_by_64_prefix():
    limiter = RateLimiter(10, 60)
    assert limiter.key_for("2001:db8:0:1::1") == "2001:db8:0:1::/64"
    assert limiter.key_for("2001:db8:0:1::2") == "2001:db8:0:1::/64"
    assert limiter.key_for("2001:db8:0:2::1") == "2001:db8:0:2::/64"
    assert limiter.key_for("2001:db8:0:1::1") == limiter.key_for("2001:db8:0:1:ffff::ffff")
    # IPv4-mapped IPv6 still keys by its IPv4 address.
    assert limiter.key_for("::ffff:203.0.113.7") == "203.0.113.7"
    # IPv4 unchanged.
    assert limiter.key_for("203.0.113.7") == "203.0.113.7"
    # Forwarded IPv6 addresses are grouped too.
    trusted = RateLimiter(10, 60, trusted_proxies=["127.0.0.1"])
    headers = ((b"x-forwarded-for", b"2001:db8:0:1::9"),)
    assert trusted.key_for("127.0.0.1", headers) == "2001:db8:0:1::/64"


def test_ipv6_addresses_in_one_prefix_share_a_bucket():
    limiter = RateLimiter(1, 60)
    assert limiter.allow(limiter.key_for("2001:db8:0:1::1"))[0]
    assert not limiter.allow(limiter.key_for("2001:db8:0:1::2"))[0]
    assert limiter.allow(limiter.key_for("2001:db8:0:2::1"))[0]


def test_legitimate_user_survives_100k_ipv6_address_flood():
    """A3 acceptance: 100k distinct IPv6 addresses, legit user keeps working.

    Target: a legitimate user with an established bucket succeeds on
    every request (100%, target >= 95%) while 100,000 distinct IPv6
    addresses — each in its own /64 — hit the limiter in the same window.
    """
    limiter = RateLimiter(100, 60)
    legit = limiter.key_for("203.0.113.7")
    assert limiter.allow(legit, now=1000.0)[0]
    successes = 1
    attempts = 1
    for i in range(100_000):
        key = limiter.key_for(f"2001:db8:{i >> 16:x}:{i & 0xFFFF:x}::1")
        limiter.allow(key, now=1000.0)
        if i % 10_000 == 9_999:
            attempts += 1
            if limiter.allow(legit, now=1000.0)[0]:
                successes += 1
    assert successes == attempts
    assert limiter.stats()["buckets"] <= 10_000


def test_ipv6_flood_within_one_prefix_creates_one_bucket():
    # 100k addresses rotating inside a single /64 share one bucket.
    limiter = RateLimiter(10, 60)
    for i in range(100_000):
        key = limiter.key_for(f"2001:db8:0:1:{i >> 16:x}:{(i >> 8) & 0xFF:x}:{i & 0xFF:x}:1")
        limiter.allow(key, now=1000.0)
    assert limiter.stats()["buckets"] == 1


def test_ipv6_48_flood_cannot_fill_new_key_window():
    # S-P1-26 (M-17) mitigation: a /48 holder minting 10,000 distinct
    # /64s inside one /48 collapses to a single bucket and a single
    # new-key admission at ipv6_prefix_len=48, so a genuinely brand-new
    # user is still admitted instead of getting 429.
    # ``now`` is anchored at the real monotonic clock: stats() prunes
    # new-key admissions against time.monotonic(), so a fixed fake
    # timestamp would look expired to it.
    now = time.monotonic()
    limiter = RateLimiter(100, 60, ipv6_prefix_len=48)
    for i in range(10_000):
        key = limiter.key_for(f"2001:db8:0:{i:x}::1")
        limiter.allow(key, now=now)
    stats = limiter.stats()
    assert stats["buckets"] == 1
    assert stats["new_keys_in_window"] == 1
    legit = limiter.key_for("2001:db8:1::1")
    assert limiter.allow(legit, now=now) == (True, None)


def test_ipv6_64_flood_fills_new_key_window_and_denies_new_key():
    # Control for the test above: at the default /64 the same flood —
    # 10,000 distinct /64s inside one /48 — fills the new-key window,
    # so a genuinely brand-new key is denied with 429 until the window
    # slides. This is the M-17 disruption the /48 grouping mitigates.
    now = time.monotonic()
    limiter = RateLimiter(100, 60)
    for i in range(10_000):
        key = limiter.key_for(f"2001:db8:0:{i:x}::1")
        limiter.allow(key, now=now)
    assert limiter.stats()["new_keys_in_window"] == 10_000
    legit = limiter.key_for("2001:db8:1::1")
    allowed, retry_after = limiter.allow(legit, now=now)
    assert allowed is False
    assert retry_after == 60


def test_is_exempt_matches_exact_paths_and_subpaths():
    limiter = RateLimiter(10, 60, exempt_paths=("/healthz", "/static/"))
    assert limiter.is_exempt("/healthz")
    assert limiter.is_exempt("/healthz/ready")
    assert limiter.is_exempt("/healthz?verbose=1")
    assert limiter.is_exempt("/static/app.js")
    assert limiter.is_exempt("/static/")
    assert limiter.is_exempt("/static")
    assert not limiter.is_exempt("/healthz2")
    assert not limiter.is_exempt("/api/ok")
    assert not RateLimiter(10, 60).is_exempt("/healthz")


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


def _write_exempt_app(tmp_path):
    _write_api_app(tmp_path)
    route = tmp_path / "app" / "api" / "healthz"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_exempt_paths_skip_rate_limit(tmp_path, mode):
    _write_exempt_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(1, 60, exempt_paths=("/api/healthz", "/health.txt")))
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5, trust_env=False)
        try:
            async with client:
                assert (await client.get("/api/ok")).status_code == 200
                assert (await client.get("/api/ok")).status_code == 429
                # Exempt paths still answer while the bucket is exhausted.
                assert (await client.get("/api/healthz")).status_code == 200
                assert (await client.get("/api/healthz")).status_code == 200
                assert (await client.get("/health.txt")).status_code == 200
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_encoded_delimiter_does_not_inherit_exemption(tmp_path):
    """An encoded delimiter stays one segment for exemption matching.

    ``/free%2Fblocked`` decodes to ``/free/blocked``, which would fall
    under the ``/free`` exemption, but routing serves it as the single
    segment ``free%2Fblocked`` (a different handler), so it must still
    consume a token. ASGI-only: the native adapter rejects ``%2F`` with
    400 before the rate-limit check.
    """
    route = tmp_path / "app" / "[name]"
    route.mkdir(parents=True)
    (route / "page.py").write_text(
        'from pynext import h, page\n'
        '@page()\ndef Page(params): return h("p", {}, params["name"])\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(1, 60, exempt_paths=("/free",)))
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/other")).status_code == 200
                assert (await client.get("/other")).status_code == 429
                # The decoded form is under /free, but the routed handler
                # is not: this must be 429, not an exempt 200.
                assert (await client.get("/free%2Fblocked")).status_code == 429
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_native_rate_limit_returns_429_with_retry_after(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(3, 60))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5, trust_env=False) as client:
                for _ in range(3):
                    response = await client.get("/api/ok")
                    assert response.status_code == 200
                response = await client.get("/api/ok")
                assert response.status_code == 429
                # Bucket empty after 3 rapid requests: one token refills
                # every window/limit = 20 seconds.
                assert response.headers["retry-after"] == "20"
                assert response.text == "Too Many Requests"
                # Static files are covered by the same bucket.
                assert (await client.get("/health.txt")).status_code == 429
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
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5, trust_env=False) as client:
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
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5, trust_env=False) as client:
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
        (["pynext", "start", "--rate-limit-window", "nan"], "--rate-limit-window must be a number > 0"),
        (["pynext", "start", "--rate-limit-window", "inf"], "--rate-limit-window must be a number > 0"),
        # "-inf" as a separate argv word looks like an option to argparse,
        # so pass it in --flag=value form to reach the type check.
        (["pynext", "start", "--rate-limit-window=-inf"], "--rate-limit-window must be a number > 0"),
        (["pynext", "start", "--rate-limit", "10", "--trusted-proxy", "not-an-ip"],
         "Invalid trusted proxy"),
        (["pynext", "start", "--rate-limit", "10", "--rate-limit-exempt-path", "healthz"],
         "must be an absolute path"),
        (["pynext", "start", "--rate-limit", "10", "--rate-limit-ipv6-prefix-len", "-1"],
         "--rate-limit-ipv6-prefix-len must be an integer >= 0"),
        (["pynext", "start", "--rate-limit", "10", "--rate-limit-ipv6-prefix-len", "129"],
         "--rate-limit-ipv6-prefix-len must be an integer <= 128"),
        (["pynext", "start", "--rate-limit", "10", "--rate-limit-ipv6-prefix-len", "not-a-number"],
         "--rate-limit-ipv6-prefix-len must be an integer in [0, 128]"),
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
                    body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms,
                     shutdown_timeout, max_waiting))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--rate-limit", "100", "--rate-limit-window", "30",
                                      "--rate-limit-exempt-path", "/healthz",
                                      "--rate-limit-ipv6-prefix-len", "48",
                                      "--trusted-proxy", "10.0.0.0/8"])
    cli.main()
    assert len(seen) == 2
    _, app_path, production, limiter = seen[0]
    assert (app_path, production) == (root, True)
    assert limiter is not None
    assert limiter.limit == 100
    assert limiter.window_seconds == 30.0
    assert limiter.exempt_paths == ("/healthz",)
    assert limiter.ipv6_prefix_len == 48
    assert limiter.key_for("2001:db8:0:1::1") == limiter.key_for("2001:db8:0:2::1")
    assert len(limiter.trusted_proxies) == 1
    assert seen[1][0] == "serve"
    assert seen[1][-2] == 30  # B6 graceful-shutdown default
    assert seen[1][-1] is None  # C7 max_waiting default


# --- S-P1-4: atomicity (a limit must never over-admit under contention) ---


def test_memory_limiter_is_atomic_under_threads():
    """One lock covers get+refill+set: concurrent threads never over-admit.

    With a check-then-set race, two threads can both see the last token
    and both consume it, admitting more than the limit. The barrier makes
    all threads hit the bucket at once, so a race would show up here.
    """
    limiter = RateLimiter(10, 60)
    barrier = threading.Barrier(8)
    admitted = []

    def hammer():
        barrier.wait(timeout=10)
        count = 0
        for _ in range(25):
            allowed, _ = limiter.allow("shared")
            count += bool(allowed)
        admitted.append(count)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert sum(admitted) == 10


def test_memory_limiter_multi_key_thread_safety():
    """Concurrent keys stay isolated and the table stays bounded."""
    limiter = RateLimiter(5, 60, max_buckets=32)
    barrier = threading.Barrier(16)
    admitted = {}

    def hammer(index):
        key = f"key-{index}"
        barrier.wait(timeout=10)
        count = 0
        for _ in range(20):
            allowed, _ = limiter.allow(key)
            count += bool(allowed)
        admitted[key] = count

    threads = [threading.Thread(target=hammer, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert admitted == {f"key-{index}": 5 for index in range(16)}
    assert limiter.stats()["buckets"] == 16


def test_memory_limiter_buckets_are_per_process():
    """Two limiter instances do not share buckets (two workers, two limits).

    The memory backend is per-process by design (S-P1-4): each worker
    enforces its own copy of the limit. A limit shared across workers
    needs RedisRateLimiter — see tests/test_ratelimit_atomicity.py.
    """
    first = RateLimiter(3, 60)
    second = RateLimiter(3, 60)
    assert [first.allow("k")[0] for _ in range(4)] == [True, True, True, False]
    assert [second.allow("k")[0] for _ in range(4)] == [True, True, True, False]
