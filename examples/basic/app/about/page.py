from pynext import h, page

@page(render="ssg")
def Page():
    return h("main", {}, h("h1", {}, "About"), h("p", {}, "Built once, served as static HTML."))
