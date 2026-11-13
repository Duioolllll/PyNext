"""Standard middleware results: next / rewrite / redirect / response.

Regression coverage for track D P2-1 of the Next.js vs PyNext analysis:
a middleware may state its outcome explicitly with
:class:`pynext.middleware.MiddlewareResult`, or implicitly by returning a
plain ``Response`` / calling ``call_next`` with a rewritten request.
Rewrites are validated with the same ``_decode_path`` the transports use
at the edge, routing is re-evaluated against the rewritten request, rate
limiting is decided once on the original path, and at most
:data:`pynext.middleware.MAX_MIDDLEWARE_REWRITES` rewrites run per
request.
"""
import asyncio
import dataclasses

import httpx
import pytest

from pynext import (
    MAX_MIDDLEWARE_REWRITES,
    MiddlewareResult,
    RateLimiter,
    Request,
    Response,
    create_app,
    redirect_response,
    rewrite_request,
)
from pynext.build import write_manifest
from pynext.middleware import classify_response, is_redirect_response
from pynext.server import start_native


def make_project(root):
    app = root / "app"
    app.mkdir()
    (app / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "home")\n'
    )
    (app / "news").mkdir()
    (app / "news" / "page.py").write_text(
        'from pynext import h, page\n@page(render="isr", revalidate=30)\n'
        'def Page(): return h("p", {}, "news")\n'
    )
    greet = app / "greet" / "[name]"
    greet.mkdir(parents=True)
    (greet / "page.py").write_text(
        'from pynext import h, page\n'
        'def generate_static_params(): return [{"name": "alice"}]\n'
        '@page(render="ssg")\ndef Page(params): return h("p", {}, "hello " + params["name"])\n'
    )
    api = app / "api"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"query": request.query, "order": request.state.get("order")})\n'
    )
    (root / "public").mkdir()
    write_manifest(root)


def run_app(app, mode, coro):
    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                await coro(client)
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Unit: the pynext.middleware helpers
# ---------------------------------------------------------------------------

def test_redirect_response_defaults_to_307_and_validates_targets():
    response = redirect_response("/news")
    assert response.status == 307
    assert (b"location", b"/news") in response.headers
    assert redirect_response("https://example.com/new", status=301).status == 301
    for bad in ("", "//evil.com", "javascript:alert(1)", "/has space", "relative/path"):
        with pytest.raises(ValueError):
            redirect_response(bad)
    with pytest.raises(ValueError):
        redirect_response("/news", status=200)


def test_redirect_response_rejects_backslash_open_redirect_a6_m1():
    """A6-M1: "/\\evil.com" must not pass validation.

    Browsers (WHATWG URL) treat "\\" as "/" for http(s), so a Location
    of "/\\evil.com" resolves to "//evil.com" — an open redirect the
    scheme-relative check alone would miss. Percent-encoded "%5c" stays
    safe (it does not normalize to a host separator) and is still
    accepted.
    """
    for bad in ("/\\evil.com", "/\\@evil.com", "https://example.com/\\evil.com",
                "/a\\b", "\\\\evil.com"):
        with pytest.raises(ValueError):
            redirect_response(bad)
    assert redirect_response("/%5cevil.com").status == 307
    assert redirect_response("/news").status == 307


def test_redirect_response_rejects_non_ascii_with_value_error_a6_l1():
    """A6-L1: a non-ASCII redirect target must fail with ValueError.

    Before the fix, redirect_response("/café") raised UnicodeEncodeError
    (from the ASCII encode of the Location header), which the middleware
    layer does not expect — the request answered 500. Validation now
    rejects non-ASCII up front, and the percent-encoded form works.
    """
    for bad in ("/café", "/카페", "/news/☕"):
        with pytest.raises(ValueError):
            redirect_response(bad)
    assert redirect_response("/caf%C3%A9").status == 307


def test_is_redirect_response_and_classify_response():
    assert is_redirect_response(redirect_response("/news"))
    assert is_redirect_response(Response("", status=308, headers=[(b"location", b"/x")]))
    assert not is_redirect_response(Response("", status=307))
    assert not is_redirect_response(Response("ok", status=200))
    assert classify_response(redirect_response("/news")) == "redirect"
    assert classify_response(Response("nope", status=403)) == "response"
    with pytest.raises(TypeError):
        classify_response("not a response")


def test_rewrite_request_copies_request_and_clears_params(tmp_path):
    make_project(tmp_path)
    # Capture a real Request via a probe middleware.
    seen = {}

    async def probe(request, call_next):
        seen["request"] = request
        request.state["kept"] = "yes"
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[probe])

    async def run_probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await client.get("/api?x=1")
        await app.isr.close()
    asyncio.run(run_probe())
    original = seen["request"]
    rewritten = rewrite_request(original, "/news")
    assert rewritten.path == "/news"
    assert rewritten.query_string == original.query_string
    assert rewritten.method == original.method
    assert rewritten.headers == original.headers
    assert rewritten.state is original.state  # shared, not copied
    assert rewritten.state["kept"] == "yes"
    assert rewritten.params == {}
    assert rewrite_request(original, "/news", "y=2").query_string == b"y=2"
    assert rewrite_request(original, "/news", b"y=3").query_string == b"y=3"
    with pytest.raises(TypeError):
        rewrite_request("not a request", "/news")
    for bad in ("relative", "/has?query", "/has#frag"):
        with pytest.raises(ValueError):
            rewrite_request(original, bad)


def test_middleware_result_constructors_validate_pairing():
    with pytest.raises(TypeError):
        MiddlewareResult.next("not a request")
    with pytest.raises(TypeError):
        MiddlewareResult.rewrite(None)
    with pytest.raises(TypeError):
        MiddlewareResult.response("not a response")
    with pytest.raises(ValueError):
        MiddlewareResult.redirect("/news", status=200)
    with pytest.raises(ValueError):
        MiddlewareResult("bogus", response=Response("x"))
    with pytest.raises(TypeError):
        MiddlewareResult("next", request=None)
    with pytest.raises(ValueError):
        MiddlewareResult("redirect", response=Response("x", status=200))
    with pytest.raises(ValueError):
        MiddlewareResult("response", request=Request("GET", "/", b"", (), b"", {}, None, {}),
                         response=Response("x"))
    result = MiddlewareResult.redirect("/news")
    assert result.kind == "redirect" and result.is_terminal and not result.is_continue
    assert result.response.status == 307
    assert MiddlewareResult.response(Response("ok")).kind == "response"
    assert MiddlewareResult.response(redirect_response("/news")).kind == "redirect"
    assert MAX_MIDDLEWARE_REWRITES == 5


# ---------------------------------------------------------------------------
# HTTP: implicit and explicit rewrites
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_implicit_rewrite_serves_rewritten_route(tmp_path, mode):
    make_project(tmp_path)

    async def rewrite_old(request, call_next):
        if request.path == "/old":
            return await call_next(rewrite_request(request, "/news"))
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[rewrite_old])

    async def check(client):
        response = await client.get("/old")
        assert response.status_code == 200
        assert "news" in response.text
        response = await client.get("/")
        assert response.status_code == 200
        assert "home" in response.text
    run_app(app, mode, check)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_explicit_rewrite_and_next_results(tmp_path, mode):
    make_project(tmp_path)

    async def rewrite_old(request, call_next):
        if request.path == "/old":
            return MiddlewareResult.rewrite(rewrite_request(request, "/news"))
        return MiddlewareResult.next(request)

    app = create_app(tmp_path, production=True, middleware=[rewrite_old])

    async def check(client):
        assert "news" in (await client.get("/old")).text
        assert "home" in (await client.get("/")).text
    run_app(app, mode, check)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_rewrite_rederives_params_and_shares_state(tmp_path, mode):
    make_project(tmp_path)

    async def rewrite_old(request, call_next):
        request.state["order"] = ["mw"]
        if request.path == "/old":
            return await call_next(rewrite_request(request, "/greet/alice"))
        if request.path == "/api-old":
            return await call_next(rewrite_request(request, "/api"))
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[rewrite_old])

    async def check(client):
        response = await client.get("/old")
        assert response.status_code == 200
        assert "hello alice" in response.text
        response = await client.get("/api-old")
        assert response.json() == {"query": {}, "order": ["mw"]}
    run_app(app, mode, check)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_query_only_rewrite(tmp_path, mode):
    make_project(tmp_path)

    async def rewrite_query(request, call_next):
        if request.path == "/api":
            return await call_next(rewrite_request(request, "/api", "x=2"))
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[rewrite_query])

    async def check(client):
        response = await client.get("/api?x=1")
        assert response.json()["query"] == {"x": ["2"]}
    run_app(app, mode, check)


# ---------------------------------------------------------------------------
# HTTP: short-circuit results (redirect / response)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_explicit_redirect_and_response_short_circuit(tmp_path, mode):
    make_project(tmp_path)
    reached = []

    async def downstream(request, call_next):
        reached.append(request.path)
        return await call_next(request)

    async def short_circuit(request, call_next):
        if request.path == "/moved":
            return MiddlewareResult.redirect("/news", status=302)
        if request.path == "/denied":
            return MiddlewareResult.response(Response("nope", status=403))
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[short_circuit, downstream])

    async def check(client):
        response = await client.get("/moved")
        assert response.status_code == 302
        assert response.headers["location"] == "/news"
        response = await client.get("/denied")
        assert response.status_code == 403
        assert response.text == "nope"
        assert reached == []
    run_app(app, mode, check)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_plain_redirect_response_still_short_circuits(tmp_path, mode):
    """The implicit form keeps working: a 3xx Response with Location."""
    make_project(tmp_path)

    async def redirect_old(request, call_next):
        if request.path == "/old":
            return Response("", status=308, headers=[(b"location", b"/news")])
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[redirect_old])

    async def check(client):
        response = await client.get("/old", follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["location"] == "/news"
    run_app(app, mode, check)


# ---------------------------------------------------------------------------
# HTTP: rewrite validation fails closed, loop cap, contract violations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["asgi", "axum"])
@pytest.mark.parametrize("bad_path", ["/%2Fnews", "/%5cnews", "/../news", "//news",
                                     "/news%00", "/news?x=1"])
def test_invalid_rewrite_fails_closed(tmp_path, mode, bad_path):
    make_project(tmp_path)

    async def rewrite_bad(request, call_next):
        if request.path == "/old":
            # Bypass rewrite_request's own shape checks so the framework
            # validation is what rejects the path.
            return await call_next(dataclasses.replace(request, path=bad_path))
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[rewrite_bad])

    async def check(client):
        assert (await client.get("/old")).status_code == 500
    run_app(app, mode, check)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_rewrite_loop_cap(tmp_path, mode):
    make_project(tmp_path)

    def hopper(index, target):
        async def hop(request, call_next):
            if request.path == f"/s{index}":
                return await call_next(rewrite_request(request, target))
            return await call_next(request)
        return hop

    # Exactly five rewrites (/s0 -> ... -> /s4 -> /news) succeed.
    app = create_app(tmp_path, production=True, middleware=[
        hopper(0, "/s1"), hopper(1, "/s2"), hopper(2, "/s3"),
        hopper(3, "/s4"), hopper(4, "/news"),
    ])

    async def check_ok(client):
        response = await client.get("/s0")
        assert response.status_code == 200
        assert "news" in response.text
    run_app(app, mode, check_ok)

    # A sixth rewrite fails closed with 500.
    app = create_app(tmp_path, production=True, middleware=[
        hopper(0, "/s1"), hopper(1, "/s2"), hopper(2, "/s3"),
        hopper(3, "/s4"), hopper(4, "/s5"), hopper(5, "/news"),
    ])

    async def check_capped(client):
        assert (await client.get("/s0")).status_code == 500
    run_app(app, mode, check_capped)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_result_after_call_next_and_bad_return_fail_closed(tmp_path, mode):
    make_project(tmp_path)

    async def result_after_next(request, call_next):
        if request.path == "/bad":
            await call_next(request)
            return MiddlewareResult.response(Response("late"))
        return await call_next(request)

    async def bad_return(request, call_next):
        if request.path == "/worse":
            return "not a response"
        return await call_next(request)

    app = create_app(tmp_path, production=True,
                     middleware=[result_after_next, bad_return])

    async def check(client):
        assert (await client.get("/bad")).status_code == 500
        assert (await client.get("/worse")).status_code == 500
    run_app(app, mode, check)


# ---------------------------------------------------------------------------
# HTTP: admission is decided once, on the original path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_rewrite_does_not_recharge_rate_limit(tmp_path, mode):
    """One token per request: the first rewritten request succeeds, and a
    second request from the same client is 429 — the token was spent once,
    not once per rewrite. (The 429 also proves admission is enforced at
    all on this transport.)"""
    make_project(tmp_path)

    async def rewrite_old(request, call_next):
        if request.path == "/old":
            return await call_next(rewrite_request(request, "/news"))
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[rewrite_old],
                     rate_limit=RateLimiter(1, 60))

    async def check(client):
        assert (await client.get("/old")).status_code == 200
        assert (await client.get("/old")).status_code == 429
    run_app(app, mode, check)


# ---------------------------------------------------------------------------
# WebSocket pre-flight (ASGI): redirect denies, rewrite keeps the upgrade
# ---------------------------------------------------------------------------

class _ScriptedConnection:
    def __init__(self, incoming):
        self._incoming = iter(incoming)
        self.sent = []

    async def receive(self):
        return next(self._incoming)

    async def send(self, message):
        self.sent.append(message)


def _ws_project(tmp_path):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        "from pynext import websocket\n"
        "@websocket\n"
        "async def handler(websocket):\n"
        "    await websocket.accept()\n"
        "    await websocket.send_text('public-data')\n"
        "    await websocket.receive()\n"
    )
    return tmp_path


def test_ws_redirect_result_denies_upgrade(tmp_path):
    _ws_project(tmp_path)

    async def redirect_ws(request, call_next):
        if request.path == "/ws":
            return MiddlewareResult.redirect("/login")
        return await call_next(request)

    app = create_app(tmp_path, middleware=[redirect_ws])

    async def run():
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws",
                   "headers": [(b"host", b"localhost"), (b"origin", b"http://localhost")],
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1008}]
        await app.isr.close()
    asyncio.run(run())


def test_ws_rewrite_keeps_upgrade(tmp_path):
    _ws_project(tmp_path)

    async def rewrite_ws(request, call_next):
        if request.path == "/ws":
            return await call_next(rewrite_request(request, "/ws", "x=1"))
        return await call_next(request)

    app = create_app(tmp_path, middleware=[rewrite_ws])

    async def run():
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app({"type": "websocket", "path": "/ws",
                   "headers": [(b"host", b"localhost"), (b"origin", b"http://localhost")],
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.accept"},
                             {"type": "websocket.send", "text": "public-data"},
                             {"type": "websocket.close", "code": 1000}]
        await app.isr.close()
    asyncio.run(run())
