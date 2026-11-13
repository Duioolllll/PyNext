"""Per-path response headers (Next.js ``headers()`` equivalent, REPORT.md §2.2 D#2).

Rules are declared on the app (``create_app(..., headers=[...])``), compiled
and validated at startup, and applied to every HTTP response whose decoded
path matches the rule source — routed pages and API routes, framework-served
paths (``/_pynext/`` assets, public files, ``/healthz``), and responses the
router never reaches (404/405/308). The Rust fast path applies the same
rules to the static pages, public/client assets, cached ISR pages and
rate-limit denials it serves without Python dispatch.
"""

import asyncio

import httpx
import pytest

from pynext import (
    JSONResponse,
    PathHeaderRule,
    PathHeaders,
    Response,
    apply_path_headers,
    apply_security_headers,
    compile_path_headers,
    create_app,
)
from pynext._native import Router
from pynext.build import write_manifest
from pynext.server import start_native


def run(coro):
    return asyncio.run(coro)


# --- Rule compilation and matching ---


@pytest.mark.parametrize("source,path,expected", [
    ("/api/users", "/api/users", True),
    ("/api/users", "/api/USERs", False),  # case-sensitive, like routing
    ("/api/users", "/api/users/", False),
    ("/api/users", "/api/users/x", False),
    ("/api/[id]", "/api/42", True),
    ("/api/[id]", "/api/42/x", False),
    ("/api/[id]", "/api/", False),
    ("/api/[...rest]", "/api/a", True),
    ("/api/[...rest]", "/api/a/b", True),
    ("/api/[...rest]", "/api", False),
    ("/docs/[[...path]]", "/docs", True),
    ("/docs/[[...path]]", "/docs/a", True),
    ("/docs/[[...path]]", "/docs/a/b", True),
    ("/docs/[[...path]]", "/other", False),
    ("/[[...rest]]", "/", True),
    ("/[[...rest]]", "/a", True),
    ("/[[...rest]]", "/a/b/c", True),
    ("/", "/", True),
    ("/", "/a", False),
    ("/a/[x]/d", "/a/b/d", True),
    ("/a/[x]/d", "/a/b/c", False),
])
def test_path_header_rule_matches_route_patterns(source, path, expected):
    rule = PathHeaderRule(source, {"X-Test": "1"})
    assert rule.matches(path) is expected


def test_path_headers_merge_all_matching_rules_later_wins():
    rules = compile_path_headers([
        ("/api/[...rest]", {"Cache-Control": "no-store", "X-Api": "1"}),
        ("/api/users", {"X-Api": "2", "X-Extra": "yes"}),
        ("/api/[id]", {"X-Id": "dyn"}),
    ])
    assert dict(rules.headers_for("/api/users")) == {
        b"cache-control": b"no-store",
        b"x-api": b"2",  # later rule wins for the shared name
        b"x-extra": b"yes",
        b"x-id": b"dyn",  # the dynamic rule matches too
    }
    assert dict(rules.headers_for("/api/a/b")) == {
        b"cache-control": b"no-store",
        b"x-api": b"1",
    }
    assert rules.headers_for("/other") == ()


def test_path_header_rule_accepts_mapping_and_pairs():
    from_mapping = compile_path_headers([("/api", {"X-A": "1"})])
    from_pairs = compile_path_headers([("/api", [("X-A", "1")])])
    assert from_mapping.headers_for("/api") == from_pairs.headers_for("/api")
    assert compile_path_headers([PathHeaderRule("/api", {"X-A": "1"})]).headers_for("/api") == (
        (b"x-a", b"1"),)
    assert len(compile_path_headers()) == 0
    assert not compile_path_headers()
    assert len(PathHeaders([PathHeaderRule("/api", {"X-A": "1"})])) == 1


@pytest.mark.parametrize("source", [
    "api/users",  # not absolute
    "/api/",  # trailing slash
    "/api//users",  # empty segment
    "/api/./users",
    "/api/../users",
    "/api/[",  # unbalanced brackets
    "/api/foo[bar]",  # brackets inside a literal segment
    "/api/[a]/[...b]/c",  # catch-all not last
    "/api/[[a]]",  # optional without ...
    "/api/[a]/[a]",  # repeated parameter
    "/api/[my-id]/[my_id]",  # ambiguous parameter (same comparable name)
    "/api/[]",
    "/api/[...]",
    "/api/*",  # globs are not part of the grammar
    "/api/x?y",
    "/api/x#y",
    "/api/x\\y",
])
def test_compile_path_headers_rejects_bad_sources(source):
    with pytest.raises(ValueError, match="path header source"):
        compile_path_headers([(source, {"X-A": "1"})])


@pytest.mark.parametrize("headers", [
    {},  # a rule must set at least one header
    {"X-A": "a\nb"},  # control character in value
    {"X-A": "héllo"},  # non-ASCII value
    {"Bad Name": "1"},  # not a header token
    {"Content-Length": "5"},  # framing header owned by the server
    {"Transfer-Encoding": "chunked"},
    {"X-A": "1", "x-a": "2"},  # duplicate name (case-insensitive)
])
def test_compile_path_headers_rejects_bad_headers(headers):
    with pytest.raises((ValueError, TypeError)):
        compile_path_headers([("/api", headers)])


def test_compile_path_headers_rejects_wrong_types():
    with pytest.raises(TypeError):
        compile_path_headers("/api")
    with pytest.raises(ValueError):
        # A bare string is iterable, but its items are not (name, value) pairs.
        compile_path_headers([("/api", "X-A: 1")])
    with pytest.raises(TypeError):
        compile_path_headers([(42, {"X-A": "1"})])
    with pytest.raises(TypeError):
        compile_path_headers([("/api", {"X-A": 1})])


def test_python_matcher_agrees_with_rust_router():
    sources = ["/api/[id]", "/api/[...rest]", "/docs/[[...path]]", "/[[...rest]]",
               "/api/users", "/", "/a/[x]/d"]
    paths = ["/", "/api/users", "/api/42", "/api/a/b", "/api", "/docs", "/docs/a/b",
             "/other", "/a", "/a/b/c", "/a/b/d", "/a/x"]
    for source in sources:
        router = Router([source])
        rule = PathHeaderRule(source, {"X-A": "1"})
        for path in paths:
            assert rule.matches(path) is (router.matches(path) is not None), (source, path)


# --- Application to responses ---


def test_apply_path_headers_handler_wins_and_beats_defaults():
    rules = compile_path_headers([
        ("/api", {"X-Frame-Options": "SAMEORIGIN", "X-Api": "1"}),
    ])
    # A handler-set header wins over the rule.
    response = Response("ok", headers=[(b"x-api", b"handler")])
    apply_path_headers(response, "/api", path_headers=rules)
    assert [v for k, v in response.headers if k == b"x-api"] == [b"handler"]
    # But the rule beats the framework default: apply_security_headers
    # afterwards keeps the rule's X-Frame-Options instead of DENY.
    apply_security_headers(response, production=False)
    assert [v for k, v in response.headers if k == b"x-frame-options"] == [b"SAMEORIGIN"]
    assert [v for k, v in response.headers if k == b"x-content-type-options"] == [b"nosniff"]


def test_apply_path_headers_csp_rule_replaces_default_but_not_explicit():
    rules = compile_path_headers([("/admin", {"Content-Security-Policy": "script-src 'none'"})])
    response = Response("ok")
    apply_path_headers(response, "/admin", path_headers=rules)
    apply_security_headers(response, production=False)
    assert [v for k, v in response.headers if k == b"content-security-policy"] == [b"script-src 'none'"]
    # A handler that set CSP explicitly keeps it.
    explicit = Response("ok", headers=[(b"content-security-policy", b"script-src 'self'")])
    apply_path_headers(explicit, "/admin", path_headers=rules)
    assert [v for k, v in explicit.headers if k == b"content-security-policy"] == [b"script-src 'self'"]


def test_apply_path_headers_is_idempotent_and_noop_without_rules():
    rules = compile_path_headers([("/api", {"X-Api": "1"})])
    response = Response("ok")
    apply_path_headers(response, "/api", path_headers=rules)
    apply_path_headers(response, "/api", path_headers=rules)
    assert [v for k, v in response.headers if k == b"x-api"] == [b"1"]
    plain = Response("ok")
    apply_path_headers(plain, "/api")
    assert b"x-api" not in [k for k, _ in plain.headers]


# --- ASGI integration ---


HEADERS = [
    ("/api/[...rest]", {"Cache-Control": "no-store", "X-Api": "1"}),
    ("/api/echo", {"X-Echo": "yes"}),
    ("/", {"X-Home": "1"}),
    ("/healthz", {"X-Health": "1"}),
    ("/[[...rest]]", {"X-Root": "1"}),
]


def make_project(root):
    api = root / "app" / "api" / "echo"
    api.mkdir(parents=True)
    (api / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"GET\")\n"
        "async def get_echo():\n"
        "    return JSONResponse({\"ok\": True}, headers=[(b\"x-api\", b\"handler\")])\n"
    )
    (root / "app" / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    public = root / "public"
    public.mkdir()
    (public / "data.bin").write_bytes(b"0123456789")


def test_path_headers_asgi(tmp_path):
    make_project(tmp_path)

    async def exercise(middleware):
        app = create_app(tmp_path, headers=HEADERS, middleware=middleware)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            echo = await client.get("/api/echo")
            assert echo.status_code == 200
            # Handler-set X-Api wins; the other rule headers still apply.
            assert echo.headers["x-api"] == "handler"
            assert echo.headers["cache-control"] == "no-store"
            assert echo.headers["x-echo"] == "yes"
            assert echo.headers["x-root"] == "1"
            # A rule also reaches a 404 and a 405 the router never serves.
            missing = await client.get("/missing")
            assert missing.status_code == 404
            assert missing.headers["x-root"] == "1"
            assert "x-api" not in missing.headers
            method = await client.post("/api/echo")
            assert method.status_code == 405
            assert method.headers["cache-control"] == "no-store"
            # Health probes and public assets are framework-served paths.
            health = await client.get("/healthz")
            assert health.headers["x-health"] == "1"
            assert health.headers["x-root"] == "1"
            asset = await client.get("/data.bin")
            assert asset.headers["x-root"] == "1"
            home = await client.get("/")
            assert home.headers["x-home"] == "1"
            assert home.headers["x-root"] == "1"
            head = await client.head("/api/echo")
            assert head.headers["cache-control"] == "no-store"

    async def passthrough(request, call_next):
        return await call_next(request)

    run(exercise(()))
    run(exercise((passthrough,)))


def test_path_headers_csp_rule_asgi(tmp_path):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path, headers=[
            ("/api/[...rest]", {"Content-Security-Policy": "script-src 'none'"}),
        ])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            echo = await client.get("/api/echo")
            assert echo.headers["content-security-policy"] == "script-src 'none'"
            home = await client.get("/")
            assert home.headers["content-security-policy"].startswith("script-src 'self'")
    run(exercise())


def test_path_headers_bad_config_fails_at_startup(tmp_path):
    make_project(tmp_path)
    with pytest.raises(ValueError, match="path header source"):
        create_app(tmp_path, headers=[("/api/*", {"X-A": "1"})])
    with pytest.raises(ValueError, match="Content-Length"):
        create_app(tmp_path, headers=[("/api", {"Content-Length": "5"})])


# --- Native integration (Rust fast path + bridge) ---
#
# The native clients use trust_env=False: this VM's no_proxy list carries
# bracketed IPv6 entries ([::1], ...) that httpx cannot parse as proxy
# patterns, and loopback requests must bypass the egress proxy anyway.


def test_path_headers_native(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)

    async def exercise():
        app = create_app(tmp_path, production=True, headers=HEADERS)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10, trust_env=False) as client:
                echo = await client.get("/api/echo")
                assert echo.status_code == 200
                assert echo.headers["x-api"] == "handler"
                assert echo.headers["cache-control"] == "no-store"
                assert echo.headers["x-echo"] == "yes"
                missing = await client.get("/missing")
                assert missing.status_code == 404
                assert missing.headers["x-root"] == "1"
                # Static page and public asset are served by the Rust layer
                # without Python dispatch: prove the rules still apply by
                # breaking dispatch and re-requesting.
                async def fail_dispatch(request):
                    raise AssertionError("native static hit crossed the Python boundary")
                app.dispatch = fail_dispatch
                home = await client.get("/")
                assert home.status_code == 200
                assert home.headers["x-home"] == "1"
                assert home.headers["x-root"] == "1"
                asset = await client.get("/data.bin")
                assert asset.status_code == 200
                assert asset.headers["x-root"] == "1"
        finally:
            await asyncio.to_thread(server.stop)
    run(exercise())


def test_path_headers_native_csp_rule(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)

    async def exercise():
        app = create_app(tmp_path, production=True, headers=[
            ("/", {"Content-Security-Policy": "script-src 'none'"}),
        ])
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10, trust_env=False) as client:
                home = await client.get("/")
                assert home.headers["content-security-policy"] == "script-src 'none'"
                asset = await client.get("/data.bin")
                assert asset.headers["content-security-policy"].startswith("script-src 'self'")
        finally:
            await asyncio.to_thread(server.stop)
    run(exercise())


def test_path_headers_native_isr_cache_hit(tmp_path):
    directory = tmp_path / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=30)\n'
        'def Page(params): return h("p", {}, params["slug"])\n'
    )
    write_manifest(tmp_path)

    async def exercise():
        app = create_app(
            tmp_path, production=True,
            headers=[("/blog/[...rest]", {"Cache-Control": "no-store", "X-Blog": "1"})],
        )
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client, \
                    httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10, trust_env=False) as native:
                # Warm the ISR cache through ASGI; the native server then
                # serves the cached page without Python dispatch, and the
                # rule headers must still apply to that Rust-built response.
                # The ISR cache-control is set by an earlier dispatch step,
                # so (like ASGI) it wins over the rule's Cache-Control —
                # only the rule's other headers are added. The value is the
                # route's ISR policy (revalidate=30), not the old default.
                warmed = await client.get("/blog/alpha")
                assert warmed.status_code == 200
                assert warmed.headers["x-blog"] == "1"
                assert warmed.headers["cache-control"] == "public, s-maxage=30, stale-while-revalidate=60"
                cached = await native.get("/blog/alpha")
                assert cached.status_code == 200
                assert cached.headers["x-blog"] == "1"
                assert cached.headers["cache-control"] == "public, s-maxage=30, stale-while-revalidate=60"
                # A cache miss goes through the bridge and gets them too.
                fresh = await native.get("/blog/beta")
                assert fresh.status_code == 200
                assert fresh.headers["x-blog"] == "1"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    run(exercise())
