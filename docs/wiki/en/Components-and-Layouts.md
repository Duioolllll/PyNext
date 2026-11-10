# Components and layouts

[Home](Home.md) · [한국어](../ko/Components-and-Layouts.md)

PyNext's server UI tree is built using Python helpers. Python code is not sent to the browser.

## Element and fragment helpers

~~~python
from pynext import fragment, h, page

@page()
def Page():
    return h(
        "main", {"class": "content"},
        h("h1", {}, "Documentation"),
        fragment(
            h("p", {}, "Safe escaped text"),
            h("a", {"href": "/about"}, "About"),
        ),
    )
~~~

- `h(tag, attrs=None, *children)` returns an element node.
- `fragment(*children)` groups nodes without an extra HTML element.
- Children may be render nodes, strings, integers, or floats; `None` children are skipped.
- Text is HTML-escaped in the Rust renderer. There is **no raw HTML injection API**.
- Invalid nodes, malformed attributes, nested islands and excessively deep trees are rejected.

## Shared layouts

Create `app/layout.py`:

~~~python
from pynext import h, layout

@layout
def Layout(children):
    return h(
        "html", {"lang": "en"},
        h("head", {}, h("title", {}, "My PyNext Site")),
        h("body", {}, h("nav", {}, "Welcome"), children),
    )
~~~

Layouts in ancestor directories wrap page output, including nested route layouts. A root HTML layout results in a full HTML document with a doctype.

Layouts receive only `children`, so the layout shell — the layouts applied to the page — is rendered once per route and cached for later requests: streaming pages reuse the cached shell prefix/suffix, and buffered pages substitute the page into the cached shell. Treat `children` as opaque and render it exactly once: the shell is built with a placeholder where the page goes. A layout that reads mutable state (a clock, a database, a global) must opt out with `@layout(dynamic=True)` so its shell is rendered on every request. Layouts must render their children exactly once.

## Rendering rules

Pages use `@page()`, `@page(render="ssg")` or `@page(render="isr", revalidate=30, stale=120)`. Shared layouts use `@layout`. A valid `Page` function returns one node dictionary; returning arbitrary text or raw HTML is not supported.

When a page contains islands, the framework binds the built JavaScript module references and injects its own client runtime. Keep secrets out of island props; props are public browser data.

See [Rendering](Rendering.md), [Islands and hydration](Islands-and-Hydration.md), and the [real example layout](../../../examples/basic/app/layout.py).
