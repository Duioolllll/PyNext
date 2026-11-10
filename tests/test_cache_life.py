"""Cache-life 3-axis lifetimes (expire + cacheLife profiles) and the
SQLite monotonic clock (REPORT §2.1 #3, §3.2 #10, §3.3 #1)."""
import asyncio
import time

import pytest

from pynext import CACHE_LIFE_PROFILES, Response, create_app, page
from pynext.build import write_manifest
from pynext.cache import (MAX_CACHE_SECONDS, ISRCache, SQLiteCacheBackend,
                          normalize_isr_policy, policy_windows)
from pynext.components import _cache_life_profile, _isr_policy


# --- policy normalization -------------------------------------------------

def test_isr_policy_defaults_expire_to_revalidate_plus_stale():
    assert _isr_policy(60, 60) == {"revalidate": 60.0, "stale": 60.0, "expire": 120.0}
    assert _isr_policy(60) == {"revalidate": 60.0, "stale": 60.0, "expire": 120.0}


def test_isr_policy_expire_only_derives_stale_window():
    assert _isr_policy(60, expire=3600) == {"revalidate": 60.0, "stale": 3540.0, "expire": 3600.0}


def test_isr_policy_consistent_stale_and_expire_are_accepted():
    assert _isr_policy(60, 3540, 3600) == {"revalidate": 60.0, "stale": 3540.0, "expire": 3600.0}


@pytest.mark.parametrize("kwargs", [
    {"revalidate": 60, "expire": 60},      # expire must be greater than revalidate
    {"revalidate": 60, "expire": 30},      # expire below revalidate
    {"revalidate": 60, "expire": 0},
    {"revalidate": 60, "expire": -1},
    {"revalidate": 60, "expire": True},
    {"revalidate": 60, "expire": float("nan")},
    {"revalidate": 60, "expire": MAX_CACHE_SECONDS + 1},
    {"revalidate": 60, "stale": 60, "expire": 3600},  # stale/expire disagree
    {"revalidate": MAX_CACHE_SECONDS + 1, "stale": 1},
    {"revalidate": 60, "stale": MAX_CACHE_SECONDS + 1},
])
def test_isr_policy_rejects_invalid_expire(kwargs):
    with pytest.raises(ValueError):
        _isr_policy(**kwargs)


def test_isr_policy_infinite_expire_caps_at_one_year():
    assert _isr_policy(60, expire=float("inf")) == {
        "revalidate": 60.0, "stale": MAX_CACHE_SECONDS - 60.0, "expire": MAX_CACHE_SECONDS}


def test_isr_policy_allows_month_long_revalidate_for_max_profile():
    assert _isr_policy(2_592_000, expire=31_536_000)["revalidate"] == 2_592_000.0


def test_normalize_isr_policy_accepts_two_key_policies():
    # Pre-expire policies keep their old meaning.
    assert normalize_isr_policy({"revalidate": 60.0, "stale": 60.0}) == {
        "revalidate": 60.0, "stale": 60.0, "expire": 120.0}
    assert policy_windows({"revalidate": 60.0, "stale": 60.0, "expire": 3600.0}) == (60.0, 3540.0)


@pytest.mark.parametrize("policy", [
    {"revalidate": 60.0},
    {"stale": 60.0, "expire": 120.0},
    {"revalidate": True, "stale": 1.0, "expire": 2.0},
    {"revalidate": 60.0, "stale": 60.0, "expire": 60.0},
    "not-a-dict",
])
def test_normalize_isr_policy_rejects_malformed(policy):
    with pytest.raises(ValueError):
        normalize_isr_policy(policy)


# --- cacheLife profiles ----------------------------------------------------

def test_profiles_are_internally_consistent():
    assert set(CACHE_LIFE_PROFILES) == {"default", "seconds", "minutes", "hours", "days", "weeks", "max"}
    for name, profile in CACHE_LIFE_PROFILES.items():
        normalized = _isr_policy(profile["revalidate"], profile["stale"], profile["expire"])
        assert normalized["expire"] > normalized["revalidate"], name
        assert normalized["stale"] == normalized["expire"] - normalized["revalidate"], name


def test_profiles_match_nextjs_defaults():
    assert CACHE_LIFE_PROFILES["seconds"] == {"revalidate": 1, "stale": 59, "expire": 60}
    assert CACHE_LIFE_PROFILES["minutes"] == {"revalidate": 60, "stale": 3_540, "expire": 3_600}
    assert CACHE_LIFE_PROFILES["hours"] == {"revalidate": 3_600, "stale": 82_800, "expire": 86_400}
    assert CACHE_LIFE_PROFILES["days"] == {"revalidate": 86_400, "stale": 518_400, "expire": 604_800}
    assert CACHE_LIFE_PROFILES["weeks"] == {"revalidate": 604_800, "stale": 1_987_200, "expire": 2_592_000}
    assert CACHE_LIFE_PROFILES["max"] == {"revalidate": 2_592_000, "stale": 28_944_000, "expire": 31_536_000}
    assert CACHE_LIFE_PROFILES["default"]["revalidate"] == 900
    assert CACHE_LIFE_PROFILES["default"]["expire"] == 31_536_000


def test_page_accepts_cache_life_profile():
    @page(render="isr", cache_life="hours")
    def Page(params):
        return None

    assert Page.__pynext_cache_policy__ == {
        "revalidate": 3600.0, "stale": 82800.0, "expire": 86400.0}


@pytest.mark.parametrize("kwargs", [
    {"render": "isr", "cache_life": "nope"},
    {"render": "isr", "cache_life": "hours", "revalidate": 60},
    {"render": "isr", "cache_life": "hours", "stale": 60},
    {"render": "isr", "cache_life": "hours", "expire": 3600},
    {"render": "isr", "cache_life": 42},
    {"render": "ssg", "cache_life": "hours"},
    {"render": "ssr", "expire": 3600},
    {"render": "ssg", "expire": 3600},
])
def test_page_rejects_bad_cache_life(kwargs):
    with pytest.raises(ValueError):
        page(**kwargs)


def test_unknown_profile_error_names_valid_profiles():
    with pytest.raises(ValueError, match="seconds"):
        _cache_life_profile("nope")


def test_cache_life_profile_reaches_manifest_and_app(tmp_path):
    directory = tmp_path / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", cache_life="hours")\n'
        'async def Page(params):\n'
        '    return h("p", {}, params["slug"])\n'
    )
    manifest = write_manifest(tmp_path)
    expected = {"revalidate": 3600.0, "stale": 82800.0, "expire": 86400.0}
    assert manifest["isr_policies"]["/blog/[slug]"] == expected
    app = create_app(tmp_path)
    assert app.isr_policies["/blog/[slug]"] == expected


# --- ISRCache maps expire to the backend windows ---------------------------

class _FakeStore:
    def __init__(self):
        self.insert_calls = []

    def lookup(self, key):
        return ("miss", None)

    def insert(self, key, body, revalidate, stale):
        self.insert_calls.append((key, body, revalidate, stale))
        return True

    def remove(self, key):
        return False


async def _render_ok():
    return Response(b"<p>ok</p>")


def _run_get_or_render(cache, policy):
    async def run():
        try:
            return await cache.get_or_render("/page", policy, _render_ok)
        finally:
            await cache.close()

    return asyncio.run(run())


def test_get_or_render_maps_expire_to_stale_window():
    cache = ISRCache()
    fake = _FakeStore()
    cache.store = fake
    assert _run_get_or_render(cache, {"revalidate": 60.0, "stale": 60.0, "expire": 3600.0}) == b"<p>ok</p>"
    assert fake.insert_calls == [("/page", b"<p>ok</p>", 60.0, 3540.0)]


def test_get_or_render_keeps_two_key_policy_meaning():
    cache = ISRCache()
    fake = _FakeStore()
    cache.store = fake
    assert _run_get_or_render(cache, {"revalidate": 60.0, "stale": 60.0}) == b"<p>ok</p>"
    assert fake.insert_calls == [("/page", b"<p>ok</p>", 60.0, 60.0)]


def test_shared_backend_reports_expire_based_windows(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=tmp_path / "cache.sqlite3")
        try:
            assert await cache.get_or_render(
                "/page", {"revalidate": 60.0, "stale": 60.0, "expire": 3600.0}, _render_ok) == b"<p>ok</p>"
            entry = cache.shared.lookup_entry("/page")
            assert entry.status == "fresh"
            assert 59.0 < entry.fresh_remaining <= 60.0
            assert 3539.0 < entry.stale_remaining <= 3540.0
        finally:
            await cache.close()

    asyncio.run(run())


# --- SQLite monotonic clock -------------------------------------------------

@pytest.fixture
def wall_shift(monkeypatch):
    """Shift the wall clock seen by the cache module, leaving monotonic alone."""
    real_time = time.time

    def shift(seconds):
        monkeypatch.setattr(time, "time", lambda: real_time() + seconds)

    return shift


def test_sqlite_entry_survives_forward_wall_clock_jump(tmp_path, wall_shift):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.insert("/page", b"<p>ok</p>", 60, 60)
    assert backend.lookup("/page") == ("fresh", b"<p>ok</p>")
    wall_shift(3600)  # NTP-style jump forward: wall age alone would expire it.
    assert backend.lookup("/page") == ("fresh", b"<p>ok</p>")
    backend.close()


def test_sqlite_entry_expires_when_monotonic_age_passes(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.insert("/page", b"<p>ok</p>", 0.05, 0.05)
    time.sleep(0.15)
    assert backend.lookup("/page") == ("expired", None)
    assert backend.lookup("/page") == ("miss", None)
    backend.close()


def test_sqlite_reboot_falls_back_to_wall_clock(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.insert("/page", b"<p>ok</p>", 60, 60)
    # Simulate a reboot: the stored monotonic timestamp is from a previous
    # boot epoch, so the monotonic age is negative and untrustworthy.
    backend._conn.execute("UPDATE entries SET generated_mono = generated_mono + 1000000")
    backend._conn.commit()
    assert backend.lookup("/page") == ("fresh", b"<p>ok</p>")
    # And a monotonic timestamp far in the past expires the entry even
    # though the wall clock says it is fresh.
    backend._conn.execute("UPDATE entries SET generated_mono = generated_mono - 2000000")
    backend._conn.commit()
    assert backend.lookup("/page") == ("expired", None)
    backend.close()


def test_sqlite_lease_not_extended_by_backward_wall_jump(tmp_path, wall_shift):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.acquire_lease("/page", "owner-a", ttl=0.2) == 0
    assert backend.acquire_lease("/page", "owner-b", ttl=0.2) is None
    time.sleep(0.3)  # The lease's monotonic TTL has passed.
    wall_shift(-3600)  # Wall clock jumps back: wall alone would keep it live.
    assert backend.acquire_lease("/page", "owner-b", ttl=0.2) == 0
    assert backend.owns_lease("/page", "owner-b")
    backend.close()


def test_sqlite_lease_still_live_within_ttl(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.acquire_lease("/page", "owner-a", ttl=60) == 0
    assert backend.acquire_lease("/page", "owner-b", ttl=60) is None
    assert backend.owns_lease("/page", "owner-a")
    assert not backend.owns_lease("/page", "owner-b")
    backend.close()


def test_sqlite_old_database_gains_monotonic_columns(tmp_path):
    path = tmp_path / "cache.sqlite3"
    old = SQLiteCacheBackend(path)
    assert old.insert("/page", b"<p>ok</p>", 60, 60)
    old._conn.execute("ALTER TABLE entries DROP COLUMN generated_mono")
    old._conn.execute("ALTER TABLE leases DROP COLUMN expires_mono")
    old._conn.commit()
    old.close()
    # Reopening migrates the old file; the pre-existing row is still readable.
    new = SQLiteCacheBackend(path)
    assert new.lookup("/page") == ("fresh", b"<p>ok</p>")
    assert new.insert("/other", b"<p>ok</p>", 60, 60)
    assert new.lookup("/other") == ("fresh", b"<p>ok</p>")
    new.close()


def test_sqlite_capacity_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("pynext.cache.MAX_SQLITE_ENTRIES", 5)
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.insert("/first", b"1", 60, 60)
    time.sleep(0.02)
    for i in range(7):
        assert backend.insert(f"/k{i}", b"x", 60, 60)
    count = backend._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert count == 5
    assert backend.lookup("/first") == ("miss", None)
    assert backend.lookup("/k6") == ("fresh", b"x")
    backend.close()


def test_sqlite_expired_rows_are_purged_on_insert(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    assert backend.insert("/old", b"old", 0.05, 0)
    time.sleep(0.15)
    assert backend.insert("/new", b"new", 60, 60)
    assert backend.lookup("/old") == ("miss", None)
    assert backend.lookup("/new") == ("fresh", b"new")
    backend.close()


def test_sqlite_prune_keeps_row_just_inserted(tmp_path, monkeypatch):
    # Regression test: with a very short TTL, a scheduling stall between
    # the insert's timestamp and its own prune must not let the prune
    # delete the row it just wrote (first lookup would see "miss"
    # instead of the entry).
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    real_time = time.time
    real_mono = time.monotonic
    calls = {"time": 0, "mono": 0}

    def fake_time():
        calls["time"] += 1
        return real_time() + (0.0 if calls["time"] <= 1 else 1.0)

    def fake_mono():
        calls["mono"] += 1
        return real_mono() + (0.0 if calls["mono"] <= 1 else 1.0)

    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "monotonic", fake_mono)
    assert backend.insert("/page", b"<p>ok</p>", 0.05, 0.05)
    monkeypatch.undo()
    assert backend.lookup("/page") == ("fresh", b"<p>ok</p>")
    backend.close()
