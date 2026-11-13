import asyncio
import re

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


LAYOUT = (
    'from pynext import h, layout\n'
    '@layout\n'
    'def Layout(children):\n'
    '    return h("html", {}, h("body", {}, h("nav", {}, "NAV"), children))\n'
)

GLOBAL_ERROR = (
    'from pynext import h\n'
    'def ErrorPage(error, request):\n'
    '    return h("main", {}, f"GLOBAL-ERROR:{type(error).__name__}",\n'
    '             h("a", {"href": request.path}, "Try again"))\n'
)

GLOBAL_NOT_FOUND = (
    'from pynext import h\n'
    'def NotFoundPage(request):\n'
    '    return h("main", {}, f"GLOBAL-NOT-FOUND:{request.path}")\n'
)


def base_app(root, *, layout=True):
    if layout:
        write(root, "app/layout.py", LAYOUT)
    write(root, "app/error.py", GLOBAL_ERROR)
    write(root, "app/not-found.py", GLOBAL_NOT_FOUND)


def run_with_client(app, mode, fn):
    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)
        try:
            async with client:
                await fn(client)
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_page_error_uses_nearest_error_boundary(tmp_path, mode):
    base_app(tmp_path)
    write(tmp_path, "app/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n    raise ValueError("boom")\n')
    write(tmp_path, "app/blog/error.py",
          'from pynext import h\ndef ErrorPage(error):\n    return h("main", {}, "BLOG-ERROR")\n')
    write(tmp_path, "app/blog/[slug]/page.py",
          'from pynext import h, page\n@page()\nasync def Page(params):\n    raise ValueError("blog boom")\n')
    write(tmp_path, "app/about/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n    raise ValueError("about boom")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/")
        assert response.status_code == 500
        assert "GLOBAL-ERROR:ValueError" in response.text
        assert "NAV" in response.text  # boundary renders inside the layout
        assert "Try again" in response.text and 'href="/"' in response.text
        assert "Internal Server Error" not in response.text

        response = await client.get("/blog/hello")
        assert response.status_code == 500
        assert "BLOG-ERROR" in response.text
        assert "GLOBAL-ERROR" not in response.text

        response = await client.get("/about")
        assert response.status_code == 500
        assert "GLOBAL-ERROR:ValueError" in response.text
    run_with_client(app, mode, check)


def test_layout_error_falls_back_to_bare_boundary(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/layout.py",
          'from pynext import h, layout\n@layout\ndef Layout(children):\n    raise ValueError("layout boom")\n')
    write(tmp_path, "app/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n    return h("main", {}, "PAGE-OK")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/")
        assert response.status_code == 500
        # The layout itself failed, so the boundary renders without it.
        assert "GLOBAL-ERROR:ValueError" in response.text
        assert "NAV" not in response.text
        assert "PAGE-OK" not in response.text
    run_with_client(app, "asgi", check)


def test_broken_boundary_falls_back_to_global_then_plain(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/blog/error.py",
          'from pynext import h\ndef ErrorPage(error):\n    raise ValueError("boundary boom")\n')
    write(tmp_path, "app/blog/[slug]/page.py",
          'from pynext import h, page\n@page()\nasync def Page(params):\n    raise ValueError("blog boom")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/blog/hello")
        assert response.status_code == 500
        assert "GLOBAL-ERROR:ValueError" in response.text
    run_with_client(app, "asgi", check)

    # No usable boundary at all: the plain 500 page is the last resort.
    broken = tmp_path / "app" / "error.py"
    broken.write_text('from pynext import h\ndef ErrorPage(error):\n    raise ValueError("global boom")\n')
    write(tmp_path, "app/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n    raise ValueError("boom")\n')
    app = create_app(tmp_path)

    async def check_plain(client):
        response = await client.get("/")
        assert response.status_code == 500
        # Plain 500 (no boundary rendered); the body carries the error
        # digest since the errdigest change, like test_errors.py expects.
        assert re.fullmatch(
            r"Internal Server Error \(request_id: [0-9a-f-]+, digest: [0-9a-f]{16}\)",
            response.text)
    run_with_client(app, "asgi", check_plain)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_route_miss_not_found_boundary_only_for_html(tmp_path, mode):
    base_app(tmp_path)
    write(tmp_path, "app/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n    return h("main", {}, "HOME")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/missing", headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert "GLOBAL-NOT-FOUND:/missing" in response.text
        # A route miss has no route, so no layouts wrap the boundary.
        assert "NAV" not in response.text
        # A non-HTML client keeps the plain body.
        response = await client.get("/missing")
        assert response.status_code == 404
        assert response.text == "Not Found"
        response = await client.get("/missing", headers={"Accept": "application/json"})
        assert response.status_code == 404
        assert response.text == "Not Found"
    run_with_client(app, mode, check)


def test_page_not_found_error_uses_nearest_not_found_boundary(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/page.py",
          'from pynext import NotFoundError, h, page\n@page()\nasync def Page():\n    raise NotFoundError("no such post")\n')
    write(tmp_path, "app/blog/not-found.py",
          'from pynext import h\ndef NotFoundPage(params):\n    return h("main", {}, f"BLOG-NOT-FOUND:{params[\'slug\']}")\n')
    write(tmp_path, "app/blog/[slug]/page.py",
          'from pynext import NotFoundError, h, page\n@page()\nasync def Page(params):\n    raise NotFoundError("no such post")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/")
        assert response.status_code == 404
        assert "GLOBAL-NOT-FOUND:/" in response.text
        response = await client.get("/blog/hello")
        assert response.status_code == 404
        assert "BLOG-NOT-FOUND:hello" in response.text
        assert "GLOBAL-NOT-FOUND" not in response.text
    run_with_client(app, "asgi", check)


def test_api_route_errors_stay_plain(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/api/boom/route.py",
          'from pynext import route\n@route("GET")\ndef get():\n    raise ValueError("api boom")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/api/boom")
        assert response.status_code == 500
        # Plain 500 (API routes never render an HTML boundary); the body
        # carries the error digest since the errdigest change.
        assert re.fullmatch(
            r"Internal Server Error \(request_id: [0-9a-f-]+, digest: [0-9a-f]{16}\)",
            response.text)
    run_with_client(app, "asgi", check)


def test_streaming_loading_placeholder_and_stream_error_marker(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/stream/loading.py",
          'from pynext import h\ndef LoadingPage():\n    return h("p", {}, "LOADING-PLACEHOLDER")\n')
    write(tmp_path, "app/stream/page.py",
          'import asyncio\nfrom pynext import h, page\n@page()\nasync def Page():\n'
          '    yield h("p", {}, "CHUNK-1")\n    await asyncio.sleep(0.05)\n    yield h("p", {}, "CHUNK-2")\n')
    write(tmp_path, "app/broken-stream/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n'
          '    yield h("p", {}, "FIRST")\n    raise ValueError("mid-stream boom")\n')
    app = create_app(tmp_path)

    async def check(client):
        response = await client.get("/stream")
        assert response.status_code == 200
        assert response.text.index("LOADING-PLACEHOLDER") < response.text.index("CHUNK-1")
        assert response.text.count("LOADING-PLACEHOLDER") == 1
        assert "CHUNK-2" in response.text
        assert "<!-- pynext-stream-error -->" not in response.text

        # A mid-stream failure cannot change the 200 status, so the client
        # gets the marker and the closed document instead of a silent cut.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw:
            response = await raw.get("/broken-stream")
            assert response.status_code == 200  # headers were already sent
            assert "FIRST" in response.text
            assert "<!-- pynext-stream-error -->" in response.text
            assert response.text.rstrip().endswith("</html>")
    run_with_client(app, "asgi", check)


def test_ssg_loading_not_baked_and_ssg_failure_fails_build(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/stream/loading.py",
          'from pynext import h\ndef LoadingPage():\n    return h("p", {}, "LOADING-PLACEHOLDER")\n')
    write(tmp_path, "app/stream/page.py",
          'from pynext import h, page\n@page(render="ssg")\nasync def Page():\n'
          '    yield h("p", {}, "SSG-CHUNK")\n')
    write_manifest(tmp_path)
    static = list((tmp_path / ".pynext" / "static").glob("*.html"))
    assert len(static) == 1
    body = static[0].read_text()
    assert "SSG-CHUNK" in body
    assert "LOADING-PLACEHOLDER" not in body  # live clients only, never cached

    write(tmp_path, "app/broken/page.py",
          'from pynext import h, page\n@page(render="ssg")\nasync def Page():\n    raise ValueError("build boom")\n')
    with pytest.raises(ValueError):
        write_manifest(tmp_path)

    write(tmp_path, "app/broken/page.py",
          'from pynext import h, page\n@page(render="ssg")\nasync def Page():\n'
          '    yield h("p", {}, "FIRST")\n    raise ValueError("mid-stream boom")\n')
    with pytest.raises(ValueError):
        write_manifest(tmp_path)


def test_isr_error_is_not_cached(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/isr/page.py",
          'from pynext import h, page\n_STATE = {"calls": 0}\n@page(render="isr", revalidate=60)\n'
          'async def Page():\n    _STATE["calls"] += 1\n'
          '    if _STATE["calls"] == 1:\n        raise ValueError("first generation fails")\n'
          '    return h("main", {}, "ISR-OK")\n')
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def check(client):
        response = await client.get("/isr")
        assert response.status_code == 500
        assert "GLOBAL-ERROR:ValueError" in response.text
        # The failed generation must not poison the cache: the retry renders.
        response = await client.get("/isr")
        assert response.status_code == 200
        assert "ISR-OK" in response.text
    run_with_client(app, "asgi", check)


def test_boundary_exports_are_validated_at_startup(tmp_path):
    base_app(tmp_path)
    write(tmp_path, "app/page.py",
          'from pynext import h, page\n@page()\nasync def Page():\n    return h("main", {}, "HOME")\n')
    (tmp_path / "app" / "error.py").write_text("def SomethingElse():\n    pass\n")
    with pytest.raises(ValueError, match="must export a ErrorPage function"):
        create_app(tmp_path)
    (tmp_path / "app" / "error.py").write_text(GLOBAL_ERROR)
    (tmp_path / "app" / "not-found.py").write_text("def NotFound():\n    pass\n")
    with pytest.raises(ValueError, match="must export a NotFoundPage function"):
        create_app(tmp_path)
    (tmp_path / "app" / "not-found.py").write_text(GLOBAL_NOT_FOUND)
    (tmp_path / "app" / "loading.py").write_text("def LoadingPage(bogus):\n    pass\n")
    with pytest.raises(ValueError, match="Unsupported required parameter"):
        create_app(tmp_path)
