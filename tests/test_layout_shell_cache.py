"""Layout shell caching: a route's layout shell is rendered once, not per request.

Layouts only receive ``children``, so the shell (the layouts applied to a
marker in place of the page) is the same for every request to a route.
These tests count layout invocations through a counter file the layout
appends to, and check that cached and uncached responses are identical.
"""
import asyncio
import re

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.http import SecurityConfig

LAYOUT = (
    "import os\n"
    "from pathlib import Path\n"
    "from pynext import h, layout\n"
    "COUNTER = Path(__file__).resolve().parent.parent / \"layout-count.txt\"\n"
    "@layout\n"
    "def Layout(children):\n"
    "    fd = os.open(COUNTER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n"
    "    try:\n"
    "        os.write(fd, b\"x\\n\")\n"
    "    finally:\n"
    "        os.close(fd)\n"
    "    return h(\"html\", {\"lang\": \"en\"},\n"
    "             h(\"head\", {}, h(\"title\", {}, \"Shell test\")),\n"
    "             h(\"body\", {}, h(\"header\", {}, \"shell\"), children))\n"
)

DYNAMIC_LAYOUT = LAYOUT.replace("@layout\n", "@layout(dynamic=True)\n")

SECTION_LAYOUT = (
    "import os\n"
    "from pathlib import Path\n"
    "from pynext import h, layout\n"
    "COUNTER = Path(__file__).resolve().parent.parent.parent / \"section-count.txt\"\n"
    "@layout\n"
    "def Layout(children):\n"
    "    fd = os.open(COUNTER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n"
    "    try:\n"
    "        os.write(fd, b\"x\\n\")\n"
    "    finally:\n"
    "        os.close(fd)\n"
    "    return h(\"section\", {\"data-section\": \"1\"}, children)\n"
)

PLAIN_PAGE = (
    "from pynext import h, page\n"
    "@page()\n"
    "def Page():\n"
    "    return h(\"main\", {}, h(\"p\", {}, \"buffered\"))\n"
)

STREAM_PAGE = (
    "import asyncio\n"
    "from pynext import h, page\n"
    "@page()\n"
    "async def Page():\n"
    "    yield h(\"p\", {}, \"chunk-one\")\n"
    "    await asyncio.sleep(0.01)\n"
    "    yield h(\"p\", {}, \"chunk-two\")\n"
)

ISR_PAGE = (
    "from pynext import h, page\n"
    "@page(render=\"isr\", revalidate=30, stale=60)\n"
    "def Page():\n"
    "    return h(\"p\", {}, \"isr-body\")\n"
)

NO_CHILDREN_LAYOUT = (
    "from pynext import h, layout\n"
    "@layout\n"
    "def Layout(children):\n"
    "    return h(\"html\", {\"lang\": \"en\"},\n"
    "             h(\"head\", {}, h(\"title\", {}, \"No children\")),\n"
    "             h(\"body\", {}, h(\"header\", {}, \"shell\")))\n"
)

DOUBLE_CHILDREN_LAYOUT = (
    "from pynext import h, layout\n"
    "@layout\n"
    "def Layout(children):\n"
    "    return h(\"html\", {\"lang\": \"en\"},\n"
    "             h(\"head\", {}, h(\"title\", {}, \"Twice\")),\n"
    "             h(\"body\", {}, h(\"header\", {}, \"shell\"), children, children))\n"
)


def write_project(root, *, layout=LAYOUT, section_layout=None, isr=False):
    app_dir = root / "app"
    (app_dir / "plain").mkdir(parents=True)
    (app_dir / "plain" / "page.py").write_text(PLAIN_PAGE, encoding="utf-8")
    (app_dir / "stream").mkdir(parents=True)
    (app_dir / "stream" / "page.py").write_text(STREAM_PAGE, encoding="utf-8")
    (app_dir / "layout.py").write_text(layout, encoding="utf-8")
    if section_layout is not None:
        (app_dir / "section" / "a").mkdir(parents=True)
        (app_dir / "section" / "a" / "page.py").write_text(PLAIN_PAGE, encoding="utf-8")
        (app_dir / "section" / "b").mkdir(parents=True)
        (app_dir / "section" / "b" / "page.py").write_text(PLAIN_PAGE, encoding="utf-8")
        (app_dir / "section" / "layout.py").write_text(section_layout, encoding="utf-8")
    if isr:
        (app_dir / "isr").mkdir(parents=True)
        (app_dir / "isr" / "page.py").write_text(ISR_PAGE, encoding="utf-8")
    return create_app(root)


def layout_calls(root, name="layout-count.txt"):
    counter = root / name
    if not counter.exists():
        return 0
    return len(counter.read_text(encoding="utf-8").splitlines())


def test_buffered_page_renders_layout_once_across_requests(tmp_path):
    app = write_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                first = await client.get("/plain")
                second = await client.get("/plain")
            assert first.status_code == 200 and second.status_code == 200
            assert first.text == second.text
            assert first.text.startswith("<!DOCTYPE html><html")
            assert "<header>shell</header>" in first.text
            assert "<p>buffered</p>" in first.text
            assert first.text.index("<header>shell</header>") < first.text.index("<p>buffered</p>")
            assert first.text.endswith("</body></html>")
            assert layout_calls(tmp_path) == 1
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_streaming_page_renders_layout_once_across_requests(tmp_path):
    app = write_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                first = await client.get("/stream")
                second = await client.get("/stream")
            assert first.status_code == 200 and second.status_code == 200
            assert first.text == second.text
            assert first.text.startswith("<!DOCTYPE html><html")
            assert "<header>shell</header>" in first.text
            assert "<p>chunk-one</p>" in first.text and "<p>chunk-two</p>" in first.text
            assert first.text.index("<p>chunk-one</p>") < first.text.index("<p>chunk-two</p>")
            assert first.text.endswith("</body></html>")
            assert layout_calls(tmp_path) == 1
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_dynamic_layout_renders_on_every_request(tmp_path):
    app = write_project(tmp_path, layout=DYNAMIC_LAYOUT)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/plain")).status_code == 200
                assert (await client.get("/plain")).status_code == 200
                assert (await client.get("/stream")).status_code == 200
            assert layout_calls(tmp_path) == 3
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_cached_and_uncached_responses_are_identical(tmp_path):
    cached_root = tmp_path / "cached"
    cached_root.mkdir()
    dynamic_root = tmp_path / "dynamic"
    dynamic_root.mkdir()
    cached = write_project(cached_root)
    dynamic = write_project(dynamic_root, layout=DYNAMIC_LAYOUT)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=cached),
                                         base_url="http://test") as cached_client, \
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=dynamic),
                                      base_url="http://test") as dynamic_client:
                for path in ("/plain", "/stream"):
                    assert (await cached_client.get(path)).text == (await dynamic_client.get(path)).text
        finally:
            await cached.isr.close()
            await dynamic.isr.close()

    asyncio.run(run())
    assert layout_calls(cached_root) == 2  # one shell fill per route
    assert layout_calls(dynamic_root) == 2  # one render per request


def test_cached_shell_keeps_a_fresh_csp_nonce_per_request(tmp_path):
    app = write_project(tmp_path)
    app.security = SecurityConfig(csp_nonce_enabled=True)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                first = await client.get("/plain")
                second = await client.get("/plain")
            assert first.status_code == 200 and second.status_code == 200
            assert first.text == second.text.replace(
                re.search(r'nonce="([^"]+)"', second.text).group(1),
                re.search(r'nonce="([^"]+)"', first.text).group(1))
            first_nonce = re.search(r'nonce="([^"]+)"', first.text).group(1)
            second_nonce = re.search(r'nonce="([^"]+)"', second.text).group(1)
            assert first_nonce and second_nonce and first_nonce != second_nonce
            assert f"'nonce-{first_nonce}'" in first.headers["content-security-policy"]
            assert f"'nonce-{second_nonce}'" in second.headers["content-security-policy"]
            assert layout_calls(tmp_path) == 1
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_nested_layouts_are_cached_per_route(tmp_path):
    app = write_project(tmp_path, section_layout=SECTION_LAYOUT)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/section/a")).status_code == 200
                assert (await client.get("/section/b")).status_code == 200
                assert (await client.get("/section/a")).status_code == 200
            # The shell is keyed per route, so the shared root layout is
            # still rendered once per route, not once globally.
            assert layout_calls(tmp_path) == 2
            assert layout_calls(tmp_path, "section-count.txt") == 2
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_concurrent_first_requests_render_layout_once(tmp_path):
    app = write_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                responses = await asyncio.gather(*(client.get("/plain") for _ in range(5)))
            assert all(response.status_code == 200 for response in responses)
            assert {response.text for response in responses} == {responses[0].text}
            assert layout_calls(tmp_path) == 1
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_invalidate_layout_shell_renders_again(tmp_path):
    app = write_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/plain")).status_code == 200
                assert layout_calls(tmp_path) == 1
                app.invalidate_layout_shell()
                assert (await client.get("/plain")).status_code == 200
                assert layout_calls(tmp_path) == 2
                assert (await client.get("/plain")).status_code == 200
                assert layout_calls(tmp_path) == 2
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_layout_must_render_children_exactly_once(tmp_path):
    for name, layout in (("no-children", NO_CHILDREN_LAYOUT),
                         ("double-children", DOUBLE_CHILDREN_LAYOUT)):
        root = tmp_path / name
        root.mkdir()
        app = write_project(root, layout=layout)
        try:
            with pytest.raises(ValueError, match="must render its children exactly once"):
                asyncio.run(app.render_page("/plain", None, {}))
        finally:
            asyncio.run(app.isr.close())


def test_isr_regeneration_reuses_cached_shell(tmp_path):
    write_project(tmp_path, isr=True)
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                first = await client.get("/isr")
                assert first.status_code == 200
                assert "<p>isr-body</p>" in first.text
                assert layout_calls(tmp_path) == 1
                assert await app.revalidate("/isr")
                second = await client.get("/isr")
                assert second.status_code == 200
                assert second.text == first.text
                assert layout_calls(tmp_path) == 1
        finally:
            await app.isr.close()

    asyncio.run(run())

USER_PAGE = (
    "from pynext import h, page\n"
    "@page()\n"
    "def Page(request):\n"
    "    headers = {name.lower(): value for name, value in request.headers}\n"
    "    user = headers.get(b\"x-user\", b\"?\").decode()\n"
    "    return h(\"main\", {}, h(\"p\", {}, f\"user:{user}\"))\n"
)


def test_shell_cache_does_not_leak_request_data_between_users(tmp_path):
    # The layout shell is cached per route, but layouts only receive
    # ``children`` — never the request — so per-request page content
    # must not bleed into the shared shell: two users get their own
    # page body under the same shell, and the layout renders once.
    app_dir = tmp_path / "app"
    (app_dir / "plain").mkdir(parents=True)
    (app_dir / "plain" / "page.py").write_text(USER_PAGE, encoding="utf-8")
    (app_dir / "layout.py").write_text(LAYOUT, encoding="utf-8")
    app = create_app(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                alice = await client.get("/plain", headers={"x-user": "alice"})
                bob = await client.get("/plain", headers={"x-user": "bob"})
            assert alice.status_code == 200 and bob.status_code == 200
            assert "<p>user:alice</p>" in alice.text
            assert "<p>user:bob</p>" in bob.text
            assert "user:alice" not in bob.text
            assert "<header>shell</header>" in alice.text
            assert "<header>shell</header>" in bob.text
            assert layout_calls(tmp_path) == 1
        finally:
            await app.isr.close()

    asyncio.run(run())
