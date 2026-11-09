import pytest
from pynext import _native


def test_next_source_order_and_parameters():
    router = _native.Router(["/posts/[id]", "/[...rest]", "/posts/new", "/", "/[root]", "/posts"])
    assert router.ordered_routes() == ["/", "/posts", "/posts/new", "/posts/[id]", "/[root]", "/[...rest]"]
    assert router.matches("/posts/new")[0] == "/posts/new"
    assert router.matches("/posts/42") == ("/posts/[id]", {"id": ["42"]}, {"id": "dynamic"})
    assert router.matches("/a/b") == ("/[...rest]", {"rest": ["a", "b"]}, {"rest": "catch_all"})


@pytest.mark.parametrize("paths", [
    ["/blog/[id]", "/blog/[slug]"], ["/blog/[...id]/a"], ["/[id]/[id]"],
    ["/[[id]]"], ["/[..id]"], ["/x", "/x"],
    ["/docs/[...p]", "/docs/[[...q]]"],
])
def test_invalid_or_ambiguous_routes(paths):
    with pytest.raises(ValueError):
        _native.Router(paths)


def test_optional_catch_all_is_valid_and_matches_zero_segments():
    router = _native.Router(["/docs/[[...path]]"])
    assert router.matches("/docs") == ("/docs/[[...path]]", {"path": []}, {"path": "optional"})
    assert router.matches("/docs/a/b") == ("/docs/[[...path]]", {"path": ["a", "b"]}, {"path": "optional"})


def test_matches_reports_parameter_kinds():
    router = _native.Router(["/posts/[id]", "/docs/[...path]", "/files/[[...rest]]"])
    assert router.matches("/posts/42")[2] == {"id": "dynamic"}
    assert router.matches("/docs/a/b")[2] == {"path": "catch_all"}
    assert router.matches("/files")[2] == {"rest": "optional"}
    # A catch-all name must not leak into a plain dynamic parameter whose
    # name is its prefix: "[...a]" is a substring of "/[a]/[...a2]".
    router = _native.Router(["/[a]/[...a2]"])
    pattern, params, kinds = router.matches("/x/y/z")
    assert pattern == "/[a]/[...a2]"
    assert kinds == {"a": "dynamic", "a2": "catch_all"}
    assert params == {"a": ["x"], "a2": ["y", "z"]}
