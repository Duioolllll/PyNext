import pytest

from pynext import _native, url_for
from pynext.build import _materialize


def test_static_pattern_needs_no_params():
    assert url_for("/", {}) == "/"
    assert url_for("/about", {}) == "/about"


def test_dynamic_parameter():
    assert url_for("/blog/[slug]", {"slug": "hello"}) == "/blog/hello"
    assert url_for("/users/[id]/posts/[post]", {"id": "7", "post": "42"}) == "/users/7/posts/42"


def test_catch_all_parameter():
    assert url_for("/docs/[...path]", {"path": ["a", "b", "c"]}) == "/docs/a/b/c"


def test_optional_catch_all_empty_and_filled():
    assert url_for("/docs/[[...path]]", {"path": []}) == "/docs"
    assert url_for("/docs/[[...path]]", {"path": ["a"]}) == "/docs/a"
    assert url_for("/docs/[[...path]]", {"path": ["a", "b"]}) == "/docs/a/b"


def test_values_are_percent_encoded_per_segment():
    assert url_for("/blog/[slug]", {"slug": "a b"}) == "/blog/a%20b"
    assert url_for("/blog/[slug]", {"slug": "한글"}) == "/blog/%ED%95%9C%EA%B8%80"
    assert url_for("/blog/[slug]", {"slug": "a+b&c=d"}) == "/blog/a%2Bb%26c%3Dd"
    assert url_for("/docs/[...path]", {"path": ["a b", "c/d".replace("/", "")]}) == "/docs/a%20b/cd"


@pytest.mark.parametrize("bad", ["a/b", "a?b", "a#b", "a\\b", ".", "..", "", "a\x01b", "a\x7fb"])
def test_rejected_values(bad):
    with pytest.raises(ValueError):
        url_for("/blog/[slug]", {"slug": bad})


@pytest.mark.parametrize("bad", [[""], ["a", ""], [".."], ["a/b"]])
def test_rejected_catch_all_values(bad):
    with pytest.raises(ValueError):
        url_for("/docs/[...path]", {"path": bad})


def test_catch_all_requires_nonempty_list():
    with pytest.raises(ValueError):
        url_for("/docs/[...path]", {"path": []})
    with pytest.raises(ValueError):
        url_for("/docs/[...path]", {"path": "a"})


def test_optional_catch_all_requires_list():
    with pytest.raises(ValueError):
        url_for("/docs/[[...path]]", {"path": "a"})


@pytest.mark.parametrize("params", [{}, {"slug": "x", "extra": "y"}, {"other": "x"}, "x", None])
def test_parameter_set_must_match_exactly(params):
    with pytest.raises(ValueError):
        url_for("/blog/[slug]", params)


def test_pattern_must_start_with_slash():
    with pytest.raises(ValueError):
        url_for("blog/[slug]", {"slug": "x"})


def test_invalid_segment_raises():
    with pytest.raises(ValueError):
        url_for("/blog/[slug", {"slug": "x"})
    with pytest.raises(ValueError):
        url_for("/blog/[[slug]]", {"slug": "x"})


def test_router_self_check_accepts_matching_router():
    router = _native.Router(["/blog/[slug]", "/docs/[[...path]]"])
    assert url_for("/blog/[slug]", {"slug": "a b"}, router=router) == "/blog/a%20b"
    assert url_for("/docs/[[...path]]", {"path": []}, router=router) == "/docs"
    assert url_for("/docs/[[...path]]", {"path": ["a", "b"]}, router=router) == "/docs/a/b"


def test_router_self_check_rejects_unknown_pattern():
    router = _native.Router(["/blog/[slug]"])
    with pytest.raises(ValueError):
        url_for("/news/[slug]", {"slug": "x"}, router=router)


def test_router_self_check_rejects_wrong_pattern_match():
    # "/blog/new" is a static route that shadows "/blog/[slug]".
    router = _native.Router(["/blog/[slug]", "/blog/new"])
    with pytest.raises(ValueError):
        url_for("/blog/[slug]", {"slug": "new"}, router=router)


def test_materialize_stays_unencoded_for_ssg_manifest_keys():
    # Build-internal _materialize() shares the validation with url_for()
    # but deliberately does NOT percent-encode: static-page manifest keys
    # are decoded paths.
    assert _materialize("/blog/[slug]", {"slug": "한글"}) == "/blog/한글"
    assert _materialize("/docs/[[...path]]", {"path": []}) == "/docs"
    assert _materialize("/docs/[...path]", {"path": ["a", "b"]}) == "/docs/a/b"
    with pytest.raises(ValueError):
        _materialize("/blog/[slug]", {"slug": "a/b"})
    with pytest.raises(ValueError):
        _materialize("/blog/[slug]", {"slug": "x", "extra": "y"})


def test_url_for_rejects_patterns_the_router_rejects():
    """A1-L1: a catch-all anywhere but last, an empty segment, a trailing
    slash, or a '.' / '..' segment must raise ValueError instead of
    silently producing a URL no route can ever match."""
    with pytest.raises(ValueError, match="must be the last segment"):
        url_for("/docs/[...path]/edit", {"path": ["a", "b"]})
    with pytest.raises(ValueError, match="must be the last segment"):
        url_for("/docs/[[...path]]/edit", {"path": ["a"]})
    with pytest.raises(ValueError, match="empty"):
        url_for("/docs//edit", {})
    with pytest.raises(ValueError, match="must not end with"):
        url_for("/docs/", {})
    with pytest.raises(ValueError, match="must not end with"):
        url_for("/docs/[id]/", {"id": "x"})
    with pytest.raises(ValueError, match="empty"):
        url_for("/docs/./edit", {})
    with pytest.raises(ValueError, match="empty"):
        url_for("/docs/../edit", {})
    # The valid shapes still work, including an optional catch-all with
    # no segments and a catch-all in last position.
    assert url_for("/docs/[...path]", {"path": ["a", "b"]}) == "/docs/a/b"
    assert url_for("/docs/[[...path]]", {"path": []}) == "/docs"
    assert url_for("/", {}) == "/"
