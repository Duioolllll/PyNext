"""Route-specificity comparator (finding: Next.js sortable-routes.ts, P2 §2.9).

``pynext.sortable_routes`` is a translation of Next.js
``shared/lib/router/utils/sortable-routes.ts`` (``getSegmentSpecificity``,
``compareRouteSegments``, ``sortPages`` / ``sortSortableRoutes``). The unit
cases below are ported from its test file
(``sortable-routes.test.ts``, 1014 lines); the property test pins the
comparator's order equal to the Rust trie's ``ordered_routes()`` on random
route sets, and the manifest test pins that ``discover()`` writes routes in
that order.
"""

import pytest
from hypothesis import given, settings, strategies as st

from pynext import _native
from pynext.build import discover
from pynext.sortable_routes import (
    compare_routes,
    compare_sortable_routes,
    get_segment_specificity,
    sort_routes,
    sort_sortable_routes,
)

_EXAMPLES = settings(max_examples=40, deadline=None, derandomize=True)


# --- get_segment_specificity: ported from sortable-routes.test.ts ---


def test_specificity_static_segments():
    for segment in ["api", "users", "dashboard", "123", "about-us"]:
        assert get_segment_specificity(segment) == 0


def test_specificity_dynamic_segments():
    for segment in ["[id]", "[slug]", "[userId]", "[post-id]"]:
        assert get_segment_specificity(segment) == 1


def test_specificity_catch_all_segments():
    for segment in ["[...slug]", "[...path]", "[...params]"]:
        assert get_segment_specificity(segment) == 2


def test_specificity_optional_catch_all_segments():
    for segment in ["[[...slug]]", "[[...path]]", "[[...params]]"]:
        assert get_segment_specificity(segment) == 3


def test_specificity_edge_cases():
    assert get_segment_specificity("") == 0
    assert get_segment_specificity("[") == 0
    assert get_segment_specificity("]") == 0
    assert get_segment_specificity("[id") == 0
    assert get_segment_specificity("id]") == 0
    # Malformed optional catch-all: treated as dynamic.
    assert get_segment_specificity("[[...slug]") == 1
    # Malformed optional catch-all: treated as catch-all.
    assert get_segment_specificity("[...slug]]") == 2


def test_specificity_segments_with_brackets_but_not_dynamic():
    assert get_segment_specificity("api[version]") == 0
    assert get_segment_specificity("users[admin]") == 0


# --- compare_routes: ported from sortable-routes.test.ts ---


def test_compare_prioritizes_shorter_routes():
    assert compare_routes("/api", "/api/users") == -1
    assert compare_routes("/api/users", "/api") == 1
    assert compare_routes("/", "/about") == -1
    assert compare_routes("/about", "/") == 1


def test_compare_prioritizes_static_over_dynamic():
    assert compare_routes("/api/users", "/api/[id]") == -1
    assert compare_routes("/api/[id]", "/api/users") == 1
    assert compare_routes("/blog/about", "/blog/[slug]") == -1


def test_compare_prioritizes_dynamic_over_catch_all():
    assert compare_routes("/api/[id]", "/api/[...path]") == -1
    assert compare_routes("/api/[...path]", "/api/[id]") == 1
    assert compare_routes("/docs/[slug]", "/docs/[...slug]") == -1


def test_compare_prioritizes_catch_all_over_optional_catch_all():
    assert compare_routes("/docs/[...slug]", "/docs/[[...slug]]") == -1
    assert compare_routes("/docs/[[...slug]]", "/docs/[...slug]") == 1


def test_compare_complete_specificity_hierarchy():
    routes = ["/docs/[[...slug]]", "/docs/[...slug]", "/docs/[slug]", "/docs/api"]
    assert sort_routes(routes) == [
        "/docs/api",
        "/docs/[slug]",
        "/docs/[...slug]",
        "/docs/[[...slug]]",
    ]


def test_compare_lexicographic_when_specificity_equal():
    assert compare_routes("/api/zebra", "/api/apple") == 1
    assert compare_routes("/api/apple", "/api/zebra") == -1
    assert compare_routes("/[lang]/zebra", "/[lang]/apple") == 1
    assert compare_routes("/[lang]/apple", "/[lang]/zebra") == -1


def test_compare_identical_routes():
    assert compare_routes("/api/users", "/api/users") == 0
    assert compare_routes("/[lang]/[slug]", "/[lang]/[slug]") == 0
    assert compare_routes("/docs/[...slug]", "/docs/[...slug]") == 0


def test_compare_empty_and_root_paths():
    assert compare_routes("", "") == 0
    assert compare_routes("", "/api") == -1
    assert compare_routes("/api", "") == 1
    assert compare_routes("/", "/api") == -1
    assert compare_routes("/api", "/") == 1
    assert compare_routes("/", "/") == 0


def test_compare_slashes_consistently():
    assert compare_routes("/api/users/", "/api/users") == 0
    assert compare_routes("api/users", "/api/users") == 0
    assert compare_routes("/api/users/", "api/users") == 0


def test_compare_complex_nested():
    assert compare_routes("/[lang]/blog/[slug]", "/en/blog/[slug]") == 1
    assert compare_routes("/en/blog/[slug]", "/[lang]/blog/[slug]") == -1
    assert compare_routes("/[lang]/blog/[...slug]", "/[lang]/blog/[slug]") == 1


def test_compare_mixed_depths_with_specificities():
    routes = [
        "/api/v1/users/[id]/posts/[...slug]",
        "/api/v1/users/[id]/posts",
        "/api/v1/users/[id]",
        "/api/v1/users",
        "/api/v1",
        "/api",
    ]
    assert sort_routes(routes) == [
        "/api",
        "/api/v1",
        "/api/v1/users",
        "/api/v1/users/[id]",
        "/api/v1/users/[id]/posts",
        "/api/v1/users/[id]/posts/[...slug]",
    ]


# --- sort_routes: ported from sortable-routes.test.ts (sortPages) ---


def test_sort_pages_by_specificity():
    pages = ["/docs/[[...slug]]", "/docs/[...slug]", "/docs/[slug]", "/docs/api", "/docs"]
    assert sort_routes(pages) == [
        "/docs",
        "/docs/api",
        "/docs/[slug]",
        "/docs/[...slug]",
        "/docs/[[...slug]]",
    ]


def test_sort_pages_mixed_depths():
    pages = ["/api/v1/[...path]", "/[slug]", "/", "/api/users/[id]", "/api"]
    assert sort_routes(pages) == [
        "/",
        "/api",
        "/api/users/[id]",
        "/api/v1/[...path]",
        "/[slug]",
    ]


def test_sort_pages_lexicographic():
    assert sort_routes(["/zebra", "/apple", "/mango"]) == ["/apple", "/mango", "/zebra"]


def test_sort_pages_empty_and_single():
    assert sort_routes([]) == []
    assert sort_routes(["/single"]) == ["/single"]


def test_sort_pages_does_not_mutate_input():
    pages = ["/static", "/dynamic/[id]"]
    assert sort_routes(pages) == ["/dynamic/[id]", "/static"]
    assert pages == ["/static", "/dynamic/[id]"]


def test_sort_pages_duplicates():
    pages = ["/blog/[slug]", "/blog/post-1", "/blog/[slug]", "/blog/[slug]"]
    assert sort_routes(pages) == [
        "/blog/post-1",
        "/blog/[slug]",
        "/blog/[slug]",
        "/blog/[slug]",
    ]


def test_sort_pages_complex_nested():
    pages = [
        "/[lang]/blog/[[...slug]]",
        "/en/blog/[slug]",
        "/[lang]/blog/[slug]",
        "/en/blog",
        "/[lang]/blog",
        "/[lang]/blog/[...slug]",
    ]
    assert sort_routes(pages) == [
        "/en/blog",
        "/en/blog/[slug]",
        "/[lang]/blog",
        "/[lang]/blog/[slug]",
        "/[lang]/blog/[...slug]",
        "/[lang]/blog/[[...slug]]",
    ]


def test_posts_order_from_report():
    # The report's pinning example (§2.9): /posts < /posts/new < /posts/[id].
    assert sort_routes(["/posts/[id]", "/posts/new", "/posts"]) == [
        "/posts",
        "/posts/new",
        "/posts/[id]",
    ]


# --- sort_sortable_routes: ported from sortable-routes.test.ts ---


def test_sortable_routes_sort_by_page():
    routes = [
        ("/[lang]/[teamSlug]/[projectSlug]/monitoring", "/[lang]/[teamSlug]/[projectSlug]/monitoring"),
        ("/[lang]/[teamSlug]/[projectSlug]/monitoring", "/en/[teamSlug]/[projectSlug]/monitoring"),
        ("/[lang]/[teamSlug]/~/monitoring", "/[lang]/[teamSlug]/~/monitoring"),
        ("/[lang]/[teamSlug]/[projectSlug]/monitoring", "/fr/[teamSlug]/[projectSlug]/monitoring"),
    ]
    assert sort_sortable_routes(routes) == [
        ("/[lang]/[teamSlug]/~/monitoring", "/[lang]/[teamSlug]/~/monitoring"),
        ("/[lang]/[teamSlug]/[projectSlug]/monitoring", "/en/[teamSlug]/[projectSlug]/monitoring"),
        ("/[lang]/[teamSlug]/[projectSlug]/monitoring", "/fr/[teamSlug]/[projectSlug]/monitoring"),
        ("/[lang]/[teamSlug]/[projectSlug]/monitoring", "/[lang]/[teamSlug]/[projectSlug]/monitoring"),
    ]


def test_sortable_routes_specificity():
    routes = [
        ("/docs/[[...slug]]", "/docs/[[...slug]]"),
        ("/docs/[...slug]", "/docs/[...slug]"),
        ("/docs/[slug]", "/docs/[slug]"),
        ("/docs/api", "/docs/api"),
    ]
    assert sort_sortable_routes(routes) == [
        ("/docs/api", "/docs/api"),
        ("/docs/[slug]", "/docs/[slug]"),
        ("/docs/[...slug]", "/docs/[...slug]"),
        ("/docs/[[...slug]]", "/docs/[[...slug]]"),
    ]


def test_sortable_routes_prioritize_source_over_page():
    routes = [
        ("/[lang]/[...slug]", "/en/docs/api"),
        ("/en/docs", "/[lang]/[...slug]"),
    ]
    assert sort_sortable_routes(routes) == [
        ("/en/docs", "/[lang]/[...slug]"),
        ("/[lang]/[...slug]", "/en/docs/api"),
    ]


def test_sortable_routes_mixed_lengths():
    routes = [
        ("/api/[...path]", "/api/[...path]"),
        ("/api", "/api"),
        ("/api/users/[id]", "/api/users/[id]"),
        ("/api/users", "/api/users"),
    ]
    assert sort_sortable_routes(routes) == [
        ("/api", "/api"),
        ("/api/users", "/api/users"),
        ("/api/users/[id]", "/api/users/[id]"),
        ("/api/[...path]", "/api/[...path]"),
    ]


def test_sortable_routes_lexicographic():
    routes = [
        ("/[lang]/zebra", "/[lang]/zebra"),
        ("/[lang]/apple", "/[lang]/apple"),
        ("/[lang]/banana", "/[lang]/banana"),
    ]
    assert sort_sortable_routes(routes) == [
        ("/[lang]/apple", "/[lang]/apple"),
        ("/[lang]/banana", "/[lang]/banana"),
        ("/[lang]/zebra", "/[lang]/zebra"),
    ]


def test_sortable_routes_empty_single_identical():
    assert sort_sortable_routes([]) == []
    single = [("/api/users/[id]", "/api/users/[id]")]
    assert sort_sortable_routes(single) == single
    identical = [("/api/users", "/api/users"), ("/api/users", "/api/users")]
    assert sort_sortable_routes(identical) == identical


def test_sortable_routes_complex_nested():
    routes = [
        ("/[lang]/blog/[[...slug]]", "/[lang]/blog/[[...slug]]"),
        ("/[lang]/blog/[slug]", "/[lang]/blog/[slug]"),
        ("/en/blog/[slug]", "/en/blog/[slug]"),
        ("/en/blog", "/en/blog"),
        ("/[lang]/blog", "/[lang]/blog"),
        ("/[lang]/blog/[...slug]", "/[lang]/blog/[...slug]"),
    ]
    assert sort_sortable_routes(routes) == [
        ("/en/blog", "/en/blog"),
        ("/en/blog/[slug]", "/en/blog/[slug]"),
        ("/[lang]/blog", "/[lang]/blog"),
        ("/[lang]/blog/[slug]", "/[lang]/blog/[slug]"),
        ("/[lang]/blog/[...slug]", "/[lang]/blog/[...slug]"),
        ("/[lang]/blog/[[...slug]]", "/[lang]/blog/[[...slug]]"),
    ]


def test_sortable_routes_do_not_mutate_input():
    routes = [("/b", "/b"), ("/a", "/a")]
    assert sort_sortable_routes(routes) == [("/a", "/a"), ("/b", "/b")]
    assert routes == [("/b", "/b"), ("/a", "/a")]


def test_compare_sortable_routes_tiebreak_is_page_only():
    assert compare_sortable_routes(("/a", "/b"), ("/a", "/a")) == 1
    assert compare_sortable_routes(("/a", "/a"), ("/a", "/b")) == -1
    assert compare_sortable_routes(("/a", "/a"), ("/a", "/a")) == 0


# --- Property test: comparator order == trie order ---


_STATIC_SEGMENT = st.text(alphabet="abcxyz019-_", min_size=1, max_size=6)
_PARAM_NAME = st.sampled_from(["id", "slug", "name", "page"])


def _draw_route(data, allow_params=True):
    depth = data.draw(st.integers(min_value=1, max_value=3))
    segments = []
    for index in range(depth):
        if allow_params and index == depth - 1 and data.draw(st.booleans()):
            name = data.draw(_PARAM_NAME)
            if data.draw(st.booleans()):
                segments.append(f"[[...{name}]]")
            else:
                segments.append(f"[...{name}]")
        elif allow_params and data.draw(st.integers(min_value=0, max_value=3)) == 0:
            segments.append(f"[{data.draw(_PARAM_NAME)}]")
        else:
            segments.append(data.draw(_STATIC_SEGMENT))
    return "/" + "/".join(segments)


@given(st.data())
@_EXAMPLES
def test_comparator_order_matches_trie_order(data):
    routes = {_draw_route(data) for _ in range(data.draw(st.integers(1, 6)))}
    routes = sorted(routes)
    try:
        router = _native.Router(routes)
    except ValueError:
        # Conflicting or ambiguous route sets are rejected; the only
        # acceptable failure mode is ValueError.
        return
    ordered = router.ordered_routes()
    # Sorting the trie's own order is a no-op, and sorting the raw set
    # (in any input order) reproduces the trie order exactly.
    assert sort_routes(ordered) == ordered
    assert sort_routes(list(reversed(ordered))) == ordered
    assert sort_routes(routes) == ordered


# --- Manifest: discover() writes routes in comparator order ---


def _write_page(base, *parts):
    directory = base.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n'
    )


def test_manifest_routes_use_comparator_order(tmp_path):
    for parts in [
        ("posts", "[id]"), ("posts", "new"), ("posts",),
        ("docs", "[...path]"), ("docs", "[slug]"), ("docs", "api"), ("docs",),
        ("[root]",), ("[...rest]",),
    ]:
        _write_page(tmp_path / "app", *parts)
    _write_page(tmp_path / "app")
    manifest = discover(tmp_path)
    assert list(manifest["routes"]) == [
        "/",
        "/docs",
        "/docs/api",
        "/docs/[slug]",
        "/docs/[...path]",
        "/posts",
        "/posts/new",
        "/posts/[id]",
        "/[root]",
        "/[...rest]",
    ]
    assert list(manifest["routes"]) == sort_routes(manifest["routes"])
    assert list(manifest["routes"]) == _native.Router(
        list(manifest["routes"])
    ).ordered_routes()
