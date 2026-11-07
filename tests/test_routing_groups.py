"""Route groups and optional catch-all routes (finding #50, spec v0.2 §5.1)."""
import asyncio

import httpx
import pytest

from pynext import _native, create_app
from pynext.build import discover, write_manifest


async def fetch(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def write_page(base, *parts, body=None):
    directory = base.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.py").write_text(
        body or 'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n'
    )


def test_router_priority_static_dynamic_optional_catch_all():
    router = _native.Router(["/docs", "/docs/[[...path]]", "/docs/[slug]"])
    assert router.matches("/docs")[0] == "/docs"
    assert router.matches("/docs/a")[0] == "/docs/[slug]"
    assert router.matches("/docs/a/b")[0] == "/docs/[[...path]]"
    assert router.matches("/docs/a/b")[1]["path"] == ["a", "b"]


def test_router_rejects_catch_all_and_optional_catch_all_together():
    with pytest.raises(ValueError, match="Cannot combine"):
        _native.Router(["/docs/[...p]", "/docs/[[...q]]"])


def test_group_directories_are_invisible_in_urls(tmp_path):
    write_page(tmp_path / "app", "(marketing)")
    (tmp_path / "app" / "(marketing)" / "layout.py").write_text(
        'from pynext import h, layout\n@layout\ndef Layout(children): return h("main", {"data-group": "1"}, children)\n'
    )
    app = create_app(tmp_path)
    assert app.router.matches("/")[0] == "/"
    assert app.router.matches("/marketing") is None
    response = asyncio.run(fetch(app, "/"))
    assert response.status_code == 200 and b'data-group="1"' in response.content


def test_optional_catch_all_page_serves_zero_or_more_segments(tmp_path):
    body = (
        'from pynext import h, page\n'
        '@page()\n'
        'def Page(params): return h("p", {}, ",".join(params.get("path", [])) or "root")\n'
    )
    write_page(tmp_path / "app", "docs", "[[...path]]", body=body)
    app = create_app(tmp_path)
    for path, expected in [("/docs", b"root"), ("/docs/a", b"a"), ("/docs/a/b", b"a,b")]:
        response = asyncio.run(fetch(app, path))
        assert response.status_code == 200, path
        assert expected in response.content, path


def test_group_and_optional_catch_all_conflicts_fail_build(tmp_path):
    write_page(tmp_path / "app", "docs", "[...p]")
    write_page(tmp_path / "app", "docs", "[[...q]]")
    with pytest.raises(ValueError, match="Cannot combine"):
        discover(tmp_path)


def test_duplicate_routes_from_different_groups_fail_build(tmp_path):
    write_page(tmp_path / "app", "(a)")
    write_page(tmp_path / "app", "(b)")
    with pytest.raises(ValueError, match="Duplicate route path"):
        discover(tmp_path)


def test_invalid_group_name_fails_build(tmp_path):
    write_page(tmp_path / "app", "(bad")
    with pytest.raises(ValueError, match="Invalid route group"):
        discover(tmp_path)


def test_optional_catch_all_ssg_generates_zero_segment_page(tmp_path):
    body = (
        'from pynext import h, page\n'
        '@page(render="ssg")\n'
        'def Page(params): return h("p", {}, ",".join(params.get("path", [])) or "root")\n'
        'def generate_static_params(): return [{"path": []}, {"path": ["a", "b"]}]\n'
    )
    write_page(tmp_path / "app", "docs", "[[...path]]", body=body)
    manifest = write_manifest(tmp_path)
    assert "/docs" in manifest["static_pages"]
    assert "/docs/a/b" in manifest["static_pages"]
