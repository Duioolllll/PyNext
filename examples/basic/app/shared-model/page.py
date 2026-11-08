from pynext import h, island, model_prop, page


def counter():
    return island(
        "shared-model", props={"name": "Ada <safe> & Co"}, hydrate=True, strategy="interaction",
        model=h("button", {"type": "button", "data-count": "1", "title": 'Hello & "world"'},
                "Shared: ", model_prop("name")),
    )


@page()
def Page():
    return h("main", {}, h("h1", {}, "Shared island model"),
             h("div", {"id": "shared-first"}, counter()),
             h("div", {"id": "shared-second"}, counter()))
