from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("main", {}, h("h1", {}, "Guide"), h("p", {}, params["slug"]))
