import asyncio
import secrets
import time

import httpx
import pytest

from pynext import SecurityConfig, SessionMiddleware, SQLiteSessionStore, create_app
from pynext.server import start_native
from pynext.sessions import SessionConflict


def make_app(root, store):
    return create_app(root, middleware=[SessionMiddleware(store, secure=False)],
                      security=SecurityConfig(csrf_enabled=True))


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_sqlite_sessions_auth_csrf_rotation_and_revocation(tmp_path, mode):
    api = tmp_path / "app" / "api"
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
        '    session.rotate()\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "private").mkdir()
    (api / "private" / "route.py").write_text(
        'from pynext import JSONResponse, require_permission, route\n@route("GET")\n'
        '@require_permission("read")\n'
        'def get(request): return JSONResponse({"user": request.state["session"]["user_id"]})\n'
        '@route("POST")\n@require_permission("write")\n'
        'def post(): return JSONResponse({"ok": True})\n'
    )
    (api / "logout").mkdir()
    (api / "logout" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n    request.state["session"].invalidate()\n    return JSONResponse({"ok": True})\n'
    )
    path = tmp_path / "sessions.sqlite3"
    app1 = make_app(tmp_path, SQLiteSessionStore(path))
    app2 = make_app(tmp_path, SQLiteSessionStore(path))

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
                response = await client.get("/api/token")
                assert "httponly" in response.headers["set-cookie"].lower()
                assert "samesite=lax" in response.headers["set-cookie"].lower()
                assert response.headers["cache-control"] == "private, no-store"
                token = response.json()["token"]
                old_cookie = client.cookies.get("pynext_session")
                assert (await client.post("/api/login", headers={"x-csrf-token": "wrong"})).status_code == 403
                assert (await client.post("/api/login", headers={"x-csrf-token": token})).status_code == 200
                new_cookie = client.cookies.get("pynext_session")
                assert new_cookie != old_cookie
                assert (await client.get("/api/private")).json() == {"user": "alice"}
                assert (await client.post("/api/logout", headers={"x-csrf-token": token})).status_code == 403
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/private", headers={"x-csrf-token": token})).status_code == 403
                async with httpx.AsyncClient(**second, cookies={"pynext_session": new_cookie}) as other:
                    assert (await other.get("/api/private")).status_code == 200
                async with httpx.AsyncClient(**second, cookies={"pynext_session": old_cookie}) as stale:
                    assert (await stale.get("/api/private")).status_code == 401
                assert (await client.post("/api/logout", headers={"x-csrf-token": token})).status_code == 200
                async with httpx.AsyncClient(**second, cookies={"pynext_session": new_cookie}) as stale:
                    assert (await stale.get("/api/private")).status_code == 401
        finally:
            for server in servers:
                await asyncio.to_thread(server.stop)
            await app1.isr.close()
            await app2.isr.close()
    asyncio.run(run())


def test_session_store_fences_concurrent_writes_and_expiry(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")

    async def run():
        token = secrets.token_urlsafe(32)
        assert await store.save(token, {"value": 1}, None, None, time.time() + 60) == 1
        first = await store.load(token)
        second = await store.load(token)
        assert await store.save(token, {"value": 2}, None, first[2], time.time() + 60) == 2
        with pytest.raises(SessionConflict):
            await store.save(token, {"value": 3}, None, second[2], time.time() + 60)
        await store.delete(token)
        with pytest.raises(SessionConflict):
            await store.save(token, {"value": 4}, None, 2, time.time() + 60)
        expired = secrets.token_urlsafe(32)
        await store.save(expired, {}, None, None, time.time() - 1)
        assert await store.load(expired) is None
        assert await store.purge_expired() == 1
    asyncio.run(run())


def test_authenticated_static_pages_are_rejected(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "page.py").write_text(
        'from pynext import h, page, require_auth\n@page(render="ssg")\n@require_auth\n'
        'def Page(): return h("p", {}, "private")\n'
    )
    with pytest.raises(ValueError, match="cannot require authentication"):
        create_app(tmp_path)


def test_rotate_on_login_preserves_data_and_rotates_csrf():
    """The login hook rotates the id and the CSRF token, keeping the data."""
    from pynext.http import unmask_csrf_token
    from pynext.sessions import Session

    session = Session({"user_id": "alice"}, csrf="old-csrf", token="old-token", version=1)
    session["user_id"] = "alice"
    session.rotate_on_login()
    assert session._rotate is True
    assert session["user_id"] == "alice"
    assert session._csrf != "old-csrf"
    # csrf_token() is masked (S-P0-2): every call returns a different
    # value over the same stored secret.
    first_issued = session.csrf_token()
    assert first_issued != session._csrf
    assert unmask_csrf_token(first_issued) == session._csrf
    assert session.csrf_token() != first_issued
    assert unmask_csrf_token(session.csrf_token()) == session._csrf
    # Rotating twice, or after invalidation, behaves like rotate().
    session.rotate_on_login()
    assert session._rotate is True
    session.invalidate()
    with pytest.raises(RuntimeError, match="invalidated"):
        session.rotate_on_login()


def test_duplicate_session_cookie_is_treated_as_no_session(tmp_path):
    """Review #4 #8: two values for the cookie name fail closed (401).

    A request presenting the session cookie name twice — in one Cookie
    header or as two Cookie headers — must not be authenticated as
    either value; the middleware treats it as having no session.
    """
    api = tmp_path / "app" / "api"
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
        '    session.rotate_on_login()\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "private").mkdir()
    (api / "private" / "route.py").write_text(
        'from pynext import JSONResponse, require_auth, route\n@route("GET")\n@require_auth\n'
        'def get(request): return JSONResponse({"user": request.state["session"]["user_id"]})\n'
    )
    app = make_app(tmp_path, SQLiteSessionStore(tmp_path / "sessions.sqlite3"))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": token})).status_code == 200
                cookie = client.cookies.get("pynext_session")
                assert cookie
                assert (await client.get("/api/private")).status_code == 200
                dup = f"pynext_session={cookie}; pynext_session={cookie}"
                assert (await client.get("/api/private",
                                         headers={"Cookie": dup})).status_code == 401
                assert (await client.get(
                    "/api/private",
                    headers=[("Cookie", f"pynext_session={cookie}"),
                             ("Cookie", f"pynext_session={cookie}")])).status_code == 401
                # And a single cookie still authenticates afterwards.
                assert (await client.get("/api/private")).status_code == 200
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_session_rejects_non_json_serializable_value_at_assignment():
    """Review #4 #9: a bad value fails at set time, naming the key.

    Before the fix the assignment succeeded silently and the request
    answered 500 only after the handler had run, from the middleware
    save, with a bare TypeError that named no key.
    """
    import datetime
    import math

    from pynext.sessions import Session

    session = Session()
    for bad in (object(), {1, 2}, {"nested": {3, 4}}, datetime.datetime.now(),
                b"bytes", lambda: None):
        with pytest.raises(TypeError, match="'bad'"):
            session["bad"] = bad
    assert "bad" not in session
    with pytest.raises(ValueError, match="'bad'"):
        session["bad"] = math.nan
    with pytest.raises(ValueError, match="'bad'"):
        session["bad"] = math.inf
    # MutableMapping helpers go through __setitem__ too.
    with pytest.raises(TypeError, match="'bad'"):
        session.update({"bad": object()})
    # The constructor fails fast as well.
    with pytest.raises(TypeError, match="'bad'"):
        Session({"bad": object()})
    # JSON-serializable values — including nested and unicode — still work.
    session["n"] = 42
    session["f"] = 1.5
    session["flag"] = True
    session["nothing"] = None
    session["text"] = "héllo"
    session["nested"] = {"a": [1, 2, {"b": "x"}]}
    assert session["nested"] == {"a": [1, 2, {"b": "x"}]}
    assert session._changed()


def test_session_store_save_names_unserializable_key(tmp_path):
    """Direct store callers get the same key-named error at save time."""
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")

    async def run():
        with pytest.raises(TypeError, match="'bad'"):
            await store.save(secrets.token_urlsafe(32), {"bad": object()},
                             None, None, time.time() + 60)
    asyncio.run(run())


def test_session_size_cap_boundary_still_enforced(tmp_path):
    """The 16 KiB cap is unchanged: exactly at the cap saves, over fails."""
    import json

    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")

    async def run():
        token = secrets.token_urlsafe(32)
        overhead = len(json.dumps({"data": {"big": ""}, "csrf": None},
                                  ensure_ascii=False, separators=(",", ":")).encode())
        fits = "x" * (16_384 - overhead)
        assert await store.save(token, {"big": fits}, None, None, time.time() + 60) == 1
        assert (await store.load(token))[0] == {"big": fits}
        with pytest.raises(ValueError, match="exceeds 16384 bytes"):
            await store.save(secrets.token_urlsafe(32), {"big": fits + "x"},
                             None, None, time.time() + 60)
    asyncio.run(run())


def test_handler_sees_assignment_error_before_returning(tmp_path):
    """The assignment error now surfaces inside the handler, debuggable.

    The handler catches the TypeError at the assignment line and reports
    it; before the fix the handler ran to completion and the middleware
    save failed afterwards with a 500.
    """
    api = tmp_path / "app" / "api"
    (api / "bad").mkdir(parents=True)
    (api / "bad" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request):\n'
        '    try:\n'
        '        request.state["session"]["bad"] = object()\n'
        '    except TypeError as exc:\n'
        '        return JSONResponse({"assignment_error": str(exc)})\n'
        '    return JSONResponse({"assignment_error": None})\n'
    )
    app = make_app(tmp_path, SQLiteSessionStore(tmp_path / "sessions.sqlite3"))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/api/bad")
                assert response.status_code == 200
                assert "'bad'" in response.json()["assignment_error"]
                assert "not JSON-serializable" in response.json()["assignment_error"]
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_session_changed_names_key_mutated_in_place():
    """In-place mutation bypasses __setitem__; _changed() still names the key.

    ``session["tags"].append(object())`` cannot be caught at assignment
    time, so the error still surfaces after the handler — but now with
    the offending key in the message instead of a bare TypeError.
    """
    from pynext.sessions import Session

    session = Session()
    session["tags"] = ["a"]
    session["tags"].append(object())
    with pytest.raises(TypeError, match="'tags'"):
        session._changed()


def _cookie_flags(set_cookie):
    """Split a Set-Cookie value into its lower-cased flag parts."""
    return {part.strip().lower() for part in set_cookie.split(";")}


def test_session_cookie_secure_defaults(tmp_path):
    """S-P0-4: the default session cookie is Secure + HttpOnly + SameSite=Lax.

    The defaults live on ``SessionMiddleware`` itself (``secure=True``,
    ``same_site="Lax"``, ``HttpOnly`` always set), so an app that
    installs the middleware without arguments gets the safe cookie —
    and the token is stored server-side under its SHA-256 hash.
    """
    from pynext.sessions import _key

    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    assert SessionMiddleware(store).secure is True
    assert SessionMiddleware(store).same_site == "Lax"
    assert len(_key("x" * 43)) == 64  # SHA-256 hex digest, not SHA-1
    api = tmp_path / "app" / "api"
    (api / "token").mkdir(parents=True)
    (api / "token" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"token": request.state["session"].csrf_token()})\n'
    )
    app = create_app(tmp_path, middleware=[SessionMiddleware(store)])

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/api/token")
                flags = _cookie_flags(response.headers["set-cookie"])
                assert "secure" in flags
                assert "httponly" in flags
                assert "samesite=lax" in flags
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_session_cookie_secure_opt_out_is_explicit(tmp_path):
    """S-P0-4: plain-http local dev opts out with an explicit ``secure=False``."""
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    assert SessionMiddleware(store, secure=False).secure is False
    api = tmp_path / "app" / "api"
    (api / "token").mkdir(parents=True)
    (api / "token" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"token": request.state["session"].csrf_token()})\n'
    )
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)])

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/api/token")
                flags = _cookie_flags(response.headers["set-cookie"])
                assert "secure" not in flags
                assert "httponly" in flags
                assert "samesite=lax" in flags
        finally:
            await app.isr.close()
    asyncio.run(run())
