from pynext import fragment, h, island, page

@page()
def Page():
    return h("main", {}, h("h1", {}, "Server-rendered island"),
             island("hydrated-counter", props={"initial": 7, "input": "server"}, hydrate=True,
                    fallback=fragment(h("button", {}, "Hydrated: 7"),
                                      h("input", {"type": "text", "value": "server"}))))
