"""Startup-compiled matchers, scoped middleware and per-path headers.

Covers ``pynext.matcher`` (compile/validate at startup, fail at startup)
and the ``App`` wiring: scoped middleware is skipped outside its scope,
per-path header rules apply in order with handler headers winning, and
any invalid matcher/condition/header fails app creation instead of
silently never matching.
"""

import asyncio

import httpx
import pytest

from pynext import (HeaderRule, Matcher, Response, ScopedMiddleware, create_app,
                    scoped_middleware)
from pynext.build import BuildError, write_manifest
from pynext.matcher import (compile_has_condition, compile_header_rules,
                            compile_matcher, compile_middleware_entries,
                            compile_scope)
from pynext.server import start_native


# ---------------------------------------------------------------------------
# compile_matcher
# ---------------------------------------------------------------------------

def test_matcher_root_and_literal():
    assert compile_matcher("/").matches("/") == {}
    assert compile_matcher("/").matches("/x") is None
    assert compile_matcher("/api/users").matches("/api/users") == {}
    assert compile_matcher("/api/users").matches("/api/users/") is None
    assert compile_matcher("/api/users").matches("/api/user") is None


def test_matcher_single_param_captures():
    matcher = compile_matcher("/users/[id]")
    assert matcher.matches("/users/42") == {"id": "42"}
    assert matcher.matches("/users/") is None
    assert matcher.matches("/users/42/posts") is None
    assert matcher.param_names == ("id",)


def test_matcher_catch_all_and_optional_catch_all():
    matcher = compile_matcher("/docs/[...path]")
    assert matcher.matches("/docs/a/b") == {"path": "a/b"}
    assert matcher.matches("/docs/a") == {"path": "a"}
    assert matcher.matches("/docs") is None
    optional = compile_matcher("/docs/[[...path]]")
    assert optional.matches("/docs") == {"path": None}
    assert optional.matches("/docs/a/b") == {"path": "a/b"}


def test_matcher_trailing_star_matches_prefix_itself():
    matcher = compile_matcher("/api/*")
    assert matcher.matches("/api") == {}
    assert matcher.matches("/api/") == {}
    assert matcher.matches("/api/users/1") == {}
    assert matcher.matches("/other") is None
    assert compile_matcher("/*").matches("/") == {}
    assert compile_matcher("/*").matches("/anything/at/all") == {}


def test_matcher_rejects_unmatchable_and_malformed_sources():
    for bad in (
        "api",                    # must start with /
        "/api/",                  # trailing slash
        "/api//users",            # empty segment
        "/api%2Fusers",           # % can never match a decoded path
        "/api?x=1",               # ? can never match a decoded path
        "/api#frag",              # # can never match a decoded path
        "/api/./users",           # dot segment
        "/api/../users",
        "/(group)/users",         # route groups never appear in a URL
        "/api/[...path]/more",    # catch-all must be last
        "/api/[[...path]]/more",  # optional catch-all must be last
        "/api/*/more",            # * must be last
        "/api/[id]/[id]",         # duplicate parameter
        "/api/[[id]]",            # [[...]] without dots
        "/api/[id",               # unbalanced brackets
        "/api/[.id]",             # invalid parameter name
        "/api/[id]extra",         # trailing junk after a parameter
        "/api/a[b",               # bracket inside a literal
        "/api/a*b",               # * inside a literal
        "/api\\users",            # backslash
        "/api/\x01users",         # control character
    ):
        with pytest.raises((ValueError, TypeError)):
            compile_matcher(bad)
    with pytest.raises(ValueError):
        compile_matcher("/" + "a" * 4096)
    with pytest.raises(TypeError):
        compile_matcher(None)
    with pytest.raises(TypeError):
        compile_matcher(b"/api")


def test_matcher_regex_specials_are_literal():
    matcher = compile_matcher("/api/v1.0/users")
    assert matcher.matches("/api/v1.0/users") == {}
    assert matcher.matches("/api/v1x0/users") is None


# ---------------------------------------------------------------------------
# has / missing conditions
# ---------------------------------------------------------------------------

def test_has_condition_presence_and_regex():
    present = compile_has_condition({"type": "header", "key": "X-Flag"})
    assert present.matches(headers=((b"x-flag", b"anything"),))
    assert not present.matches(headers=((b"x-other", b"1"),))
    assert not present.matches(headers=())
    regex = compile_has_condition({"type": "query", "key": "v", "value": r"\d+"})
    assert regex.matches(query={"v": ["42"]})
    assert not regex.matches(query={"v": ["abc"]})
    assert not regex.matches(query={})
    # The value must fully match, not just match a prefix.
    assert not regex.matches(query={"v": ["42x"]})


def test_has_condition_cookie_and_host():
    cookie = compile_has_condition({"type": "cookie", "key": "session"})
    assert cookie.matches(cookies={"session": "abc"})
    assert not cookie.matches(cookies={})
    host = compile_has_condition({"type": "host", "value": r"example\.com"})
    assert host.matches(host="example.com")
    assert not host.matches(host="other.com")
    assert not host.matches(host="")


def test_has_condition_rejects_bad_specs():
    for bad in (
        {"type": "header"},                       # key required
        {"type": "query", "key": "v", "extra": 1},  # unknown key
        {"type": "host", "key": "example.com"},   # host takes no key
        {"type": "bogus", "key": "x"},            # unknown type
        {"type": "header", "key": "bad key"},     # not a token
        {"type": "header", "key": "x", "value": "["},  # invalid regex
        {"type": "header", "key": "x", "value": ""},   # empty value
        {"type": "header", "key": "x", "value": "a" * 1025},
        {"type": "header", "key": 42},
    ):
        with pytest.raises((ValueError, TypeError)):
            compile_has_condition(bad)
    with pytest.raises(TypeError):
        compile_has_condition("header")


def test_scope_has_all_and_missing_none():
    scope = compile_scope(
        match="/api/*",
        has=[{"type": "header", "key": "x-flag"}],
        missing=[{"type": "query", "key": "debug"}],
    )
    assert scope.matches("/api/x", headers=((b"x-flag", b"1"),), query={})
    assert not scope.matches("/other", headers=((b"x-flag", b"1"),), query={})
    assert not scope.matches("/api/x", headers=(), query={})
    assert not scope.matches("/api/x", headers=((b"x-flag", b"1"),), query={"debug": ["1"]})
    global_scope = compile_scope()
    assert global_scope.matches("/anything", headers=(), query={})


def test_scope_matches_request_parses_cookies_and_host():
    scope = compile_scope(
        has=[{"type": "cookie", "key": "session", "value": r"abc"}],
        missing=[{"type": "host", "value": r"bad\.example"}],
    )
    assert scope.matches("/x", headers=((b"cookie", b"session=abc; other=1"),),
                         cookies={"session": "abc"}, host="good.example")
    assert not scope.matches("/x", headers=((b"cookie", b"session=abc"),),
                            cookies={"session": "abc"}, host="bad.example")


# ---------------------------------------------------------------------------
# scoped_middleware / compile_middleware_entries
# ---------------------------------------------------------------------------

def test_scoped_middleware_wrapper_and_decorator_forms():
    async def inner(request, call_next):
        return await call_next(request)

    wrapped = scoped_middleware(inner, match="/api/*")
    assert isinstance(wrapped, ScopedMiddleware)
    assert wrapped.scope.matcher.source == "/api/*"

    @scoped_middleware(match="/api/*")
    async def decorated(request, call_next):
        return await call_next(request)

    assert isinstance(decorated, ScopedMiddleware)
    assert decorated.scope.matcher.source == "/api/*"


def test_scoped_middleware_validates_scope_eagerly():
    async def inner(request, call_next):
        return await call_next(request)

    with pytest.raises(ValueError):
        scoped_middleware(inner, match="api/*")
    with pytest.raises(ValueError):
        scoped_middleware(inner, match="/api/*",
                          has=[{"type": "header", "key": "x", "value": "["}])
    with pytest.raises(TypeError):
        scoped_middleware(lambda request, call_next: None, match="/api/*")


def test_compile_middleware_entries():
    async def plain(request, call_next):
        return await call_next(request)

    scoped = scoped_middleware(plain, match="/api/*")
    entries = compile_middleware_entries([plain, scoped])
    assert entries[0] == (plain, None)
    assert entries[1][0] is plain and entries[1][1] is scoped.scope
    assert compile_middleware_entries(None) == ()
    assert compile_middleware_entries([]) == ()
    with pytest.raises(TypeError):
        compile_middleware_entries([lambda request, call_next: None])
    with pytest.raises(TypeError):
        compile_middleware_entries(["not-callable"])


# ---------------------------------------------------------------------------
# compile_header_rules
# ---------------------------------------------------------------------------

def test_compile_header_rules_forms_and_matching():
    rules = compile_header_rules([
        ("/api/*", {"Cache-Control": "no-store"}),
        {"source": "/api/[id]", "headers": {"X-Api": "1"},
         "has": [{"type": "header", "key": "x-flag"}]},
    ])
    assert len(rules) == 2
    assert isinstance(rules[0], HeaderRule)
    assert rules[0].matches("/api/x")
    assert not rules[0].matches("/other")
    assert rules[1].matches("/api/42", headers=((b"x-flag", b"yes"),))
    assert not rules[1].matches("/api/42", headers=())
    assert compile_header_rules(None) == ()


def test_compile_header_rules_rejects_invalid():
    for bad in (
        [{"source": "api", "headers": {"X-A": "1"}}],          # no leading /
        [{"source": "/api", "headers": {}}],                   # empty headers
        [{"source": "/api"}],                                 # missing headers
        [{"headers": {"X-A": "1"}}],                          # missing source
        [{"source": "/api", "headers": {"X-A": "1"}, "typo": 1}],  # unknown key
        [{"source": "/api", "headers": {"Bad Name": "1"}}],    # invalid name
        [{"source": "/api", "headers": {"Content-Length": "5"}}],  # framing
        [{"source": "/api", "headers": {"Transfer-Encoding": "chunked"}}],
        [{"source": "/api", "headers": {"X-A": "bad\nvalue"}}],  # control char
        [{"source": "/api", "headers": {"X-A": "é"}}],         # non-ASCII
        [{"source": "/api", "headers": {"X-A": "1", "x-a": "2"}}],  # dup (case-insensitive)
        [{"source": "/api", "headers": {"X-A": 5}}],           # non-str/bytes value
        [{"source": "/api%3Fx", "headers": {"X-A": "1"}}],     # % in source
        ["not-a-rule"],
        [{"source": "/api", "headers": [("X-A", "1")]}],       # headers must be a dict
    ):
        with pytest.raises((ValueError, TypeError)):
            compile_header_rules(bad)
    with pytest.raises(TypeError):
        compile_header_rules({"source": "/api", "headers": {"X-A": "1"}})
    with pytest.raises(ValueError):
        compile_header_rules([("/x", {"X-A": "1"})] * 1025)


def test_header_rule_values_may_be_bytes():
    rules = compile_header_rules([("/api/*", {"X-A": b"bytes"})])
    assert rules[0].headers == ((b"x-a", b"bytes"),)


# ---------------------------------------------------------------------------
# App integration
# ---------------------------------------------------------------------------

def make_project(root, middleware_py=None):
    app = root / "app"
    app.mkdir()
    (app / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\n'
        'def Page(): return h("p", {}, "static")\n'
    )
    api = app / "api"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request):\n'
        '    return JSONResponse({"seen": request.state.get("seen", [])}, '
        'headers=[(b"x-handler", b"yes")])\n'
    )
    (root / "public").mkdir()
    (root / "public" / "file.txt").write_text("asset")
    if middleware_py is not None:
        (root / "middleware.py").write_text(middleware_py)
    write_manifest(root)


def test_scoped_middleware_runs_only_in_scope(tmp_path):
    make_project(tmp_path)

    async def glob(request, call_next):
        request.state.setdefault("seen", []).append("global")
        response = await call_next(request)
        response.headers.append((b"x-global", b"yes"))
        return response

    async def api_only(request, call_next):
        request.state.setdefault("seen", []).append("scoped")
        response = await call_next(request)
        response.headers.append((b"x-scoped", b"yes"))
        return response

    app = create_app(tmp_path, production=True,
                     middleware=[glob, scoped_middleware(api_only, match="/api/*")])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api")
            assert response.json() == {"seen": ["global", "scoped"]}
            assert response.headers["x-global"] == "yes"
            assert response.headers["x-scoped"] == "yes"
            response = await client.get("/")
            assert response.headers["x-global"] == "yes"
            assert "x-scoped" not in response.headers
            response = await client.get("/file.txt")
            assert response.headers["x-global"] == "yes"
            assert "x-scoped" not in response.headers

    asyncio.run(run())


def test_scoped_middleware_has_and_missing_conditions(tmp_path):
    make_project(tmp_path)

    async def flagged(request, call_next):
        response = await call_next(request)
        response.headers.append((b"x-flagged", b"yes"))
        return response

    app = create_app(
        tmp_path, production=True,
        middleware=[scoped_middleware(
            flagged, match="/api/*",
            has=[{"type": "header", "key": "x-flag"}],
            missing=[{"type": "query", "key": "debug"}])])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert "x-flagged" not in (await client.get("/api")).headers
            assert (await client.get("/api", headers={"x-flag": "1"})).headers["x-flagged"] == "yes"
            assert "x-flagged" not in (
                await client.get("/api?debug=1", headers={"x-flag": "1"})).headers

    asyncio.run(run())


def test_header_rules_apply_in_order_handler_wins(tmp_path):
    # NB: create_app headers= are compiled twice — by compile_path_headers
    # (router grammar: no '*' globs, see tests/test_path_headers.py) and by
    # compile_header_rules (matcher grammar) — so sources here use the
    # router grammar. '/api/[[...slug]]' matches '/api' itself plus any
    # depth below, exactly like the matcher glob '/api/*'; '[...slug]'
    # would NOT match '/api' itself. '*' globs stay valid for
    # compile_matcher, scoped_middleware scopes and middleware.py HEADERS,
    # which only go through the matcher grammar.
    make_project(tmp_path)
    app = create_app(tmp_path, production=True, headers=[
        ("/api/[[...slug]]", {"Cache-Control": "no-store", "X-Rule": "first"}),
        ("/api/[[...slug]]", {"X-Rule": "second", "X-Second": "yes"}),
        ("/", {"X-Home": "1"}),
    ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api")
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-rule"] == "first"  # first rule wins
            assert response.headers["x-second"] == "yes"
            assert response.headers["x-handler"] == "yes"
            response = await client.get("/")
            assert "cache-control" not in response.headers or \
                response.headers["cache-control"] != "no-store"
            assert response.headers["x-home"] == "1"
            assert "x-rule" not in response.headers

    asyncio.run(run())


def test_header_rule_does_not_overwrite_handler_header(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path, production=True, headers=[
        ("/api/[[...slug]]", {"X-Handler": "rule"}),
    ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api")
            assert response.headers["x-handler"] == "yes"  # handler wins

    asyncio.run(run())


def test_header_rule_csp_replaces_default_but_not_explicit(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\n'
        'def Page(): return h("p", {}, "static")\n'
    )
    api = tmp_path / "app" / "api"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"ok": True})\n'
    )
    explicit = tmp_path / "app" / "explicit"
    explicit.mkdir()
    (explicit / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request):\n'
        '    return JSONResponse({"ok": True}, headers=[(b"content-security-policy", '
        'b"default-src \'none\'")])\n'
    )
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True, headers=[
        ("/[[...slug]]", {"Content-Security-Policy": "default-src 'self'"}),
    ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api")
            assert response.headers["content-security-policy"] == "default-src 'self'"
            response = await client.get("/explicit")
            assert response.headers["content-security-policy"] == "default-src 'none'"

    asyncio.run(run())


def test_header_rules_apply_to_static_assets_and_404(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path, production=True, headers=[
        ("/file.txt", {"X-Asset": "yes"}),
        ("/[[...slug]]", {"X-Every": "yes"}),
    ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/file.txt")
            assert response.headers["x-asset"] == "yes"
            assert response.headers["x-every"] == "yes"
            response = await client.get("/missing")
            assert response.status_code == 404
            assert response.headers["x-every"] == "yes"

    asyncio.run(run())


def test_middleware_py_headers_are_picked_up(tmp_path):
    make_project(tmp_path, middleware_py=(
        'from pynext import Response\n'
        'async def layer(request, call_next):\n'
        '    response = await call_next(request)\n'
        '    response.headers.append((b"x-file", b"yes"))\n'
        '    return response\n'
        'MIDDLEWARE = [layer]\n'
        'HEADERS = [("/api/*", {"Cache-Control": "no-store"})]\n'
    ))
    app = create_app(tmp_path, production=True)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api")
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-file"] == "yes"

    asyncio.run(run())


def test_invalid_matcher_or_headers_fail_startup(tmp_path):
    make_project(tmp_path)
    with pytest.raises(ValueError):
        create_app(tmp_path, production=True,
                   headers=[{"source": "api", "headers": {"X-A": "1"}}])
    with pytest.raises(ValueError):
        create_app(tmp_path, production=True,
                   headers=[("/api/*", {"Content-Length": "5"})])
    with pytest.raises(TypeError):
        create_app(tmp_path, production=True,
                   middleware=[lambda request, call_next: None])
    with pytest.raises(TypeError):
        create_app(tmp_path, production=True, headers="not-a-list")


def test_invalid_middleware_py_headers_fail_startup(tmp_path):
    # write_manifest builds a dev App to discover routes, so the invalid
    # HEADERS already fail there — the point is app creation fails, not
    # that the rule silently never matches.
    with pytest.raises(ValueError):
        make_project(tmp_path, middleware_py=(
            'MIDDLEWARE = []\n'
            'HEADERS = [{"source": "/api/*", "headers": {"X-A": "bad\\nvalue"}}]\n'
        ))


def test_invalid_middleware_py_middleware_fails_startup(tmp_path):
    # write_manifest wraps any render-stage failure in BuildError (which
    # subclasses ValueError), so a bad MIDDLEWARE entry surfaces as a
    # BuildError naming the render stage, not the raw TypeError — same as
    # the invalid-HEADERS case above, which passes only because BuildError
    # is a ValueError. Match on the message to keep the cause precise.
    with pytest.raises(BuildError, match="middleware entries must be async callables"):
        make_project(tmp_path, middleware_py='MIDDLEWARE = [lambda r, n: None]\n')


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_scoped_middleware_and_headers_on_both_transports(tmp_path, mode):
    make_project(tmp_path)

    async def api_only(request, call_next):
        response = await call_next(request)
        response.headers.append((b"x-scoped", b"yes"))
        return response

    app = create_app(tmp_path, production=True,
                     middleware=[scoped_middleware(api_only, match="/api/*")],
                     headers=[("/api/[[...slug]]", {"Cache-Control": "no-store"})])

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                response = await client.get("/api")
                assert response.headers["x-scoped"] == "yes"
                assert response.headers["cache-control"] == "no-store"
                response = await client.get("/")
                assert "x-scoped" not in response.headers
                assert response.headers.get("cache-control") != "no-store"
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())
