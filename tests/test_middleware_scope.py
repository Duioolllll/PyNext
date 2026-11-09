"""Conditional middleware scopes: Next.js-style has/missing matchers.

Covers REPORT.md §2.2 D#13 / track D P1-1: a middleware wrapped with
``pynext.scoped`` runs only when its ``source`` path pattern and its
``has``/``missing`` conditions match, on both transports, and the same
scope gates the WebSocket pre-flight. Invalid scopes fail at
registration (startup), never silently per request.
"""

import asyncio

import httpx
import pytest

from pynext import (MiddlewareScope, Request, Response, RouteHas, SessionMiddleware, SQLiteSessionStore,
                    create_app, scoped)
from pynext.build import write_manifest
from pynext.server import start_native


def make_project(root):
    app = root / "app"
    app.mkdir()
    (app / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "home")\n'
    )
    api = app / "api"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\ndef get(request): return JSONResponse({"ok": True})\n'
    )
    echo = api / "echo"
    echo.mkdir()
    (echo / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\ndef get(request): return JSONResponse({"ok": True})\n'
    )
    ws = app / "ws"
    ws.mkdir()
    (ws / "route.py").write_text(
        'from pynext import websocket\n@websocket\nasync def socket(websocket):\n'
        '    await websocket.accept()\n    await websocket.close()\n'
    )
    write_manifest(root)


def req(path="/", *, query=b"", headers=(), raw_path=None):
    return Request("GET", path, query, tuple(headers), b"", raw_path=raw_path)


# --- Registration-time validation ---------------------------------------


@pytest.mark.parametrize("source", [
    "api",                 # must start with /
    "/api/",               # trailing slash
    "/api//x",             # empty segment
    "/api/%2F",            # % can never match a decoded path
    "/api?x=1",            # ? is not part of a path pattern
    "/api#frag",
    "/(group)/x",          # route groups are not URL segments
    "/@slot/x",            # slots are not URL segments
    "/api/[id",            # malformed segment
    "/api/[[id]]",         # optional catch-all needs [[...name]]
    "/api/[...rest]/x",    # catch-all must be last
    "/api/[id]/[id]",      # duplicate parameter name
    "/" + "a" * 4096,      # over the 4096 cap
])
def test_source_validation_rejects_bad_patterns(source):
    async def layer(request, call_next):
        return await call_next(request)

    with pytest.raises((TypeError, ValueError)):
        scoped(layer, source=source)


@pytest.mark.parametrize("kwargs", [
    {"has": [{"type": "bogus", "key": "x"}]},
    {"has": [{"type": "header"}]},                       # key required
    {"has": [{"type": "host", "key": "example.com", "value": "example.com"}]},  # host takes no key
    {"has": [{"type": "host"}]},                         # host requires a value
    {"has": [{"type": "header", "key": "bad key", "value": "x"}]},  # not a token
    {"has": [{"type": "query", "key": "a=b"}]},
    {"has": [{"type": "cookie", "key": "x", "value": "("}]},  # invalid regex
    {"has": [{"type": "cookie", "key": "x", "value": "a" * 1025}]},  # over the cap
    {"has": [42]},
    {"missing": "header"},                               # a bare string is not a condition
])
def test_condition_validation_rejects_bad_conditions(kwargs):
    async def layer(request, call_next):
        return await call_next(request)

    with pytest.raises((TypeError, ValueError)):
        scoped(layer, **kwargs)


def test_scoped_rejects_sync_middleware():
    with pytest.raises(TypeError, match="async callables"):
        scoped(lambda request, call_next: Response(), source="/api/[...rest]")


def test_empty_value_is_an_existence_check():
    cond = RouteHas(type="header", key="X-Flag", value="")
    assert cond.value is None
    assert cond.matches(req("/", headers=[(b"x-flag", b"yes")]))
    assert not cond.matches(req("/"))


# --- Matching semantics (unit) ------------------------------------------


def test_source_patterns():
    scope = MiddlewareScope(source="/api/[...rest]")
    assert scope.matches(req("/api/x"))
    assert scope.matches(req("/api/x/y"))
    assert not scope.matches(req("/api"))
    assert not scope.matches(req("/other/x"))

    scope = MiddlewareScope(source="/admin/[id]")
    assert scope.matches(req("/admin/42"))
    assert not scope.matches(req("/admin/42/extra"))
    assert not scope.matches(req("/admin"))

    scope = MiddlewareScope(source="/docs/[[...path]]")
    assert scope.matches(req("/docs"))
    assert scope.matches(req("/docs/a/b"))
    assert not scope.matches(req("/other"))

    scope = MiddlewareScope(source="/")
    assert scope.matches(req("/"))
    assert not scope.matches(req("/x"))

    assert MiddlewareScope().matches(req("/anything"))


def test_has_missing_semantics():
    scope = MiddlewareScope(
        source="/api/[...rest]",
        has=[{"type": "header", "key": "X-Admin", "value": "yes"},
             {"type": "query", "key": "debug"}],
        missing=[{"type": "cookie", "key": "optout"}],
    )
    headers = [(b"x-admin", b"yes"), (b"cookie", b"theme=dark")]
    assert scope.matches(req("/api/x", query=b"debug=1", headers=headers))
    assert not scope.matches(req("/api/x", headers=headers))  # debug missing
    assert not scope.matches(req("/api/x", query=b"debug=1",
                                 headers=[(b"x-admin", b"no"), (b"cookie", b"theme=dark")]))
    assert not scope.matches(req("/api/x", query=b"debug=1",
                                 headers=headers + [(b"cookie", b"optout=1")]))
    assert not scope.matches(req("/other", query=b"debug=1", headers=headers))  # source


def test_header_value_matches_any_value_and_is_case_insensitive():
    scope = MiddlewareScope(has=[{"type": "header", "key": "X-Flag", "value": "a|b"}])
    assert scope.matches(req("/", headers=[(b"X-FLAG", b"a")]))
    assert scope.matches(req("/", headers=[(b"x-flag", b"no"), (b"x-flag", b"b")]))
    assert not scope.matches(req("/", headers=[(b"x-flag", b"c")]))


def test_query_uses_last_value_and_percent_decoding():
    scope = MiddlewareScope(has=[{"type": "query", "key": "q", "value": "second"}])
    assert scope.matches(req("/", query=b"q=first&q=second"))
    assert not scope.matches(req("/", query=b"q=second&q=first"))
    scope = MiddlewareScope(has=[{"type": "query", "key": "q", "value": "a b"}])
    assert scope.matches(req("/", query=b"q=a%20b"))


def test_cookie_and_host_conditions():
    scope = MiddlewareScope(has=[{"type": "cookie", "key": "session", "value": "[0-9]+"}])
    assert scope.matches(req("/", headers=[(b"cookie", b"theme=dark; session=123")]))
    assert not scope.matches(req("/", headers=[(b"cookie", b"session=abc")]))

    scope = MiddlewareScope(has=[{"type": "host", "value": r"api\.example\.com"}])
    assert scope.matches(req("/", headers=[(b"host", b"API.Example.COM:8443")]))
    assert not scope.matches(req("/", headers=[(b"host", b"example.com")]))
    assert not scope.matches(req("/"))


def test_raw_path_or_match_is_fail_closed():
    # The decoded path misses but the raw target hits (and vice versa):
    # either one runs the middleware, so an encoded path cannot dodge it.
    scope = MiddlewareScope(source="/api/[...rest]")
    assert scope.matches(req("/other", raw_path=b"/api/x"))
    assert scope.matches(req("/api/x", raw_path=b"/other"))
    assert not scope.matches(req("/other", raw_path=b"/other"))
    # Undecodable raw target: still evaluated against the literal form.
    assert scope.matches(req("/other", raw_path=b"/api/%ff"))


def test_malformed_query_runs_middleware_fail_closed():
    scope = MiddlewareScope(has=[{"type": "query", "key": "token", "value": "secret"}])
    assert scope.matches(req("/", query=b"q=%ff"))


# --- Integration: HTTP on both transports -------------------------------


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_scoped_middleware_runs_only_in_scope(tmp_path, mode):
    make_project(tmp_path)

    async def marker(request, call_next):
        request.state["seen"] = True
        response = await call_next(request)
        response.headers.append((b"x-global", b"yes"))
        return response

    async def api_only(request, call_next):
        response = await call_next(request)
        response.headers.append((b"x-api", b"yes"))
        return response

    async def debug_only(request, call_next):
        response = await call_next(request)
        response.headers.append((b"x-debug", b"yes"))
        return response

    async def dark_only(request, call_next):
        response = await call_next(request)
        response.headers.append((b"x-dark", b"yes"))
        return response

    app = create_app(tmp_path, production=True, middleware=[
        marker,
        scoped(api_only, source="/api/[...rest]"),
        scoped(debug_only, has=[{"type": "query", "key": "debug", "value": "1"}]),
        scoped(dark_only, has=[{"type": "cookie", "key": "theme", "value": "dark"}]),
    ])

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                response = await client.get("/")
                assert response.headers["x-global"] == "yes"
                assert "x-api" not in response.headers
                assert "x-debug" not in response.headers

                # /api itself is outside /api/[...rest] (catch-all needs a segment).
                response = await client.get("/api")
                assert "x-api" not in response.headers
                assert "x-debug" not in response.headers

                response = await client.get("/api/echo")
                assert response.status_code == 200
                assert response.headers["x-api"] == "yes"
                assert "x-debug" not in response.headers

                response = await client.get("/api/echo?debug=1")
                assert response.headers["x-api"] == "yes"
                assert response.headers["x-debug"] == "yes"

                response = await client.get("/api/echo?debug=0")
                assert response.headers["x-api"] == "yes"
                assert "x-debug" not in response.headers

                response = await client.get("/", headers={"cookie": "theme=dark"})
                assert response.headers["x-dark"] == "yes"
                response = await client.get("/", headers={"cookie": "theme=light"})
                assert "x-dark" not in response.headers

                # An encoded path still decodes to the scoped path.
                response = await client.get("/%61pi/echo")
                assert response.status_code == 200
                assert response.headers["x-api"] == "yes"
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_scoped_auth_short_circuits_only_in_scope(tmp_path, mode):
    make_project(tmp_path)

    async def auth(request, call_next):
        token = next((value.decode("latin-1") for key, value in request.headers
                      if key.lower() == b"x-token"), None)
        if token != "secret":
            return Response("forbidden", status=403)
        return await call_next(request)

    app = create_app(tmp_path, production=True,
                     middleware=[scoped(auth, source="/api/[...rest]")])

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/")).status_code == 200
                # /api itself is outside /api/[...rest]: no auth runs there.
                assert (await client.get("/api")).status_code == 200
                assert (await client.get("/api/echo")).status_code == 403
                response = await client.get("/api/echo", headers={"x-token": "secret"})
                assert response.status_code == 200
                assert response.json() == {"ok": True}
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_scoped_middleware_from_middleware_py(tmp_path):
    make_project(tmp_path)
    (tmp_path / "middleware.py").write_text(
        'from pynext import Response, scoped\n'
        'async def layer(request, call_next):\n'
        '    response = await call_next(request)\n'
        '    response.headers.append((b"x-file", b"yes"))\n'
        '    return response\n'
        'MIDDLEWARE = [scoped(layer, source="/api/[...rest]")]\n'
    )
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert "x-file" not in (await client.get("/")).headers
            assert "x-file" not in (await client.get("/api")).headers
            assert (await client.get("/api/echo")).headers["x-file"] == "yes"
        await app.isr.close()
    asyncio.run(run())


def test_scoped_session_middleware_still_enables_csrf(tmp_path):
    make_project(tmp_path)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[
        scoped(SessionMiddleware(store, secure=False), source="/api/[...rest]")])
    assert app.security.csrf_enabled is True

    async def run():
        await app.isr.close()
    asyncio.run(run())


# --- WebSocket pre-flight shares the scope ------------------------------


def test_ws_preflight_shares_scope(tmp_path):
    make_project(tmp_path)

    async def gate(request, call_next):
        token = next((value.decode("latin-1") for key, value in request.headers
                      if key.lower() == b"x-token"), None)
        if token != "secret":
            return Response("forbidden", status=403)
        return await call_next(request)

    app = create_app(tmp_path, middleware=[scoped(gate, source="/ws")])

    async def run():
        # Scoped to /ws: the handshake is denied without the token...
        denied = await app._authorize_websocket("/ws", b"", [(b"host", b"test")])
        assert denied is not None and denied.status == 403
        # ...and allowed with it.
        assert await app._authorize_websocket(
            "/ws", b"", [(b"host", b"test"), (b"x-token", b"secret")]) is None
        await app.isr.close()
    asyncio.run(run())


def test_ws_preflight_ignores_out_of_scope_middleware(tmp_path):
    make_project(tmp_path)

    async def deny_all(request, call_next):
        return Response("forbidden", status=403)

    # Scoped to /api: a /ws upgrade must not see this middleware at all.
    app = create_app(tmp_path, middleware=[scoped(deny_all, source="/api/[...rest]")])

    async def run():
        assert await app._authorize_websocket("/ws", b"", [(b"host", b"test")]) is None
        await app.isr.close()
    asyncio.run(run())
