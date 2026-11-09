"""Filesystem ISR cache backend (Next.js FileSystemCache-style).

Covers the on-disk layout (``.body`` + ``.meta`` sidecar), version
fencing, cross-process leases, variant generations, capacity eviction,
and ISR integration. The multiprocessing test exercises the real
cross-process path (``flock`` + lease files), not just two handles in
one process.
"""
import asyncio
import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from pynext import Response
from pynext.cache import (
    ISRCache,
    FileSystemCacheBackend,
    _fs_entry_name,
)

POLICY = {"revalidate": 30.0, "stale": 60.0}


def _meta_for(backend, key):
    name = _fs_entry_name(key)
    return json.loads((Path(backend.path) / "entries" / (name + ".meta")).read_text(encoding="utf-8"))


def _body_for(backend, key):
    name = _fs_entry_name(key)
    return (Path(backend.path) / "entries" / (name + ".body")).read_bytes()


def test_entry_name_is_safe_and_collision_free():
    assert "/" not in _fs_entry_name("/news/../etc/passwd")
    assert ".." not in _fs_entry_name("../../../etc/passwd").split("-")[0]
    assert _fs_entry_name("a/b") != _fs_entry_name("a:b")
    assert _fs_entry_name("a/b") != _fs_entry_name("a_b")
    assert "news_hello" in _fs_entry_name("build:abc:/news/hello")


def test_insert_lookup_fresh_stale_expired(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.lookup("/page") == ("miss", None)
    assert backend.lookup_entry("/page") is None
    assert backend.insert("/page", b"<p>hi</p>", 0.2, 0.2) is True
    assert backend.lookup("/page") == ("fresh", b"<p>hi</p>")
    entry = backend.lookup_entry("/page")
    assert entry.status == "fresh" and entry.version == 1
    assert 0.0 < entry.fresh_remaining <= 0.2
    time.sleep(0.25)
    assert backend.lookup("/page") == ("stale", b"<p>hi</p>")
    time.sleep(0.25)
    assert backend.lookup("/page") == ("expired", None)
    # An expired lookup deletes the entry files.
    assert not (Path(backend.path) / "entries").exists() or not list(
        (Path(backend.path) / "entries").glob("*.meta")
    )


def test_meta_sidecar_is_human_readable(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.insert("/news/hello", b"<p>hi</p>", 30.0, 60.0) is True
    meta = _meta_for(backend, "/news/hello")
    assert meta["key"] == "/news/hello"
    assert meta["version"] == 1
    assert meta["fresh_for"] == 30.0 and meta["stale_for"] == 60.0
    assert meta["generated_at"] > 0 and meta["generated_mono"] > 0
    assert _body_for(backend, "/news/hello") == b"<p>hi</p>"
    assert "news_hello" in next((Path(backend.path) / "entries").glob("*.meta")).name


def test_entry_age_uses_the_surviving_clock(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    meta = {"generated_at": 1000.0, "generated_mono": 500.0}
    # Both clocks agree: age 10.
    assert backend._entry_age(meta, 1010.0, 510.0) == pytest.approx(10.0)
    # Wall clock jumped forward: monotonic age wins.
    assert backend._entry_age(meta, 2000.0, 510.0) == pytest.approx(10.0)
    # Wall clock jumped backward: monotonic age wins.
    assert backend._entry_age(meta, 900.0, 510.0) == pytest.approx(10.0)
    # Monotonic clock reset (reboot): wall age wins.
    assert backend._entry_age(meta, 1010.0, 10.0) == pytest.approx(10.0)


def test_version_fencing(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.insert_fenced("/page", b"v1", 30.0, 60.0, 0) is True
    # Stale expected version: rejected, old body kept.
    assert backend.insert_fenced("/page", b"v2", 30.0, 60.0, 0) is False
    assert backend.lookup("/page") == ("fresh", b"v1")
    # Correct version: accepted, version bumps.
    assert backend.insert_fenced("/page", b"v2", 30.0, 60.0, 1) is True
    assert backend.lookup("/page") == ("fresh", b"v2")
    assert _meta_for(backend, "/page")["version"] == 2
    # Unfenced insert bumps the version too.
    assert backend.insert("/page", b"v3", 30.0, 60.0) is True
    assert _meta_for(backend, "/page")["version"] == 3
    # Expected version for a missing entry must be 0.
    assert backend.insert_fenced("/other", b"x", 30.0, 60.0, 7) is False
    assert backend.lookup("/other") == ("miss", None)


def test_invalid_durations_and_sizes_are_rejected(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.insert("/page", b"x", 0.0, 60.0) is False
    assert backend.insert("/page", b"x", -1.0, 60.0) is False
    assert backend.insert("/page", b"x", 30.0, -1.0) is False
    assert backend.insert("/page", b"x", float("nan"), 60.0) is False
    assert backend.insert("/page", b"x", 30.0, 999_999.0) is False
    assert backend.lookup("/page") == ("miss", None)


def test_lease_excludes_other_workers_and_fences_writes(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    owner_a = "worker-a"
    version = backend.acquire_lease("/page", owner_a)
    assert version == 0
    # A second worker cannot take the lease while it is live.
    assert backend.acquire_lease("/page", "worker-b") is None
    assert backend.owns_lease("/page", owner_a) is True
    assert backend.owns_lease("/page", "worker-b") is False
    # A write without the lease owner is fenced out.
    assert backend.insert_fenced("/page", b"x", 30.0, 60.0, 0, owner="worker-b") is False
    # The owner writes under its lease.
    assert backend.insert_fenced("/page", b"x", 30.0, 60.0, 0, owner=owner_a) is True
    # Only the owner can release.
    assert backend.release_lease("/page", "worker-b") is False
    assert backend.owns_lease("/page", owner_a) is True
    assert backend.release_lease("/page", owner_a) is True
    assert backend.owns_lease("/page", owner_a) is False
    assert backend.acquire_lease("/page", "worker-b") == 1


def test_expired_lease_can_be_taken_over(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.acquire_lease("/page", "worker-a", ttl=0.1) == 0
    assert backend.acquire_lease("/page", "worker-b") is None
    time.sleep(0.15)
    assert backend.acquire_lease("/page", "worker-b") == 0
    assert backend.owns_lease("/page", "worker-b") is True


def test_expired_entry_survives_while_leased(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    owner = "worker-a"
    assert backend.acquire_lease("/page", owner, ttl=30.0) == 0
    assert backend.insert_fenced("/page", b"x", 0.1, 0.1, 0, owner=owner) is True
    time.sleep(0.25)
    # The lease is live, so the expired entry is reported but not deleted.
    assert backend.lookup("/page") == ("expired", None)
    assert (Path(backend.path) / "entries" / (_fs_entry_name("/page") + ".meta")).exists()
    assert backend.release_lease("/page", owner) is True
    # With the lease gone, the next lookup deletes it.
    assert backend.lookup("/page") == ("expired", None)
    assert not (Path(backend.path) / "entries" / (_fs_entry_name("/page") + ".meta")).exists()


def test_remove_deletes_entry_and_lease(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.insert("/page", b"x", 30.0, 60.0) is True
    assert backend.acquire_lease("/page", "worker-a") == 1
    assert backend.remove("/page") is True
    assert backend.lookup("/page") == ("miss", None)
    assert backend.owns_lease("/page", "worker-a") is False
    assert backend.remove("/page") is False


def test_body_without_meta_is_a_miss_and_is_cleaned_up(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    name = _fs_entry_name("/page")
    entries = Path(backend.path) / "entries"
    (entries / (name + ".body")).write_bytes(b"<p>torn</p>")
    assert backend.lookup("/page") == ("miss", None)
    assert not (entries / (name + ".body")).exists()


def test_corrupt_meta_is_a_miss(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    assert backend.insert("/page", b"x", 30.0, 60.0) is True
    name = _fs_entry_name("/page")
    (Path(backend.path) / "entries" / (name + ".meta")).write_text("{not json", encoding="utf-8")
    assert backend.lookup("/page") == ("miss", None)


def test_variant_generation(tmp_path):
    backend = FileSystemCacheBackend(tmp_path)
    token, existed = backend.variant_generation("/news")
    assert token and existed is False
    token2, existed2 = backend.variant_generation("/news")
    assert token2 == token and existed2 is True
    token3, existed3 = backend.variant_generation("/news", invalidate=True)
    assert token3 != token and existed3 is True
    token4, _ = backend.variant_generation("/news")
    assert token4 == token3


def test_capacity_evicts_oldest_first(tmp_path):
    backend = FileSystemCacheBackend(tmp_path, max_entries=3)
    for index in range(4):
        assert backend.insert(f"/page-{index}", b"x", 30.0, 60.0) is True
        time.sleep(0.01)
    names = {path.name for path in (Path(backend.path) / "entries").glob("*.meta")}
    assert len(names) == 3
    assert (_fs_entry_name("/page-3") + ".meta") in names
    assert (_fs_entry_name("/page-0") + ".meta") not in names
    assert backend.lookup("/page-0") == ("miss", None)
    assert backend.lookup("/page-3") == ("fresh", b"x")


def test_capacity_evicts_by_bytes(tmp_path):
    backend = FileSystemCacheBackend(tmp_path, max_entries=100, max_bytes=10)
    assert backend.insert("/a", b"12345", 30.0, 60.0) is True
    assert backend.insert("/b", b"12345", 30.0, 60.0) is True
    time.sleep(0.01)
    assert backend.insert("/c", b"12345", 30.0, 60.0) is True
    assert backend.lookup("/c") == ("fresh", b"12345")
    remaining = [path.name for path in (Path(backend.path) / "entries").glob("*.meta")]
    assert len(remaining) <= 2


def test_capacity_never_evicts_leased_entries(tmp_path):
    backend = FileSystemCacheBackend(tmp_path, max_entries=2)
    assert backend.insert("/a", b"x", 30.0, 60.0) is True
    assert backend.acquire_lease("/a", "worker-a", ttl=30.0) == 1
    assert backend.insert("/b", b"x", 30.0, 60.0) is True
    assert backend.insert("/c", b"x", 30.0, 60.0) is True
    assert backend.lookup("/a") == ("fresh", b"x")


def test_constructor_rejects_a_file_path(tmp_path):
    target = tmp_path / "cache-file"
    target.write_bytes(b"not a dir")
    with pytest.raises(ValueError):
        FileSystemCacheBackend(target)
    with pytest.raises(ValueError):
        FileSystemCacheBackend(tmp_path, max_entries=0)
    with pytest.raises(ValueError):
        FileSystemCacheBackend(tmp_path, max_bytes=0)


def _child_hold_lease(cache_dir, queue):
    from pynext.cache import FileSystemCacheBackend

    backend = FileSystemCacheBackend(cache_dir)
    assert backend.acquire_lease("/shared", "child", ttl=30.0) == 0
    assert backend.insert_fenced("/shared", b"from-child", 30.0, 60.0, 0, owner="child") is True
    queue.put("held")
    # Hold the lease while the parent probes; the queue blocks until the
    # parent closes it.
    queue.get()
    backend.release_lease("/shared", "child")


def test_cross_process_lease_and_visibility(tmp_path):
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=_child_hold_lease, args=(str(tmp_path), queue))
    proc.start()
    try:
        assert queue.get(timeout=10) == "held"
        backend = FileSystemCacheBackend(tmp_path)
        # The child's lease blocks this process, and the child's entry
        # is already visible here.
        assert backend.acquire_lease("/shared", "parent") is None
        assert backend.lookup("/shared") == ("fresh", b"from-child")
        # A write claiming a lease this process does not hold is fenced
        # out. (An ownerless write checks only the version, exactly like
        # the SQLite backend; ISR generations always pass their owner.)
        assert backend.insert_fenced("/shared", b"from-parent", 30.0, 60.0, 1, owner="parent") is False
        assert backend.lookup("/shared") == ("fresh", b"from-child")
        # A different key is unaffected by the child's lease.
        assert backend.insert("/other", b"ok", 30.0, 60.0) is True
    finally:
        queue.put("release")
        queue.close()
        proc.join(timeout=10)
        assert proc.exitcode == 0


async def _render_ok():
    return Response(b"<p>ok</p>")


def test_isr_cache_integration(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="filesystem", cache_path=str(tmp_path))
        try:
            calls = []

            async def render():
                calls.append(1)
                return Response(b"<p>ok</p>")

            assert await cache.get_or_render("/page", POLICY, render) == b"<p>ok</p>"
            assert await cache.get_or_render("/page", POLICY, render) == b"<p>ok</p>"
            assert len(calls) == 1
            assert await cache.invalidate("/page") is True
            assert await cache.get_or_render("/page", POLICY, render) == b"<p>ok</p>"
            assert len(calls) == 2
        finally:
            await cache.close()

    asyncio.run(run())


def test_isr_cache_reopen_after_close(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="filesystem", cache_path=str(tmp_path))
        try:
            assert await cache.get_or_render("/page", POLICY, _render_ok) == b"<p>ok</p>"
        finally:
            await cache.close()
        # A fresh instance on the same directory still sees the entry:
        # it survived on disk, not just in the first process's memory.
        cache2 = ISRCache(cache_backend="filesystem", cache_path=str(tmp_path))
        try:
            calls = []

            async def render():
                calls.append(1)
                return Response(b"<p>ok</p>")

            assert await cache2.get_or_render("/page", POLICY, render) == b"<p>ok</p>"
            assert calls == []
        finally:
            await cache2.close()
        # open() after close() also restores the shared store.
        cache.open()
        try:
            assert cache.shared is not None
        finally:
            await cache.close()

    asyncio.run(run())


def test_isr_cache_requires_cache_path():
    with pytest.raises(ValueError, match="cache_path is required"):
        ISRCache(cache_backend="filesystem")
    with pytest.raises(ValueError, match="Unknown cache backend"):
        ISRCache(cache_backend="disk")
