"""processPrelude-style stream inspection (REPORT.md §2.3 P2, track B P2-1).

A streaming page that yields nothing inside an empty shell used to go out
as a 0-byte document with a 200 status. ``process_prelude`` peeks at the
first chunk without consuming the stream, so the empty case fails before
headers are sent (500 at runtime, build failure for SSG), while a
non-empty shell is still a legitimate content-free page.
"""
import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.app import process_prelude
from pynext.build import write_manifest


# --- process_prelude unit tests -------------------------------------------

async def _collect(stream):
    return [item async for item in stream]


def test_prelude_replays_the_whole_stream_in_order():
    async def run():
        async def source():
            yield "first"
            yield "second"
            yield "third"

        prelude, is_empty = await process_prelude(source())
        assert is_empty is False
        assert await _collect(prelude) == ["first", "second", "third"]

    asyncio.run(run())


def test_prelude_reports_an_empty_stream():
    async def run():
        async def source():
            if False:
                yield "never"

        prelude, is_empty = await process_prelude(source())
        assert is_empty is True
        assert await _collect(prelude) == []

    asyncio.run(run())


def test_prelude_buffers_only_the_first_item():
    """Items after the first are pulled lazily, not upfront."""
    async def run():
        pulled = []

        async def source():
            for item in ("a", "b", "c"):
                pulled.append(item)
                yield item

        prelude, is_empty = await process_prelude(source())
        assert is_empty is False
        assert pulled == ["a"]  # only the peek happened so far
        assert await _collect(prelude) == ["a", "b", "c"]
        assert pulled == ["a", "b", "c"]

    asyncio.run(run())


def test_prelude_propagates_an_error_on_the_first_item():
    async def run():
        async def source():
            raise RuntimeError("page blew up before its first chunk")
            yield "never"

        with pytest.raises(RuntimeError, match="blew up before its first chunk"):
            await process_prelude(source())

    asyncio.run(run())


def test_prelude_aclose_closes_the_source():
    """A response that is never consumed (HEAD, early disconnect) must
    still release the page generator."""
    async def run():
        closed = False

        async def source():
            try:
                yield "first"
                yield "second"
            finally:
                nonlocal closed
                closed = True

        prelude, is_empty = await process_prelude(source())
        assert is_empty is False
        await prelude.aclose()
        assert closed is True

    asyncio.run(run())


# --- runtime tests ---------------------------------------------------------

def _write_streaming_app(root, page_source, layout_source=None):
    app_dir = root / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "page.py").write_text(page_source, encoding="utf-8")
    if layout_source is not None:
        (app_dir / "layout.py").write_text(layout_source, encoding="utf-8")
    return root


_EMPTY_PAGE = (
    "from pynext import page, h\n"
    "@page()\n"
    "async def Page():\n"
    "    if False:\n"
    "        yield h(\"p\", {}, \"never\")\n"
)

_SHELL_LAYOUT = (
    "from pynext import h, layout\n"
    "@layout\n"
    "def Layout(children):\n"
    "    return h(\"html\", {}, h(\"body\", {}, h(\"header\", {}, \"Shell\"), children))\n"
)


def test_empty_streaming_page_with_empty_shell_is_a_500_not_an_empty_200(tmp_path):
    _write_streaming_app(tmp_path, _EMPTY_PAGE)

    async def run():
        app = create_app(tmp_path)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 500
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_empty_streaming_page_with_a_shell_still_renders_the_shell(tmp_path):
    _write_streaming_app(tmp_path, _EMPTY_PAGE, _SHELL_LAYOUT)

    async def run():
        app = create_app(tmp_path)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 200
                assert response.text.startswith("<!DOCTYPE html><html")
                assert "Shell" in response.text
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_empty_streaming_page_with_fragment_only_shell_is_a_500(tmp_path):
    """A layout that only wraps children in a fragment adds no content of
    its own: the shell is still just the marker plus the runtime tag."""
    _write_streaming_app(tmp_path, _EMPTY_PAGE,
                         "from pynext import fragment, layout\n"
                         "@layout\n"
                         "def Layout(children):\n"
                         "    return fragment(children)\n")

    async def run():
        app = create_app(tmp_path)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 500
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_streaming_page_keeps_its_first_chunk(tmp_path):
    """Regression guard: the peek must not swallow the first chunk."""
    _write_streaming_app(tmp_path,
                         "from pynext import page, h\n"
                         "@page()\n"
                         "async def Page():\n"
                         "    yield h(\"p\", {}, \"first\")\n"
                         "    yield h(\"p\", {}, \"second\")\n",
                         _SHELL_LAYOUT)

    async def run():
        app = create_app(tmp_path)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 200
                assert response.text.index("<p>first</p>") < response.text.index("<p>second</p>")
        finally:
            await app.isr.close()

    asyncio.run(run())


# --- SSG build tests -------------------------------------------------------

def test_streamed_ssg_page_yielding_nothing_fails_the_build(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "page.py").write_text(
        "from pynext import page, h\n"
        "@page(render=\"ssg\")\n"
        "async def Page():\n"
        "    if False:\n"
        "        yield h(\"p\", {}, \"never\")\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rendered an empty document"):
        write_manifest(tmp_path)


def test_streamed_ssg_page_yielding_only_empty_nodes_fails_the_build(tmp_path):
    """Byte-level backstop: nodes that serialize to nothing must not ship
    as a 0-byte static file."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "page.py").write_text(
        "from pynext import page, h\n"
        "@page(render=\"ssg\")\n"
        "async def Page():\n"
        "    yield {\"kind\": \"text\", \"value\": \"\"}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rendered an empty document"):
        write_manifest(tmp_path)


def test_non_streaming_ssg_page_rendering_to_nothing_fails_the_build(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "page.py").write_text(
        "from pynext import page, h\n"
        "@page(render=\"ssg\")\n"
        "def Page():\n"
        "    return {\"kind\": \"text\", \"value\": \"\"}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rendered an empty document"):
        write_manifest(tmp_path)
