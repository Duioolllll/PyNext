from conftest import page_html
"""Shared (SQLite) ISR cache backend and two-process ISR coordination.

Finding #42 / spec v0.2 section 10.3 and section 15 M4: one shared
cache adapter plus multi-worker ISR coordination, with no external
service. httpx clients use ``trust_env=False`` so the sandbox proxy
environment (whose bracketed IPv6 no_proxy entries httpx cannot parse)
cannot break local test requests.
"""
import asyncio
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pynext import Response, create_app
from pynext.build import write_manifest
from pynext.cache import ISRCache, SQLiteCacheBackend

POLICY = {"revalidate": 30.0, "stale": 60.0}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _pynext_bin():
    return str(Path(sys.executable).parent / "pynext")


def _counter_lines(counter):
    if not counter.exists():
        return []
    return [line for line in counter.read_text(encoding="utf-8").splitlines() if line]


def _make_project(root, revalidate=30, stale=60, delay=0.6):
    counter = root / "generations.txt"
    directory = root / "app"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        "import asyncio, uuid\n"
        "from pynext import h, page\n"
        f"@page(render=\"isr\", revalidate={revalidate}, stale={stale})\n"
        "async def Page(params):\n"
        f"    await asyncio.sleep({delay})\n"
        "    token = uuid.uuid4().hex[:8]\n"
        f"    with open({str(counter)!r}, \"a\", encoding=\"utf-8\") as handle:\n"
        "        handle.write(token + \"\\n\")\n"
        "    return h(\"p\", {}, token)\n"
    )
    write_manifest(root)
    return counter


class _Server:
    def __init__(self, project, port, cache_path):
        self.port = port
        self.proc = subprocess.Popen(
            [_pynext_bin(), "start", "--dir", str(project), "--host", "127.0.0.1",
             "--port", str(port), "--cache-backend", "sqlite",
             "--cache-path", str(cache_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert _wait_port(port), f"server on {port} did not start"

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@pytest.fixture
def servers(tmp_path):
    started = []

    def start(project, cache_path):
        server = _Server(project, _free_port(), cache_path)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


async def _until(condition, timeout=10):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


def test_sqlite_backend_ttl_transitions_and_fencing(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.lookup("k") == ("miss", None)
        assert backend.insert("k", b"one", 0.2, 0.3)
        assert backend.lookup("k") == ("fresh", b"one")
        entry = backend.lookup_entry("k")
        assert entry.status == "fresh" and entry.version == 1
        time.sleep(0.25)
        assert backend.lookup("k") == ("stale", b"one")
        # A fenced write with the wrong version must not overwrite.
        assert not backend.insert_fenced("k", b"stale-writer", 30, 30, expected_version=99)
        assert backend.lookup("k") == ("stale", b"one")
        # The observed version wins, and bumps the version.
        assert backend.insert_fenced("k", b"two", 30, 30, expected_version=1)
        assert backend.lookup("k") == ("fresh", b"two")
        assert backend.lookup_entry("k").version == 2
        # First write to a missing key fences on version 0.
        assert backend.insert_fenced("new", b"n", 30, 30, expected_version=0)
        assert not backend.insert_fenced("new", b"n2", 30, 30, expected_version=0)
        assert backend.lookup("new") == ("fresh", b"n")
        assert backend.insert("expiring", b"e", 0.2, 0.3)
        time.sleep(0.6)
        assert backend.lookup("expiring")[0] == "expired"
        assert backend.lookup("expiring") == ("miss", None)
        assert backend.remove("new")
        assert not backend.remove("new")
    finally:
        backend.close()


def test_sqlite_lease_is_single_flight_across_connections(tmp_path):
    path = tmp_path / "cache.sqlite3"
    first = SQLiteCacheBackend(path)
    second = SQLiteCacheBackend(path)
    try:
        assert first.insert("k", b"one", 30, 30)
        assert first.acquire_lease("k", "worker-a") == 1
        assert second.acquire_lease("k", "worker-b") is None
        assert not second.release_lease("k", "worker-b")
        assert first.release_lease("k", "worker-a")
        assert second.acquire_lease("k", "worker-b") == 1
        assert second.release_lease("k", "worker-b")
        # An expired lease can be taken over, so a crashed worker cannot
        # block regeneration forever.
        assert first.acquire_lease("k", "worker-a", ttl=0.05) == 1
        time.sleep(0.1)
        assert second.acquire_lease("k", "worker-b") == 1
    finally:
        first.close()
        second.close()


def test_expired_lookup_does_not_delete_concurrent_fresh_write(tmp_path):
    path = tmp_path / "cache.sqlite3"
    first = SQLiteCacheBackend(path)
    second = SQLiteCacheBackend(path)
    try:
        assert first.insert("k", b"old", 1, 0)
        first._conn.execute("UPDATE entries SET generated_at = 0 WHERE key = ?", ("k",))
        first._conn.commit()

        class InterleavedConnection:
            def __init__(self, connection):
                self.connection = connection
                self.ran = False

            def execute(self, sql, *args):
                if sql.startswith("DELETE FROM entries") and not self.ran:
                    self.ran = True
                    assert second.insert("k", b"new", 30, 0)
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        first._conn = InterleavedConnection(first._conn)
        assert first.lookup("k") == ("fresh", b"new")
        assert second.lookup("k") == ("fresh", b"new")
    finally:
        first.close()
        second.close()


def test_expired_entry_keeps_version_while_live_lease_regenerates(tmp_path):
    backend = SQLiteCacheBackend(tmp_path / "cache.sqlite3")
    try:
        assert backend.insert("k", b"old", 1, 0)
        assert backend.acquire_lease("k", "worker") == 1
        backend._conn.execute("UPDATE entries SET generated_at = 0 WHERE key = ?", ("k",))
        backend._conn.commit()
        assert backend.lookup("k")[0] in {"expired", "miss"}
        assert backend.insert_fenced("k", b"new", 30, 0, 1, "worker")
        assert backend.lookup("k") == ("fresh", b"new")
    finally:
        backend.close()


def test_shared_cache_rejects_private_responses(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=tmp_path / "cache.sqlite3")
        try:
            async def public_render():
                return Response(b"<p>public</p>")

            async def cookie_render():
                return Response(b"<p>cookie</p>", headers=[(b"set-cookie", b"session=x")])

            async def private_render():
                return Response(b"<p>private</p>", headers=[(b"cache-control", b"private, no-store")])

            assert await cache.get_or_render("/public", POLICY, public_render) == b"<p>public</p>"
            assert cache.shared.lookup("/public") == ("fresh", b"<p>public</p>")
            assert await cache.get_or_render("/cookie", POLICY, cookie_render) == b"<p>cookie</p>"
            assert cache.shared.lookup("/cookie") == ("miss", None)
            assert cache.store.lookup("/cookie")[0] == "miss"
            assert await cache.get_or_render("/private", POLICY, private_render) == b"<p>private</p>"
            assert cache.shared.lookup("/private") == ("miss", None)

            async def conflicting_headers():
                return Response(b"<p>secret</p>", headers=[
                    (b"cache-control", b"no-store"), (b"cache-control", b"public")])

            assert await cache.get_or_render("/secret", POLICY, conflicting_headers) == b"<p>secret</p>"
            assert cache.shared.lookup("/secret") == ("miss", None)
            assert cache.store.lookup("/secret")[0] == "miss"
        finally:
            await cache.close()
    asyncio.run(run())


def test_shared_cache_failure_preserves_old_entry(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=tmp_path / "cache.sqlite3")
        try:
            assert cache.shared.insert("/old", b"<p>old</p>", 0.05, 30)
            await _until(lambda: cache.shared.lookup("/old")[0] == "stale")

            async def failing_render():
                raise RuntimeError("generation failed")

            # The stale entry is served while regeneration fails, and the
            # failed generation does not replace it.
            assert await cache.get_or_render("/old", POLICY, failing_render) == b"<p>old</p>"
            await _until(lambda: "/old" not in cache._inflight)
            assert cache.shared.lookup("/old") == ("stale", b"<p>old</p>")
        finally:
            await cache.close()
    asyncio.run(run())


class _BrokenBackend:
    def lookup_entry(self, key):
        raise RuntimeError("backend down")

    def insert_fenced(self, *args):
        raise RuntimeError("backend down")

    def acquire_lease(self, *args):
        raise RuntimeError("backend down")

    def release_lease(self, *args):
        raise RuntimeError("backend down")

    def remove(self, key):
        raise RuntimeError("backend down")

    def close(self):
        pass


def test_broken_shared_backend_degrades_to_local_generation(tmp_path):
    async def run():
        cache = ISRCache()
        cache.shared = _BrokenBackend()
        try:
            async def render():
                return Response(b"<p>ok</p>")

            assert await asyncio.wait_for(cache.get_or_render("/page", POLICY, render), 2) == b"<p>ok</p>"
            # The page was still served and cached locally.
            assert cache.store.lookup("/page") == ("fresh", b"<p>ok</p>")
        finally:
            await cache.close()
    asyncio.run(run())


def test_shared_invalidate_removes_both_layers(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="sqlite", cache_path=tmp_path / "cache.sqlite3")
        try:
            async def render():
                return Response(b"<p>one</p>")

            assert await cache.get_or_render("/item", POLICY, render) == b"<p>one</p>"
            assert cache.shared.lookup("/item")[0] == "fresh"
            assert await cache.invalidate("/item") is True
            assert cache.store.lookup("/item")[0] == "miss"
            assert cache.shared.lookup("/item") == ("miss", None)
            assert await cache.invalidate("/item") is False
        finally:
            await cache.close()
    asyncio.run(run())


def test_other_worker_invalidation_is_seen_before_local_fresh_hit(tmp_path):
    async def run():
        path = tmp_path / "cache.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path)
        second = ISRCache(cache_backend="sqlite", cache_path=path)
        count = 0

        async def render():
            nonlocal count
            count += 1
            return Response(str(count))

        try:
            assert await first.get_or_render("/page", POLICY, render) == b"1"
            assert await second.get_or_render("/page", POLICY, render) == b"1"
            assert await second.invalidate("/page")
            assert await first.get_or_render("/page", POLICY, render) == b"2"
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


def test_build_namespaces_isolate_shared_entries(tmp_path):
    async def run():
        path = tmp_path / "cache.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path, namespace="build-a:")
        second = ISRCache(cache_backend="sqlite", cache_path=path, namespace="build-b:")
        async def render_a():
            return Response("version-a")
        async def render_b():
            return Response("version-b")
        try:
            assert await first.get_or_render("/page", POLICY, render_a) == b"version-a"
            assert await second.get_or_render("/page", POLICY, render_b) == b"version-b"
            assert await first.invalidate("/page")
            assert await second.get_or_render("/page", POLICY, render_b) == b"version-b"
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


def test_axum_shared_cache_checks_invalidation_before_fast_hit(tmp_path):
    from pynext.server import start_native

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import h, page\n'
        'count = 0\n'
        '@page(render="isr", revalidate=60)\n'
        'def Page():\n'
        '    global count\n'
        '    count += 1\n'
        '    return h("p", {}, str(count))\n'
    )
    write_manifest(tmp_path)
    path = tmp_path / "cache.sqlite3"
    first = create_app(tmp_path, production=True, cache_backend="sqlite", cache_path=path)
    second = create_app(tmp_path, production=True, cache_backend="sqlite", cache_path=path)

    async def run():
        server = start_native(first, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.get("/")).text == page_html("<p>1</p>")
                assert (await client.get("/")).text == page_html("<p>1</p>")
                assert await second.revalidate("/")
                assert (await client.get("/")).text == page_html("<p>2</p>")
        finally:
            await asyncio.to_thread(server.stop)
            await first.isr.close()
            await second.isr.close()
    asyncio.run(run())


def test_invalidation_retires_other_workers_inflight_task(tmp_path):
    async def run():
        path = tmp_path / "cache.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path)
        second = ISRCache(cache_backend="sqlite", cache_path=path)
        started, finish = asyncio.Event(), asyncio.Event()

        async def old():
            started.set()
            await finish.wait()
            return Response("old")

        async def new():
            return Response("new")

        task = asyncio.create_task(first.get_or_render("/page", POLICY, old))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert await second.invalidate("/page")
            assert await asyncio.wait_for(first.get_or_render("/page", POLICY, new), 5) == b"new"
            finish.set()
            assert await task == b"old"
            assert await second.get_or_render("/page", POLICY, new) == b"new"
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
            await first.close()
            await second.close()
    asyncio.run(run())


def test_invalid_cache_config_is_rejected_at_startup(tmp_path):
    with pytest.raises(ValueError, match="Unknown cache backend"):
        ISRCache(cache_backend="bogus")
    with pytest.raises(ValueError, match="cache_path is required"):
        ISRCache(cache_backend="sqlite")
    with pytest.raises(ValueError, match="is a directory"):
        ISRCache(cache_backend="sqlite", cache_path=tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(ValueError, match="Cannot create SQLite cache directory"):
        ISRCache(cache_backend="sqlite", cache_path=blocker / "cache.sqlite3")
    _make_project(tmp_path)
    with pytest.raises(ValueError, match="Unknown cache backend"):
        create_app(tmp_path, cache_backend="bogus")


def test_two_processes_share_first_generation(tmp_path, servers):
    counter = _make_project(tmp_path)
    cache_path = tmp_path / "shared.sqlite3"
    first = servers(tmp_path, cache_path)
    second = servers(tmp_path, cache_path)

    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            responses = await asyncio.gather(
                *(client.get(first.url + "/") for _ in range(3)),
                *(client.get(second.url + "/") for _ in range(3)),
            )
        assert all(r.status_code == 200 for r in responses)
        bodies = {r.text for r in responses}
        assert len(bodies) == 1
        # Only one worker generated the page; the other waited and served
        # the shared result.
        assert len(_counter_lines(counter)) == 1
        assert bodies == {page_html(f"<p>{_counter_lines(counter)[0]}</p>")}
    asyncio.run(run())


def test_late_lease_acquisition_reuses_fresh_result(tmp_path):
    async def run():
        cache_path = tmp_path / "shared.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=cache_path)
        second = ISRCache(cache_backend="sqlite", cache_path=cache_path)
        observed_miss = asyncio.Event()
        continue_after_generation = asyncio.Event()
        lookup = second._shared_entry
        calls = []

        async def delayed_first_lookup(key):
            entry = await lookup(key)
            if not observed_miss.is_set():
                assert entry is None
                observed_miss.set()
                await continue_after_generation.wait()
            return entry

        async def render_first():
            calls.append("first")
            return Response(b"first")

        async def render_second():
            calls.append("second")
            return Response(b"second")

        second._shared_entry = delayed_first_lookup
        delayed = asyncio.create_task(second.get_or_render("/page", POLICY, render_second))
        try:
            await asyncio.wait_for(observed_miss.wait(), 5)
            assert await first.get_or_render("/page", POLICY, render_first) == b"first"
            continue_after_generation.set()
            assert await asyncio.wait_for(delayed, 5) == b"first"
            assert calls == ["first"]
        finally:
            continue_after_generation.set()
            await asyncio.gather(delayed, return_exceptions=True)
            await first.close()
            await second.close()
    asyncio.run(run())


def test_other_worker_invalidation_fences_inflight_generation(tmp_path):
    async def run():
        path = tmp_path / "shared.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path)
        second = ISRCache(cache_backend="sqlite", cache_path=path)
        started, finish = asyncio.Event(), asyncio.Event()

        async def render():
            started.set()
            await finish.wait()
            return Response(b"old")

        task = asyncio.create_task(first.get_or_render("/page", POLICY, render))
        try:
            await asyncio.wait_for(started.wait(), 5)
            invalidated = await second.invalidate("/page")
            finish.set()
            assert await asyncio.wait_for(task, 5) == b"old"
            assert second.shared.lookup("/page") == ("miss", None)
            assert first.store.lookup("/page")[0] == "miss"
            assert invalidated is True
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
            await first.close()
            await second.close()
    asyncio.run(run())


def test_expired_lease_owner_cannot_publish_after_takeover(tmp_path):
    async def run():
        path = tmp_path / "shared.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path)
        second = ISRCache(cache_backend="sqlite", cache_path=path)
        old_started, new_started = asyncio.Event(), asyncio.Event()
        finish_old, finish_new = asyncio.Event(), asyncio.Event()

        async def old_render():
            old_started.set()
            await finish_old.wait()
            return Response(b"old")

        async def new_render():
            new_started.set()
            await finish_new.wait()
            return Response(b"new")

        old_task = asyncio.create_task(first.get_or_render("/page", POLICY, old_render))
        new_task = None
        try:
            await asyncio.wait_for(old_started.wait(), 5)
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE leases SET expires_at = 0 WHERE key = ?", ("/page",))
            new_task = asyncio.create_task(second.get_or_render("/page", POLICY, new_render))
            await asyncio.wait_for(new_started.wait(), 5)
            finish_old.set()
            assert await asyncio.wait_for(old_task, 5) == b"old"
            assert second.shared.lookup("/page") == ("miss", None)
            assert first.store.lookup("/page")[0] == "miss"
            finish_new.set()
            assert await asyncio.wait_for(new_task, 5) == b"new"
            assert second.shared.lookup("/page") == ("fresh", b"new")
        finally:
            finish_old.set()
            finish_new.set()
            await asyncio.gather(*(t for t in (old_task, new_task) if t), return_exceptions=True)
            await first.close()
            await second.close()
    asyncio.run(run())


def test_two_processes_stale_serving_and_single_regeneration(tmp_path, servers):
    counter = _make_project(tmp_path, revalidate=0.2, stale=60, delay=0.6)
    cache_path = tmp_path / "shared.sqlite3"
    first = servers(tmp_path, cache_path)
    second = servers(tmp_path, cache_path)

    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            initial = await client.get(first.url + "/")
            assert initial.status_code == 200
            token = initial.text
            assert len(_counter_lines(counter)) == 1
            await asyncio.sleep(0.4)
            stale = await asyncio.gather(
                *(client.get(first.url + "/") for _ in range(3)),
                *(client.get(second.url + "/") for _ in range(3)),
            )
            assert {r.text for r in stale} == {token}
            # Exactly one worker regenerates while the stale page is served.
            await _until(lambda: len(_counter_lines(counter)) == 2)
            await asyncio.sleep(0.1)
            assert len(_counter_lines(counter)) == 2
            fresh = await client.get(second.url + "/")
            assert fresh.text != token
            assert fresh.text == page_html(f"<p>{_counter_lines(counter)[1]}</p>")
    asyncio.run(run())


def test_two_processes_private_response_never_enters_shared_cache(tmp_path, servers):
    counter = _make_project(tmp_path)
    cache_path = tmp_path / "shared.sqlite3"
    first = servers(tmp_path, cache_path)
    second = servers(tmp_path, cache_path)

    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            public = await client.get(first.url + "/")
            assert public.status_code == 200
            assert len(_counter_lines(counter)) == 1
            private = await client.get(second.url + "/", headers={"cookie": "session=x"})
            assert private.status_code == 200
            assert private.headers["cache-control"] == "private, no-store"
            # The private render ran, but must not replace the shared entry.
            assert len(_counter_lines(counter)) == 2
            again = await client.get(second.url + "/")
            assert again.text == public.text
            assert len(_counter_lines(counter)) == 2
            with sqlite3.connect(cache_path) as conn:
                rows = conn.execute("SELECT key, body FROM entries").fetchall()
            assert len(rows) == 1 and rows[0][0].endswith(":/")
            assert rows[0][1] == public.text.encode()
    asyncio.run(run())


def test_shared_cache_survives_worker_restart(tmp_path, servers):
    counter = _make_project(tmp_path)
    cache_path = tmp_path / "shared.sqlite3"
    first = servers(tmp_path, cache_path)

    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.get(first.url + "/")
            assert response.status_code == 200
            assert len(_counter_lines(counter)) == 1
            first.stop()
            replacement = servers(tmp_path, cache_path)
            again = await client.get(replacement.url + "/")
            assert again.text == response.text
            assert len(_counter_lines(counter)) == 1
    asyncio.run(run())
