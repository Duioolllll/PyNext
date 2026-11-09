"""SQLite backend: monotonic clock + capacity cap (P1-6).

REPORT.md section 3.2 #10/#11 and tracks/c-caching.md P1-6: the
SQLite backend used wall-clock ``time.time()`` for every lifetime
decision, so a clock jump could resurrect expired entries or kill
fresh ones, and the ``entries`` table grew without bound because
expired rows were only deleted when their own key was looked up.
"""
import sqlite3
import time

import pytest

from pynext.cache import ISRCache, SQLiteCacheBackend


def _row_count(backend, key=None):
    if key is None:
        return backend._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return backend._conn.execute(
        "SELECT COUNT(*) FROM entries WHERE key = ?", (key,)).fetchone()[0]


def test_forward_wall_jump_keeps_fresh_entry_fresh(tmp_path, monkeypatch):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.insert("k", b"one", 30, 60)
        assert backend.lookup("k") == ("fresh", b"one")
        real_now = time.time()
        monkeypatch.setattr(time, "time", lambda: real_now + 3600)
        # A forward jump must not age the entry: the decision clock is
        # monotonic, so the entry is still fresh.
        assert backend.lookup("k") == ("fresh", b"one")
        assert backend.lookup_entry("k").status == "fresh"
        # An entry written while the wall clock is jumped stays fresh
        # once the clock comes back: its stored stamp is wall-clock,
        # the decision clock never jumped.
        assert backend.insert("jumped", b"two", 30, 60)
        monkeypatch.undo()
        assert backend.lookup("jumped") == ("fresh", b"two")
        assert backend.lookup("k") == ("fresh", b"one")
    finally:
        backend.close()


def test_backward_wall_jump_does_not_resurrect_expired_entry(tmp_path, monkeypatch):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.insert("k", b"one", 0.2, 0.3)
        # A live lease keeps the expired row in place (it is not
        # deleted on lookup), so the jump test reads the row itself
        # rather than an already-deleted key.
        assert backend.acquire_lease("k", "worker") == 1
        time.sleep(0.6)
        assert backend.lookup("k") == ("expired", None)
        real_now = time.time()
        monkeypatch.setattr(time, "time", lambda: real_now - 3600)
        # Wall-clock expiry would read a negative age here and serve
        # the entry fresh again; the monotonic decision clock keeps it
        # expired.
        assert backend.lookup("k") == ("expired", None)
        assert backend.lookup("k") == ("expired", None)
    finally:
        backend.close()


def test_forward_wall_jump_keeps_lease_and_generation_alive(tmp_path, monkeypatch):
    path = tmp_path / "cache.sqlite3"
    first = SQLiteCacheBackend(path)
    second = SQLiteCacheBackend(path)
    try:
        assert first.insert("k", b"one", 30, 30)
        assert first.acquire_lease("k", "worker-a", ttl=30) == 1
        token, existed = first.variant_generation("/page")
        assert existed is False
        real_now = time.time()
        monkeypatch.setattr(time, "time", lambda: real_now + 90000)
        # Both the lease (ttl 30s) and the generation (TTL 1 day) must
        # survive a wall-clock jump far past their wall-clock expiry.
        assert second.acquire_lease("k", "worker-b") is None
        assert first.owns_lease("k", "worker-a")
        assert first.variant_generation("/page") == (token, True)
    finally:
        first.close()
        second.close()


def test_max_entries_evicts_oldest_first(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3", max_entries=3)
    try:
        for key in ("k1", "k2", "k3", "k4"):
            assert backend.insert(key, b"v", 30, 30)
            time.sleep(0.01)
        assert _row_count(backend) == 3
        assert backend.lookup("k1") == ("miss", None)
        for key in ("k2", "k3", "k4"):
            assert backend.lookup(key) == ("fresh", b"v")
        # Re-inserting the evicted key evicts the next oldest.
        assert backend.insert("k1", b"v", 30, 30)
        assert backend.lookup("k2") == ("miss", None)
        assert backend.lookup("k1") == ("fresh", b"v")
    finally:
        backend.close()


def test_max_bytes_evicts_oldest_first(tmp_path):
    # Key (1 byte) + body (100 bytes) = 101 bytes per row.
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3", max_entries=100, max_bytes=250)
    try:
        for key in ("a", "b", "c"):
            assert backend.insert(key, b"x" * 100, 30, 30)
            time.sleep(0.01)
        assert _row_count(backend) == 2
        assert backend.lookup("a") == ("miss", None)
        assert backend.lookup("b") == ("fresh", b"x" * 100)
        assert backend.lookup("c") == ("fresh", b"x" * 100)
    finally:
        backend.close()


def test_insert_prunes_expired_rows_that_are_never_read(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.insert("old", b"o", 0.2, 0.2)
        time.sleep(0.5)
        assert backend.insert("new", b"n", 30, 30)
        # Without the insert-time cleanup the expired row would linger
        # in the file until something looked "old" up again.
        assert _row_count(backend, "old") == 0
        assert _row_count(backend, "new") == 1
    finally:
        backend.close()


def test_prune_keeps_expired_entry_under_live_lease(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.insert("k", b"old", 1, 0)
        assert backend.acquire_lease("k", "worker") == 1
        backend._conn.execute("UPDATE entries SET generated_at = 0 WHERE key = ?", ("k",))
        backend._conn.commit()
        assert backend.insert("other", b"o", 30, 30)
        # The expired row must survive cleanup while the lease is live,
        # or the regenerating worker's fenced write would find no row
        # at its expected version and be rejected.
        assert _row_count(backend, "k") == 1
        assert backend.lookup("k")[0] == "expired"
        assert backend.insert_fenced("k", b"new", 30, 0, 1, "worker")
        assert backend.lookup("k") == ("fresh", b"new")
    finally:
        backend.close()


def test_prune_removes_expired_rows_and_leases(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.insert("old", b"o", 0.2, 0.2)
        assert backend.acquire_lease("gone", "worker", ttl=0.05) is not None
        time.sleep(0.5)
        assert backend.prune()
        assert _row_count(backend, "old") == 0
        assert backend._conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    finally:
        backend.close()


def test_capacity_defaults_and_validation(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.max_entries == SQLiteCacheBackend.DEFAULT_MAX_ENTRIES == 4096
        assert backend.max_bytes == SQLiteCacheBackend.DEFAULT_MAX_BYTES == 128 * 1024 * 1024
    finally:
        backend.close()
    for kwargs in ({"max_entries": 0}, {"max_entries": -1}, {"max_entries": "3"},
                   {"max_bytes": 0}, {"max_bytes": 1.5}):
        with pytest.raises(ValueError):
            SQLiteCacheBackend(tmp_path / "bad.sqlite3", **kwargs)


def test_isrcache_passes_sqlite_caps_through(tmp_path):
    cache = ISRCache(cache_backend="sqlite", cache_path=tmp_path / "cache.sqlite3",
                     sqlite_max_entries=2, sqlite_max_bytes=1024)
    try:
        assert cache.shared.max_entries == 2
        assert cache.shared.max_bytes == 1024
    finally:
        import asyncio
        asyncio.run(cache.close())
    cache = ISRCache(cache_backend="sqlite", cache_path=tmp_path / "cache2.sqlite3")
    try:
        assert cache.shared.max_entries == SQLiteCacheBackend.DEFAULT_MAX_ENTRIES
    finally:
        import asyncio
        asyncio.run(cache.close())
