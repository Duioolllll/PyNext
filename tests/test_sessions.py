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
