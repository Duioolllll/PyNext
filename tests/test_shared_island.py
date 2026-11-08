import json
from html.parser import HTMLParser

import pytest

from pynext import _native, fragment, h, island, model_prop
from pynext.components import bind_islands


MANIFEST = {"runtime": "a" * 64, "bundles": {"sample": "b" * 64}, "hydration": {"sample": True}}


def test_shared_model_drives_server_fallback_and_is_serialized_safely():
    attack = '</button><script>alert("x")</script>&'
    model = h("button", {"type": "button"}, "Hello ", model_prop("name"))
    node = island("sample", props={"name": attack}, hydrate=True, model=model)
    # Re-resolve at binding, so caller changes to a stale fallback cannot
    # alter the DOM independently of the shared model.
    node["children"] = [h("p", {}, "stale")]
    rendered = _native.render(json.dumps(bind_islands(node, MANIFEST)))
    assert "stale" not in rendered and "<script>alert" not in rendered
    assert "Hello &lt;/button&gt;" in rendered

    class Parser(HTMLParser):
        attrs = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if "data-pynext-model" in attributes:
                self.attrs = attributes

    parser = Parser()
    parser.feed(rendered)
    assert json.loads(parser.attrs["data-pynext-model"]) == {"version": 1, "tree": model}
    assert json.loads(parser.attrs["data-pynext-props"]) == {"name": attack}


@pytest.mark.parametrize("model,props", [
    (model_prop("missing"), {}),
    (model_prop("name"), {"name": 42}),
    (h("script", {}, "bad"), {}),
    (h("button", {"onclick": "bad"}, "bad"), {}),
    (h("a", {"href": "java\nscript:alert(1)"}, "bad"), {}),
    (h("div", {"data-pynext-island": "fake"}), {}),
    (fragment(*(h("span", {}, "x") for _ in range(513))), {}),
])
def test_shared_model_rejects_unsafe_or_unbounded_input(model, props):
    with pytest.raises(ValueError):
        island("sample", props=props, hydrate=True, model=model)


def test_shared_model_requires_hydration_and_separate_fallback_is_rejected():
    with pytest.raises(ValueError, match="requires hydrate"):
        island("sample", model=h("p", {}, "x"))
    with pytest.raises(ValueError, match="separate fallback"):
        island("sample", hydrate=True, model=h("p", {}, "x"), fallback=h("p", {}, "x"))
    with pytest.raises(ValueError, match="prop name"):
        model_prop("invalid-key")


def test_native_rejects_forged_model_version():
    bound = bind_islands(island("sample", props={"name": "x"}, hydrate=True,
                                model=h("p", {}, model_prop("name"))), MANIFEST)
    bound["children"][0]["model"]["version"] = 2
    with pytest.raises(ValueError, match="Invalid shared island model"):
        _native.render(json.dumps(bound))
