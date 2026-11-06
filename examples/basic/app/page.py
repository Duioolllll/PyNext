from pynext import h, page

@page()
async def Page(request):
    note = request.query.get("note", [""])[0]
    return h("main", {}, h("h1", {}, "Hello PyNext!"), h("p", {}, note))
