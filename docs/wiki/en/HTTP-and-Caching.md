# HTTP, admission control and caching

[Home](Home.md) · [한국어](../ko/HTTP-and-Caching.md)

## Native HTTP boundaries

The native server uses Axum/Hyper with HTTP/1.1 and **HTTP/2 prior knowledge (h2c)**. The defaults exposed by `pynext start/dev` are:

| Control | Default | Meaning |
| --- | --- | --- |
| `--max-concurrent` | 64 | Python execution slots |
| `--acquire-timeout-ms` | 1000 | Wait for execution slot, then 503 |
| `--max-waiting` | min(`--max-concurrent`, 64) | Requests waiting for an execution slot; 0 waits for nobody |
| `--body-capacity` | 16 | Concurrent body reads |
| `--body-timeout-ms` | 10000 | Body deadline; timeout returns 408 |
| Body size | 1 MiB | Oversized requests return 413 |
| HTTP/1.1 headers | 10s | Header-read deadline |

The number of waiting execution requests is bounded by `--max-waiting` (default: the smaller of the execution limit and 64; 0 means a slot must be free immediately). Overload policy is bounded wait, then reject: a request that waits the full `--acquire-timeout-ms` without a slot gets **503** with `Retry-After` of the timeout rounded up to whole seconds (at least 1), and a request rejected because the wait queue is full gets `Retry-After: 1`. The queue is not enlarged without bound and requests are not shed probabilistically: each waiter can already hold a fully buffered request body. Some static/SSG/fresh anonymous ISR GET/HEAD requests bypass Python execution slots. **The native server also caps total connections (default 1024) and connections per IP (default 128), configurable with `--max-connections` / `--max-connections-per-ip`.** External ASGI servers provide their own connection/timeout policy.

## Asset HTTP behavior

The built public assets, SSG output, and JS island bundles support content-derived strong ETags and conditional `If-Match` / `If-None-Match` requests. Single byte `Range` requests and strong-ETag `If-Range` are supported for the applicable GET resources; invalid/multiple ranges fall back to full responses. Date-based validators and ISR response ranges are not implemented.

- Content-addressed island bundles: `public, max-age=31536000, immutable`.
- Public files / SSG HTML: `public, max-age=0, must-revalidate`.
- Public files are mapped from `public/path` to `/path`; hidden files and symlinks are rejected.

## ISR boundaries

The bounded ISR cache uses single-flight regeneration. The default `memory` backend is process-local; the `sqlite` and `redis` backends share entries across workers with expiring leases and fenced writes, so another worker's invalidation is observed on the next lookup while the store is available (local fallback during an outage may be stale). The SQLite backend measures entry lifetimes with a monotonic clock (a wall-clock jump can neither resurrect an expired entry nor kill a fresh one) and bounds the table to 10,000 entries / 128 MiB by default (tunable via `sqlite_max_entries` / `sqlite_max_bytes`), dropping expired rows and evicting the oldest entries on insert. Fresh anonymous cache hits can bypass Python; stale entries may be served during background regeneration. `Cookie` or `Authorization` bypass the shared cache. Invalidate through `await request.revalidate("/news/example")` or `await app.revalidate(path)`, after authenticating the API caller. `revalidate(path, type="page")` invalidates every cached entry for a dynamic route pattern such as `"/news/[slug]"` (a dynamic pattern without `type` only warns and invalidates nothing), and `revalidate(path, type="layout")` invalidates every ISR page using the layout defined at `path` (`"/"` is the root layout, so it covers all pages). `"/"` and `"/index"` are aliases.


Tag-based invalidation groups pages by the data they depend on instead of by path. Declare tags on an ISR page with `@page(render="isr", revalidate=30, stale=60, tags=("post:42", "author:7"))` (at most 64 tags per page; a tag is 1–256 characters from `[A-Za-z0-9:_-./]`). Call `await app.revalidate_tag("post:42")` to expire every entry carrying that tag at once, or `await app.revalidate_tag("post:42", expire=60)` to keep entries merely stale — served once more while a regeneration runs — for 60 seconds before they expire. Entries generated after the call are unaffected, and with the `sqlite`/`redis` backends the invalidation is stored in the shared store so other workers see it (the `memory` backend is process-local). Changing a page's tags requires a rebuild in production.

An ISR policy can also be overridden at runtime without rewriting the build manifest or rebuilding: `app.set_isr_policy("/blog/[slug]", revalidate=60)` changes the policy for generations started after the call, while `app.get_isr_policy(pattern)` reports the effective policy (override first, build-time policy otherwise) and `app.clear_isr_policy(pattern)` / `app.clear_isr_policies()` drop overrides. Overrides are process-local (each worker needs its own call) and are lost on restart; entries already cached keep the lifetimes they were stored with, so pair an override with `await app.revalidate(path)` when a cached entry must pick the new policy up immediately. This mirrors Next.js `SharedCacheControls`, which keeps runtime cache controls in a copy-on-write in-memory map in front of the prerender manifest.

## On-demand revalidation

An ISR page can opt out of time-based expiry with `@page(render="isr", revalidate=False)`: it stays fresh until it is explicitly invalidated. Pages declare data tags with `@page(render="isr", revalidate=30, tags=("post:42", "author:7"))`, and `await app.revalidate_tag("post:42")` (or `await request.revalidate_tag("post:42")`) invalidates every cached entry carrying that tag, across workers when a shared backend is configured. `revalidate_tag(tag, expire=60)` first marks matching entries stale — served once more while a background regeneration runs — and expires them 60 seconds later. `await app.revalidate(path, type="layout")` invalidates every ISR page sharing the layout at that path (`/blog` for `app/blog/layout.py`, `/` for the root layout); `revalidate(path)` treats `/` and `/index` as aliases. A generation that started before a tag invalidation still serves its caller but is never stored. Tag invalidation is recorded as timestamps in the shared backend, so entries generated after the invalidation are unaffected, and other workers observe it on their next lookup. Each worker's local store is also evicted eagerly, so a native fresh hit does not keep serving an invalidated entry.

External systems can trigger the same invalidations over HTTP: `POST /_pynext/revalidate` with `{"paths": [...], "tags": [...], "type": "page"|"layout", "expire": 60}`. The endpoint is disabled (404) unless a secret is configured — pass `revalidate_secret=` to `create_app()` or set `PYNEXT_REVALIDATE_SECRET` — and otherwise requires `Authorization: Bearer <secret>`. Every target is validated before anything is invalidated, so a bad request changes nothing.

## Safety notes

The native adapter can return 504 after a Python request timeout, but an already-running synchronous function may continue. Always add DB deadlines and idempotency for state-changing operations. This framework is still experimental; see [Deployment and security](Deployment-and-Security.md).
