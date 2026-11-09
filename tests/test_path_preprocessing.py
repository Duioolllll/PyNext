"""Path preprocessing: basePath + trailing-slash policy (REPORT.md §2.3 P2, track A §2.8).

Covers the ``getNextPathnameInfo()`` equivalent in ``pynext.routing``
(``preprocess_pathname`` / ``add_base_path`` / ``url_for``) and its
wiring into ``create_app`` dispatch, WebSocket upgrades and
``revalidate()``. The i18n locale step of Next.js is intentionally
absent — PyNext has no locale routing.
"""
import asyncio

import httpx
import pytest

from pynext import add_base_path, create_app, preprocess_pathname, url_for
from pynext.routing import normalize_base_path, validate_trailing_slash

ECHO_ROUTE = (
    "from pynext import WebSocket, WebSocketDisconnect, websocket\n"
    "\n"
    "@websocket\n"
    "async def echo(websocket: WebSocket):\n"
    "    await websocket.accept()\n"
    "    try:\n"
    "        while True:\n"
    "            kind, data = await websocket.receive()\n"
    "            if kind == \"text\":\n"
    "                await websocket.send_text(data)\n"
    "            else:\n"
    "                await websocket.send_bytes(data)\n"
    "    except WebSocketDisconnect:\n"
    "        pass\n"
)


def write_page(base, *parts, body=None):
    directory = base.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.py").write_text(
        body or 'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n'
    )


def write_app(tmp_path):
    write_page(tmp_path / "app")
    write_page(tmp_path / "app", "about")


async def fetch(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --- validation -------------------------------------------------------


@pytest.mark.parametrize("value", ["", "/", "/app", "/app/v2", "/a-b_c/d"])
def test_normalize_base_path_accepts(value):
    assert normalize_base_path(value) == ("" if value in ("", "/") else value)


@pytest.mark.parametrize("value", ["app", "/app/", "/app//x", "/app/./x", "/app/../x", "/app?x", "/app#x",
                                    "/app\\x", "/app[x]", " /app"])
def test_normalize_base_path_rejects(value):
    with pytest.raises(ValueError):
        normalize_base_path(value)


@pytest.mark.parametrize("value", [None, 42, b"/app", ["/app"]])
def test_normalize_base_path_wrong_type(value):
    with pytest.raises(TypeError):
        normalize_base_path(value)


def test_validate_trailing_slash():
    assert validate_trailing_slash("redirect") == "redirect"
    assert validate_trailing_slash("ignore") == "ignore"
    with pytest.raises(ValueError):
        validate_trailing_slash("append")
    with pytest.raises(TypeError):
        validate_trailing_slash(None)


# --- preprocess_pathname ----------------------------------------------


def test_preprocess_without_base_path():
    assert preprocess_pathname("/") == "/"
    assert preprocess_pathname("/about") == "/about"
    # Default policy keeps the trailing slash for the 308 branch.
    assert preprocess_pathname("/about/") == "/about/"
    assert preprocess_pathname("/about/", trailing_slash="ignore") == "/about"
    assert preprocess_pathname("/", trailing_slash="ignore") == "/"


def test_preprocess_strips_base_path():
    assert preprocess_pathname("/app", base_path="/app") == "/"
    assert preprocess_pathname("/app/", base_path="/app") == "/"
    assert preprocess_pathname("/app/about", base_path="/app") == "/about"
    assert preprocess_pathname("/app/about/", base_path="/app") == "/about/"
    assert preprocess_pathname("/app/about/", base_path="/app", trailing_slash="ignore") == "/about"


def test_preprocess_outside_base_path_is_none():
    assert preprocess_pathname("/about", base_path="/app") is None
    assert preprocess_pathname("/", base_path="/app") is None
    # The prefix must end on a segment boundary.
    assert preprocess_pathname("/application/about", base_path="/app") is None
    assert preprocess_pathname("/app2/about", base_path="/app") is None


def test_preprocess_rejects_non_absolute_path():
    with pytest.raises(ValueError):
        preprocess_pathname("about")
    with pytest.raises(ValueError):
        preprocess_pathname("/about", base_path="app")


# --- add_base_path / url_for ------------------------------------------


def test_add_base_path():
    assert add_base_path("/about") == "/about"
    assert add_base_path("/", "/app") == "/app"
    assert add_base_path("/about", "/app") == "/app/about"
    assert add_base_path("/about", "/") == "/about"
    with pytest.raises(ValueError):
        add_base_path("about", "/app")


def test_url_for_without_base_path():
    assert url_for("/", {}) == "/"
    assert url_for("/blog/[slug]", {"slug": "hi"}) == "/blog/hi"
    assert url_for("/docs/[...path]", {"path": ["a", "b"]}) == "/docs/a/b"
    assert url_for("/docs/[[...path]]", {"path": []}) == "/docs"


def test_url_for_reapplies_base_path():
    assert url_for("/", {}, base_path="/app") == "/app"
    assert url_for("/blog/[slug]", {"slug": "hi"}, base_path="/app") == "/app/blog/hi"
    assert url_for("/docs/[...path]", {"path": ["a"]}, base_path="/app") == "/app/docs/a"


def test_url_for_rejects_bad_params():
    with pytest.raises(ValueError):
        url_for("/blog/[slug]", {})
    with pytest.raises(ValueError):
        url_for("/blog/[slug]", {"slug": "a/b"})


# --- app wiring --------------------------------------------------------


def test_invalid_path_settings_fail_at_startup(tmp_path):
    write_app(tmp_path)
    with pytest.raises(ValueError):
        create_app(tmp_path, base_path="app")
    with pytest.raises(ValueError):
        create_app(tmp_path, trailing_slash="append")


def test_base_path_root_normalizes_to_no_prefix(tmp_path):
    write_app(tmp_path)
    app = create_app(tmp_path, base_path="/")
    assert app.base_path == ""
    assert asyncio.run(fetch(app, "/about")).status_code == 200


def test_default_trailing_slash_still_redirects(tmp_path):
    write_app(tmp_path)
    app = create_app(tmp_path)
    assert app.trailing_slash == "redirect"
    response = asyncio.run(fetch(app, "/about/"))
    assert response.status_code == 308
    assert response.headers["location"] == "/about"


def test_base_path_redirect_mode(tmp_path):
    write_app(tmp_path)
    app = create_app(tmp_path, base_path="/app")
    assert app.base_path == "/app"
    cases = [
        ("/app", 200), ("/app/", 200), ("/app/about", 200),
        ("/about", 404), ("/", 404),
        ("/application/about", 404), ("/app2/about", 404),
    ]
    for path, expected in cases:
        response = asyncio.run(fetch(app, path))
        assert response.status_code == expected, path
    response = asyncio.run(fetch(app, "/app/about/"))
    assert response.status_code == 308
    assert response.headers["location"] == "/app/about"
    # Health probes stay unprefixed by design.
    assert asyncio.run(fetch(app, "/healthz")).status_code == 200
    assert asyncio.run(fetch(app, "/app/healthz")).status_code == 404


def test_base_path_ignore_mode_serves_trailing_slash(tmp_path):
    write_app(tmp_path)
    app = create_app(tmp_path, base_path="/app", trailing_slash="ignore")
    assert asyncio.run(fetch(app, "/app/about/")).status_code == 200
    assert asyncio.run(fetch(app, "/app/about")).status_code == 200
    assert asyncio.run(fetch(app, "/about/")).status_code == 404


def test_base_path_dynamic_route_and_url_for(tmp_path):
    write_page(tmp_path / "app", "blog", "[slug]",
               body='from pynext import h, page\n@page()\ndef Page(params): return h("p", {}, params["slug"])\n')
    app = create_app(tmp_path, base_path="/app")
    response = asyncio.run(fetch(app, "/app/blog/hello"))
    assert response.status_code == 200
    assert b"hello" in response.content
    assert asyncio.run(fetch(app, "/blog/hello")).status_code == 404
    assert url_for("/blog/[slug]", {"slug": "hello"}, base_path=app.base_path) == "/app/blog/hello"


class _ScriptedConnection:
    def __init__(self, incoming):
        self._incoming = iter(incoming)
        self.sent = []

    async def receive(self):
        return next(self._incoming)

    async def send(self, message):
        self.sent.append(message)


def test_base_path_websocket_upgrade(tmp_path):
    directory = tmp_path / "app" / "ws" / "echo"
    directory.mkdir(parents=True)
    (directory / "route.py").write_text(ECHO_ROUTE)
    app = create_app(tmp_path, base_path="/app")

    async def run():
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "hello"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app({"type": "websocket", "path": "/app/ws/echo",
                   "headers": [(b"host", b"localhost"), (b"origin", b"http://localhost")],
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [
            {"type": "websocket.accept"},
            {"type": "websocket.send", "text": "hello"},
        ]
        await app.isr.close()

    asyncio.run(run())

    async def run_unprefixed():
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws/echo",
                   "headers": [(b"host", b"localhost"), (b"origin", b"http://localhost")],
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1008}]
        await app.isr.close()

    asyncio.run(run_unprefixed())
