"""P1-4: custom_routes must behave identically on ASGI and native.

The native fast path serves static pages/assets without Python
dispatch, so it used to ignore custom routes: a redirect on an SSG
page answered 307 on ASGI but 200 with the original body on native.
``start_native`` now leaves custom-routed paths out of the fast-path
tables, so those requests fall through to the Python bridge (the same
dispatch as ASGI).
"""
import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native
from pynext.custom_routes import compile_custom_routes

CUSTOM_ROUTES = {
    "redirects": [
        {"source": "/old", "destination": "/new"},
        {"source": "/legacy.txt", "destination": "/new"},
    ],
    "rewrites": [{"source": "/alias", "destination": "/new"}],
    "headers": [
        {"source": "/old", "headers": [{"key": "X-Custom", "value": "yes"}]},
        {"source": "/headed", "headers": [{"key": "X-Headed", "value": "yes"}]},
    ],
}


def _build_project(tmp_path):
    pages = {
        "old": "original-page",
        "new": "new-page",
        "alias": "alias-page",
        "headed": "headed-page",
        "plain": "plain-page",
    }
    for name, text in pages.items():
        directory = tmp_path / "app" / name
        directory.mkdir(parents=True)
        (directory / "page.py").write_text(
            'from pynext import page, h\n@page(render="ssg")\n'
            f'def Page(): return h("p", {{}}, "{text}")\n'
        )
    public = tmp_path / "public"
    public.mkdir()
    (public / "legacy.txt").write_text("legacy-file")
    write_manifest(tmp_path)
    return tmp_path


def _fetch_both(app, method, path, **kwargs):
    """GET ``path`` on ASGI and on native; return (asgi, native)."""
    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test",
                trust_env=False,
            ) as asgi, httpx.AsyncClient(
                base_url=f"http://{server.address}", trust_env=False,
            ) as native:
                asgi_response = await asgi.request(method, path, **kwargs)
                native_response = await native.request(method, path, **kwargs)
                return asgi_response, native_response
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    return asyncio.run(run())


def _assert_same(asgi_response, native_response):
    assert native_response.status_code == asgi_response.status_code
    assert native_response.headers.get("location") == asgi_response.headers.get("location")
    assert native_response.headers.get("x-custom") == asgi_response.headers.get("x-custom")
    assert native_response.headers.get("x-headed") == asgi_response.headers.get("x-headed")
    assert native_response.text == asgi_response.text


def test_redirect_and_headers_match_asgi_native(tmp_path):
    root = _build_project(tmp_path)
    app = create_app(root, production=True, custom_routes=CUSTOM_ROUTES)
    asgi_response, native_response = _fetch_both(
        app, "GET", "/old", follow_redirects=False)
    _assert_same(asgi_response, native_response)
    assert asgi_response.status_code == 307
    assert asgi_response.headers["location"] == "/new"
    assert asgi_response.headers["x-custom"] == "yes"
    assert "original-page" not in asgi_response.text


def test_rewrite_before_files_match_asgi_native(tmp_path):
    root = _build_project(tmp_path)
    app = create_app(root, production=True, custom_routes=CUSTOM_ROUTES)
    asgi_response, native_response = _fetch_both(app, "GET", "/alias")
    _assert_same(asgi_response, native_response)
    assert asgi_response.status_code == 200
    assert "new-page" in asgi_response.text
    assert "alias-page" not in asgi_response.text


def test_headers_only_match_asgi_native(tmp_path):
    root = _build_project(tmp_path)
    app = create_app(root, production=True, custom_routes=CUSTOM_ROUTES)
    asgi_response, native_response = _fetch_both(app, "GET", "/headed")
    _assert_same(asgi_response, native_response)
    assert asgi_response.status_code == 200
    assert asgi_response.headers["x-headed"] == "yes"
    assert "headed-page" in asgi_response.text


def test_redirect_on_public_asset_match_asgi_native(tmp_path):
    root = _build_project(tmp_path)
    app = create_app(root, production=True, custom_routes=CUSTOM_ROUTES)
    asgi_response, native_response = _fetch_both(
        app, "GET", "/legacy.txt", follow_redirects=False)
    _assert_same(asgi_response, native_response)
    assert asgi_response.status_code == 307
    assert asgi_response.headers["location"] == "/new"
    assert "legacy-file" not in asgi_response.text


def test_plain_static_page_matches_asgi_native(tmp_path):
    # Control: a route without any custom route still answers the
    # same on both transports (fast path preserved for it).
    root = _build_project(tmp_path)
    app = create_app(root, production=True, custom_routes=CUSTOM_ROUTES)
    asgi_response, native_response = _fetch_both(app, "GET", "/plain")
    _assert_same(asgi_response, native_response)
    assert asgi_response.status_code == 200
    assert "plain-page" in asgi_response.text


def test_path_has_custom_route_unit():
    # Imported lazily so the integration tests above still run (and
    # fail behaviourally) on code without the helper.
    from pynext.server import _path_has_custom_route

    routes = compile_custom_routes(CUSTOM_ROUTES)
    assert _path_has_custom_route(routes, "/old") is True
    assert _path_has_custom_route(routes, "/legacy.txt") is True
    assert _path_has_custom_route(routes, "/alias") is True
    assert _path_has_custom_route(routes, "/headed") is True
    assert _path_has_custom_route(routes, "/plain") is False
    assert _path_has_custom_route(routes, "/new") is False
    assert _path_has_custom_route(compile_custom_routes(None), "/old") is False
    assert _path_has_custom_route(compile_custom_routes({}), "/old") is False
    assert _path_has_custom_route(None, "/old") is False
