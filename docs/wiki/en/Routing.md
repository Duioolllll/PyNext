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
- A dynamic parameter may carry a static prefix and/or suffix: `v[version]` matches `/v2`, `[name].json` matches `/report.json`. The middle must be nonempty; catch-all parameters cannot carry a prefix or suffix. Affixed segments outrank a bare dynamic segment, and a static segment still wins over both.
- Parameter conflicts are checked; syntactically different names may normalize to the same route slot.
- **Unsupported:** parallel routes, intercepting routes and client-side navigation.

## Reverse routing

`pynext.url_for(pattern, params)` builds a URL from a route pattern (the public form of the build-internal static-path materialisation). Parameter names must match the pattern exactly; catch-all parameters take a list of segments, and an optional catch-all takes an empty list for "no segments". Every value is percent-encoded per segment, so a value can never smuggle a `/`, `?` or `#` into the URL.

~~~python
from pynext import url_for

url_for("/blog/[slug]", {"slug": "a b"})            # "/blog/a%20b"
url_for("/docs/[[...path]]", {"path": []})          # "/docs"
url_for("/docs/[...path]", {"path": ["a", "b"]})    # "/docs/a/b"
~~~

Pass a `router` (a `pynext._native.Router` holding the pattern) to verify the generated URL matches the pattern it was built from. A missing or extra parameter, or a value containing `/`, `?`, `#`, a control character, `.` or `..`, raises `ValueError`.

## Custom routes

`create_app(..., custom_routes={...})` accepts minimal redirects, rewrites and headers. Sources use the same `[slug]` / `[...rest]` / `[[...rest]]` segment grammar as file routes; destinations interpolate parameters as `[name]`. Rules are compiled and validated at startup — a bad source, a destination naming an unknown parameter, or a malformed header fails `create_app()` instead of failing per request. `has` / `missing` conditions are not supported.

~~~python
app = create_app(root, custom_routes={
    "redirects": [
        {"source": "/old/[slug]", "destination": "/blog/[slug]", "permanent": True},
    ],
    "rewrites": [
        {"source": "/legacy/[slug]", "destination": "/blog/[slug]"},
    ],
    "headers": [
        {"source": "/blog/[slug]",
         "headers": [{"key": "Cache-Control", "value": "public, max-age=3600"}]},
    ],
})
~~~

- **Redirects** answer 308 when `permanent` is true, 307 otherwise, and preserve the request query string when the destination has none. Destinations may be absolute `http(s)` URLs; rewrite destinations must start with `/`.
- **Rewrites** serve the destination path as if it had been requested. A list means the `beforeFiles` phase; a `{"beforeFiles": [...], "afterFiles": [...], "fallback": [...]}` object selects phases explicitly, in Next.js order: redirect → rewrite (`beforeFiles`) → file/route check → rewrite (`afterFiles`) → rewrite (`fallback`). At most one rewrite applies per request, so rewrites cannot chain or loop.
- **Headers** apply to every response whose requested path matches, including redirects and 404s.

Example page:

~~~python
from pynext import h, page

@page()
async def Page(params):
    return h("h1", {}, params["slug"])
~~~

The decorated function must be exported as `Page`. It may receive named `request` and/or `params` arguments where allowed; no positional varargs or unrecognized mandatory arguments.

## Paths, URLs and imports

- Non-root paths with a trailing slash redirect with HTTP 308 by default; see "Base path and trailing slash" below.
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

## Base path and trailing slash

Path preprocessing (`getNextPathnameInfo()` in Next.js, i18n locale step excluded — PyNext has no locale routing):

- `create_app(..., base_path="/app")` serves the whole app under `/app`. The prefix is stripped before route matching, so routes are still defined as `/`, `/about`, `/blog/[slug]`, and `url_for("/blog/[slug]", {"slug": "hi"}, base_path="/app")` returns `/app/blog/hi`. A path outside the prefix is 404, and the prefix must end on a segment boundary (`/application` is not under `/app`). The same stripping applies to `/_pynext/` assets, public files, WebSocket upgrades (ASGI) and `revalidate()` paths.
- `create_app(..., trailing_slash="redirect")` (default) keeps the long-standing behaviour: a non-root path with a trailing slash answers HTTP 308 to the same path without it, with the base path preserved in `Location`. `trailing_slash="ignore"` serves the path without redirecting instead.
- Both settings are validated at startup: a base path must start with `/`, must not end with `/`, and must not contain empty, `.` or `..` segments; `trailing_slash` must be `"redirect"` or `"ignore"`.
- Two paths stay unprefixed by design: the health probes (`/healthz`, `/readyz`) are answered before preprocessing, and rate-limit exemption is evaluated on the raw (prefixed) path. A load balancer should probe `/healthz` at the root, not under the base path.
