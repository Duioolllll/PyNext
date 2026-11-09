# Rendering: SSR, SSG, ISR

[Home](Home.md) · [한국어](../ko/Rendering.md)

PyNext provides three explicit page rendering modes. **Page SSR is buffered**; it does not stream HTML chunks.

## SSR (default)

~~~python
from pynext import h, page

@page()
async def Page(request):
    note = request.query.get("note", [""])[0]
    return h("p", {}, note)
~~~

The handler runs per request. Python constructs the tree, serializes JSON, and Rust renders escaped HTML. This is not React SSR/RSC.

## SSG (build-time)

~~~python
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

Place this in `app/guides/[slug]/page.py`. Run `pynext build --dir PROJECT`. Only generated dynamic paths are served in production; other paths return **404**. A static route needs no generator. Builds are capped at **10,000** generated SSG pages. **SSG page functions must not accept `request`**; they render public content at build time.

Static renders are tracked for dynamic accesses: `time.time()`, `datetime.now()`, `random.*`, `uuid.uuid4()`, and `Database.session()` reads during a prerender are recorded with the page position that read them. The default `@page(dynamic="auto")` keeps building and records the accesses in the build manifest's `dynamic_accesses` report with a warning; `@page(dynamic="error")` fails the build — or the ISR generation — on the first recorded access; `@page(render="ssg", dynamic="force-dynamic")` skips build-time generation and serves the page dynamically in production instead. `no_store()` marks a scope dynamic on purpose, and `mark_dynamic_access("...")` records anything else the automatic hooks cannot see (an external fetch, or a `from datetime import datetime` binding, which escapes the prerender patch). In development, requesting an SSG path that `generate_static_params()` would not produce logs a warning that it will 404 in production; `pynext dev --strict-ssg` (or `create_app(..., strict_ssg=True)`) answers 404 in development as well.

Ungenerated dynamic paths can opt into a fallback instead of 404: `@page(render="ssg", fallback="blocking")` generates the page on first request and serves it statically afterwards, while `fallback="prerender"` streams the layout shell first and then the generated page, and also serves it statically afterwards. `fallback="not_found"` is the default. `dynamic_params=False` forces 404 for ungenerated paths even when a fallback is set. Concurrent first requests share one generation; a failed generation returns 500 and the next request retries.

## ISR (public content)

~~~python
from pynext import h, page

@page(render="isr", revalidate=30, stale=120)
def Page(params):
    return h("h1", {}, params["slug"])
~~~

A route such as `app/news/[slug]/page.py` generates on demand, serves fresh cached HTML, and can serve stale HTML while one background regeneration runs. The cache is bounded; the default `memory` backend is process-local, while the `sqlite`, `redis`, and `filesystem` backends share entries across workers. ISR page functions must not accept `request`.

A dynamic ISR page may export `generate_static_params()` (a static ISR route may not). The listed paths are enumerated at build time and warmed into the cache in the background at startup, so the first request after a deploy does not pay the full render cost. Warmup is best-effort: a path that fails to warm is logged and still renders on demand.
- Anonymous **query strings are not part of the ISR cache key**.
- Requests with `Cookie` or `Authorization` bypass shared ISR and receive private, no-store responses.
- Exposed ISR pages should not embed secrets, user information or request-specific content.
- `revalidate` must be positive; `stale` may be zero; both have upper bounds. `expire` is the absolute hard cap measured from generation: after `revalidate` the entry is served stale while one background regeneration runs, and after `expire` it is never served again (`expire` must be greater than `revalidate`; `float("inf")` means one year). `stale` and `expire` are two spellings of the same window (`expire == revalidate + stale`), so give only one of them, or both if they agree. A named profile can replace the numbers: `@page(render="isr", cache_life="hours")` — `default`, `seconds`, `minutes`, `hours`, `days`, `weeks`, `max` (see `pynext.CACHE_LIFE_PROFILES`).
- `revalidate` must be positive; `stale` may be zero; both have upper bounds.
- A layout can set the section policy with `@layout(revalidate=60, stale=120)`. ISR pages below it inherit any value they leave unset, and among the numbers the shortest `revalidate` (and smallest `stale`) wins — a page only beats its layouts with a shorter value. An ISR page with no `revalidate` anywhere in its layout chain fails at load, and a page that asks for a longer value than its layout gets is overridden with a build-time warning naming both files. Layout policies are recorded in the build manifest, so changing one requires a rebuild.

## Explicit invalidation

An authenticated API handler can run:

~~~python
success = await request.revalidate("/news/example")
~~~

An app instance can call `await app.revalidate(path)`. Pass a **decoded ISR path without a query string**. Invalidation protects against old in-flight generations repopulating the cache, but is **local to the current process**. Do not expose invalidation endpoints without authorization.

Related: [HTTP and caching](HTTP-and-Caching.md) · [Architecture](Architecture.md).
