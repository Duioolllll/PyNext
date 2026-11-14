"""Session lifetime (S-P1-9), single-use tokens + re-auth (S-P1-21), CSRF cookie lifetime.

Covers: absolute lifetime + idle timeout (with rolling touch), the
shortened ``max_age`` default, purpose-bound single-use tokens,
re-authentication freshness, and the timestamped double-submit CSRF
cookie (``csrf_cookie_max_age``).
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

import httpx
import pytest

from pynext import SecurityConfig, SessionMiddleware, SQLiteSessionStore, create_app
from pynext.http import (
    DEFAULT_SECURITY,
    Request,
    Response,
    _signed_csrf_token,
    set_csrf_cookie,
    sign_csrf_token,
    validate_csrf,
)
from pynext.sessions import Session, SessionRecord


def _build_lifetime_api(root):
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
        '    session["permissions"] = ["read"]\n'
        '    session.rotate_on_login()\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "private").mkdir()
    (api / "private" / "route.py").write_text(
        'from pynext import JSONResponse, require_permission, route\n@route("GET")\n@require_permission("read")\n'
        'def get(request): return JSONResponse({"user": request.state["session"]["user_id"]})\n'
    )
    (api / "logout").mkdir()
    (api / "logout" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n    request.state["session"].invalidate()\n    return JSONResponse({"ok": True})\n'
    )
    (api / "issue").mkdir()
    (api / "issue" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    token = request.state["session"].issue_single_use_token("delete-account")\n'
        '    return JSONResponse({"single_use": token})\n'
    )
    (api / "confirm").mkdir()
    (api / "confirm" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    headers = {key.lower(): value for key, value in request.headers}\n'
        '    submitted = headers.get(b"x-single-use-token", b"").decode()\n'
        '    ok = request.state["session"].verify_single_use_token("delete-account", submitted)\n'
        '    return JSONResponse({"ok": ok})\n'
    )
    (api / "reauth").mkdir()
    (api / "reauth" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"recent": request.state["session"].recently_authenticated()})\n'
    )


def _make_app(root, store, **middleware_kwargs):
    return create_app(root,
                      middleware=[SessionMiddleware(store, secure=False, **middleware_kwargs)],
                      security=SecurityConfig(csrf_enabled=True))


async def _login(client):
    token = (await client.get("/api/token")).json()["token"]
    assert (await client.post("/api/login", headers={"x-csrf-token": token})).status_code == 200
    # Rotation issued a fresh CSRF secret; fetch the current token.
    return (await client.get("/api/token")).json()["token"]


# ----------------------------------------------------------------------
# S-P1-9: middleware defaults and validation
# ----------------------------------------------------------------------

def test_session_lifetime_defaults(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    middleware = SessionMiddleware(store)
    assert middleware.max_age == 43200
    assert middleware.idle_timeout == 43200
    assert middleware.absolute_max_age == 604800
    assert middleware.touch_interval == 300


def test_session_lifetime_rejects_bad_values(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    with pytest.raises(ValueError, match="max_age"):
        SessionMiddleware(store, max_age=0)
    with pytest.raises(ValueError, match="max_age"):
        SessionMiddleware(store, max_age=2_592_001)
    with pytest.raises(ValueError, match="max_age"):
        SessionMiddleware(store, max_age=True)
    with pytest.raises(ValueError, match="idle_timeout"):
        SessionMiddleware(store, idle_timeout=0)
    with pytest.raises(ValueError, match="idle_timeout"):
        SessionMiddleware(store, idle_timeout=2_592_001)
    with pytest.raises(ValueError, match="absolute_max_age"):
        SessionMiddleware(store, absolute_max_age=0)
    with pytest.raises(ValueError, match="absolute_max_age"):
        SessionMiddleware(store, absolute_max_age=31_536_001)
    with pytest.raises(ValueError, match="touch_interval"):
        SessionMiddleware(store, touch_interval=-1)
    with pytest.raises(ValueError, match="touch_interval"):
        SessionMiddleware(store, touch_interval=True)
    # Explicit overrides are honoured, including a separate idle timeout.
    middleware = SessionMiddleware(store, max_age=3600, idle_timeout=600,
                                   absolute_max_age=86400, touch_interval=None)
    assert middleware.max_age == 3600
    assert middleware.idle_timeout == 600
    assert middleware.absolute_max_age == 86400
    assert middleware.touch_interval is None


# ----------------------------------------------------------------------
# S-P1-9: store round-trip keeps the lifetime anchors (SQLite)
# ----------------------------------------------------------------------

def test_sqlite_store_preserves_created_at_across_update_and_rotation(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")

    async def run():
        token = secrets.token_urlsafe(32)
        before = time.time()
        assert await store.save(token, {"value": 1}, "csrf-1", None, time.time() + 3600) == 1
        first = await store.load(token)
        assert isinstance(first, SessionRecord)
        # A SessionRecord still unpacks and compares as a plain 3-tuple.
        data, csrf, version = first
        assert (data, csrf, version) == ({"value": 1}, "csrf-1", 1)
        assert first == ({"value": 1}, "csrf-1", 1)
        assert before <= first.created_at <= time.time()
        assert before <= first.last_seen <= time.time()
        created_at = first.created_at
        await asyncio.sleep(0.05)
        assert await store.save(token, {"value": 2}, "csrf-1", 1, time.time() + 3600) == 2
        second = await store.load(token)
        # An update refreshes last_seen but never moves created_at.
        assert second.created_at == created_at
        assert second.last_seen > first.last_seen
        assert second[2] == 2
        new_token = secrets.token_urlsafe(32)
        assert await store.save(new_token, {"value": 2}, "csrf-2", 2,
                                time.time() + 3600, previous_token=token) == 1
        rotated = await store.load(new_token)
        # Rotation does not reset the absolute clock either.
        assert rotated.created_at == created_at
        assert await store.load(token) is None
    asyncio.run(run())


def test_sqlite_store_loads_rows_written_before_timestamps(tmp_path):
    """Pre-S-P1-9 rows (no created_at/last_seen) get a fresh clock, not an error."""
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(path)
    token = secrets.token_urlsafe(32)
    key = hashlib.sha256(token.encode("ascii")).hexdigest()
    payload = json.dumps({"data": {"value": 1}, "csrf": None}, separators=(",", ":"))
    with sqlite3.connect(str(path), timeout=5) as db:
        db.execute("INSERT INTO pynext_sessions VALUES (?, ?, ?, ?)",
                   (key, payload, time.time() + 3600, 1))

    async def run():
        loaded = await store.load(token)
        assert loaded is not None
        assert loaded[0] == {"value": 1}
        assert time.time() - loaded.created_at < 5
        assert time.time() - loaded.last_seen < 5
    asyncio.run(run())


# ----------------------------------------------------------------------
# S-P1-9: absolute lifetime and idle timeout over HTTP
# ----------------------------------------------------------------------

def test_absolute_lifetime_expires_despite_activity(tmp_path):
    """Rotation + constant activity must not extend the absolute deadline."""
    _build_lifetime_api(tmp_path)
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(path)
    app = _make_app(tmp_path, store, absolute_max_age=3, idle_timeout=3600, touch_interval=0)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                await _login(client)
                cookie = client.cookies.get("pynext_session")
                await asyncio.sleep(0.5)
                assert (await client.get("/api/private")).status_code == 200
                await asyncio.sleep(3.5)
                assert (await client.get("/api/private")).status_code == 401
                # The expired entry was deleted best-effort by the middleware.
                assert await store.load(cookie) is None
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_idle_timeout_expires_inactive_session(tmp_path):
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = _make_app(tmp_path, store, idle_timeout=2, touch_interval=3600, absolute_max_age=3600)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                await _login(client)
                assert (await client.get("/api/private")).status_code == 200
                await asyncio.sleep(3.0)
                assert (await client.get("/api/private")).status_code == 401
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_touch_keeps_active_session_alive(tmp_path):
    """Requests inside the idle window refresh it; silence afterwards still expires."""
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = _make_app(tmp_path, store, idle_timeout=3, touch_interval=0, absolute_max_age=3600)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                await _login(client)
                for _ in range(4):
                    await asyncio.sleep(1.0)
                    assert (await client.get("/api/private")).status_code == 200
                await asyncio.sleep(4.0)
                assert (await client.get("/api/private")).status_code == 401
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_expired_session_starts_fresh_instead_of_failing(tmp_path):
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = _make_app(tmp_path, store, idle_timeout=2, touch_interval=3600, absolute_max_age=3600)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                await _login(client)
                old_cookie = client.cookies.get("pynext_session")
                await asyncio.sleep(3.0)
                response = await client.get("/api/token")
                assert response.status_code == 200
                assert client.cookies.get("pynext_session") != old_cookie
                assert (await client.get("/api/private")).status_code == 401
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_session_cookie_max_age_is_twelve_hours_by_default(tmp_path):
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = _make_app(tmp_path, store)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                token = (await client.get("/api/token")).json()["token"]
                response = await client.post("/api/login", headers={"x-csrf-token": token})
                assert "max-age=43200" in response.headers["set-cookie"].lower()
        finally:
            await app.isr.close()
    asyncio.run(run())


# ----------------------------------------------------------------------
# S-P1-21: single-use tokens
# ----------------------------------------------------------------------

def test_single_use_token_is_one_shot_and_purpose_bound():
    session = Session({"user_id": "alice"})
    token = session.issue_single_use_token("delete-account")
    assert isinstance(token, str) and len(token) >= 32
    assert session.verify_single_use_token("delete-account", token) is True
    # Consumed: a replay of the same value fails.
    assert session.verify_single_use_token("delete-account", token) is False
    # A wrong value consumes the token too.
    other = session.issue_single_use_token("delete-account")
    assert session.verify_single_use_token("delete-account", "wrong-value") is False
    assert session.verify_single_use_token("delete-account", other) is False
    # A token issued for another purpose does not verify here.
    transfer = session.issue_single_use_token("transfer")
    assert session.verify_single_use_token("delete-account", transfer) is False
    assert session.verify_single_use_token("transfer", transfer) is True
    # Unknown purpose, non-string input, and bad purpose names.
    assert session.verify_single_use_token("no-such-purpose", token) is False
    assert session.verify_single_use_token("delete-account", None) is False
    with pytest.raises(ValueError, match="purpose"):
        session.issue_single_use_token("")
    with pytest.raises(ValueError, match="purpose"):
        session.issue_single_use_token("bad purpose!")
    with pytest.raises(ValueError, match="max_age"):
        session.issue_single_use_token("delete-account", max_age=0)
    with pytest.raises(ValueError, match="max_age"):
        session.issue_single_use_token("delete-account", max_age=86_401)


def test_single_use_token_expires(tmp_path):
    session = Session()
    token = session.issue_single_use_token("delete-account", max_age=1)
    time.sleep(1.5)
    assert session.verify_single_use_token("delete-account", token) is False


def test_single_use_token_survives_store_round_trip(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    session = Session({"user_id": "alice"})
    token = session.issue_single_use_token("delete-account")

    async def run():
        key = secrets.token_urlsafe(32)
        assert await store.save(key, dict(session._data), None, None, time.time() + 3600) == 1
        loaded = await store.load(key)
        restored = Session(loaded[0])
        assert restored.verify_single_use_token("delete-account", token) is True
        assert restored.verify_single_use_token("delete-account", token) is False
    asyncio.run(run())


def test_single_use_metadata_is_hidden_from_the_mapping():
    session = Session({"user_id": "alice"})
    session.issue_single_use_token("delete-account")
    assert "__pynext_meta__" not in session
    assert "__pynext_meta__" not in list(session)
    assert "__pynext_meta__" not in dict(session)
    assert len(session) == 1
    assert list(session) == ["user_id"]
    with pytest.raises(KeyError):
        session["__pynext_meta__"]
    with pytest.raises(ValueError, match="reserved"):
        session["__pynext_meta__"] = {}
    with pytest.raises(KeyError):
        del session["__pynext_meta__"]
    session.mark_authenticated()
    assert len(session) == 1  # auth_at lives in the same hidden record


def test_single_use_token_on_invalidated_session_raises():
    session = Session()
    session.invalidate()
    with pytest.raises(RuntimeError, match="invalidated"):
        session.issue_single_use_token("delete-account")
    with pytest.raises(RuntimeError, match="invalidated"):
        session.verify_single_use_token("delete-account", "x")
    with pytest.raises(RuntimeError, match="invalidated"):
        session.mark_authenticated()


def test_single_use_token_over_http(tmp_path):
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = _make_app(tmp_path, store)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                csrf = await _login(client)
                issued = await client.post("/api/issue", headers={"x-csrf-token": csrf})
                assert issued.status_code == 200
                single_use = issued.json()["single_use"]
                confirm = await client.post("/api/confirm", headers={
                    "x-csrf-token": csrf, "x-single-use-token": single_use})
                assert confirm.json() == {"ok": True}
                replay = await client.post("/api/confirm", headers={
                    "x-csrf-token": csrf, "x-single-use-token": single_use})
                assert replay.json() == {"ok": False}
        finally:
            await app.isr.close()
    asyncio.run(run())


class _BumpSessionVersion:
    """Test helper: simulates a concurrent writer (A3-L1).

    Re-saves the session row — bumping its fencing version — before
    the handler runs, so the middleware's post-handler save hits a
    SessionConflict. A plain async callable, like SessionMiddleware.
    """

    def __init__(self, store, paths):
        self.store = store
        self.paths = paths

    async def __call__(self, request, call_next):
        if request.path in self.paths:
            session = request.state.get("session")
            token = session._token if session is not None else None
            if token is not None:
                loaded = await self.store.load(token)
                if loaded is not None:
                    await self.store.save(token, loaded[0], loaded[1], loaded[2],
                                          time.time() + 3600)
        return await call_next(request)


def test_single_use_consumption_survives_save_conflict_a3_l1(tmp_path):
    """A3-L1: a token consumed by the handler must stay consumed when
    the post-handler save fails with a fencing conflict.

    Before the fix, the middleware answered 409 and dropped the session
    object, so the in-memory-only consumption never reached the store
    and the same token verified again on retry. The middleware now
    salvages just the consumption (reload, drop the purpose, save).
    """
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    bump = _BumpSessionVersion(store, {"/api/confirm"})
    app = create_app(tmp_path,
                     middleware=[SessionMiddleware(store, secure=False), bump],
                     security=SecurityConfig(csrf_enabled=True))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                csrf = await _login(client)
                issued = await client.post("/api/issue", headers={"x-csrf-token": csrf})
                assert issued.status_code == 200
                single_use = issued.json()["single_use"]
                # The bump middleware re-saved the row before the
                # handler ran, so the post-handler save conflicts.
                confirm = await client.post("/api/confirm", headers={
                    "x-csrf-token": csrf, "x-single-use-token": single_use})
                assert confirm.status_code == 409
                # The handler verified the token (its action ran), and
                # the recovery save persisted the consumption: a fresh
                # load must no longer verify the same token.
                token = client.cookies.get("pynext_session")
                assert token
                loaded = await store.load(token)
                assert loaded is not None
                restored = Session(loaded[0])
                assert restored.verify_single_use_token("delete-account", single_use) is False
        finally:
            await app.isr.close()
    asyncio.run(run())


# ----------------------------------------------------------------------
# S-P1-21: re-authentication freshness
# ----------------------------------------------------------------------

def test_reauthentication_freshness():
    session = Session()
    assert session.authenticated_at is None
    assert session.recently_authenticated() is False
    session.mark_authenticated()
    assert session.authenticated_at is not None
    assert session.recently_authenticated() is True
    assert session.recently_authenticated(max_age=3600) is True
    with pytest.raises(ValueError, match="max_age"):
        session.recently_authenticated(max_age=0)
    session2 = Session()
    session2.rotate_on_login()
    assert session2.recently_authenticated() is True


def test_reauthentication_window_expires():
    session = Session()
    session.mark_authenticated()
    assert session.recently_authenticated(max_age=1) is True
    time.sleep(1.5)
    assert session.recently_authenticated(max_age=1) is False


def test_reauthentication_over_http(tmp_path):
    _build_lifetime_api(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = _make_app(tmp_path, store)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/api/reauth")).json() == {"recent": False}
                await _login(client)
                assert (await client.get("/api/reauth")).json() == {"recent": True}
        finally:
            await app.isr.close()
    asyncio.run(run())


# ----------------------------------------------------------------------
# S-P1-21: timestamped double-submit CSRF cookie
# ----------------------------------------------------------------------

def test_csrf_cookie_max_age_default_and_validation():
    assert SecurityConfig().csrf_cookie_max_age == 43200
    assert SecurityConfig(csrf_cookie_max_age=60).csrf_cookie_max_age == 60
    with pytest.raises(ValueError, match="csrf_cookie_max_age"):
        SecurityConfig(csrf_cookie_max_age=0)
    with pytest.raises(ValueError, match="csrf_cookie_max_age"):
        SecurityConfig(csrf_cookie_max_age=2_592_001)


def test_signed_csrf_token_carries_issue_time():
    signed = sign_csrf_token("token-value")
    parts = signed.split(".")
    assert len(parts) == 3
    assert parts[0] == "token-value"
    assert parts[1].isdigit()
    assert int(parts[1]) <= time.time()
    assert _signed_csrf_token(signed) == "token-value"
    # Deterministic issue time for tests.
    fixed = sign_csrf_token("token-value", issued_at=1_700_000_000)
    assert fixed.split(".")[1] == "1700000000"


def test_signed_csrf_token_rejects_legacy_unsigned_and_expired():
    token = "token-value"
    # Legacy "<token>.<HMAC>" format (no issue time) is rejected.
    legacy_sig = hmac.new(DEFAULT_SECURITY.csrf_signing_key, token.encode("ascii"),
                          hashlib.sha256).hexdigest()
    assert _signed_csrf_token(f"{token}.{legacy_sig}") is None
    # A bare token is rejected too.
    assert _signed_csrf_token(token) is None
    # Expired: issued more than csrf_cookie_max_age ago.
    old = sign_csrf_token(token, issued_at=time.time() - 50_000)
    assert _signed_csrf_token(old) is None
    # Issued in the future (beyond clock-skew allowance) is rejected.
    future = sign_csrf_token(token, issued_at=time.time() + 3600)
    assert _signed_csrf_token(future) is None
    # Tampering with the signature fails.
    signed = sign_csrf_token(token)
    tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")
    assert _signed_csrf_token(tampered) is None
    # A shorter custom max age rejects sooner.
    short_config = SecurityConfig(csrf_cookie_max_age=60)
    hour_old = sign_csrf_token(token, issued_at=time.time() - 3600)
    assert _signed_csrf_token(hour_old, config=short_config) is None
    assert _signed_csrf_token(sign_csrf_token(token), config=short_config) == token


def test_validate_csrf_rejects_expired_cookie():
    token = "token-value"
    expired = sign_csrf_token(token, issued_at=time.time() - 50_000)
    headers = ((b"cookie", f"pynext_csrf={expired}".encode()),
               (b"x-csrf-token", token.encode()))
    assert validate_csrf(Request("POST", "/api/echo", headers=headers)) is False
    fresh = sign_csrf_token(token)
    headers = ((b"cookie", f"pynext_csrf={fresh}".encode()),
               (b"x-csrf-token", token.encode()))
    assert validate_csrf(Request("POST", "/api/echo", headers=headers)) is True


def test_set_csrf_cookie_max_age_follows_config():
    response = set_csrf_cookie(Response("ok"), "token-value",
                               config=SecurityConfig(csrf_cookie_max_age=60))
    cookies = [value.decode("ascii") for name, value in response.headers if name == b"set-cookie"]
    assert len(cookies) == 1
    assert "Max-Age=60" in cookies[0]
