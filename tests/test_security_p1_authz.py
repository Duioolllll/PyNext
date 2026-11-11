"""S-P1-22 (CSRF token issuance), S-P1-23 (GET idempotency lint) and
S-P1-33 (authorization enforced once, at registration, fail-closed)."""
import asyncio
import json
import logging
import re
import secrets
import time

import httpx
import pytest

from pynext import (JSONResponse, Request, Response, SecurityConfig, Session,
                    SessionMiddleware, SQLiteSessionStore, authorization_policy,
                    create_app, csrf_input, csrf_protect,
                    csrf_token_response, get_csrf_token, require_permission,
                    require_scope, unmask_csrf_token)
from pynext.app import check_get_idempotency
from pynext.auth import check_authorization


def _run(coro):
    return asyncio.run(coro)


# --- S-P1-22: CSRF token issuance helper / endpoint ---


def test_get_csrf_token_uses_session_token():
    session = Session({"user_id": "alice"})
    request = Request("GET", "/api/echo", state={"session": session})
    # Session.csrf_token() is masked (S-P0-2): every call returns a
    # different value over the same stored secret, so the issued tokens
    # are compared after unmasking, not as raw strings.
    first = get_csrf_token(request)
    second = get_csrf_token(request)
    assert first != second
    assert unmask_csrf_token(first) == session._csrf
    assert unmask_csrf_token(second) == session._csrf


def test_get_csrf_token_without_session_is_fresh():
    request = Request("GET", "/api/echo")
    first, second = get_csrf_token(request), get_csrf_token(request)
    assert first != second
    assert len(first) >= 32


def test_csrf_token_response_sets_double_submit_cookie_without_session():
    response = csrf_token_response(Request("GET", "/_pynext/csrf-token"))
    assert response.status == 200
    assert isinstance(response, JSONResponse)
    token = json.loads(response.body)["csrf_token"]
    assert len(token) >= 32
    cookie = [v for k, v in response.headers if k == b"set-cookie"]
    assert len(cookie) == 1
    # The cookie carries the HMAC-signed value for the issued token.
    assert cookie[0].startswith(f"pynext_csrf={token}.".encode("ascii"))


def test_csrf_input_renders_hidden_field_and_sets_cookie():
    session = Session({"user_id": "alice"})
    request = Request("GET", "/form", state={"session": session})
    field = csrf_input(request)
    # The field carries a masked session token (S-P0-2): every call
    # masks with a fresh one-time pad, so compare the unmasked secret.
    match = re.fullmatch(
        r'<input type="hidden" name="csrf_token" value="([^"]+)">', field)
    assert match is not None
    assert unmask_csrf_token(match.group(1)) == session._csrf
    # Without a session the cookie must travel on the response carrying
    # the form; without that response the helper refuses rather than
    # rendering a form whose POST could never validate.
    response = Response("ok")
    field = csrf_input(Request("GET", "/form"), response)
    assert field.startswith('<input type="hidden" name="csrf_token" value="')
    assert any(k == b"set-cookie" for k, _ in response.headers)
    with pytest.raises(ValueError, match="needs the response"):
        csrf_input(Request("GET", "/form"))


def _csrf_project(root):
    api = root / "app" / "api"
    (api / "echo").mkdir(parents=True)
    (api / "echo" / "route.py").write_text(
        "from pynext import JSONResponse, csrf_protect, route\n"
        "@route(\"POST\")\n"
        "@csrf_protect\n"
        "async def post_echo(request):\n"
        "    return JSONResponse({\"ok\": True}, status=201)\n"
    )


def test_builtin_csrf_token_endpoint_session_flow(tmp_path):
    """GET /_pynext/csrf-token issues the session-bound token; the
    issued token then passes the CSRF check on POST."""
    _csrf_project(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)],
                     security=SecurityConfig(csrf_enabled=True))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/echo", json={})).status_code == 403
            issued = await client.get("/_pynext/csrf-token")
            assert issued.status_code == 200
            assert issued.headers["cache-control"] == "private, no-store"
            token = issued.json()["csrf_token"]
            assert (await client.post("/api/echo", json={},
                                      headers={"x-csrf-token": token})).status_code == 201
            assert (await client.post("/_pynext/csrf-token", json={})).status_code == 405

    _run(exercise())


def test_builtin_csrf_token_endpoint_sessionless_flow(tmp_path):
    """Without sessions the endpoint issues a double-submit token and
    sets the signed cookie on the same response."""
    _csrf_project(tmp_path)
    # csrf_cookie_secure=False: the test client is plain http://, and a
    # Secure cookie is never sent back over it (same reason the local
    # dev server disables the flag).
    app = create_app(tmp_path, security=SecurityConfig(csrf_enabled=True, csrf_cookie_secure=False))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            issued = await client.get("/_pynext/csrf-token")
            assert issued.status_code == 200
            token = issued.json()["csrf_token"]
            assert (await client.post("/api/echo", json={},
                                      headers={"x-csrf-token": token})).status_code == 201
            assert (await client.post("/api/echo", json={},
                                      headers={"x-csrf-token": "wrong"})).status_code == 403

    _run(exercise())


# --- S-P1-23: GET idempotency lint ---


def _idempotency_project(root, get_source):
    api = root / "app" / "api"
    (api / "mutate").mkdir(parents=True)
    (api / "mutate" / "route.py").write_text(get_source)


def test_check_get_idempotency_flags_session_write(tmp_path):
    _idempotency_project(tmp_path,
                         "from pynext import JSONResponse, route\n"
                         "@route(\"GET\")\n"
                         "async def get(request):\n"
                         "    request.state[\"session\"][\"seen\"] = True\n"
                         "    return JSONResponse({\"ok\": True})\n")
    app = create_app(tmp_path)
    assert check_get_idempotency(app) == [("/api/mutate", "writes to the session")]


def test_check_get_idempotency_ignores_pure_get_and_acknowledged(tmp_path):
    api = tmp_path / "app" / "api"
    (api / "read").mkdir(parents=True)
    (api / "read" / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"GET\")\n"
        "async def get(request):\n"
        "    return JSONResponse({\"ok\": True})\n")
    (api / "known").mkdir(parents=True)
    (api / "known" / "route.py").write_text(
        "from pynext import JSONResponse, route, state_changing_get\n"
        "@route(\"GET\")\n"
        "@state_changing_get\n"
        "async def get(request):\n"
        "    request.state[\"session\"][\"seen\"] = True\n"
        "    return JSONResponse({\"ok\": True})\n")
    app = create_app(tmp_path)
    assert check_get_idempotency(app) == []


def test_dev_app_warns_for_state_changing_get(tmp_path, caplog):
    _idempotency_project(tmp_path,
                         "from pynext import JSONResponse, route\n"
                         "@route(\"GET\")\n"
                         "async def get(request):\n"
                         "    session = request.state[\"session\"]\n"
                         "    session[\"seen\"] = True\n"
                         "    return JSONResponse({\"ok\": True})\n")
    with caplog.at_level(logging.WARNING, logger="pynext"):
        create_app(tmp_path)
    assert any("/api/mutate" in record.getMessage() and "CSRF" in record.getMessage()
               for record in caplog.records)


# --- S-P1-33: authorization enforced once, fail-closed ---


def test_require_scope_marks_handler_and_policy():
    @require_scope("items:read", "items:write")
    async def handler(request):
        return Response("ok")

    assert handler.__pynext_auth__ is True
    policy = authorization_policy(handler)
    assert policy.requires_auth is True
    assert policy.scopes == ("items:read", "items:write")
    assert policy.permissions == ()
    with pytest.raises(ValueError, match="at least one scope"):
        require_scope()
    with pytest.raises(ValueError, match="scope"):
        require_scope("")


def test_authorization_policy_rejects_markers_without_auth_flag():
    async def handler(request):
        return Response("ok")

    handler.__pynext_permissions__ = ("read",)
    with pytest.raises(ValueError, match="without @require_auth"):
        authorization_policy(handler)


def test_check_authorization_403_hides_missing_scope():
    session = Session({"user_id": "alice", "scopes": ["items:read"]})
    request = Request("POST", "/api/scoped", state={"session": session})

    @require_scope("items:write")
    async def handler(request):
        return Response("ok")

    denied = check_authorization(request, handler)
    assert denied is not None
    assert denied.status == 403
    assert denied.body == b"Forbidden"
    assert b"items:write" not in denied.body


def _scoped_project(root):
    api = root / "app" / "api"
    (api / "scoped").mkdir(parents=True)
    (api / "scoped" / "route.py").write_text(
        "from pynext import JSONResponse, require_scope, route\n"
        "@route(\"POST\")\n"
        "@require_scope(\"items:write\")\n"
        "async def post_scoped(request):\n"
        "    return JSONResponse({\"ok\": True})\n"
    )


async def _save_session(store, data):
    token = secrets.token_urlsafe(32)
    await store.save(token, data, None, None, time.time() + 3600)
    return token


def test_require_scope_enforced_end_to_end(tmp_path):
    _scoped_project(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    # CSRF off here on purpose: the 403s below must come from the
    # authorization decision, not from a missing CSRF token.
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)],
                     security=SecurityConfig(csrf_enabled=False))

    async def exercise():
        denied = await _save_session(store, {"user_id": "alice", "scopes": ["items:read"]})
        allowed = await _save_session(store, {"user_id": "alice", "scopes": ["items:write"]})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/scoped", json={})).status_code == 401
            response = await client.post("/api/scoped", json={},
                                         headers={"cookie": f"pynext_session={denied}"})
            assert response.status_code == 403
            assert response.text == "Forbidden"
            assert "items:write" not in response.text
            assert (await client.post("/api/scoped", json={},
                                      headers={"cookie": f"pynext_session={allowed}"})).status_code == 200

    _run(exercise())


def test_scope_check_fail_closed_on_malformed_scopes(tmp_path):
    _scoped_project(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)],
                     security=SecurityConfig(csrf_enabled=False))

    async def exercise():
        # A bare string is not a scopes list: fail closed, not "one scope".
        malformed = await _save_session(store, {"user_id": "alice", "scopes": "items:write"})
        missing = await _save_session(store, {"user_id": "alice"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            for token in (malformed, missing):
                response = await client.post(
                    "/api/scoped", json={}, headers={"cookie": f"pynext_session={token}"})
                assert response.status_code == 403

    _run(exercise())


def test_snapshot_survives_lost_decorator_attrs(tmp_path):
    """The policy is snapshotted at registration: stripping the
    handler's live markers afterwards must not open the route."""
    _scoped_project(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)],
                     security=SecurityConfig(csrf_enabled=False))
    handler = app.entries["/api/scoped"]["POST"]
    assert app.route_auth["/api/scoped"]["POST"].scopes == ("items:write",)
    del handler.__pynext_auth__
    del handler.__pynext_scopes__

    async def exercise():
        denied = await _save_session(store, {"user_id": "alice", "scopes": ["items:read"]})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/scoped", json={},
                                      headers={"cookie": f"pynext_session={denied}"})).status_code == 403

    _run(exercise())


def test_registration_rejects_markers_without_auth_flag(tmp_path):
    api = tmp_path / "app" / "api"
    (api / "broken").mkdir(parents=True)
    (api / "broken" / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"GET\")\n"
        "async def get_broken(request):\n"
        "    return JSONResponse({\"ok\": True})\n"
        "get_broken.__pynext_permissions__ = (\"read\",)\n")
    with pytest.raises(ValueError, match="without @require_auth"):
        create_app(tmp_path)


def test_require_scope_enforced_for_websocket(tmp_path):
    """Scopes are enforced in the WebSocket pre-flight too: a session
    without the scope gets close 1008, never an accepted socket."""
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        "from pynext import require_scope, websocket\n"
        "@websocket\n"
        "@require_scope(\"ws:join\")\n"
        "async def handler(websocket):\n"
        "    await websocket.accept()\n"
        "    await websocket.send_text('secret-data')\n"
        "    await websocket.receive()\n")

    class _ScriptedConnection:
        def __init__(self, incoming):
            self._incoming = iter(incoming)
            self.sent = []

        async def receive(self):
            return next(self._incoming)

        async def send(self, message):
            self.sent.append(message)

    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)])

    async def run():
        headers = [(b"host", b"localhost"), (b"origin", b"http://localhost")]
        denied_token = await _save_session(store, {"user_id": "alice", "scopes": []})
        denied = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws",
                   "headers": headers + [(b"cookie", f"pynext_session={denied_token}".encode())],
                   "query_string": b""}, denied.receive, denied.send)
        assert denied.sent == [{"type": "websocket.close", "code": 1008}]
        allowed_token = await _save_session(store, {"user_id": "alice", "scopes": ["ws:join"]})
        allowed = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app({"type": "websocket", "path": "/ws",
                   "headers": headers + [(b"cookie", f"pynext_session={allowed_token}".encode())],
                   "query_string": b""}, allowed.receive, allowed.send)
        assert allowed.sent[1] == {"type": "websocket.send", "text": "secret-data"}
        await app.isr.close()

    _run(run())
