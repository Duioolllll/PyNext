"""``pendingSets``-style set-waiting for the shared ISR cache.

Next.js ``cache-handlers/default.ts`` keeps a per-key ``pendingSets`` map:
``get`` awaits an in-flight ``set`` for the same key instead of reading
the previous version, and ``set`` resolves its waiters in a ``finally``.
PyNext ports that as ``ISRCache._pending_sets`` (tracks/c-caching.md
P2-2): concurrent shared writes for one key coalesce into one write, and
a ``get`` that arrives while a write is landing waits (bounded) and then
re-reads the shared entry.
"""
import asyncio
import threading
import time

from pynext import Response
from pynext.cache import ISRCache

POLICY = {"revalidate": 30.0, "stale": 60.0}


def test_concurrent_shared_sets_coalesce_into_one_write(tmp_path):
    """A second set for the same key waits for the first and skips its own
    write when the shared entry is fresh afterwards."""
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=str(tmp_path / "cache.sqlite"))
        try:
            writes = []
            started = threading.Event()
            release = threading.Event()
            real_insert = cache.shared.insert_fenced

            def slow_insert(key, body, fresh, stale, expected_version, owner=None):
                writes.append(body)
                if len(writes) == 1:
                    started.set()
                    assert release.wait(timeout=10)
                return real_insert(key, body, fresh, stale, expected_version, owner)

            cache.shared.insert_fenced = slow_insert
            version = await asyncio.to_thread(cache.shared.acquire_lease, "/page", "owner-1")
            assert version == 0
            first = asyncio.create_task(
                cache._store_shared("/page", b"first", POLICY, version, "owner-1"))
            assert await asyncio.to_thread(started.wait, 10)
            second = asyncio.create_task(
                cache._store_shared("/page", b"second", POLICY, version, "owner-1"))
            await asyncio.sleep(0.05)
            assert not second.done()
            release.set()
            assert await first == (True, False)
            assert await second == (True, True)
            assert writes == [b"first"]
            assert cache.shared.lookup("/page") == ("fresh", b"first")
            # The coalesced set populated L1 from the winning write.
            assert cache.store.lookup("/page") == ("fresh", b"first")
            assert cache._pending_sets == {}
        finally:
            await cache.close()

    asyncio.run(run())


def test_get_waits_for_pending_set_and_serves_fresh_body(tmp_path):
    """A get that arrives while a shared write is landing waits for it and
    serves the new body instead of the stale one — without rendering."""
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=str(tmp_path / "cache.sqlite"))
        try:
            await asyncio.to_thread(
                cache.shared.insert_fenced, "/page", b"stale-body", 0.05, 3600.0, 0)
            await asyncio.sleep(0.1)
            assert cache.shared.lookup("/page")[0] == "stale"

            async def _render_must_not_run():
                raise AssertionError("get must not render while a pending set lands")

            event = asyncio.Event()
            cache._pending_sets["/page"] = event
            result = asyncio.create_task(
                cache.get_or_render("/page", POLICY, _render_must_not_run))
            await asyncio.sleep(0.05)
            assert not result.done()
            # The write lands while the get waits; only then is it released.
            await asyncio.to_thread(
                cache.shared.insert_fenced, "/page", b"fresh-body", 30.0, 60.0, None)
            event.set()
            assert await result == b"fresh-body"
        finally:
            await cache.close()

    asyncio.run(run())


def test_get_does_not_wait_forever_for_a_stuck_set(tmp_path, monkeypatch):
    """When a pending set never finishes, the get times out and falls back
    to the pre-existing behaviour (serve stale, regenerate behind it)."""
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=str(tmp_path / "cache.sqlite"))
        try:
            await asyncio.to_thread(
                cache.shared.insert_fenced, "/page", b"stale-body", 0.05, 3600.0, 0)
            await asyncio.sleep(0.1)
            assert cache.shared.lookup("/page")[0] == "stale"
            cache._pending_sets["/page"] = asyncio.Event()  # never set

            async def _render_new():
                return Response(b"<p>new</p>")

            started = time.monotonic()
            body = await cache.get_or_render("/page", POLICY, _render_new)
            assert body == b"stale-body"
            assert time.monotonic() - started < 1.0
        finally:
            await cache.close()

    monkeypatch.setattr("pynext.cache.PENDING_SET_GET_TIMEOUT", 0.05)
    asyncio.run(run())


def test_failed_set_resolves_its_waiters_and_leaves_no_registration(tmp_path):
    """A set whose backend write fails must still resolve the pending event
    (Next.js ``finally``), so a concurrent get does not wait forever — and
    a later set for the same key can write again."""
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=str(tmp_path / "cache.sqlite"))
        try:
            real_insert = cache.shared.insert_fenced
            started = threading.Event()
            release = threading.Event()

            def failing_insert(*args, **kwargs):
                started.set()
                assert release.wait(timeout=10)
                raise RuntimeError("backend down")

            cache.shared.insert_fenced = failing_insert
            failing = asyncio.create_task(cache._store_shared("/page", b"body", POLICY, 0, None))
            assert await asyncio.to_thread(started.wait, 10)
            waiter = asyncio.create_task(cache._wait_pending_set("/page", timeout=5.0))
            await asyncio.sleep(0.05)
            assert not waiter.done()
            release.set()
            assert await failing == (False, False)
            assert await waiter is True
            assert cache._pending_sets == {}
            cache.shared.insert_fenced = real_insert
            assert await cache._store_shared("/page", b"body", POLICY, 0, None) == (True, False)
            assert cache.shared.lookup("/page") == ("fresh", b"body")
        finally:
            await cache.close()

    asyncio.run(run())
