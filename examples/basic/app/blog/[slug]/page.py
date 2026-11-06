from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, "Blog post"), h("p", {}, params["slug"]))
