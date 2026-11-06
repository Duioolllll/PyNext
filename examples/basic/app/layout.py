from pynext import h, layout

@layout
def Layout(children):
    return h("html", {"lang": "en"},
             h("head", {}, h("meta", {"charset": "utf-8"}), h("title", {}, "PyNext example"),
               h("link", {"rel": "stylesheet", "href": "/styles/site.css"})),
             h("body", {}, h("nav", {}, h("a", {"href": "/"}, "Home")), children))
