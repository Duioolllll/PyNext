"""Tag-based ISR invalidation: manifest + two-phase (stale/expired) + revalidate_tag()."""
import asyncio
import os
import sqlite3
import time
import uuid

import httpx
import pytest

from pynext import create_app, page
from pynext.build import build_id_for, write_manifest
from pynext.cache import ISRCache
from pynext.cache import SQLiteCacheBackend
from pynext.components import _validate_tag, _validate_tag_expire
from pynext import Response

POLICY = {"revalidate": 30.0, "stale": 60.0}


# --- component-level validation -------------------------------------------

def test_page_tags_are_validated_sorted_and_recorded():
    @page(render="isr", revalidate=30, stale=60, tags=["post:42", "author/7"])
    def Page(params):
        return None

    assert Page.__pynext_cache_tags__ == ("author/7", "post:42")


def test_page_tags_default_to_empty():
    @page(render="isr", revalidate=30, stale=60)
    def Page(params):
        return None

    assert Page.__pynext_cache_tags__ == ()


@pytest.mark.parametrize("tags", [
    [""],
    ["has space"],
    ["bad*char"],
    ["x" * 257],
    [42],
    ["dup", "dup"],
    ["ok"] * 65,
    "post:42",
    42,
])
def test_invalid_page_tags_are_rejected(tags):
    with pytest.raises(ValueError):
        page(render="isr", revalidate=30, stale=60, tags=tags)


@pytest.mark.parametrize("kwargs", [
    {"render": "ssr", "tags": ["post:42"]},
    {"render": "ssg", "tags": ["post:42"]},
])
def test_tags_are_isr_only(kwargs):
    with pytest.raises(ValueError):
        page(**kwargs)


@pytest.mark.parametrize("tag", ["", "a b", "a/b/c", "post:42", "a" * 256])
def test_validate_tag(tag):
    if tag in ("", "a b"):
        with pytest.raises(ValueError):
            _validate_tag(tag)
    else:
        assert _validate_tag(tag) == tag


@pytest.mark.parametrize("expire", [-1, True, "60", float("inf"), float("nan"), 604801])
def test_invalid_tag_expire_is_rejected(expire):
    with pytest.raises(ValueError):
        _validate_tag_expire(expire)


@pytest.mark.parametrize("expire", [None, 0, 60, 604800])
def test_valid_tag_expire_is_accepted(expire):
    assert _validate_tag_expire(expire) == expire


# --- SQLite backend --------------------------------------------------------

@pytest.fixture
def sqlite_backend(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    yield backend
    backend.close()


def test_sqlite_tag_immediate_expiry_and_cross_instance_visibility(tmp_path):
    first = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    second = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert first.insert("/a", b"one", 60, 60, tags=("post:42",))
        assert first.insert("/b", b"two", 60, 60, tags=("other",))
        assert first.lookup("/a") == ("fresh", b"one")
        assert first.revalidate_tag("post:42")
        # The invalidation is stored in the shared file, so a second
        # connection (another worker) sees it too.
        assert second.lookup("/a") == ("expired", None)
        assert second.lookup("/b") == ("fresh", b"two")
        assert first.lookup("/a") == ("miss", None)
    finally:
        first.close()
        second.close()


def test_sqlite_tag_stale_phase_then_expired(sqlite_backend):
    assert sqlite_backend.insert("/a", b"one", 60, 60, tags=("post:42",))
    assert sqlite_backend.revalidate_tag("post:42", expire=3600)
    assert sqlite_backend.lookup("/a") == ("stale", b"one")
    # Fast-forward past the stale window by moving the manifest row.
    conn = sqlite3.connect(sqlite_backend.path)
    conn.execute("UPDATE tags SET expired_at = stale_at")
    conn.commit()
    conn.close()
    assert sqlite_backend.lookup("/a") == ("expired", None)


def test_sqlite_tag_invalidation_is_not_retroactive(sqlite_backend):
    assert sqlite_backend.revalidate_tag("post:42")
    assert sqlite_backend.insert("/a", b"one", 60, 60, tags=("post:42",))
    assert sqlite_backend.lookup("/a") == ("fresh", b"one")


def test_sqlite_tag_expire_zero_expires_immediately(sqlite_backend):
    assert sqlite_backend.insert("/a", b"one", 60, 60, tags=("post:42",))
    assert sqlite_backend.revalidate_tag("post:42", expire=0)
    assert sqlite_backend.lookup("/a") == ("expired", None)


def test_sqlite_revalidate_tag_rejects_bad_input(sqlite_backend):
    with pytest.raises(ValueError):
        sqlite_backend.revalidate_tag("bad tag")
    with pytest.raises(ValueError):
        sqlite_backend.revalidate_tag("post:42", expire=-1)


def test_sqlite_insert_with_bad_tags_returns_false(sqlite_backend):
    assert not sqlite_backend.insert("/a", b"one", 60, 60, tags=("bad tag",))


def test_sqlite_backend_migrates_pre_tag_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE entries ("
        "  key TEXT PRIMARY KEY, body BLOB NOT NULL,"
        "  generated_at REAL NOT NULL, fresh_for REAL NOT NULL,"
        "  stale_for REAL NOT NULL, version INTEGER NOT NULL);"
        "CREATE TABLE leases ("
        "  key TEXT PRIMARY KEY, owner TEXT NOT NULL,"
        "  expires_at REAL NOT NULL, version INTEGER NOT NULL);"
        "CREATE TABLE variant_generations ("
        "  key TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO entries VALUES ('/old', ?, ?, 60, 60, 1)",
        (b"old-body", time.time()),
    )
    conn.commit()
    conn.close()
    backend = SQLiteCacheBackend(path)
    try:
        assert backend.lookup("/old") == ("fresh", b"old-body")
        assert backend.insert("/new", b"new-body", 60, 60, tags=("post:1",))
        assert backend.revalidate_tag("post:1")
        assert backend.lookup("/new") == ("expired", None)
        assert backend.lookup("/old") == ("fresh", b"old-body")
    finally:
        backend.close()


# --- ISRCache: memory backend ----------------------------------------------

def test_memory_tag_invalidation_forces_rerender():
    async def run():
        cache = ISRCache()
        count = 0

        async def render():
            nonlocal count
            count += 1
            return Response(f"v{count}")

        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v1"
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v1"
        assert count == 1
        assert await cache.revalidate_tag("post:42") is True
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v2"
        assert count == 2
        # An unrelated tag leaves the entry alone.
        assert await cache.revalidate_tag("other") is True
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v2"
        assert count == 2
        await cache.close()
    asyncio.run(run())


def test_memory_tag_stale_phase_serves_old_body_then_regenerates():
    async def run():
        cache = ISRCache()
        count = 0

        async def render():
            nonlocal count
            count += 1
            return Response(f"v{count}")

        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v1"
        assert await cache.revalidate_tag("post:42", expire=3600) is True
        # Still stale: the old body is served while a regeneration runs.
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v1"
        async with asyncio.timeout(5):
            while count < 2:
                await asyncio.sleep(0.01)
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v2"
        await cache.close()
    asyncio.run(run())


def test_memory_tag_invalidation_is_not_retroactive():
    async def run():
        cache = ISRCache()
        count = 0

        async def render():
            nonlocal count
            count += 1
            return Response(f"v{count}")

        assert await cache.revalidate_tag("post:42") is True
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v1"
        assert await cache.get_or_render("/a", POLICY, render, tags=("post:42",)) == b"v1"
        assert count == 1
        await cache.close()
    asyncio.run(run())


def test_memory_tag_invalidation_retires_inflight_generation():
    async def run():
        cache = ISRCache()
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_render():
            started.set()
            await release.wait()
            return Response("old")

        async def new_render():
            return Response("new")

        task = asyncio.create_task(
            cache.get_or_render("/item", POLICY, slow_render, tags=("post:1",)))
        await asyncio.wait_for(started.wait(), 3)
        assert await cache.revalidate_tag("post:1") is True
        release.set()
        assert await task == b"old"
        # The retired generation must not have stored its stale result.
        assert cache.store.lookup("/item") == ("miss", None)
        assert await cache.get_or_render("/item", POLICY, new_render, tags=("post:1",)) == b"new"
        await cache.close()
    asyncio.run(run())


def test_memory_revalidate_tag_rejects_bad_input():
    async def run():
        cache = ISRCache()
        with pytest.raises(ValueError):
            await cache.revalidate_tag("bad tag")
        with pytest.raises(ValueError):
            await cache.revalidate_tag("post:42", expire=-1)
        with pytest.raises(ValueError):
            await cache.get_or_render("/a", POLICY, lambda: None, tags=("bad tag",))
        await cache.close()
    asyncio.run(run())


# --- ISRCache: SQLite backend (shared across workers) ----------------------

def test_sqlite_workers_share_tag_invalidation(tmp_path):
    async def run():
        path = tmp_path / "cache.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path)
        second = ISRCache(cache_backend="sqlite", cache_path=path)
        counts = {"first": 0, "second": 0}

        def render_for(name):
            async def render():
                counts[name] += 1
                return Response(f"{name}:{counts[name]}")
            return render

        assert await first.get_or_render("/a", POLICY, render_for("first"),
                                         tags=("post:42",)) == b"first:1"
        # The second worker serves the first worker's entry without rendering.
        assert await second.get_or_render("/a", POLICY, render_for("second"),
                                          tags=("post:42",)) == b"first:1"
        assert counts == {"first": 1, "second": 0}
        assert await first.revalidate_tag("post:42") is True
        assert await second.get_or_render("/a", POLICY, render_for("second"),
                                          tags=("post:42",)) == b"second:1"
        assert counts == {"first": 1, "second": 1}
        await first.close()
        await second.close()
    asyncio.run(run())


def test_sqlite_slow_generation_started_before_invalidation_is_not_fresh(tmp_path):
    """Regression: a generation that started before a ``revalidate_tag``
    but stored after it must not come back as fresh. Its content
    predates the invalidation, so the tag check must use the render
    *start* time, not the insert time — otherwise the other worker's
    invalidation is silently undone by the late store."""
    async def run():
        path = tmp_path / "cache.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path)
        second = ISRCache(cache_backend="sqlite", cache_path=path)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_render():
            started.set()
            await release.wait()
            return Response("old")

        async def new_render():
            return Response("new")

        # Worker B starts a slow render.
        task = asyncio.create_task(
            second.get_or_render("/a", POLICY, slow_render, tags=("post:42",)))
        await asyncio.wait_for(started.wait(), 3)
        # Worker A invalidates while B is still rendering.
        assert await first.revalidate_tag("post:42") is True
        release.set()
        assert await task == b"old"
        # B's stored entry predates the invalidation, so the next
        # lookup must not serve it as fresh — it regenerates.
        assert await second.get_or_render("/a", POLICY, new_render, tags=("post:42",)) == b"new"
        await first.close()
        await second.close()
    asyncio.run(run())


def test_memory_tag_manifest_is_pruned():
    """The process-local tag manifest must not grow without bound:
    rows older than any live entry (see the SQLite backend's prune)
    are dropped when a new invalidation lands."""
    from pynext.cache import MAX_CACHE_SECONDS

    async def run():
        cache = ISRCache()
        old = time.time() - 3 * MAX_CACHE_SECONDS
        cache._tag_manifest["old:1"] = (old, old)
        cache._tag_manifest["old:2"] = (old, old + 1)
        assert await cache.revalidate_tag("post:42") is True
        assert "old:1" not in cache._tag_manifest
        assert "old:2" not in cache._tag_manifest
        assert "post:42" in cache._tag_manifest
        await cache.close()
    asyncio.run(run())


# --- Redis backend (skipped without PYNEXT_TEST_REDIS_URL) -----------------

URL = os.environ.get("PYNEXT_TEST_REDIS_URL")


@pytest.mark.skipif(not URL, reason="PYNEXT_TEST_REDIS_URL is not configured")
def test_redis_tag_invalidation():
    from pynext.redis_cache import RedisCacheBackend

    cache = RedisCacheBackend(URL)
    key = "/pynext-test-" + uuid.uuid4().hex
    tag = "post:" + uuid.uuid4().hex
    try:
        assert cache.insert(key, b"one", 60, 60, tags=(tag,))
        assert cache.lookup(key) == ("fresh", b"one")
        # Redis tag records use millisecond timestamps: without a pause
        # the insert and this invalidation can land in the same
        # millisecond, making expired_at == generated_at so the entry
        # reads "fresh" instead of "expired". The premise here is that
        # the entry was generated strictly before the invalidation.
        time.sleep(0.005)
        assert cache.revalidate_tag(tag)
        assert cache.lookup(key) == ("expired", None)
        # Entries generated after the invalidation are unaffected.
        assert cache.insert(key, b"two", 60, 60, tags=(tag,))
        assert cache.lookup(key) == ("fresh", b"two")
        # Stale phase: served once more, then expired.
        # Redis tag records use millisecond timestamps: without a pause
        # the insert and this invalidation can land in the same
        # millisecond, making stale_at == generated_at so the entry
        # reads "fresh" instead of "stale". The premise here is that
        # the entry was generated strictly before the invalidation.
        time.sleep(0.005)
        assert cache.revalidate_tag(tag, expire=3600)
        assert cache.lookup(key) == ("stale", b"two")
        assert cache.revalidate_tag(tag, expire=0)
        assert cache.lookup(key) == ("expired", None)
        with pytest.raises(ValueError):
            cache.revalidate_tag("bad tag")
    finally:
        cache.remove(key)
        cache.client.delete(cache._tag_key(tag))
        cache.close()


# --- App level --------------------------------------------------------------

def _write_page(root, relative, tags, name="Page"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    tags_repr = "" if not tags else f", tags={tags!r}"
    path.write_text(
        "from pynext import h, page\n"
        "calls = 0\n"
        "@page(render=\"isr\", revalidate=30, stale=60" + tags_repr + ")\n"
        f"async def {name}(params):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return h(\"p\", {}, f\"v{calls}\")\n"
    )
    return path


def test_app_revalidate_tag_invalidates_tagged_pages_only(tmp_path):
    _write_page(tmp_path, "app/blog/page.py", ("post:42",))
    _write_page(tmp_path, "app/news/page.py", ("post:42",))
    _write_page(tmp_path, "app/about/page.py", ())
    manifest = write_manifest(tmp_path)
    assert manifest["isr_tags"] == {
        "/blog": ["post:42"],
        "/news": ["post:42"],
        "/about": [],
    }

    async def run():
        app = create_app(tmp_path, production=True)
        blog = app._module("app/blog/page.py")
        news = app._module("app/news/page.py")
        about = app._module("app/about/page.py")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/blog")).status_code == 200
            assert (await client.get("/news")).status_code == 200
            assert (await client.get("/about")).status_code == 200
            assert (blog.calls, news.calls, about.calls) == (1, 1, 1)
            assert (await client.get("/blog")).text.count("v1") == 1
            assert (blog.calls, news.calls, about.calls) == (1, 1, 1)
            assert await app.revalidate_tag("post:42") is True
            assert (await client.get("/blog")).text.count("v2") == 1
            assert (await client.get("/news")).text.count("v2") == 1
            # The untagged page is still served from cache.
            assert (await client.get("/about")).text.count("v1") == 1
            assert (blog.calls, news.calls, about.calls) == (2, 2, 1)
            with pytest.raises(ValueError):
                await app.revalidate_tag("bad tag")
    asyncio.run(run())


def test_app_rejects_tag_change_without_rebuild(tmp_path):
    import json

    _write_page(tmp_path, "app/blog/page.py", ("post:42",))
    write_manifest(tmp_path)
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    saved["isr_tags"]["/blog"] = ["post:43"]
    raw = json.dumps(saved).encode("utf-8")
    manifest_path.write_bytes(raw)
    # Re-sign the edited manifest so the BUILD_ID integrity check passes
    # and the tag check is the one that fires.
    (tmp_path / ".pynext" / "BUILD_ID").write_text(build_id_for(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="ISR tags changed since build"):
        create_app(tmp_path, production=True)


def test_app_rejects_invalid_tags_at_load(tmp_path):
    source = _write_page(tmp_path, "app/blog/page.py", ("post:42",))
    source.write_text(source.read_text(encoding="utf-8") + "\nPage.__pynext_cache_tags__ = (\"bad tag\",)\n",
                      encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ISR tags"):
        create_app(tmp_path)
