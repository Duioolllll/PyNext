from pynext import h, island, page

@page()
async def Page(request):
    note = request.query.get("note", [""])[0]
    return h("main", {}, h("h1", {}, "Hello PyNext!"),
             h("p", {}, "Python pages, Rust routing and HTML rendering."),
             island("counter", props={"initial": 1, "note": note}, fallback=h("p", {}, "Enable JavaScript to use the counter.")),
             h("a", {"href": "/blog/hello"}, "Read a post"))
