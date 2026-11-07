import pytest

from pynext import h


@pytest.mark.parametrize("tag", [123, None, b"p", ["p"], 3.5])
def test_h_rejects_non_string_tag(tag):
    with pytest.raises(TypeError, match="Element tag must be a string"):
        h(tag)


@pytest.mark.parametrize("attrs", ["class=x", [("class", "x")], 42, {"a", "b"}])
def test_h_rejects_non_dict_attrs(attrs):
    with pytest.raises(TypeError, match="Element attributes must be a dict"):
        h("p", attrs)


def test_h_accepts_string_tag_and_dict_attrs():
    assert h("p", {"class": "x"}, "hi") == {
        "kind": "element",
        "tag": "p",
        "attrs": {"class": "x"},
        "children": [{"kind": "text", "value": "hi"}],
    }
    assert h("br")["attrs"] == {}
    assert h("p", None, "x")["attrs"] == {}
