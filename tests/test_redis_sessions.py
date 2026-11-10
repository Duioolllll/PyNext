"""Redis session store: fake-backend unit tests plus real-Redis integration.

The fake client below implements the exact ``redis-py`` surface the store
uses (``get``/``set``/``delete``/``pipeline`` with WATCH/MULTI), including
per-key versions so optimistic-lock conflicts are observable without a
server. Tests that need a real server are skipped unless
``PYNEXT_TEST_REDIS_URL`` is set (the CI redis job sets it).
"""
import asyncio
import os
import secrets
import threading
import time
import uuid

import httpx
import pytest

from pynext import RedisSessionStore, SecurityConfig, SessionMiddleware, create_app
from pynext.server import start_native
from pynext.sessions import SessionConflict

URL = os.environ.get("PYNEXT_TEST_REDIS_URL")
REAL_REDIS = pytest.mark.skipif(not URL, reason="PYNEXT_TEST_REDIS_URL is not configured")


class WatchError(Exception):
    """Stands in for ``redis.exceptions.WatchError`` in the fake client."""


class _FakeRedis:
    """Minimal in-memory Redis with TTL and per-key versions for WATCH."""

    def __init__(self):
        self._data = {}  # key -> [value: bytes, version: int, expire_at: float | None]
        self._lock = threading.Lock()

    def _purge_unlocked(self, key):
        row = self._data.get(key)
        if row is not None and row[2] is not None and time.monotonic() >= row[2]:
            del self._data[key]
            return True
        return False

    def _get_unlocked(self, key):
        self._purge_unlocked(key)
        row = self._data.get(key)
        return None if row is None else row[0]

    def _set_unlocked(self, key, value, *, nx=False, xx=False, px=None, ex=None):
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._purge_unlocked(key)
        exists = key in self._data
        if nx and exists:
            return None
        if xx and not exists:
            return None
        expire_at = None
        if px is not None:
            expire_at = time.monotonic() + px / 1000
        elif ex is not None:
            expire_at = time.monotonic() + ex
        version = self._data[key][1] + 1 if exists else 1
        self._data[key] = [value, version, expire_at]
        return True

    def _delete_unlocked(self, *keys):
        count = 0
        for key in keys:
            self._purge_unlocked(key)
            if key in self._data:
                del self._data[key]
                count += 1
        return count

    def get(self, key):
        with self._lock:
            return self._get_unlocked(key)

    def set(self, key, value, nx=False, xx=False, px=None, ex=None):
        with self._lock:
            return self._set_unlocked(key, value, nx=nx, xx=xx, px=px, ex=ex)

    def delete(self, *keys):
        with self._lock:
            return self._delete_unlocked(*keys)

    def pipeline(self):
        return _FakePipeline(self)

    def close(self):
        pass


class _FakePipeline:
    def __init__(self, client):
        self._client = client
        self._watched = None
        self._queued = []
        self._in_multi = False

    def watch(self, *keys):
        with self._client._lock:
            snapshot = {}
            for key in keys:
                self._client._purge_unlocked(key)
                row = self._client._data.get(key)
                snapshot[key] = None if row is None else row[1]
            self._watched = snapshot
        return True

    def get(self, key):
        if self._in_multi:
            raise RuntimeError("get() inside MULTI is not supported by the fake")
        return self._client.get(key)

    def multi(self):
        self._in_multi = True
        self._queued = []

    def set(self, key, value, px=None, ex=None, nx=False, xx=False):
        if self._in_multi:
            self._queued.append(("set", key, value, px, ex))
            return self
        return self._client.set(key, value, nx=nx, xx=xx, px=px, ex=ex)

    def delete(self, *keys):
        if self._in_multi:
            self._queued.append(("delete", keys))
            return self
        return self._client.delete(*keys)

    def execute(self):
        with self._client._lock:
            if self._watched is not None:
                for key, version in self._watched.items():
                    self._client._purge_unlocked(key)
                    row = self._client._data.get(key)
                    current = None if row is None else row[1]
                    if current != version:
                        raise WatchError("watched key changed")
            results = []
            for op in self._queued:
                if op[0] == "set":
                    _, key, value, px, ex = op
                    results.append(self._client._set_unlocked(key, value, px=px, ex=ex))
                else:
                    results.append(self._client._delete_unlocked(*op[1]))
            self._queued = []
            self._in_multi = False
            self._watched = None
            return results

    def reset(self):
        self._queued = []
        self._in_multi = False
        self._watched = None


def _fake_store():
    return RedisSessionStore(client=_FakeRedis())


def test_redis_session_store_roundtrip_fencing_and_expiry_with_fake():
    store = _fake_store()

    async def run():
        token = secrets.token_urlsafe(32)
        assert await store.save(token, {"value": 1}, "csrf-1", None, time.time() + 60) == 1
        data, csrf, version = await store.load(token)
        assert (data, csrf, version) == ({"value": 1}, "csrf-1", 1)
        # A second insert for the same token is a conflict, like SQLite.
        with pytest.raises(SessionConflict):
            await store.save(token, {"value": 9}, None, None, time.time() + 60)
        assert await store.save(token, {"value": 2}, "csrf-2", 1, time.time() + 60) == 2
        assert (await store.load(token))[0] == {"value": 2}
        # Stale versions are fenced, and the losing write changes nothing.
        with pytest.raises(SessionConflict):
            await store.save(token, {"value": 3}, None, 1, time.time() + 60)
        assert (await store.load(token))[0] == {"value": 2}
        # Rotation moves the session to a fresh token at version 1.
        new_token = secrets.token_urlsafe(32)
        assert await store.save(new_token, {"value": 2}, "csrf-3", 2,
                                time.time() + 60, previous_token=token) == 1
        assert await store.load(token) is None
        assert (await store.load(new_token))[2] == 1
        with pytest.raises(SessionConflict):
            await store.save(secrets.token_urlsafe(32), {}, None, 1,
                             time.time() + 60, previous_token=token)
        # Expiry: already-expired saves are invisible, and TTL removes live keys.
        expired = secrets.token_urlsafe(32)
        assert await store.save(expired, {}, None, None, time.time() - 1) == 1
        assert await store.load(expired) is None
        short = secrets.token_urlsafe(32)
        await store.save(short, {"value": 1}, None, None, time.time() + 0.2)
        assert await store.load(short) is not None
        await asyncio.sleep(0.3)
        assert await store.load(short) is None
        # Redis expires keys with TTL, so there is nothing to purge.
        assert await store.purge_expired() == 0
        await store.delete(new_token)
        assert await store.load(new_token) is None
        with pytest.raises(SessionConflict):
            await store.save(new_token, {}, None, 1, time.time() + 60)
        with pytest.raises(ValueError, match="exceeds 16384 bytes"):
            await store.save(secrets.token_urlsafe(32), {"big": "x" * 16384}, None, None,
                             time.time() + 60)
    asyncio.run(run())


def test_redis_session_store_rejects_bad_configuration():
    with pytest.raises(ValueError, match="redis://"):
        RedisSessionStore("http://127.0.0.1:6379/0")
    with pytest.raises(ValueError, match="key prefix"):
        RedisSessionStore(client=_FakeRedis(), key_prefix="")
    with pytest.raises(TypeError, match="get, set, delete and pipeline"):
        RedisSessionStore(client=object())
    store = RedisSessionStore(client=_FakeRedis(), key_prefix="test-sessions")
    assert store.key_prefix == "test-sessions"


def test_middleware_purge_interval_validation():
    store = _fake_store()
    with pytest.raises(ValueError, match="purge_interval"):
        SessionMiddleware(store, purge_interval=0)
    with pytest.raises(ValueError, match="purge_interval"):
        SessionMiddleware(store, purge_interval=-5)
    assert SessionMiddleware(store, purge_interval=None).purge_interval is None


def _build_session_api(root):
    api = root / "app" / "api"
    (api / "token").mkdir(parents=True)
    (api / "token" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"token": request.state["session"].csrf_token()})\n'
    )
    (api / "login").mkdir()
    (api / "login" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    session = request.state["session"]\n'
        '    session["user_id"] = "alice"\n'
        '    session.rotate()\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "private").mkdir()
    (api / "private" / "route.py").write_text(
        'from pynext import JSONResponse, require_auth, route\n@route("GET")\n@require_auth\n'
        'def get(request): return JSONResponse({"user": request.state["session"]["user_id"]})\n'
    )
    (api / "logout").mkdir()
    (api / "logout" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n    request.state["session"].invalidate()\n    return JSONResponse({"ok": True})\n'
    )


def _make_app(root, store):
    return create_app(root, middleware=[SessionMiddleware(store, secure=False)],
                      security=SecurityConfig(csrf_enabled=True))


def test_redis_sessions_shared_across_two_nodes_with_fake(tmp_path):
    """Two app instances sharing one Redis see the same login/rotation/logout."""
    _build_session_api(tmp_path)
    client = _FakeRedis()
    app1 = _make_app(tmp_path, RedisSessionStore(client=client))
    app2 = _make_app(tmp_path, RedisSessionStore(client=client))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app1),
                                         base_url="http://test") as first:
                assert (await first.get("/api/private")).status_code == 401
                token = (await first.get("/api/token")).json()["token"]
                old_cookie = first.cookies.get("pynext_session")
                assert (await first.post("/api/login",
                                         headers={"x-csrf-token": "wrong"})).status_code == 403
                assert (await first.post("/api/login",
                                         headers={"x-csrf-token": token})).status_code == 200
                new_cookie = first.cookies.get("pynext_session")
                assert new_cookie != old_cookie
                # The second node reads the session the first node wrote.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                             base_url="http://test",
                                             cookies={"pynext_session": new_cookie}) as second:
                    assert (await second.get("/api/private")).json() == {"user": "alice"}
                    assert (await second.get("/api/private")).status_code == 200
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                             base_url="http://test",
                                             cookies={"pynext_session": old_cookie}) as stale:
                    assert (await stale.get("/api/private")).status_code == 401
                token = (await first.get("/api/token")).json()["token"]
                assert (await first.post("/api/logout",
                                         headers={"x-csrf-token": token})).status_code == 200
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                             base_url="http://test",
                                             cookies={"pynext_session": new_cookie}) as gone:
                    assert (await gone.get("/api/private")).status_code == 401
        finally:
            await app1.isr.close()
            await app2.isr.close()
    asyncio.run(run())


class _CountingStore:
    """Store that only counts purge calls, for the janitor test."""

    def __init__(self):
        self.purges = 0

    async def load(self, token):
        return None

    async def save(self, token, data, csrf, version, expires, *, previous_token=None):
        return 1

    async def delete(self, token):
        pass

    async def purge_expired(self):
        self.purges += 1
        return 0


def test_middleware_runs_purge_in_background_and_close_stops_it(tmp_path):
    _build_session_api(tmp_path)
    store = _CountingStore()
    middleware = SessionMiddleware(store, secure=False, purge_interval=0.05)
    app = create_app(tmp_path, middleware=[middleware],
                     security=SecurityConfig(csrf_enabled=True))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/api/token")).status_code == 200
            await asyncio.sleep(0.2)
            assert store.purges >= 1
            await middleware.close()
            seen = store.purges
            await asyncio.sleep(0.15)
            assert store.purges == seen
        finally:
            await middleware.close()
            await app.isr.close()
    asyncio.run(run())


@REAL_REDIS
def test_redis_session_store_fences_and_expires_on_real_redis():
    pytest.importorskip("redis")
    store = RedisSessionStore(URL, key_prefix="test-sessions-" + uuid.uuid4().hex)

    async def run():
        token = secrets.token_urlsafe(32)
        assert await store.save(token, {"value": 1}, "csrf-1", None, time.time() + 60) == 1
        assert (await store.load(token)) == ({"value": 1}, "csrf-1", 1)
        assert await store.save(token, {"value": 2}, None, 1, time.time() + 60) == 2
        with pytest.raises(SessionConflict):
            await store.save(token, {"value": 3}, None, 1, time.time() + 60)
        short = secrets.token_urlsafe(32)
        await store.save(short, {}, None, None, time.time() + 0.3)
        assert await store.load(short) is not None
        await asyncio.sleep(0.4)
        assert await store.load(short) is None
        assert await store.purge_expired() == 0
        await store.delete(token)
        assert await store.load(token) is None
    try:
        asyncio.run(run())
    finally:
        store.close()


@REAL_REDIS
@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_redis_sessions_two_nodes_login_rotation_logout(tmp_path, mode):
    """Login on node 1, read on node 2, logout on node 1: node 2 must forget."""
    pytest.importorskip("redis")
    _build_session_api(tmp_path)
    prefix = "test-sessions-" + uuid.uuid4().hex
    store1 = RedisSessionStore(URL, key_prefix=prefix)
    store2 = RedisSessionStore(URL, key_prefix=prefix)
    app1 = _make_app(tmp_path, store1)
    app2 = _make_app(tmp_path, store2)

    async def run():
        servers = []
        if mode == "asgi":
            first = {"transport": httpx.ASGITransport(app=app1), "base_url": "http://test"}
            second = {"transport": httpx.ASGITransport(app=app2), "base_url": "http://test"}
        else:
            servers = [start_native(app, asyncio.get_running_loop(), port=0) for app in (app1, app2)]
            first = {"base_url": f"http://{servers[0].address}", "trust_env": False}
            second = {"base_url": f"http://{servers[1].address}", "trust_env": False}
        try:
            async with httpx.AsyncClient(**first) as client:
                assert (await client.get("/api/private")).status_code == 401
                token = (await client.get("/api/token")).json()["token"]
                old_cookie = client.cookies.get("pynext_session")
                assert (await client.post("/api/login",
                                         headers={"x-csrf-token": "wrong"})).status_code == 403
                assert (await client.post("/api/login",
                                         headers={"x-csrf-token": token})).status_code == 200
                new_cookie = client.cookies.get("pynext_session")
                assert new_cookie != old_cookie
                async with httpx.AsyncClient(**second, cookies={"pynext_session": new_cookie}) as other:
                    assert (await other.get("/api/private")).json() == {"user": "alice"}
                async with httpx.AsyncClient(**second, cookies={"pynext_session": old_cookie}) as stale:
                    assert (await stale.get("/api/private")).status_code == 401
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/logout",
                                         headers={"x-csrf-token": token})).status_code == 200
                async with httpx.AsyncClient(**second, cookies={"pynext_session": new_cookie}) as gone:
                    assert (await gone.get("/api/private")).status_code == 401
        finally:
            for server in servers:
                await asyncio.to_thread(server.stop)
            await app1.isr.close()
            await app2.isr.close()
    try:
        asyncio.run(run())
    finally:
        store1.close()
        store2.close()


# ----------------------------------------------------------------------
# Review #4 #19: Redis session failure policy (mocked outage — no server).
# ----------------------------------------------------------------------
from pynext.sessions import SessionUnavailable  # noqa: E402


class _FailingRedis:
    """redis-py-shaped client whose every operation raises ConnectionError."""

    def get(self, *args, **kwargs):
        raise ConnectionError("redis down (mock)")

    def set(self, *args, **kwargs):
        raise ConnectionError("redis down (mock)")

    def delete(self, *args, **kwargs):
        raise ConnectionError("redis down (mock)")

    def pipeline(self, *args, **kwargs):
        raise ConnectionError("redis down (mock)")

    def close(self):
        pass


class _CorruptRedis(_FakeRedis):
    """Returns bytes that are not a valid session payload."""

    def get(self, key):
        return b"not a session payload"


class _WatchErrorRedis(_FakeRedis):
    """A store whose optimistic-lock execute always reports a conflict."""

    def pipeline(self):
        pipe = _FakePipeline(self)

        def failing_execute():
            raise WatchError("watched key changed")

        pipe.execute = failing_execute
        return pipe


def _outage_token():
    return secrets.token_urlsafe(32)


def test_redis_session_store_default_policy_is_fail_closed():
    """Review #4 #19: no policy was defined; outages raised raw errors.

    The default is now fail_closed: a Redis outage raises the specific
    SessionUnavailable (chaining the Redis error) instead of leaking a
    raw ConnectionError, and the request is answered 503, not 500.
    """
    store = RedisSessionStore(client=_FailingRedis())
    assert store.failure_policy == "fail_closed"

    async def run():
        with pytest.raises(SessionUnavailable) as excinfo:
            await store.load(_outage_token())
        assert isinstance(excinfo.value.__cause__, ConnectionError)
        with pytest.raises(SessionUnavailable):
            await store.save(_outage_token(), {"user_id": "alice"}, None, None,
                             time.time() + 60)
        with pytest.raises(SessionUnavailable):
            await store.delete(_outage_token())
    asyncio.run(run())


def test_redis_session_store_fail_open_starts_ephemeral_session():
    """fail_open degrades to a fresh ephemeral session: nothing persists."""
    store = RedisSessionStore(client=_FailingRedis(), failure_policy="fail_open")
    assert store.failure_policy == "fail_open"

    async def run():
        token = _outage_token()
        assert await store.load(token) is None
        # Save reports the version that would have been stored, but the
        # session is ephemeral: a later load still finds nothing.
        assert await store.save(token, {"user_id": "alice"}, None, None,
                                time.time() + 60) == 1
        assert await store.load(token) is None
        assert await store.save(token, {"user_id": "alice"}, None, 2,
                                time.time() + 60) == 3
        new_token = _outage_token()
        assert await store.save(new_token, {"user_id": "alice"}, None, 2,
                                time.time() + 60, previous_token=token) == 1
        # A failed delete under fail_open is queued for revocation on
        # recovery (S-P1-20), so delete() reports False instead of None.
        assert await store.delete(token) is False
        assert len(store.pending_revocations) == 1
    asyncio.run(run())


def test_redis_session_store_rejects_invalid_failure_policy():
    with pytest.raises(ValueError, match="failure policy"):
        RedisSessionStore(client=_FailingRedis(), failure_policy="sometimes")


def test_redis_session_outage_never_absorbs_conflict_or_corrupt_data():
    """Fencing conflicts and corrupt data are not outages (either policy)."""
    for policy in ("fail_closed", "fail_open"):
        store = RedisSessionStore(client=_WatchErrorRedis(), failure_policy=policy)

        async def run_conflict(store=store):
            token = _outage_token()
            assert await store.save(token, {"value": 1}, None, None, time.time() + 60) == 1
            with pytest.raises(SessionConflict):
                await store.save(token, {"value": 2}, None, 1, time.time() + 60)
        asyncio.run(run_conflict())

        corrupt = RedisSessionStore(client=_CorruptRedis(), failure_policy=policy)

        async def run_corrupt(corrupt=corrupt):
            with pytest.raises(ValueError, match="invalid session data"):
                await corrupt.load(_outage_token())
        asyncio.run(run_corrupt())


def test_redis_session_outage_middleware_answers_503_by_default(tmp_path):
    """fail_closed (default): a request during an outage gets 503, not 500.

    A request that presents a session cookie fails at load (503), and a
    request that would store session data fails at save (503). A request
    that never touches the store — no cookie, no session change, like
    the unauthenticated /api/private probe below — is unaffected and
    still answers 401 from @require_auth.
    """
    _build_session_api(tmp_path)
    app = _make_app(tmp_path, RedisSessionStore(client=_FailingRedis()))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                # No cookie, but the handler stores a CSRF token, so the
                # save fails: 503 (before the fix this was a raw 500).
                assert (await client.get("/api/token")).status_code == 503
                assert (await client.get("/api/private")).status_code == 401
                # A presented cookie fails at load: 503.
                cookie = secrets.token_urlsafe(32)
                assert (await client.get(
                    "/api/private",
                    headers={"Cookie": f"pynext_session={cookie}"})).status_code == 503
        finally:
            await app.isr.close()
    asyncio.run(run())


class _FailingWriteRedis(_FakeRedis):
    """Loads miss, but every write fails — isolates the save path."""

    def set(self, *args, **kwargs):
        raise ConnectionError("redis down on write (mock)")

    def delete(self, *args, **kwargs):
        raise ConnectionError("redis down on write (mock)")

    def pipeline(self, *args, **kwargs):
        raise ConnectionError("redis down on write (mock)")


def test_redis_session_save_outage_answers_503_even_when_load_succeeds(tmp_path):
    """A save failure alone (load healthy) is also a 503, not a 500."""
    _build_session_api(tmp_path)
    app = _make_app(tmp_path, RedisSessionStore(client=_FailingWriteRedis()))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/api/token")).status_code == 503
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_redis_session_outage_fail_open_keeps_reads_but_login_cannot_complete(tmp_path):
    """fail_open: reads proceed on an ephemeral session; login cannot finish.

    The CSRF token issued by GET /api/token is never stored, so the
    login POST fails CSRF (403) — session-bound CSRF cannot be
    established while the store is down — and /api/private stays 401.
    """
    _build_session_api(tmp_path)
    app = _make_app(tmp_path, RedisSessionStore(client=_FailingRedis(),
                                                failure_policy="fail_open"))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                token_response = await client.get("/api/token")
                assert token_response.status_code == 200
                token = token_response.json()["token"]
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": token})).status_code == 403
                assert (await client.get("/api/private")).status_code == 401
                # Logout is best-effort under fail_open, but it is still a
                # state-changing request, so it fails CSRF the same way:
                # no stored session means no stored CSRF token to match.
                assert (await client.post("/api/logout",
                                          headers={"x-csrf-token": token})).status_code == 403
        finally:
            await app.isr.close()
    asyncio.run(run())


# ----------------------------------------------------------------------
# S-P1-20 (M-11): Redis fail_open pending revocations.
# ----------------------------------------------------------------------

class _ToggleRedis(_FakeRedis):
    """Fake Redis that can be taken down and brought back."""

    def __init__(self):
        super().__init__()
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("redis down (mock)")

    def get(self, key):
        self._check()
        return super().get(key)

    def set(self, *args, **kwargs):
        self._check()
        return super().set(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check()
        return super().delete(*args, **kwargs)

    def pipeline(self):
        self._check()
        return super().pipeline()


def _token_hash(token):
    import hashlib
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def test_redis_fail_open_delete_queues_revocation_and_flush_revokes():
    """A logout during an outage is queued, not dropped (S-P1-20)."""
    client = _ToggleRedis()
    store = RedisSessionStore(client=client, failure_policy="fail_open")

    async def run():
        token = secrets.token_urlsafe(32)
        assert await store.save(token, {"user_id": "alice"}, None, None, time.time() + 3600) == 1
        assert await store.load(token) is not None
        client.down = True
        # The delete cannot reach Redis: report False and queue the hash.
        assert await store.delete(token) is False
        assert store.pending_revocations == (_token_hash(token),)
        # While down the token still loads as a miss (fail_open), and the
        # queue survives further failed operations.
        assert await store.load(token) is None
        assert await store.delete(token) is False
        assert store.pending_revocations == (_token_hash(token),)
        client.down = False
        revoked, pending = await store.flush_pending_revocations()
        assert (revoked, pending) == (1, 0)
        assert store.pending_revocations == ()
        assert await store.load(token) is None
        # A successful delete reports True and queues nothing.
        assert await store.delete(token) is True
        assert store.pending_revocations == ()
    asyncio.run(run())


def test_redis_fail_open_rotation_failure_queues_old_token():
    """A rotation that fails mid-outage queues the old id for revocation."""
    client = _ToggleRedis()
    store = RedisSessionStore(client=client, failure_policy="fail_open")

    async def run():
        old_token = secrets.token_urlsafe(32)
        assert await store.save(old_token, {"user_id": "alice"}, None, None, time.time() + 3600) == 1
        client.down = True
        new_token = secrets.token_urlsafe(32)
        # The rotation never happened: report the would-be version and
        # queue the old id instead of leaving it valid until its TTL.
        assert await store.save(new_token, {"user_id": "alice"}, None, 1,
                                time.time() + 3600, previous_token=old_token) == 1
        assert store.pending_revocations == (_token_hash(old_token),)
        client.down = False
        assert await store.flush_pending_revocations() == (1, 0)
        assert await store.load(old_token) is None
        # The new id was never stored: the session is ephemeral, as before.
        assert await store.load(new_token) is None
    asyncio.run(run())


def test_redis_pending_revocations_flush_before_first_read_after_recovery():
    """Recovery revokes before the next load, so the dead token gets no last read."""
    client = _ToggleRedis()
    store = RedisSessionStore(client=client, failure_policy="fail_open")

    async def run():
        token = secrets.token_urlsafe(32)
        assert await store.save(token, {"user_id": "alice"}, None, None, time.time() + 3600) == 1
        client.down = True
        assert await store.delete(token) is False
        client.down = False
        # The first load after recovery already misses: the flush runs
        # before the read, not after it.
        assert await store.load(token) is None
        assert store.pending_revocations == ()
    asyncio.run(run())


def test_redis_pending_revocation_queue_is_bounded():
    store = RedisSessionStore(client=_FailingRedis(), failure_policy="fail_open",
                              max_pending_revocations=2)

    async def run():
        tokens = [secrets.token_urlsafe(32) for _ in range(3)]
        for token in tokens:
            assert await store.delete(token) is False
        assert len(store.pending_revocations) == 2
        # The oldest entry was evicted; the two newest are retained.
        assert _token_hash(tokens[0]) not in store.pending_revocations
        assert _token_hash(tokens[1]) in store.pending_revocations
        assert _token_hash(tokens[2]) in store.pending_revocations
    asyncio.run(run())
    # max_pending_revocations=0 disables queueing (revocation is lost).
    store = RedisSessionStore(client=_FailingRedis(), failure_policy="fail_open",
                              max_pending_revocations=0)

    async def run_zero():
        assert await store.delete(secrets.token_urlsafe(32)) is False
        assert store.pending_revocations == ()
    asyncio.run(run_zero())
    with pytest.raises(ValueError, match="max_pending_revocations"):
        RedisSessionStore(client=_FakeRedis(), max_pending_revocations=-1)


def test_redis_store_preserves_created_at_across_update_and_rotation():
    """The Redis payload carries the same lifetime anchors as SQLite (S-P1-9)."""
    store = _fake_store()

    async def run():
        token = secrets.token_urlsafe(32)
        before = time.time()
        assert await store.save(token, {"value": 1}, "csrf-1", None, time.time() + 3600) == 1
        first = await store.load(token)
        assert first == ({"value": 1}, "csrf-1", 1)
        assert before <= first.created_at <= time.time()
        assert before <= first.last_seen <= time.time()
        created_at = first.created_at
        await asyncio.sleep(0.05)
        assert await store.save(token, {"value": 2}, "csrf-1", 1, time.time() + 3600) == 2
        second = await store.load(token)
        assert second.created_at == created_at
        assert second.last_seen > first.last_seen
        new_token = secrets.token_urlsafe(32)
        assert await store.save(new_token, {"value": 2}, "csrf-2", 2,
                                time.time() + 3600, previous_token=token) == 1
        assert (await store.load(new_token)).created_at == created_at
        assert await store.load(token) is None
    asyncio.run(run())


def test_redis_fail_open_logout_queues_revocation_end_to_end(tmp_path):
    """Logout during an outage still clears the cookie; the server session
    is revoked on recovery instead of staying valid until its TTL."""
    _build_session_api(tmp_path)
    client = _ToggleRedis()
    store = RedisSessionStore(client=client, failure_policy="fail_open")
    # CSRF is off here so the logout reaches the middleware delete path
    # even though the store is down (with CSRF on, the outage fails the
    # CSRF check first — see the fail_open test above).
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)],
                     security=SecurityConfig(csrf_enabled=False))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as first:
                token = (await first.get("/api/token")).json()["token"]
                assert (await first.post("/api/login",
                                         headers={"x-csrf-token": token})).status_code == 200
                cookie = first.cookies.get("pynext_session")
                assert (await first.get("/api/private")).status_code == 200
                client.down = True
                assert (await first.post("/api/logout")).status_code == 200
                assert first.cookies.get("pynext_session") in ("", None)
                assert len(store.pending_revocations) == 1
                client.down = False
                # The old cookie is dead on the first request after recovery.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test",
                                             cookies={"pynext_session": cookie}) as stale:
                    assert (await stale.get("/api/private")).status_code == 401
                assert store.pending_revocations == ()
        finally:
            await app.isr.close()
    asyncio.run(run())
