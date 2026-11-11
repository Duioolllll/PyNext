"""Failure-policy behaviour (roadmap item B4).

The shared ISR cache and the Redis rate limiter used to disagree on
outages with no way to change it: the cache always fell back locally
(fail_open) while the limiter always answered 503 (fail_closed).
These tests pin the exposed ``fail_open`` / ``fail_closed`` policies,
their defaults, and their independence from each other — without a
real Redis server, by injecting a failing shared backend and by
breaking a limiter's Redis client.
"""
import asyncio

import pytest

from pynext import FAIL_CLOSED, FAIL_OPEN, Response, create_app
from pynext.build import write_manifest
from pynext.cache import ISRCache, SharedCacheUnavailable
from pynext.failure_policy import normalize_failure_policy
from pynext.http import Request
from pynext.ratelimit import RateLimitUnavailable

POLICY = {"revalidate": 30, "stale": 30}


class _FailingSharedBackend:
    """Shared backend whose read/decision operations always fail."""

    def lookup(self, key):
        raise SharedCacheUnavailable("down")

    def lookup_entry(self, key):
        raise SharedCacheUnavailable("down")

    def insert(self, key, body, fresh_seconds, stale_seconds):
        return True

    def insert_fenced(self, key, body, fresh_seconds, stale_seconds, expected_version, owner=None):
        return True

    def remove(self, key):
        raise SharedCacheUnavailable("down")

    def acquire_lease(self, key, owner, ttl=35.0):
        raise SharedCacheUnavailable("down")

    def release_lease(self, key, owner):
        return True

    def owns_lease(self, key, owner):
        raise SharedCacheUnavailable("down")

    def variant_generation(self, key, *, invalidate=False):
        raise SharedCacheUnavailable("down")

    def close(self):
        pass


def _cache_with_failing_backend(policy):
    cache = ISRCache(cache_backend="memory", failure_policy=policy)
    cache.shared = _FailingSharedBackend()
    return cache


def _render_once(calls):
    async def render():
        calls.append(1)
        return Response(b"hello")

    return render


def test_normalize_failure_policy_defaults_and_rejects():
    assert normalize_failure_policy(None, default=FAIL_OPEN) == FAIL_OPEN
    assert normalize_failure_policy(None, default=FAIL_CLOSED) == FAIL_CLOSED
    assert normalize_failure_policy("fail_open", default=FAIL_CLOSED) == FAIL_OPEN
    assert normalize_failure_policy("fail_closed", default=FAIL_OPEN) == FAIL_CLOSED
    for bad in ("sometimes", "FAIL_OPEN", "", 1, True):
        with pytest.raises(ValueError):
            normalize_failure_policy(bad, default=FAIL_OPEN)


def test_isr_cache_rejects_invalid_failure_policy():
    with pytest.raises(ValueError):
        ISRCache(cache_backend="memory", failure_policy="sometimes")


def test_cache_default_policy_is_fail_open():
    assert ISRCache(cache_backend="memory").failure_policy == FAIL_OPEN


def test_cache_fail_open_falls_back_to_local_render():
    cache = _cache_with_failing_backend(None)  # default policy
    calls = []

    async def run():
        assert await cache.get_or_render("/page", POLICY, _render_once(calls)) == b"hello"
        # The page was rendered locally and cached in L1, so a second
        # request does not render again even while the backend is down.
        assert await cache.get_or_render("/page", POLICY, _render_once(calls)) == b"hello"
        assert len(calls) == 1
        assert await cache.invalidate("/page")

    asyncio.run(run())


def test_cache_fail_closed_raises_instead_of_falling_back():
    cache = _cache_with_failing_backend("fail_closed")
    calls = []

    async def run():
        with pytest.raises(SharedCacheUnavailable):
            await cache.get_or_render("/page", POLICY, _render_once(calls))
        # The lookup failed before any render started.
        assert calls == []
        with pytest.raises(SharedCacheUnavailable):
            await cache.invalidate("/page")
        with pytest.raises(SharedCacheUnavailable):
            await cache.variant_generation("/page")

    asyncio.run(run())


def test_cache_fail_closed_healthy_sqlite_backend_still_shares(tmp_path):
    path = tmp_path / "isr.sqlite3"
    first = ISRCache(cache_backend="sqlite", cache_path=str(path), failure_policy="fail_closed")
    second = ISRCache(cache_backend="sqlite", cache_path=str(path), failure_policy="fail_closed")
    calls = []

    async def run():
        try:
            assert await first.get_or_render("/page", POLICY, _render_once(calls)) == b"hello"
            assert await second.get_or_render("/page", POLICY, _render_once(calls)) == b"hello"
            assert len(calls) == 1
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def _make_isr_project(root):
    directory = root / "app"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        "from pynext import h, page\n"
        "@page(render=\"isr\", revalidate=30, stale=30)\n"
        "async def Page(params):\n"
        "    return h(\"p\", {}, \"hello\")\n"
    )
    write_manifest(root)


def test_app_returns_503_for_isr_page_under_fail_closed_cache(tmp_path):
    root = tmp_path.resolve()
    _make_isr_project(root)
    app = create_app(root, production=True, cache_backend="sqlite",
                     cache_path=str(root / "isr.sqlite3"), cache_failure_policy="fail_closed")
    app.isr.shared = _FailingSharedBackend()

    async def run():
        request = Request("GET", "/", b"", (), b"", _app=app)
        response = await app.dispatch(request)
        assert response.status == 503
        # Switching the same app to fail_open serves the page locally.
        app.isr.failure_policy = FAIL_OPEN
        request = Request("GET", "/", b"", (), b"", _app=app)
        response = await app.dispatch(request)
        assert response.status == 200
        assert b"hello" in response.body

    asyncio.run(run())


def _redis_limiter(monkeypatch, policy):
    pytest.importorskip("redis")
    import redis

    from pynext import RedisRateLimiter

    limiter = RedisRateLimiter(10, 60, url="redis://127.0.0.1:6390/0",
                               namespace="b4-test", failure_policy=policy)

    def broken(*args, **kwargs):
        raise redis.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(limiter.client, "eval", broken)
    return limiter


def test_redis_limiter_default_policy_is_fail_closed(monkeypatch):
    limiter = _redis_limiter(monkeypatch, None)
    assert limiter.failure_policy == FAIL_CLOSED
    with pytest.raises(RateLimitUnavailable):
        limiter.allow("127.0.0.1")


def test_redis_limiter_fail_open_admits_when_redis_is_down(monkeypatch):
    limiter = _redis_limiter(monkeypatch, "fail_open")
    assert limiter.allow("127.0.0.1") == (True, None)


def test_redis_limiter_rejects_invalid_failure_policy():
    pytest.importorskip("redis")
    from pynext import RedisRateLimiter

    with pytest.raises(ValueError):
        RedisRateLimiter(10, 60, url="redis://127.0.0.1:6390/0",
                         namespace="b4-test", failure_policy="sometimes")


def test_combined_policies_are_independent(monkeypatch):
    """One component's outage policy does not change the other's.

    Cache fail_open degrades to a local render while the limiter stays
    fail_closed and refuses; cache fail_closed refuses while the
    limiter stays fail_open and admits.
    """
    limiter = _redis_limiter(monkeypatch, None)
    cache = _cache_with_failing_backend("fail_open")
    calls = []
    limiter_open = _redis_limiter(monkeypatch, "fail_open")

    async def run():
        assert await cache.get_or_render("/page", POLICY, _render_once(calls)) == b"hello"
        with pytest.raises(RateLimitUnavailable):
            limiter.allow("127.0.0.1")
        cache.failure_policy = FAIL_CLOSED
        with pytest.raises(SharedCacheUnavailable):
            await cache.get_or_render("/other", POLICY, _render_once(calls))
        assert limiter_open.allow("127.0.0.1") == (True, None)

    asyncio.run(run())


def test_cli_passes_failure_policies(tmp_path, monkeypatch):
    import sys

    from pynext import cli

    root = tmp_path.resolve()
    seen = {}

    class FakeLimiter:
        def __init__(self, limit, window, **kwargs):
            seen["limiter"] = (limit, window, kwargs)

    def fake_create_app(path, production, **kwargs):
        seen["app"] = (path, production, kwargs)
        return object()

    monkeypatch.setattr(cli, "RedisRateLimiter", FakeLimiter)
    monkeypatch.setattr(cli, "create_app", fake_create_app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms, **kwargs):
        seen["serve"] = received_app

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--workers", "1",
                                      "--rate-limit", "10", "--rate-limit-backend", "redis",
                                      "--rate-limit-failure-policy", "fail_open",
                                      "--cache-backend", "redis",
                                      "--cache-failure-policy", "fail_closed"])
    cli.main()
    assert seen["limiter"] == (10, 60.0, {"trusted_proxies": [],
                                           "exempt_paths": ("/healthz", "/readyz"),
                                           "ipv6_prefix_len": 64,
                                           "namespace": "default",
                                           "failure_policy": "fail_open"})
    assert seen["app"][2]["cache_failure_policy"] == "fail_closed"


def test_cache_fail_open_invalidate_reports_local_removal_only():
    """Review #4 #20: in fail_open, True means local removal succeeded.

    The shared removal fails (backend down) while the local removal
    succeeds, and invalidate() returns True. That True does not
    guarantee the shared entry is gone — shared removal is best-effort
    in fail_open mode — which is what the docstring and manual now say.
    """
    cache = _cache_with_failing_backend("fail_open")
    calls = []

    async def run():
        assert await cache.get_or_render("/page", POLICY, _render_once(calls)) == b"hello"
        assert cache.store.lookup("/page")[0] == "fresh"
        assert await cache.invalidate("/page") is True
        assert cache.store.lookup("/page")[0] == "miss"
    asyncio.run(run())


def test_invalidate_docstring_states_fail_open_semantics():
    doc = ISRCache.invalidate.__doc__ or ""
    assert "fail_open" in doc
    assert "best-effort" in doc
    assert "does not guarantee the shared entry is gone" in doc
    assert "fail_closed" in doc
