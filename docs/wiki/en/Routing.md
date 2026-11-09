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

## Error boundaries

A failing page no longer always becomes the global 500 page. Each route
segment can define its own fallback, discovered at build time from files
next to `page.py`:

~~~text
app/
  error.py                    # global error boundary (every route)
  not-found.py                # global not-found boundary
  blog/
    error.py                  # /blog/* pages use this one first
    not-found.py
    [slug]/page.py
  stream/
    loading.py                # placeholder while a streaming page loads
~~~

- `error.py` must export `ErrorPage(error, request=None, params=None)`,
  returning an `h()` tree. It renders with status 500 when the page or
  one of its layouts raises. The status is decided by the framework: a
  boundary returns a tree and can never change it. The boundary first
  renders inside the route layouts, so a failing page still shows the
  site shell; when a layout itself fails, the boundary renders bare.
  When the nearest boundary also fails, the global `app/error.py` is
  tried, and finally the plain 500 page.
- `not-found.py` must export `NotFoundPage(request=None, params=None)`.
  It renders with status 404 for a route miss and for a page (or layout)
  that raises `NotFoundError` from `pynext`. The same nearest-first
  lookup applies. Route misses render the boundary only for clients
  that accept `text/html`; other clients keep the plain `Not Found`
  body, as do API routes and static assets.
- `loading.py` must export `LoadingPage(request=None, params=None)`.
  It applies to streaming pages only: its node is sent as the first
  chunk, before the page generator's first chunk, so a slow page still
  paints a placeholder inside the layout shell. It is never baked into
  SSG output or the ISR cache; only the live client sees it.
- A streaming page that fails after the headers were sent cannot change
  its status to 500. The stream instead ends with the
  `<!-- pynext-stream-error -->` marker and the closing tags, so the
  client can tell the document is truncated.
- API routes do not use boundaries: an API handler that raises still
  gets the plain 500 page. `NotFoundError` is a page signal only.

`ErrorPage` receives the exception object. Do not echo it verbatim in
production: it can contain internals. Show a generic message and a link
back (the request path is available as `request.path`).

Next: [Rendering](Rendering.md) and [API routes](API-Routes-and-Streaming.md). See [upstream attribution](../../../third_party/NEXTJS.md) for the limited Next.js routing logic adaptation.
