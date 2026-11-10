"""Build-time route validation: @slot rejection and actionable errors.

Covers the validate_routes() layer in pynext.build (Next.js
validate-app-paths.ts equivalent) and the parameter-kind fix in
Router.matches: handler params must come from the router's kind info,
not from a substring test on the pattern.
"""
import asyncio

import httpx
import pytest

from pynext import _native, create_app
from pynext.build import discover


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


def test_discovery_uses_posix_project_relative_paths(tmp_path):
    write_page(tmp_path / "app", "blog", "[slug]")
    for path in (tmp_path / "app" / "layout.py",
                 tmp_path / "app" / "blog" / "layout.py",
                 tmp_path / "middleware.py", tmp_path / "resources.py"):
        path.write_text("# source\n", encoding="utf-8")
    manifest = discover(tmp_path)
    assert manifest["routes"]["/blog/[slug]"] == {
        "entry": "app/blog/[slug]/page.py", "kind": "page",
        "layouts": ["app/layout.py", "app/blog/layout.py"],
    }
    assert set(manifest["sources"]) == {
        "app/blog/[slug]/page.py", "app/layout.py", "app/blog/layout.py",
        "middleware.py", "resources.py",
    }


def test_slot_directory_fails_build(tmp_path):
    write_page(tmp_path / "app", "@team")
    with pytest.raises(ValueError, match=r"Parallel routes \(@slot\) are not supported"):
        discover(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    assert "@team" in str(excinfo.value)
    assert "rename the directory" in str(excinfo.value)


def test_slot_directory_without_a_page_still_fails_build(tmp_path):
    # A @slot dir holding only a layout must not slip through silently.
    directory = tmp_path / "app" / "@team"
    directory.mkdir(parents=True)
    (directory / "layout.py").write_text(
        'from pynext import h, layout\n@layout\ndef Layout(children): return h("main", {}, children)\n'
    )
    write_page(tmp_path / "app")
    with pytest.raises(ValueError, match=r"Parallel routes \(@slot\) are not supported"):
        discover(tmp_path)


def test_default_py_fails_build(tmp_path):
    write_page(tmp_path / "app")
    (tmp_path / "app" / "default.py").write_text("# parallel-route leftover\n")
    with pytest.raises(ValueError, match="Parallel routes are not supported"):
        discover(tmp_path)


def test_conflicting_slug_names_name_the_file_and_the_fix(tmp_path):
    write_page(tmp_path / "app", "blog", "[id]")
    write_page(tmp_path / "app", "blog", "[slug]")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "Different slug names" in message
    # The offending route's source file is named, plus how to fix it.
    assert "app/blog/[slug]/page.py" in message
    assert "Fix:" in message and "same parameter name" in message


def test_catch_all_not_last_names_the_file_and_the_fix(tmp_path):
    write_page(tmp_path / "app", "blog", "[...rest]", "x")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "Catch-all must be the last part" in message
    assert "app/blog/[...rest]/x/page.py" in message
    assert "Move the [...name] segment to the end" in message


def test_ellipsis_segment_suggests_three_dots(tmp_path):
    write_page(tmp_path / "app", "docs", "[[…slug]]")
    with pytest.raises(ValueError, match=r"three-dot character.*did you mean \('\.\.\.'\)"):
        discover(tmp_path)


def test_name_starting_with_period_is_rejected(tmp_path):
    write_page(tmp_path / "app", "docs", "[.id]")
    with pytest.raises(ValueError, match=r"may not start with erroneous periods"):
        discover(tmp_path)


def test_optional_single_parameter_is_rejected_with_guidance(tmp_path):
    write_page(tmp_path / "app", "docs", "[[id]]")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    assert "[[...name]]" in str(excinfo.value)


def test_interception_group_names_the_missing_feature(tmp_path):
    write_page(tmp_path / "app", "(.)")
    with pytest.raises(ValueError, match=r"interception routes are not supported"):
        discover(tmp_path)


def test_handler_params_use_router_kinds_not_substring_match(tmp_path):
    # Regression test for the old `f"[...{k}]" in pattern` check: "[...a]"
    # is a substring of "/[a]/[...a2]", so the old code handed the handler
    # a list ["x"] for the dynamic parameter `a` instead of "x".
    body = (
        'from pynext import h, page\n'
        '@page()\n'
        'def Page(params):\n'
        '    a = params["a"]\n'
        '    a2 = params["a2"]\n'
        '    return h("p", {}, type(a).__name__ + ":" + str(a) + "|" '
        '        + type(a2).__name__ + ":" + ",".join(a2))\n'
    )
    write_page(tmp_path / "app", "[a]", "[...a2]", body=body)
    app = create_app(tmp_path)
    response = asyncio.run(fetch(app, "/x/y/z"))
    assert response.status_code == 200
    assert "str:x|list:y,z" in response.text
