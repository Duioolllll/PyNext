"""Property-based tests for the router and the HTML serializer.

Finding #48 (spec v0.2 section 13): the router and HTML serialization must
be covered by property-based tests and fuzzing, not only by hand-picked
examples. These tests use hypothesis with a fixed seed (``derandomize``) and
small example counts so CI stays fast and results stay reproducible.

What each property pins down:

* router construction either succeeds or fails cleanly with ``ValueError``;
* a successful router matches every route it was built from, deterministically,
  and a matched route's parameters rebuild the matched path;
* ``matches`` never raises for arbitrary input paths;
* rendered text nodes never leak a raw ``<``, ``>`` or bare ``&``;
* rendered attribute values round-trip through HTML unescaping;
* void elements render without a closing tag and reject children;
* rendering an arbitrary (possibly malformed) tree either returns a string
  or raises ``ValueError`` -- never another exception type.
"""

import html as _html
import json
import re

import pytest
from hypothesis import given, settings, strategies as st

from pynext import _native, h

_EXAMPLES = settings(max_examples=40, deadline=None, derandomize=True)

# Text that exercises escaping: markup characters, quotes, whitespace,
# non-ASCII text and an astral-plane character.
_TEXT = st.text(
    alphabet=list("abcXYZ019 <> & \" ' / \n\t한글\U0001f600"),
    min_size=0,
    max_size=30,
)
_STATIC_SEGMENT = st.text(alphabet="abcxyz019-_", min_size=1, max_size=6)
_PARAM_NAME = st.sampled_from(["id", "slug", "name", "page"])
_VOID_TAGS = [
    "area", "base", "br", "col", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
]
# The tag blocklist rejects "embed", so it is excluded here.
_ELEMENT_TAGS = [
    "div", "p", "span", "a", "ul", "li", "h1", "section", "br", "img",
]


def _draw_route(data, allow_params=True):
    depth = data.draw(st.integers(min_value=1, max_value=3))
    segments = []
    for index in range(depth):
        if allow_params and index == depth - 1 and data.draw(st.booleans()):
            segments.append(f"[...{data.draw(_PARAM_NAME)}]")
        elif allow_params and data.draw(st.integers(min_value=0, max_value=3)) == 0:
            segments.append(f"[{data.draw(_PARAM_NAME)}]")
        else:
            segments.append(data.draw(_STATIC_SEGMENT))
    return "/" + "/".join(segments)


def _concrete_path(route):
    """Replace parameters with values no static segment can collide with."""
    parts = []
    for segment in route.strip("/").split("/"):
        if segment.startswith("[...") and segment.endswith("]"):
            parts.extend(["Q7", "Q8"])
        elif segment.startswith("[") and segment.endswith("]"):
            parts.append("Q7")
        else:
            parts.append(segment)
    return "/" + "/".join(parts) if parts else "/"


def _rebuild_path(pattern, params):
    parts = []
    for segment in pattern.strip("/").split("/"):
        if segment.startswith("[...") and segment.endswith("]"):
            parts.extend(params[segment[4:-1]])
        elif segment.startswith("[") and segment.endswith("]"):
            parts.append(params[segment[1:-1]][0])
        else:
            parts.append(segment)
    return "/" + "/".join(parts) if parts else "/"


@given(st.data())
@_EXAMPLES
def test_router_construction_is_clean_and_matches_are_deterministic(data):
    routes = {_draw_route(data) for _ in range(data.draw(st.integers(1, 6)))}
    routes = sorted(routes)
    try:
        router = _native.Router(routes)
    except ValueError:
        # Conflicting or ambiguous route sets are rejected; the only
        # acceptable failure mode is ValueError.
        return
    assert sorted(router.ordered_routes()) == routes
    for route in routes:
        assert router.matches(route) is not None
    path = _concrete_path(data.draw(st.sampled_from(routes)))
    first = router.matches(path)
    assert router.matches(path) == first
    if first is not None:
        pattern, params = first
        assert _rebuild_path(pattern, params) == path
        assert router.matches(_rebuild_path(pattern, params)) == first


@given(st.data())
@_EXAMPLES
def test_router_static_routes_match_their_own_literal_path(data):
    routes = set()
    while len(routes) < data.draw(st.integers(1, 5)):
        depth = data.draw(st.integers(1, 3))
        segments = [data.draw(_STATIC_SEGMENT) for _ in range(depth)]
        routes.add("/" + "/".join(segments))
    router = _native.Router(sorted(routes))
    for route in routes:
        match = router.matches(route)
        assert match is not None and match[0] == route and match[1] == {}


@given(st.text(min_size=0, max_size=40))
@_EXAMPLES
def test_router_matches_never_raises(path):
    router = _native.Router(["/", "/posts/[id]", "/posts/new", "/[...rest]"])
    result = router.matches(path)
    assert result is None or (
        isinstance(result, tuple) and len(result) == 2
        and isinstance(result[0], str) and isinstance(result[1], dict)
    )


@given(_TEXT)
@_EXAMPLES
def test_rendered_text_never_leaks_raw_specials(text):
    rendered = _native.render(json.dumps(h("p", {}, text)))
    assert rendered.startswith("<p>") and rendered.endswith("</p>")
    inner = rendered[3:-4]
    assert "<" not in inner and ">" not in inner
    assert "&" not in re.sub(r"&(amp|lt|gt|quot|#x27);", "", inner)
    assert _html.unescape(inner) == text


@given(_TEXT)
@_EXAMPLES
def test_rendered_attribute_values_round_trip(value):
    rendered = _native.render(
        json.dumps(h("a", {"title": value, "href": "/x"}, "t"))
    )
    match = re.search(r'title="([^"]*)"', rendered)
    assert match is not None
    assert _html.unescape(match.group(1)) == value


@given(st.sampled_from(_VOID_TAGS))
@_EXAMPLES
def test_void_elements_have_no_closing_tag_and_reject_children(tag):
    assert _native.render(json.dumps(h(tag, {}))) == f"<{tag}>"
    assert _native.render(
        json.dumps(h(tag, {"src": "/x.png"} if tag in {"img", "source"} else {}))
    ) == f"<{tag}" + (
        ' src="/x.png">' if tag in {"img", "source"} else ">"
    )
    with pytest.raises(ValueError):
        _native.render(json.dumps(h(tag, {}, "child")))


def _draw_tree(data, depth):
    if depth == 0 or data.draw(st.integers(0, 2)) == 0:
        return {"kind": "text", "value": data.draw(_TEXT)}
    if data.draw(st.booleans()):
        return {
            "kind": "fragment",
            "children": [
                _draw_tree(data, depth - 1)
                for _ in range(data.draw(st.integers(0, 3)))
            ],
        }
    tag = data.draw(st.sampled_from(_ELEMENT_TAGS))
    attrs = {}
    for _ in range(data.draw(st.integers(0, 2))):
        attrs[data.draw(st.sampled_from(["title", "id", "data-x"]))] = data.draw(_TEXT)
    children = [] if tag in _VOID_TAGS else [
        _draw_tree(data, depth - 1) for _ in range(data.draw(st.integers(0, 3)))
    ]
    return {"kind": "element", "tag": tag, "attrs": attrs, "children": children}


@given(st.data())
@_EXAMPLES
def test_random_trees_render_or_fail_cleanly(data):
    tree = _draw_tree(data, 3)
    try:
        rendered = _native.render(json.dumps(tree))
    except ValueError:
        return
    assert isinstance(rendered, str)


_JSON_VALUE = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.text(alphabet=list("abc <> & \" ' / \n\t"), min_size=0, max_size=12),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(
            st.text(alphabet="abcxyz", min_size=0, max_size=8),
            children,
            max_size=4,
        ),
    ),
    max_leaves=8,
)


@given(_JSON_VALUE)
@_EXAMPLES
def test_malformed_trees_fail_cleanly(value):
    try:
        rendered = _native.render(json.dumps(value))
    except ValueError:
        return
    assert isinstance(rendered, str)
