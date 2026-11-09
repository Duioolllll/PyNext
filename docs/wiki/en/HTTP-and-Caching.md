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

The bounded ISR cache uses single-flight regeneration. The default `memory` backend is process-local; the `sqlite` and `redis` backends share entries across workers with expiring leases and fenced writes, so another worker's invalidation is observed on the next lookup while the store is available (local fallback during an outage may be stale). Fresh anonymous cache hits can bypass Python; stale entries may be served during background regeneration. `Cookie` or `Authorization` bypass the shared cache. Invalidate through `await request.revalidate("/news/example")` or `await app.revalidate(path)`, after authenticating the API caller.

## On-demand revalidation

An ISR page can opt out of time-based expiry with `@page(render="isr", revalidate=False)`: it stays fresh until it is explicitly invalidated. Pages declare data tags with `@page(render="isr", revalidate=30, tags=("post:42", "author:7"))`, and `await app.revalidate_tag("post:42")` (or `await request.revalidate_tag("post:42")`) invalidates every cached entry carrying that tag, across workers when a shared backend is configured. `revalidate_tag(tag, expire=60)` first marks matching entries stale — served once more while a background regeneration runs — and expires them 60 seconds later. `await app.revalidate(path, type="layout")` invalidates every ISR page sharing the layout at that path (`/blog` for `app/blog/layout.py`, `/` for the root layout); `revalidate(path)` treats `/` and `/index` as aliases. A generation that started before a tag invalidation still serves its caller but is never stored. Tag invalidation is recorded as timestamps in the shared backend, so entries generated after the invalidation are unaffected, and other workers observe it on their next lookup. Each worker's local store is also evicted eagerly, so a native fresh hit does not keep serving an invalidated entry.

External systems can trigger the same invalidations over HTTP: `POST /_pynext/revalidate` with `{"paths": [...], "tags": [...], "type": "page"|"layout", "expire": 60}`. The endpoint is disabled (404) unless a secret is configured — pass `revalidate_secret=` to `create_app()` or set `PYNEXT_REVALIDATE_SECRET` — and otherwise requires `Authorization: Bearer <secret>`. Every target is validated before anything is invalidated, so a bad request changes nothing.

## Safety notes

The native adapter can return 504 after a Python request timeout, but an already-running synchronous function may continue. Always add DB deadlines and idempotency for state-changing operations. This framework is still experimental; see [Deployment and security](Deployment-and-Security.md).
