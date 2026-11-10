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

## ISR (public content)

~~~python
from pynext import h, page

@page(render="isr", revalidate=30, stale=120)
def Page(params):
    return h("h1", {}, params["slug"])
~~~

A route such as `app/news/[slug]/page.py` generates on demand, serves fresh cached HTML, and can serve stale HTML while one background regeneration runs. The cache is bounded; the default `memory` backend is process-local, while the `sqlite` and `redis` backends share entries across workers. ISR page functions must not accept `request` or export `generate_static_params()`.

- Anonymous **query strings are not part of the ISR cache key**.
- Requests with `Cookie` or `Authorization` bypass shared ISR and receive private, no-store responses.
- Exposed ISR pages should not embed secrets, user information or request-specific content.
- `revalidate` must be positive; `stale` may be zero; both have upper bounds. `expire` is the absolute hard cap measured from generation: after `revalidate` the entry is served stale while one background regeneration runs, and after `expire` it is never served again (`expire` must be greater than `revalidate`; `float("inf")` means one year). `stale` and `expire` are two spellings of the same window (`expire == revalidate + stale`), so give only one of them, or both if they agree. A named profile can replace the numbers: `@page(render="isr", cache_life="hours")` — `default`, `seconds`, `minutes`, `hours`, `days`, `weeks`, `max` (see `pynext.CACHE_LIFE_PROFILES`).

## Explicit invalidation

An authenticated API handler can run:

~~~python
success = await request.revalidate("/news/example")
~~~

An app instance can call `await app.revalidate(path)`. Pass a **decoded ISR path without a query string**. Invalidation protects against old in-flight generations repopulating the cache, but is **local to the current process**. Do not expose invalidation endpoints without authorization.

Related: [HTTP and caching](HTTP-and-Caching.md) · [Architecture](Architecture.md).
