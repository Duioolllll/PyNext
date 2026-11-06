from pynext import h, island, page

@page(render="ssg")
def Page():
    return h("main", {}, h("h1", {}, "About"), h("p", {}, "Built once, served as static HTML."),
             island("counter", props={"initial": 5}, fallback=h("p", {}, "Enable JavaScript to use the counter.")),
             island("hydrated-counter", props={"initial": 3}, hydrate=True,
                    fallback=h("button", {}, "Hydrated: 3")))
