import pytest
from pynext import _native


def test_next_source_order_and_parameters():
    router = _native.Router(["/posts/[id]", "/[...rest]", "/posts/new", "/", "/[root]", "/posts"])
    assert router.ordered_routes() == ["/", "/posts", "/posts/new", "/posts/[id]", "/[root]", "/[...rest]"]
    assert router.matches("/posts/new")[0] == "/posts/new"
    assert router.matches("/posts/42") == ("/posts/[id]", {"id": ["42"]})
    assert router.matches("/a/b") == ("/[...rest]", {"rest": ["a", "b"]})


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
    assert router.matches("/docs") == ("/docs/[[...path]]", {"path": []})
    assert router.matches("/docs/a/b") == ("/docs/[[...path]]", {"path": ["a", "b"]})
