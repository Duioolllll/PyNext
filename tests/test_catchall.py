"""Catch-all normalization (reduced port of Next.js normalizeCatchAllRoutes).

Covers the pure normalization in ``pynext.catchall`` and its build-time
integration in ``build.write_manifest`` / the ``pynext build`` CLI summary.
"""
import sys

import pytest

from pynext import cli
from pynext.build import write_manifest
from pynext.catchall import (
    CatchAllReport,
    normalize_catch_all_routes,
    verify_static_enumeration,
)


# ---------------------------------------------------------------------------
# normalize_catch_all_routes
# ---------------------------------------------------------------------------

def test_required_catch_all_shadows_static_page_below_its_base():
    report = normalize_catch_all_routes(
        ["/docs", "/docs/intro", "/docs/[...path]"],
        {"/docs": "/docs", "/docs/intro": "/docs/intro",
         "/docs/guide": "/docs/[...path]"},
    )
    assert report.shadowed == {"/docs/intro": ["/docs/[...path]"]}
    assert report.coverage == {"/docs/[...path]": ["/docs/intro"]}
    assert report.catch_all_pages == {"/docs/[...path]": ["/docs/guide"]}
    assert report.dynamic_only == []


def test_required_catch_all_does_not_match_its_own_base():
    report = normalize_catch_all_routes(
        ["/docs", "/docs/[...path]"],
        {"/docs": "/docs", "/docs/guide": "/docs/[...path]"},
    )
    assert report.shadowed == {}
    assert report.coverage == {}
    assert report.dynamic_only == []


def test_optional_catch_all_matches_its_own_base():
    report = normalize_catch_all_routes(
        ["/docs", "/docs/[[...path]]"],
        {"/docs": "/docs", "/docs/guide": "/docs/[[...path]]"},
    )
    assert report.shadowed == {"/docs": ["/docs/[[...path]]"]}
    assert report.coverage == {"/docs/[[...path]]": ["/docs"]}
    assert report.catch_all_pages == {"/docs/[[...path]]": ["/docs/guide"]}
    assert report.dynamic_only == []


def test_optional_catch_all_at_root_matches_root_and_below():
    report = normalize_catch_all_routes(
        ["/", "/[[...path]]"],
        {"/": "/", "/a": "/[[...path]]", "/a/b": "/[[...path]]"},
    )
    assert report.shadowed == {"/": ["/[[...path]]"]}
    assert report.coverage == {"/[[...path]]": ["/"]}
    assert report.catch_all_pages == {"/[[...path]]": ["/a", "/a/b"]}
    assert report.dynamic_only == []


def test_catch_all_never_shadows_its_own_enumerated_paths():
    report = normalize_catch_all_routes(
        ["/docs/[...path]"],
        {"/docs/a": "/docs/[...path]", "/docs/a/b": "/docs/[...path]"},
    )
    assert report.shadowed == {}
    assert report.coverage == {}
    assert report.catch_all_pages == {
        "/docs/[...path]": ["/docs/a", "/docs/a/b"]}
    assert report.dynamic_only == []


def test_catch_all_with_no_enumerated_paths_is_dynamic_only():
    report = normalize_catch_all_routes(
        ["/docs", "/docs/[...path]"],
        {"/docs": "/docs"},
    )
    assert report.dynamic_only == ["/docs/[...path]"]
    assert report.coverage == {}
    assert report.shadowed == {}


def test_dynamic_segment_page_is_shadowed_by_catch_all():
    report = normalize_catch_all_routes(
        ["/blog/[slug]", "/blog/[...path]"],
        {"/blog/hello": "/blog/[slug]", "/blog/a/b": "/blog/[...path]"},
    )
    assert report.shadowed == {"/blog/hello": ["/blog/[...path]"]}
    assert report.coverage == {"/blog/[...path]": ["/blog/hello"]}


def test_more_specific_catch_all_is_listed_first():
    # Both catch-alls match /docs/x (owned by the more specific [slug]
    # page); the one with the longer static base wins at runtime, so it
    # comes first in the report.
    report = normalize_catch_all_routes(
        ["/docs/[slug]", "/[...all]", "/docs/[...path]"],
        {"/docs/x": "/docs/[slug]"},
    )
    assert report.shadowed == {"/docs/x": ["/docs/[...path]", "/[...all]"]}


def test_report_json_is_deterministic_and_sorted():
    report = normalize_catch_all_routes(
        ["/b/[...x]", "/a/[...y]", "/a/p", "/b/q"],
        {"/a/p": "/a/p", "/b/q": "/b/q"},
    )
    assert report.to_json() == {
        "coverage": {"/a/[...y]": ["/a/p"], "/b/[...x]": ["/b/q"]},
        "shadowed": {"/a/p": ["/a/[...y]"], "/b/q": ["/b/[...x]"]},
        "dynamic_only": [],
        "catch_all_pages": {},
    }
    assert isinstance(report, CatchAllReport)


@pytest.mark.parametrize("pattern", [
    "/docs/[...path]/extra",  # catch-all must be last
    "/docs/[[slug]]",         # optional without dots
    "/docs/[slug",            # malformed segment
    "/docs/[...]",            # empty name
    "docs/[...path]",         # must start with /
])
def test_invalid_patterns_raise(pattern):
    with pytest.raises(ValueError):
        normalize_catch_all_routes([pattern], {})


# ---------------------------------------------------------------------------
# verify_static_enumeration
# ---------------------------------------------------------------------------

def test_verify_passes_for_consistent_enumeration():
    patterns = ["/", "/docs", "/docs/intro", "/docs/[...path]",
                "/blog/[slug]", "/docs/[[...opt]]"]
    static = {"/": "/", "/docs": "/docs", "/docs/intro": "/docs/intro",
              "/docs/a": "/docs/[...path]", "/blog/hi": "/blog/[slug]"}
    verify_static_enumeration(patterns, static)  # must not raise


def test_verify_rejects_owner_that_is_not_most_specific():
    # /docs/x is owned by the catch-all, but /docs/[slug] is more specific.
    with pytest.raises(ValueError, match="most specific"):
        verify_static_enumeration(
            ["/docs/[slug]", "/docs/[...path]"],
            {"/docs/x": "/docs/[...path]"},
        )


def test_verify_rejects_unknown_owner():
    with pytest.raises(ValueError, match="unknown route"):
        verify_static_enumeration(["/docs"], {"/docs": "/missing"})


def test_verify_rejects_path_matched_by_no_route():
    with pytest.raises(ValueError, match="not matched by any route"):
        verify_static_enumeration(["/docs"], {"/docs/x": "/docs"})


def test_verify_rejects_ambiguous_match():
    # Two dynamic patterns with identical specificity both match /x; the
    # router would reject the slug conflict, but the check itself must
    # refuse to pick a winner.
    with pytest.raises(ValueError, match="ambiguously matched"):
        verify_static_enumeration(["/[a]", "/[b]"], {"/x": "/[a]"})


# ---------------------------------------------------------------------------
# Build integration: write_manifest + CLI summary
# ---------------------------------------------------------------------------

def _write_page(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _make_catchall_app(root):
    app = root / "app"
    _write_page(app / "page.py",
                'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "home")\n')
    # Static pages must be SSG to be enumerated at build time; a plain
    # @page() is dynamic SSR and never enters the coverage report.
    _write_page(app / "docs" / "page.py",
                'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "docs")\n')
    _write_page(app / "docs" / "intro" / "page.py",
                'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "intro")\n')
    _write_page(app / "docs" / "[...path]" / "page.py",
                'from pynext import page, h\n'
                'def generate_static_params():\n'
                '    return [{"path": ["guide"]}, {"path": ["guide", "deep"]}]\n'
                '@page(render="ssg")\n'
                'def Page(params): return h("p", {}, "/".join(params["path"]))\n')
    # An API catch-all must not appear in the page-only coverage report.
    _write_page(app / "api" / "[...rest]" / "route.py",
                'from pynext import JSONResponse, route\n'
                '@route("GET")\n'
                'async def get(request): return JSONResponse({"ok": True})\n')
    return root


def test_build_manifest_records_catch_all_coverage(tmp_path):
    manifest = write_manifest(_make_catchall_app(tmp_path))
    assert manifest["catch_all_coverage"] == {
        "coverage": {"/docs/[...path]": ["/docs/intro"]},
        "shadowed": {"/docs/intro": ["/docs/[...path]"]},
        "dynamic_only": [],
        "catch_all_pages": {"/docs/[...path]": ["/docs/guide", "/docs/guide/deep"]},
    }
    assert "/api/[...rest]" not in str(manifest["catch_all_coverage"])
    assert manifest["routes"]["/docs/[...path]"]["kind"] == "page"


def test_build_manifest_marks_unenumerated_catch_all_dynamic_only(tmp_path):
    app = tmp_path / "app"
    _write_page(app / "page.py",
                'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "home")\n')
    _write_page(app / "docs" / "[...path]" / "page.py",
                'from pynext import page, h\n'
                'def generate_static_params(): return []\n'
                '@page(render="ssg")\n'
                'def Page(params): return h("p", {}, "docs")\n')
    manifest = write_manifest(tmp_path)
    assert manifest["catch_all_coverage"]["dynamic_only"] == ["/docs/[...path]"]
    assert manifest["catch_all_coverage"]["coverage"] == {}
    assert manifest["catch_all_coverage"]["shadowed"] == {}


def test_cli_build_prints_catch_all_summary(tmp_path, monkeypatch, capsys):
    _make_catchall_app(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path)])
    cli.main()
    out = capsys.readouterr().out
    assert "Built 5 routes" in out
    assert ("Catch-all coverage: 1 static page(s) also match a catch-all "
            "route (the static page wins); 0 catch-all route(s) serve only "
            "paths generate_static_params did not enumerate") in out


def test_cli_build_is_quiet_without_catch_alls(tmp_path, monkeypatch, capsys):
    app = tmp_path / "app"
    _write_page(app / "page.py",
                'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "home")\n')
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path)])
    cli.main()
    out = capsys.readouterr().out
    assert "Built 1 routes" in out
    assert "Catch-all coverage" not in out


def test_verify_affixed_dynamic_beats_plain_dynamic():
    """A1-M3: an affixed dynamic segment (v[id]) is more specific than a
    plain dynamic segment ([id]), matching the Rust trie's
    static > affixed > dynamic order — the build must not fail for a
    valid app, whichever of the two owns the path.
    """
    # /v1 is owned by the affixed route: the trie serves it, so the
    # enumeration must agree instead of naming /[id] the winner.
    verify_static_enumeration(["/v[id]", "/[id]"], {"/v1": "/v[id]"})
    # The same pair with the plain route as owner is genuinely wrong:
    # the trie serves /v[id], so the owner must be /v[id].
    with pytest.raises(ValueError, match="most specific"):
        verify_static_enumeration(["/v[id]", "/[id]"], {"/v1": "/[id]"})
    # Suffix affix: /posts/x.json is owned by /posts/[name].json.
    verify_static_enumeration(
        ["/posts/[name].json", "/posts/[id]"], {"/posts/x.json": "/posts/[name].json"})


def test_verify_affixed_tiebreak_follows_trie_order():
    """A1-M3: two affixed segments matching one path have a deterministic
    runtime winner (the trie's BTreeMap<(prefix, suffix)> order), so the
    build must accept the trie winner and only reject a true tie.
    """
    # ("", ".json") < ("v", ""): [id].json wins /v1.json at runtime.
    verify_static_enumeration(
        ["/v[id]", "/[id].json"], {"/v1.json": "/[id].json"})
    with pytest.raises(ValueError, match="most specific"):
        verify_static_enumeration(
            ["/v[id]", "/[id].json"], {"/v1.json": "/v[id]"})
    # Same (prefix, suffix) with different names: the trie stores both
    # under one key and cannot distinguish them — still ambiguous.
    with pytest.raises(ValueError, match="ambiguously matched"):
        verify_static_enumeration(
            ["/[a].json", "/[b].json"], {"/x.json": "/[a].json"})
