import asyncio
from pathlib import Path

import httpx
import pytest

from pynext import Response, create_app
from pynext.custom_routes import CustomRoutes, compile_custom_routes

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"


def run(coro):
    return asyncio.run(coro)


# --- compile-time validation -------------------------------------------


def test_none_means_empty_and_falsy():
    routes = compile_custom_routes(None)
    assert not routes
    assert routes.match_redirect("/x") is None
    assert routes.match_rewrite("/x", "beforeFiles") is None
    assert routes.headers_for("/x") == []


def test_empty_config_is_empty():
    assert not compile_custom_routes({})


def test_config_must_be_object_with_known_keys():
    with pytest.raises(ValueError):
        compile_custom_routes([])
    with pytest.raises(ValueError):
        compile_custom_routes({"redirect": []})


@pytest.mark.parametrize("source", ["old", "", "/healthz", "/readyz", "/_pynext", "/_pynext/x"])
def test_reserved_or_relative_source_rejected(source):
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": source, "destination": "/x"}]})


def test_destination_must_name_known_parameter():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old/[slug]",
                                              "destination": "/blog/[other]"}]})


def test_destination_must_be_ascii_and_control_free():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/blog/한글"}]})
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/blog/a\x01b"}]})


def test_destination_must_not_contain_dot_segments():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/blog/../x"}]})


def test_destination_must_not_contain_backslash_a6_l2():
    """A6-L2: a backslash in the redirect destination path is rejected.

    Browsers (WHATWG URL) treat "\\" as "/" for http(s), so a Location
    of "/\\evil.com" resolves to "//evil.com" — an open redirect of the
    same class as A6-M1. The percent-encoded form stays accepted.
    """
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/\\evil.com"}]})
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "https://example.com/\\evil.com"}]})
    routes = compile_custom_routes(
        {"redirects": [{"source": "/old", "destination": "/%5cevil.com"}]})
    assert routes.match_redirect("/old") == (307, "/%5cevil.com")


def test_redirect_destination_may_be_absolute_but_rewrite_may_not():
    routes = compile_custom_routes(
        {"redirects": [{"source": "/out", "destination": "https://example.com/x"}]})
    assert routes.match_redirect("/out") == (307, "https://example.com/x")
    with pytest.raises(ValueError):
        compile_custom_routes({"rewrites": [{"source": "/api", "destination": "https://example.com/x"}]})
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/out", "destination": "https://"}]})


def test_redirect_destination_may_not_be_reserved():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/healthz"}]})


def test_permanent_must_be_bool():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/x",
                                              "permanent": "yes"}]})


def test_rule_shape_is_checked():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old"}]})
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/x", "extra": 1}]})
    with pytest.raises(ValueError):
        compile_custom_routes({"rewrites": [{"source": "/old", "destination": "/x", "permanent": True}]})


def test_rewrites_accept_list_or_phase_object():
    routes = compile_custom_routes({"rewrites": [{"source": "/a", "destination": "/b"}]})
    assert routes.match_rewrite("/a", "beforeFiles") == "/b"
    assert routes.match_rewrite("/a", "afterFiles") is None
    routes = compile_custom_routes(
        {"rewrites": {"afterFiles": [{"source": "/a", "destination": "/b"}]}})
    assert routes.match_rewrite("/a", "beforeFiles") is None
    assert routes.match_rewrite("/a", "afterFiles") == "/b"
    with pytest.raises(ValueError):
        compile_custom_routes({"rewrites": {"before": [{"source": "/a", "destination": "/b"}]}})


@pytest.mark.parametrize("headers", [[], [{"key": "X-A", "value": "1", "extra": 2}],
                                     [{"key": "Bad Key", "value": "1"}],
                                     [{"key": "X-A", "value": "1"}, {"key": "x-a", "value": "2"}],
                                     [{"key": "X-A", "value": "a\x01b"}],
                                     [{"key": "X-A", "value": "한글"}],
                                     [{"key": "X-A"}]])
def test_bad_header_rules_rejected(headers):
    with pytest.raises(ValueError):
        compile_custom_routes({"headers": [{"source": "/x", "headers": headers}]})


def test_duplicate_sources_are_rejected_by_the_router():
    with pytest.raises(ValueError):
        compile_custom_routes({"redirects": [{"source": "/old", "destination": "/a"},
                                             {"source": "/old", "destination": "/b"}]})


# --- compiled matching ---------------------------------------------------


def test_redirect_status_and_interpolation():
    routes = compile_custom_routes(
        {"redirects": [{"source": "/old/[slug]", "destination": "/blog/[slug]"},
                       {"source": "/gone", "destination": "/about", "permanent": True}]})
    assert routes.match_redirect("/old/hello") == (307, "/blog/hello")
    assert routes.match_redirect("/old/a%20b") == (307, "/blog/a%2520b")
    assert routes.match_redirect("/gone") == (308, "/about")
    assert routes.match_redirect("/other") is None


def test_rewrite_interpolates_catch_all_segments():
    routes = compile_custom_routes(
        {"rewrites": [{"source": "/files/[...rest]", "destination": "/static/[rest]"}]})
    assert routes.match_rewrite("/files/a/b", "beforeFiles") == "/static/a/b"


def test_destination_rejects_catch_all_placeholder_syntax():
    # Destinations interpolate parameters as [name], not [...name].
    with pytest.raises(ValueError):
        compile_custom_routes(
            {"rewrites": [{"source": "/files/[...rest]", "destination": "/static/[...rest]"}]})


def test_headers_for_returns_matching_rule():
    routes = compile_custom_routes(
        {"headers": [{"source": "/blog/[slug]",
                      "headers": [{"key": "Cache-Control", "value": "public, max-age=3600"}]}]})
    assert routes.headers_for("/blog/hello") == [("Cache-Control", "public, max-age=3600")]
    assert routes.headers_for("/about") == []


def test_headers_for_merges_overlapping_rules():
    # P2-4: a global catch-all rule and a specific rule both apply.
    # Previously only the router's single best match was returned, so
    # the global header was dropped for /blog/hello.
    routes = compile_custom_routes(
        {"headers": [{"source": "/[[...path]]",
                      "headers": [{"key": "X-Global", "value": "all"}]},
                     {"source": "/blog/[slug]",
                      "headers": [{"key": "X-Blog", "value": "blog"}]}]})
    assert routes.headers_for("/blog/hello") == [("X-Global", "all"), ("X-Blog", "blog")]
    assert routes.headers_for("/other") == [("X-Global", "all")]
    assert routes.headers_for("/") == [("X-Global", "all")]


def test_headers_for_later_rule_wins_duplicate_key():
    # Next.js: "If two headers match the same path and set the same
    # header key, the last header key will override the first", so a
    # specific rule listed after a catch-all wins the shared key while
    # the catch-all's other headers still apply.
    routes = compile_custom_routes(
        {"headers": [{"source": "/[[...path]]",
                      "headers": [{"key": "X-Shared", "value": "global"},
                                  {"key": "X-Global", "value": "all"}]},
                     {"source": "/blog/[slug]",
                      "headers": [{"key": "X-Shared", "value": "blog"},
                                  {"key": "X-Blog", "value": "blog"}]}]})
    assert routes.headers_for("/blog/hello") == [
        ("X-Shared", "blog"), ("X-Global", "all"), ("X-Blog", "blog")]


def test_headers_for_catch_all_and_static_overlap():
    routes = compile_custom_routes(
        {"headers": [{"source": "/[...rest]",
                      "headers": [{"key": "X-All", "value": "yes"}]},
                     {"source": "/about",
                      "headers": [{"key": "X-About", "value": "yes"}]}]})
    assert routes.headers_for("/about") == [("X-All", "yes"), ("X-About", "yes")]
    # A non-optional catch-all needs at least one segment.
    assert routes.headers_for("/") == []


def test_apply_to_response_replaces_existing_header():
    routes = compile_custom_routes(
        {"headers": [{"source": "/x", "headers": [{"key": "X-Test", "value": "new"}]}]})
    response = Response("ok", headers=[(b"x-test", b"old"), (b"x-other", b"keep")])
    routes.apply_to_response(response, "/x")
    headers = dict(response.headers)
    assert headers[b"x-test"] == b"new"
    assert headers[b"x-other"] == b"keep"


# --- end-to-end through the app ------------------------------------------


def _client(**kwargs):
    return httpx.ASGITransport(app=create_app(EXAMPLE, **kwargs))


def test_redirect_temporary_preserves_query_and_interpolates():
    async def run_case():
        transport = _client(custom_routes={
            "redirects": [{"source": "/old/[slug]", "destination": "/blog/[slug]"}]})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/old/hello?x=1", follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/blog/hello?x=1"
    run(run_case())


def test_redirect_permanent_308():
    async def run_case():
        transport = _client(custom_routes={
            "redirects": [{"source": "/gone", "destination": "/about", "permanent": True}]})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/gone", follow_redirects=False)
            assert response.status_code == 308
            assert response.headers["location"] == "/about"
    run(run_case())


def test_redirect_wins_over_existing_route():
    async def run_case():
        transport = _client(custom_routes={
            "redirects": [{"source": "/blog/[slug]", "destination": "/news/[slug]",
                           "permanent": True}]})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/blog/hello", follow_redirects=False)
            assert response.status_code == 308
            assert response.headers["location"] == "/news/hello"
    run(run_case())


def test_rewrite_before_files_serves_destination():
    async def run_case():
        transport = _client(custom_routes={
            "rewrites": [{"source": "/legacy/[slug]", "destination": "/blog/[slug]"}]})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/legacy/hello")
            assert response.status_code == 200
            assert "Blog post" in response.text and "hello" in response.text
    run(run_case())


def test_rewrite_after_files_only_on_miss():
    async def run_case():
        transport = _client(custom_routes={
            "rewrites": {"afterFiles": [{"source": "/gone/[slug]", "destination": "/blog/[slug]"}]}})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/gone/hello")
            assert response.status_code == 200
            assert "Blog post" in response.text
            # An existing route is not rewritten by afterFiles.
            response = await client.get("/blog/hello")
            assert response.status_code == 200
    run(run_case())


def test_rewrite_fallback_serves_destination_on_miss():
    async def run_case():
        transport = _client(custom_routes={
            "rewrites": {"fallback": [{"source": "/lost/[slug]", "destination": "/about"}]}})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/lost/anything")
            assert response.status_code == 200
            assert "About" in response.text
            assert (await client.get("/no/such/route")).status_code == 404
    run(run_case())


def test_rewrite_applies_only_once():
    async def run_case():
        transport = _client(custom_routes={
            "rewrites": [{"source": "/a", "destination": "/blog/hello"},
                         {"source": "/blog/[slug]", "destination": "/about"}]})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/a")
            assert response.status_code == 200
            assert "Blog post" in response.text
            assert "About" not in response.text
    run(run_case())


def test_headers_apply_to_page_redirect_and_404():
    async def run_case():
        transport = _client(custom_routes={
            "headers": [
                {"source": "/about",
                 "headers": [{"key": "X-About", "value": "yes"}]},
                {"source": "/old",
                 "headers": [{"key": "X-Old", "value": "yes"}]},
                {"source": "/missing/[slug]",
                 "headers": [{"key": "X-Missing", "value": "yes"}]},
            ],
            "redirects": [{"source": "/old", "destination": "/about"}]})
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/about")).headers["x-about"] == "yes"
            response = await client.get("/old", follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["x-old"] == "yes"
            response = await client.get("/missing/thing")
            assert response.status_code == 404
            assert response.headers["x-missing"] == "yes"
    run(run_case())


def test_bad_config_fails_at_startup():
    with pytest.raises(ValueError):
        create_app(EXAMPLE, custom_routes={"redirects": [{"source": "/old",
                                                          "destination": "/blog/[nope]"}]})


def test_custom_routes_attribute_exposed_on_app():
    app = create_app(EXAMPLE, custom_routes={"redirects": [{"source": "/old", "destination": "/x"}]})
    assert isinstance(app.custom_routes, CustomRoutes)
    assert bool(app.custom_routes)
    assert not create_app(EXAMPLE).custom_routes
