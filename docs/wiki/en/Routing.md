# File-based routing

[Home](Home.md) · [한국어](../ko/Routing.md)

A directory below `app/` becomes a URL segment. `page.py` defines an HTML page; `route.py` defines API handlers. `layout.py` wraps descendant **pages**, not API routes. Page and API files cannot occupy the same URL.

~~~text
app/
  layout.py                   # page layout
  page.py                     # /
  about/page.py               # /about
  blog/[slug]/page.py         # /blog/hello
  docs/[...path]/page.py      # /docs/a/b
  api/users/route.py          # /api/users
~~~

## Parameters and precedence

- **Static routes** outrank dynamic segments, which outrank catch-all segments. The Rust trie supports backtracking.
- `[slug]` passes a string: `params["slug"] == "hello"`.
- `[...path]` passes a nonempty list of strings: `params["path"] == ["a", "b"]`.
- Parameter conflicts are checked; syntactically different names may normalize to the same route slot.
- **Unsupported:** optional catch-all (`[[...slug]]`), route groups, parallel routes, intercepting routes and client-side navigation.

Example page:

~~~python
from pynext import h, page

@page()
async def Page(params):
    return h("h1", {}, params["slug"])
~~~

The decorated function must be exported as `Page`. It may receive named `request` and/or `params` arguments where allowed; no positional varargs or unrecognized mandatory arguments.

## Paths, URLs and imports

- Non-root paths with a trailing slash redirect with HTTP 308.
- Invalid UTF-8 or encoded separators in request paths are rejected.
- `/_pynext/` is reserved for compiled client assets.
- Files in `public/` are exposed from the URL root; a collision with an app route fails discovery.
- Use relative imports such as `from .helpers import title` in app modules. The framework does **not** insert your project root into global `sys.path`.

Next: [Rendering](Rendering.md) and [API routes](API-Routes-and-Streaming.md). See [upstream attribution](../../../third_party/NEXTJS.md) for the limited Next.js routing logic adaptation.
