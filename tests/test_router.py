import pytest
from pynext import _native


def test_next_source_order_and_parameters():
    router = _native.Router(["/posts/[id]", "/[...rest]", "/posts/new", "/", "/[root]", "/posts"])
    assert router.ordered_routes() == ["/", "/posts", "/posts/new", "/posts/[id]", "/[root]", "/[...rest]"]
    assert router.matches("/posts/new")[0] == "/posts/new"
    assert router.matches("/posts/42") == ("/posts/[id]", {"id": ["42"]})
    assert router.matches("/a/b") == ("/[...rest]", {"rest": ["a", "b"]})


def test_trie_matching_prefers_static_and_backtracks():
    router = _native.Router(["/a/b/c", "/a/[x]/d", "/a/b"])
    assert router.matches("/a/b/d") == ("/a/[x]/d", {"x": ["b"]})
    assert router.matches("/a/b/c")[0] == "/a/b/c"
    assert router.matches("/a/b")[0] == "/a/b"
    assert router.matches("/a/b/c/d") is None


def test_catch_all_requires_a_segment():
    router = _native.Router(["/[...rest]"])
    assert router.matches("/") is None
    assert router.matches("/a") == ("/[...rest]", {"rest": ["a"]})


@pytest.mark.parametrize("paths", [
    ["/blog/[id]", "/blog/[slug]"], ["/blog/[...id]/a"], ["/[id]/[id]"],
    ["/[[...id]]"], ["/[..id]"], ["/x", "/x"],
])
def test_invalid_or_ambiguous_routes(paths):
    with pytest.raises(ValueError):
        _native.Router(paths)
