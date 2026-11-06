from pynext import h, island, page

@page()
def Page():
    return h("main", {}, h("h1", {}, "Island loading strategies"),
             island("idle-counter", props={"initial": 1}, strategy="idle",
                    fallback=h("p", {}, "Waiting for idle time.")),
             island("hydrated-counter", props={"initial": 6}, strategy="idle", hydrate=True,
                    fallback=h("button", {}, "Hydrated: 6")),
             island("unused", strategy="idle", fallback=h("p", {}, "Pending idle island.")),
             h("div", {"class": "island-spacer"}, "Scroll down to activate the next island."),
             island("visible-counter", props={"initial": 3}, strategy="visible", hydrate=True,
                    fallback=h("button", {}, "Visible: 3")),
             island("unused", strategy="visible", fallback=h("p", {}, "Pending visible island.")))
