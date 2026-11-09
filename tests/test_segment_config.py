"""Segment config inheritance: layout ISR policies + shortest revalidate wins.

Next.js grounding: ``reduceAppConfig`` (``build/utils.ts``) — a revalidate
number beats ``False`` and among numbers the shortest wins; ``stale`` takes
the smallest set value. Layouts set the section policy; a page only wins
with a shorter value.
"""
import asyncio
import json
import logging

import httpx
import pytest

from pynext import create_app, layout, page
from pynext.build import write_manifest
from pynext.components import reduce_segment_config


# --- reduce_segment_config: pure rules -------------------------------------

def test_reduce_shortest_revalidate_wins():
    effective, winners = reduce_segment_config([
        ("app/layout.py", {"revalidate": 60, "stale": None}),
        ("app/blog/layout.py", {"revalidate": 120, "stale": None}),
        ("app/blog/page.py", {"revalidate": 30, "stale": None}),
    ])
    assert effective == {"revalidate": 30.0, "stale": None}
    assert winners["revalidate"] == ("app/blog/page.py", 30.0)


def test_reduce_layout_wins_when_page_asks_for_more():
    effective, winners = reduce_segment_config([
        ("app/layout.py", {"revalidate": 60, "stale": None}),
        ("app/blog/page.py", {"revalidate": 300, "stale": None}),
    ])
    assert effective["revalidate"] == 60.0
    assert winners["revalidate"] == ("app/layout.py", 60.0)


def test_reduce_number_beats_false():
    effective, _ = reduce_segment_config([
        ("app/layout.py", {"revalidate": False, "stale": None}),
        ("app/blog/page.py", {"revalidate": 45, "stale": None}),
    ])
    assert effective["revalidate"] == 45.0
    effective, _ = reduce_segment_config([
        ("app/layout.py", {"revalidate": 45, "stale": None}),
        ("app/blog/page.py", {"revalidate": False, "stale": None}),
    ])
    assert effective["revalidate"] == 45.0


def test_reduce_all_false_stays_false():
    effective, winners = reduce_segment_config([
        ("app/layout.py", {"revalidate": False, "stale": None}),
        ("app/blog/page.py", {"revalidate": False, "stale": None}),
    ])
    assert effective["revalidate"] is False
    assert winners["revalidate"] == ("app/layout.py", False)


def test_reduce_stale_takes_minimum_and_unset_stays_none():
    effective, winners = reduce_segment_config([
        ("app/layout.py", {"revalidate": None, "stale": 120}),
        ("app/blog/page.py", {"revalidate": None, "stale": 30}),
    ])
    assert effective == {"revalidate": None, "stale": 30.0}
    assert winners["stale"] == ("app/blog/page.py", 30.0)
    effective, _ = reduce_segment_config([
        ("app/layout.py", {"revalidate": None, "stale": None}),
    ])
    assert effective == {"revalidate": None, "stale": None}


def test_reduce_rejects_bad_values():
    with pytest.raises(ValueError, match="Invalid revalidate"):
        reduce_segment_config([("x", {"revalidate": "60", "stale": None})])
    with pytest.raises(ValueError, match="Invalid stale"):
        reduce_segment_config([("x", {"revalidate": None, "stale": False})])
    with pytest.raises(ValueError, match="Invalid segment policy"):
        reduce_segment_config([("x", {"revalidate": 60})])


# --- decorators ------------------------------------------------------------

def test_layout_decorator_forms():
    @layout
    def plain(children):
        return children
    assert plain.__pynext_layout__ is True
    assert plain.__pynext_layout_policy__ == {"revalidate": None, "stale": None}

    @layout()
    def empty(children):
        return children
    assert empty.__pynext_layout_policy__ == {"revalidate": None, "stale": None}

    @layout(revalidate=60, stale=120)
    def with_policy(children):
        return children
    assert with_policy.__pynext_layout_policy__ == {"revalidate": 60.0, "stale": 120.0}


@pytest.mark.parametrize("kwargs", [
    {"revalidate": 0}, {"revalidate": -1}, {"revalidate": True},
    {"revalidate": float("nan")}, {"stale": -1},
])
def test_layout_policy_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        layout(**kwargs)


def test_layout_policy_rejects_false_with_on_demand_hint():
    with pytest.raises(ValueError, match="not supported yet"):
        layout(revalidate=False)


def test_page_isr_may_omit_values_for_inheritance():
    def target(params):
        return None

    fn = page(render="isr")(target)
    assert fn.__pynext_cache_policy__ == {"revalidate": None, "stale": None}
    fn = page(render="isr", revalidate=30)(target)
    assert fn.__pynext_cache_policy__ == {"revalidate": 30.0, "stale": None}


# --- app integration -------------------------------------------------------

def make_project(root, *, root_layout=None, nested_layout=None, page_kwargs="revalidate=30, stale=1",
                 page_render="isr"):
    app_dir = root / "app" / "blog"
    app_dir.mkdir(parents=True)
    if root_layout is not None:
        (root / "app" / "layout.py").write_text(
            "from pynext import h, layout\n"
            f"@layout({root_layout})\n"
            "def Layout(children):\n"
            "    return h(\"html\", {}, h(\"body\", {}, children))\n")
    if nested_layout is not None:
        (app_dir / "layout.py").write_text(
            "from pynext import h, layout\n"
            f"@layout({nested_layout})\n"
            "def Layout(children):\n"
            "    return h(\"div\", {}, children)\n")
    (app_dir / "page.py").write_text(
        "from pynext import h, page\n"
        f"@page(render=\"{page_render}\", {page_kwargs})\n"
        "def Page(params):\n"
        "    return h(\"p\", {}, \"blog\")\n")


def test_layout_policy_caps_page_revalidate_and_warns(tmp_path, caplog):
    make_project(tmp_path, root_layout="revalidate=60, stale=120",
                 page_kwargs="revalidate=300, stale=200")
    with caplog.at_level(logging.WARNING, logger="pynext"):
        manifest = write_manifest(tmp_path)
    assert manifest["isr_policies"]["/blog"] == {"revalidate": 60.0, "stale": 120.0}
    assert manifest["layout_policies"]["/blog"] == [
        {"source": "app/layout.py", "revalidate": 60.0, "stale": 120.0}]
    assert "revalidate=300" in caplog.text and "app/blog/page.py" in caplog.text
    assert "revalidate=60" in caplog.text and "app/layout.py" in caplog.text
    assert "stale=200" in caplog.text


def test_page_shorter_revalidate_wins_without_warning(tmp_path, caplog):
    make_project(tmp_path, root_layout="revalidate=60, stale=120",
                 page_kwargs="revalidate=30, stale=10")
    with caplog.at_level(logging.WARNING, logger="pynext"):
        manifest = write_manifest(tmp_path)
    assert manifest["isr_policies"]["/blog"] == {"revalidate": 30.0, "stale": 10.0}
    assert "overridden" not in caplog.text


def test_page_inherits_revalidate_and_stale_from_layout(tmp_path):
    make_project(tmp_path, root_layout="revalidate=60, stale=10",
                 page_kwargs="revalidate=None")
    manifest = write_manifest(tmp_path)
    assert manifest["isr_policies"]["/blog"] == {"revalidate": 60.0, "stale": 10.0}


def test_nested_layouts_take_shortest(tmp_path):
    make_project(tmp_path, root_layout="revalidate=120",
                 nested_layout="revalidate=45", page_kwargs="revalidate=90")
    manifest = write_manifest(tmp_path)
    assert manifest["isr_policies"]["/blog"] == {"revalidate": 45.0, "stale": 60.0}
    assert manifest["layout_policies"]["/blog"] == [
        {"source": "app/layout.py", "revalidate": 120.0, "stale": None},
        {"source": "app/blog/layout.py", "revalidate": 45.0, "stale": None}]


def test_isr_page_without_revalidate_anywhere_fails_at_load(tmp_path):
    make_project(tmp_path, page_kwargs="revalidate=None")
    with pytest.raises(ValueError, match="must set revalidate or inherit"):
        write_manifest(tmp_path)


def test_layout_policy_change_requires_rebuild(tmp_path):
    make_project(tmp_path, root_layout="revalidate=60",
                 page_kwargs="revalidate=90")
    manifest = write_manifest(tmp_path)
    assert manifest["isr_policies"]["/blog"] == {"revalidate": 60.0, "stale": 60.0}
    saved = tmp_path / ".pynext" / "manifest.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["layout_policies"]["/blog"][0]["revalidate"] = 30.0
    saved.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Layout policies changed since build"):
        create_app(tmp_path, production=True)


def test_layout_policy_ignored_for_non_isr_pages(tmp_path):
    make_project(tmp_path, root_layout="revalidate=60",
                 page_kwargs="", page_render="ssr")
    manifest = write_manifest(tmp_path)
    assert manifest["render_modes"]["/blog"] == "ssr"
    assert "/blog" not in manifest["isr_policies"]
    assert manifest["layout_policies"] == {}


def test_inherited_isr_page_renders(tmp_path):
    make_project(tmp_path, root_layout="revalidate=60, stale=10",
                 page_kwargs="revalidate=None")
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/blog")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert "<p>blog</p>" in response.text
