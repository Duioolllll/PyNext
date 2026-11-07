from pynext import h, island, page


def counter(initial):
    return island(
        "interaction-counter", props={"initial": initial}, hydrate=True, strategy="interaction",
        fallback=h("section", {},
                   h("button", {"type": "button"}, h("span", {"data-count": "1"}, f"Interaction: {initial}")),
                   h("a", {"href": "/about"}, "Normal link"),
                   h("form", {"action": "/about", "method": "get"},
                     h("button", {"type": "submit"}, "Normal submit"))),
    )


@page()
def Page():
    return h("main", {}, h("h1", {}, "Interaction example"),
             h("div", {"id": "first"}, counter(0)), h("div", {"id": "second"}, counter(10)))
