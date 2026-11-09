# PyNext manual — English

[한국어](../ko/manual.md) · [日本語](../ja/manual.md) · [简体中文](../zh-CN/manual.md)

This manual describes the code in this checkout, corresponding to the experimental **0.1.0** version. PyNext is an experimental Python + Rust full-stack framework. It is not compatible with Next.js, React, or React Server Components. Do not expose this prototype directly to the public internet or run untrusted applications with it. See the [version history](../../CHANGELOG.md) and [future work](../PRODUCTION_ROADMAP.md).

> Canonical source: this English manual is the single source of truth for PyNext documentation. The Korean, Japanese, and Simplified Chinese manuals are translations of it; if they disagree, this manual wins. The capability list is generated from code — see [FEATURES](../FEATURES.md).

## Requirements and first run

Use CPython 3.13 or 3.14, a Rust toolchain with Cargo, and [uv](https://docs.astral.sh/uv/). Supported environments are verified by the CI matrix: Ubuntu, macOS, and Windows (latest), CPython 3.13 and 3.14, and the standalone Axum and Uvicorn-backed ASGI deploy modes (the in-process ASGI mode is covered by pytest in every CI job). Browser smoke runs on all three OSes. The example uses islands and additionally requires Node.js 18 or newer and npm to install esbuild 0.28.1.

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout main
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

Open http://127.0.0.1:3000/, /about, /guides/intro, /news/example, /hydrate, /strategies, and /api/users. Stop the server with Ctrl+C.

PyNext serves plain HTTP only and does not terminate TLS; see [ADR-0001](../adr/0001-tls-termination.md). For anything beyond localhost, put a TLS-terminating reverse proxy in front of it and keep PyNext bound to loopback or a trusted private network.

Warning: `pynext build` imports and executes the application's Python code (page, layout, middleware and resource modules, plus SSG generators) and runs the island bundler. Only build apps you trust. To build an app you do not fully trust, use the sandboxed build recipe in [docs/SANDBOXED_BUILD.md](../SANDBOXED_BUILD.md).

`pynext dev --dir examples/basic` serves source without a build and reloads when `app/`, `islands/`, `public/` or `pynext.config.py` change, restarting the server on the same port. `pynext start` uses the build manifest and fails if tracked source files changed; rebuild first. For an app without islands, Node and esbuild are not required. The repository is the installation source; no PyPI publication is implied.

The dev watcher uses native filesystem events and hashes watched files, falling back to polling if needed. JS/TS island edits rebuild and replace affected modules without reloading the document; cleanup runs first and that island’s state may reset. Other source changes restart the server and reload open pages. Broken edits retain the last working version. With `--port 0`, the assigned port is reused on restart.

## Project structure and routing

~~~text
app/
  layout.py
  page.py
  blog/[slug]/page.py
  docs/[...path]/page.py
  api/users/route.py
public/
  styles/site.css
islands/
  counter.ts
~~~

A `page.py` exports a decorated `Page`; a `route.py` exports decorated HTTP handlers. `layout.py` wraps descendant pages. Static segments take precedence over `[slug]`, which takes precedence over `[...path]`, which takes precedence over `[[...path]]`. A single parameter is a string; catch-all parameters are lists of strings. An optional catch-all `[[...path]]` matches zero or more segments, so `/docs/[[...path]]` also serves `/docs` with an empty list. A required and an optional catch-all cannot share the same level; the build fails on that conflict. Parallel/intercepting routes are not implemented.

Parenthesised directories are route groups: `app/(marketing)/page.py` serves `/`, and layouts inside a group still apply. Group names may only contain letters, digits, `_` and `-`; two groups that map to the same URL fail the build.

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)` builds an HTML tree; strings are escaped. It is buffered SSR by default; a page may also stream UI chunks (see Streaming SSR below). It is not an arbitrary raw-HTML API.

## Application imports and migration

Page, layout and API modules may use package-relative imports, such as `from .helpers import title`. Each app receives its own module namespace; the framework no longer prepends the project root to process-wide `sys.path`. If a root-based absolute import stops working after upgrading from v0.5, change it to a relative import or install your project as a Python package. Third-party packages must be installed in the environment.

## API requests and errors

~~~python
# app/api/echo/route.py
from pynext import BadRequest, JSONResponse, route

@route("POST")
async def echo(request):
    data = await request.json()
    if not isinstance(data, dict) or "name" not in data:
        raise BadRequest("name is required")
    return JSONResponse({"name": data["name"]}, status=201)
~~~

`request.query` returns a mapping to lists of values, for example `request.query.get("q", [""])[0]`. Reading it rejects invalid UTF-8, including malformed percent-decoded UTF-8, and more than 1,000 query fields. `await request.json()` rejects invalid JSON and non-finite JSON constants. These operations raise `BadRequest`, which the ASGI and Axum adapters return as HTTP 400 with a generic body. Parsing is lazy: a handler that never reads the value does not trigger validation. Other unhandled errors become HTTP 500.

Responses default to the Content Security Policy `script-src 'self'; object-src 'none'`. This limits which scripts a browser may load; account for it when adding client code. Response headers and media types reject control characters.

State-changing API requests are CSRF-protected (spec v0.2 §11). Installing a session middleware protects every state-changing API route automatically, with the token bound to the server-side session. Without a session middleware CSRF is off by default and uses the double-submit pattern: the app issues an unpredictable token, stores it in a `pynext_csrf` cookie, and the client sends the same value back in the `X-CSRF-Token` header (or a `csrf_token` form field). The cookie value is HMAC-signed, so an unsigned cookie is rejected even when the submitted token matches (this stops subdomain cookie injection). `Origin` and `Sec-Fetch-Site` are checked too: requests from cross-site or subdomain pages get HTTP 403 even with a valid token. GET, HEAD and OPTIONS are exempt; other methods without a matching token get HTTP 403.

~~~python
from pynext import JSONResponse, csrf_protect, new_csrf_token, route, set_csrf_cookie

@route("GET")
async def get_token(request):
    token = new_csrf_token()
    return set_csrf_cookie(JSONResponse({"token": token}), token, config=request.security)

@route("POST")
@csrf_protect
async def update(request):
    return JSONResponse({"ok": True})
~~~

With SessionMiddleware, CSRF turns on automatically — no security config needed. Without sessions, pass `security=SecurityConfig(csrf_enabled=True)` to `create_app` to protect every state-changing API route instead of marking handlers one by one. The double-submit token only proves the request came from a page that could read the cookie. A page that can read the cookie through XSS can bypass it, as with any double-submit scheme. The cookie defaults to `SameSite=Lax` without `HttpOnly` (JavaScript must read it) and is `Secure` by default; for plain-`http://` local development pass `csrf_cookie_secure=False` to both the app config and `set_csrf_cookie()` (`pynext dev` does this automatically). Multi-worker deployments share one signing key: when started with `--workers N`, the supervisor generates one 32-byte key — or adopts `--csrf-signing-key` / `PYNEXT_CSRF_SIGNING_KEY` when given — and shares it with every worker, including replacements started after a crash or a rolling restart, so a cookie issued by one worker verifies on the others. A key may be a string of at least 16 bytes, `hex:<hex>` or `base64:<base64>`; an empty, too-short or undecodable key fails at startup instead of silently falling back to a random key. A single-process server keeps its own per-process fallback key, which is fine because every config lives in that one process.

## Sessions

Server-side sessions live in `request.state["session"]` when a `SessionMiddleware` is installed (`SQLiteSessionStore` for a single file shared by workers, `RedisSessionStore` for Redis). The session cookie holds only an opaque token and is `HttpOnly`, `Secure` by default, and `SameSite=Lax`. Session data must be JSON-serializable: assigning a value that is not (for example an arbitrary object or a set) raises `TypeError` naming the key at assignment time, instead of failing later when the session is saved. Stored data is capped at 16 KiB. A request that presents the session cookie name more than once is treated as having no session at all (fail-closed).

Rotate the session id on login — this is required, not optional. After verifying credentials and storing the authenticated identity, call `session.rotate_on_login()` before returning the login response:

~~~python
from pynext import JSONResponse, route

@route("POST")
def login(request):
    user = authenticate(request)  # verify credentials; raise/return 401 on failure
    session = request.state["session"]
    session["user_id"] = user.id
    session.rotate_on_login()  # fresh session id + fresh CSRF token; old id stops working
    return JSONResponse({"ok": True})
~~~

Skipping rotation leaves the session open to fixation: a session id fixed by an attacker before login would stay valid after it. Rotate again whenever privilege changes (for example after a password change). Call `session.invalidate()` on logout to delete the server-side session and clear the cookie.

A `RedisSessionStore` follows a failure policy when Redis is unreachable (see `failure_policy`): the default is `fail_closed`, which raises `SessionUnavailable` and the request is answered 503 instead of silently losing session state. Pass `failure_policy="fail_open"` to start a fresh ephemeral session instead — reads keep working, but nothing is persisted, and a login cannot complete during the outage: the CSRF token issued for it is never stored, so the login request fails CSRF (403). A rotation during the outage may also leave the old session id usable until it expires. Only use `fail_open` where availability matters more than session continuity.

Cross-origin requests are denied by default (spec v0.2 §11): no `Access-Control-Allow-Origin` header is sent unless the origin is configured. Pass the policy to `create_app` with `SecurityConfig`:

~~~python
from pynext import SecurityConfig, create_app

app = create_app(".", security=SecurityConfig(
    cors_allowed_origins=("https://example.com",),
    cors_allowed_methods=("GET", "POST"),
    cors_allowed_headers=("Content-Type", "X-CSRF-Token"),
    cors_allow_credentials=False,
    cors_max_age=600,
))
~~~

A preflight (`OPTIONS` with `Origin` and `Access-Control-Request-Method`) from an allowed origin gets 204 with `Allow` and the `Access-Control-Allow-*` headers; a disallowed origin, method or header gets 403. Actual cross-origin requests from allowed origins get `Access-Control-Allow-Origin` (and `Access-Control-Allow-Credentials: true` when configured); other origins get no CORS headers, so the browser refuses to expose the response. Every response to a request carrying an `Origin` also gets `Vary: Origin`. Same-origin requests are never blocked by CORS, so a same-origin page can call the app's own API with custom headers even when no origin is configured. `cors_allowed_origins=("*",)` allows any origin; with `cors_allow_credentials=True` the exact origin is echoed instead of `*`.

Static files, SSG pages and cached ISR responses are served directly by the native server and do not get CORS headers; CORS applies to responses produced by Python dispatch (API routes and dynamically rendered pages).

The default CSP is `script-src 'self'; object-src 'none'`. With `SecurityConfig(csp_nonce_enabled=True)`, each dynamically rendered page gets a fresh per-request nonce: the header becomes `script-src 'self' 'nonce-<value>'; object-src 'none'`, and the same nonce is added to the island runtime `<script>` tag. The nonce differs on every response, so do not cache a nonce-bearing page yourself. Pre-rendered SSG pages, cached ISR responses and static files keep the static policy without a nonce: a nonce baked into shared HTML would be reused across responses, which defeats its purpose. ISR pages requested with a Cookie or Authorization header are rendered fresh per request, so they do get a nonce.

## Streaming API responses

Return `StreamingResponse` from an API handler with an asynchronous iterable of strings or bytes. ASGI and Axum send chunks incrementally and close the iterator on completion, disconnect, or error. Each chunk is limited to 1 MiB. Do not set `Content-Length` or `Transfer-Encoding`; the server selects protocol framing. Axum allows one queued chunk and stops a stalled chunk or backpressure wait after 30 seconds. Once response headers are sent, an error ends the stream rather than changing its status. HEAD does not iterate the stream. This is not React/RSC streaming.

## Streaming SSR

A page function may return an asynchronous iterable of UI nodes. SSR sends the layout shell and each serialized chunk as they are produced. SSG consumes the chunks during the build and serves the resulting static file. For a public ISR miss, the first request streams and concurrent requests wait for the completed result; stale content is served immediately. Only a complete result is cached. SSG pages are limited to 8 MiB, and SSG and ISR streams are additionally limited to 30 seconds; the ISR cache stores bodies up to 2 MiB. An ISR generation can continue after its initiating client disconnects. Layouts must render children exactly once; errors after headers terminate the stream. See the [example page](../../examples/basic/app/stream/page.py).

~~~python
from pynext import h, page

@page()
async def Page():
    yield h("h1", {}, "Streaming SSR")
    yield h("p", {}, "Each chunk is sent as it is produced.")
~~~

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield "first\n"
        yield "second\n"
    return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")
~~~

## WebSocket

A `route.py` may export one `@websocket` handler alongside its HTTP handlers. The handler receives a `WebSocket` and must call `await websocket.accept()` before sending or receiving. Text and binary messages round-trip; the peer closing the connection raises `WebSocketDisconnect` in `receive()`, so per-connection cleanup belongs in a `finally` block.

In the development version, the handshake accepts only a same-origin `Origin` by default, before ASGI or Axum accepts the connection. Configure additional exact HTTP(S) origins with `SecurityConfig(websocket_allowed_origins=("https://client.example",))` passed to `create_app(..., security=...)`, or repeat `--ws-allowed-origin https://client.example` on the CLI. Include the scheme and any non-default port; wildcard and `null` are rejected. Existing non-browser clients that omit `Origin` must explicitly opt in using `websocket_allow_missing_origin=True` or `--ws-allow-missing-origin`. The published 0.1.0 package does not have this check. Origin is not authentication: clients can forge or omit it. Before the handshake, the upgrade also runs the global middleware chain (including SessionMiddleware) and the handler's `@require_auth`/`@require_permission` policy on both ASGI and Axum; a denied upgrade is rejected before the 101 response (HTTP 403 on Axum, close 1008 on ASGI), so the handler never runs. The app still has to establish identity — for example, a login route that sets `session["user_id"]` — and any credential scheme beyond the session (such as a token) must be checked in middleware. When behind a proxy, configure only actual trusted peers with `--trusted-proxy` and make the proxy overwrite client-supplied forwarded headers.

For PyNext to identify a trusted proxy on ASGI, the server must preserve the original peer in `scope["client"]`. Disable upstream proxy-header rewriting (for Uvicorn, use `--no-proxy-headers`) when PyNext is responsible for interpreting forwarded headers.

~~~python
# app/ws/echo/route.py
from pynext import WebSocket, WebSocketDisconnect, websocket

@websocket
async def echo(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            kind, data = await websocket.receive()
            if kind == "text":
                await websocket.send_text(data)
            else:
                await websocket.send_bytes(data)
    except WebSocketDisconnect:
        pass
~~~

See the [echo example](../../examples/basic/app/ws/echo/route.py). Both ASGI and Axum support text and binary messages. Messages default to 64 KiB; set `create_app(..., websocket_max_message=...)` or `--ws-max-message-bytes` (up to 16 MiB). Oversized incoming messages close with code 1009. `@websocket(protocols=("chat.v1", "chat.v2"))` chooses the first server-preferred protocol offered by the client; inspect `websocket.subprotocol`. `WebSocketPubSub` provides bounded, process-local fan-out with `async with broker.subscribe(topic) as sub`, `await sub.receive()` and `await broker.publish(topic, message)`. Slow subscribers raise `PubSubOverflow`; messages are not shared across workers or persisted. Axum completes the handshake before the handler, so `accept()` there marks it accepted.

## SSR, SSG and ISR

- `@page()` renders on each request. It may return an async iterable of UI nodes to stream the page instead of buffering it; see Streaming SSR.
- `@page(render="ssg")` renders during build. Dynamic SSG routes must export `generate_static_params()` returning a list of dictionaries. Ungenerated paths return 404 in production.
- `@page(render="isr", revalidate=30, stale=120)` generates a public page on demand, reuses it while fresh, and serves stale content while one regeneration runs. Times are seconds.

~~~python
# app/guides/[slug]/page.py
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

SSG and ISR pages cannot accept `request` and must contain public content. ISR ignores query strings by default. To vary a page by selected keys, declare `@page(render="isr", revalidate=30, query_keys=("lang",))` and accept a `query` parameter; only listed keys and their string lists are passed to the page and included in the cache key. Revalidating a path invalidates all its query variants. Cookie or Authorization requests bypass the shared cache and return `private, no-store`. The default cache is process-local; SQLite and optional Redis (`pynext-framework[redis]`, `PYNEXT_REDIS_URL`) share entries across workers. Shared lookups consult the backend, leases and fencing protect writes, and a backend failure follows the configured failure policy: by default (`fail_open`, `--cache-failure-policy fail_open`) it falls back to a possibly stale local cache, while `fail_closed` answers 503 instead. `Set-Cookie` and private/no-store responses are not cached. SSG async generators are consumed during the build and served as static files. Rebuild SSG pages to update them.

A build produces at most 10,000 SSG pages. Development mode renders SSG pages for each request. ISR pages cannot export `generate_static_params()` and permit at most 128 concurrent generation tasks per process.

An authorized API handler can call `await request.revalidate("/news/example")`; code holding an app instance can call `await app.revalidate(path)`. Pass a decoded ISR path without a query string. The result is `True` if a local cached entry or in-flight generation was invalidated. With a shared backend the shared entry is removed too while the store is reachable; in `fail_open` mode a `True` result does not guarantee the shared entry is gone when the shared store is unreachable (shared removal is best-effort there), so other workers may keep serving the stale entry until it expires — use `fail_closed` if an unapplied invalidation must raise instead of reporting success. Existing requests may still receive an older in-flight result, but that result cannot repopulate the cache. With the memory backend, invalidation is process-local and is not sent to other workers; with the SQLite backend it removes the shared entry and other workers observe that on their next lookup while the store is available. Authenticate and authorize any endpoint that exposes it.

## Public files and islands

Public files, SSG pages, and island bundles have a strong SHA-256-based ETag. GET/HEAD support `If-Match` and `If-None-Match`; GET supports one byte `Range` and strong-ETag `If-Range`, returning 206 or 416 when applicable. Multiple or malformed ranges are ignored and return the full representation. Date-based validators and ranges for ISR responses are not implemented.

Files under `public/` are public at the corresponding URL, e.g. `public/styles/site.css` is `/styles/site.css`. The build snapshots and checks these files; source files are still needed at production startup. Hidden files, symlinks and conflicting paths are rejected. Changing a public file in development mode restarts the server automatically.

An island pairs a Python declaration with a JavaScript or TypeScript module in `islands/`:

~~~python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island(
        "counter", props={"initial": 1},
        fallback=h("p", {}, "Enable JavaScript to use the counter."),
    ))
~~~

The matching `islands/counter.ts` exports `mount(element, props)` and may return a cleanup function. Props are public JSON objects limited to 16 KiB; never put secrets in them. The build uses esbuild; nested islands, npm-package imports, external imports and CSS imports are not supported. `/_pynext` is reserved.

Use `hydrate=True` with server-rendered fallback and a module exporting `render`, `hydrate` and `mount` to reuse matching DOM. See [hydrated-counter.ts](../../examples/basic/islands/hydrated-counter.ts). If the trees differ, the island is mounted again and input state or focus can be lost.

A shared model can also supply both server and browser output: `island("shared-model", props={"name": "Ada"}, hydrate=True, model=h("button", {"type": "button"}, "Hello ", model_prop("name")))`. Import `model_prop` from `pynext`. The versioned model supports element, text and fragment nodes, string-valued prop placeholders, and static string/boolean/None attributes; it is limited to 16 KiB, 512 nodes and depth 32. The JS module exports `hydrate`, `mount` and `render`; it attaches events and provides mismatch recovery. Dynamic attributes, loops and client state calculations are outside this model. See the [server example](../../examples/basic/app/shared-model/page.py) and [client module](../../examples/basic/islands/shared-model.js).

The default activation strategy is `load`. `visible` activates when the island enters the viewport; `idle` requests an idle callback with a 2-second timeout. Unsupported observer/idle APIs fall back to idle/timer scheduling. An idle callback can run later than requested. These three strategies do not replay earlier events.

With `hydrate=True, strategy="interaction"`, a user click on a `<button type="button">` activates the island. See the [example page](../../examples/basic/app/interaction/page.py) and [module](../../examples/basic/islands/interaction-counter.js). After the loader registers the island's listener, up to 16 supported clicks are queued and replayed in order when hydration succeeds; extra clicks are dropped with a warning. Links, form submissions, input, labels and editable regions are not intercepted or replayed. Events before listener registration are not captured. Pending clicks are discarded on a DOM mismatch, removal or activation failure. Replayed events are synthetic with `isTrusted=false` and do not guarantee operations requiring browser user activation; capture listeners outside the island may observe both the original and replayed events.

## Client-side navigation

All pages load the client runtime. Same-origin links use partial navigation when document metadata and CSP are compatible; otherwise they fall back to a full load. Hover, focus and viewport prefetch use at most four concurrent requests and a 1 MiB response limit. Only explicitly public fresh responses without `Vary: Cookie` or `Authorization` are retained, for at most 60 seconds and 20 entries. Cross-origin, download and same-page hash links retain normal browser behavior. `window.__pynextNav` exposes `navigate`, `prefetch` and `isPrefetched` for debugging.

## Python client DSL (experimental)

`pynext.client_dsl.compile_client_dsl(source)` compiles a small, fixed subset of Python to JavaScript (spec §7.3). Supported: literals (int within the JavaScript safe-integer range, float, str, bool, None, list, tuple, dict), arithmetic (`+ - * / // % **`), comparisons, `and`/`or`/`not`, `if`/`else`, `for` over `range()` or a list, `while`, function definitions, `len`, `str`, `int`, `float`, `bool`, `abs`, `min`, `max`, `list`, `print`, `.append()`, indexing, slices and one-clause list comprehensions. Everything else raises `ClientDSLCompileError`: imports, reflection, file I/O, classes, lambdas, `try`, f-strings and server-only names such as `open`, `os`, `Request` or `h` are rejected at compile time so server code cannot leak into a browser bundle. See `pynext.client_dsl.SUPPORTED` and `KNOWN_DIFFERENCES` for the exact list and the known semantic differences (for example `str(1.0)` drops the trailing `.0`). See the [example](../../examples/basic/client_dsl_example.py). The DSL is a standalone compiler: it does not bundle code into islands automatically.

## ASGI, timeouts and safety

To use the included development ASGI app with Uvicorn:

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

For a built app, create the ASGI app with `create_app(project_path, production=True)`. ASGI server timeout settings are managed by that server. The Axum adapter waits up to 30 seconds for Python and requests cancellation before returning 504. A synchronous function already running in a thread can continue after the 504, including writes. Use idempotent writes and explicit database/upstream timeouts; process isolation is available with `--workers` (below) — a single process still provides none. ISR cancellation has the same limitation.

Distributed rate limiting is implemented with `--rate-limit-backend redis` (shared Redis token buckets). Production hardening remains a roadmap item. SQLite invalidation is observed on the next lookup while the shared store is available. OpenTelemetry spans are emitted when the optional packages are installed and tracing is enabled (see Observability). ASGI conformance is verified by `tests/test_asgi_conformance.py` (HTTP event order, chunked bodies, disconnects, lifespan including failure paths). See [production roadmap](../PRODUCTION_ROADMAP.md) for proposed work and its acceptance criteria.

Both adapters limit request bodies to 1 MiB and return 413 when exceeded. By default Axum permits 16 simultaneous body reads with a 10-second body deadline (408), 64 Python handlers, and at most the smaller of the handler limit and 64 waiting requests; full queues return 503. The execution and body settings can be changed with the CLI. HTTP/1.1 header reads have a 10-second deadline. Axum defaults to 1,024 total connections and 128 per IP per worker, configurable with `--max-connections` and `--max-connections-per-ip`. External ASGI servers manage their own timeouts. Non-root paths ending in a slash redirect with 308. Each compiled island is limited to 2 MiB, with 32 MiB total.

## Observability

Structured logs are on by default: the `pynext` logger emits one JSON object per line with `timestamp`, `level`, `message`, `request_id`, `trace_id` (when a `traceparent` header was received), `route`, `status` and `latency_ms`. Each request gets a request id: an incoming `X-Request-Id` header is echoed when it is a valid token, otherwise a fresh id is generated, propagated through the request, and returned as the `x-request-id` response header. Never put headers, cookies, bodies or `Authorization` values in log messages: the JSON formatter drops extra fields named like credentials, and the access log records only the route pattern (for example `/blog/[slug]`), never the raw URL.

Metrics are collected in memory and are on by default. `app.metrics.snapshot()` returns request counts and status codes by route pattern, latency p50/p95/p99 by route, Python execution queue depth and wait times, Python execution time versus Rust render time, ISR cache hit/miss/stale and regeneration success/failure/started, worker starts/timeouts/unavailability, response bytes and island bundle sizes. `app.metrics.prometheus_text()` exposes the same counters in Prometheus text format. Latency samples are bounded (2048 per route). Native fresh ISR hits are served by Rust without entering Python, so they are not counted here; only ISR outcomes that reach Python dispatch are counted.

OpenTelemetry tracing is off by default and optional: install `opentelemetry-api` (plus an SDK/exporter of your choice) and enable it with `--tracing` or `PYNEXT_TRACING=1`. When enabled, `pynext.request` and `pynext.render` spans are created and incoming W3C `traceparent`/`tracestate` context is propagated. Without the packages installed, tracing is a no-op and nothing breaks.

Configure with the CLI flags `--structured-logs` / `--no-structured-logs`, `--metrics` / `--no-metrics`, `--tracing` / `--no-tracing`, the environment variables `PYNEXT_STRUCTURED_LOGS`, `PYNEXT_METRICS`, `PYNEXT_TRACING`, `PYNEXT_LOG_LEVEL`, or in code with `ObservabilityConfig` passed to `create_app(..., observability=...)`.

Measured overhead of the default observability settings (structured logs + metrics on, tracing off), `benchmarks/bench.py --mode axum --requests 1000 --concurrency 10 --path / --production` on the built example: two runs each, on a shared VM where other work was building and testing in parallel, so absolute latencies are inflated and run-to-run spread is large. ON: 25.4 / 14.2 req/s, p50 309 / 586 ms, process CPU 4.79 / 5.02 s. OFF (`PYNEXT_STRUCTURED_LOGS=0 PYNEXT_METRICS=0`): 18.7 / 18.5 req/s, p50 437 / 478 ms, CPU 5.01 / 4.69 s. Means: ON 19.8 req/s and p50 447 ms vs OFF 18.6 req/s and p50 457 ms — no overhead outside run-to-run noise (the ON runs also wrote ~1000 JSON log lines to stderr). Re-measure on a quiet machine for a tighter bound.

## Rate limiting

Rate limiting is opt-in and per process: `pynext start --rate-limit 100 --rate-limit-window 60` allows 100 requests per client IP per 60 seconds (token bucket; the burst is the limit). Requests over the limit get 429 with a `Retry-After` header in seconds. WebSocket handshakes are rate-limited the same way: each handshake consumes one token from the same per-IP bucket, and an over-limit handshake gets 429 with `Retry-After`. Keys are client IPs; `X-Forwarded-For` is honoured only from peers listed with `--trusted-proxy` (repeatable, IP or CIDR) — an untrusted proxy's header is ignored because it is spoofable. Programmatic use: `create_app(path, rate_limit=RateLimiter(100, 60, trusted_proxies=["10.0.0.0/8"]))`. Buckets are per process and do not survive restarts. With `--rate-limit-backend redis` buckets are shared; if Redis is unreachable the limiter follows its failure policy: by default (`fail_closed`, `--rate-limit-failure-policy fail_closed`) the request gets 503, while `fail_open` admits it. Configured limits also cover static files, SSG pages and fresh ISR pages on both adapters.

The development version checks HTTP admission from headers before receiving the body on both adapters, including requests using `Expect: 100-continue`. One token is charged at admission even if the upload is later abandoned, too large, too slow, or unable to acquire execution capacity. Axum runs at most 64 HTTP/WebSocket limiter checks concurrently with a 2-second deadline; saturation, a timeout or an unexpected error returns 503 with `Retry-After: 1`. This budget also applies with `fail_open`, which only controls a backend failure that returns within the deadline. A denied HTTP/1.1 upload with an unread body closes its connection; HTTP/2 leaves other streams available. Health probes bypass the limiter, and configured exempt paths are checked after strict path decoding. This change is not in the published 0.1.0 package.

The forwarded chain is walked from right to left through trusted proxies; the nearest untrusted address is used. Configure every intermediate proxy accurately. Each process keeps at most 10,000 buckets; when all are active, a new key evicts the stalest bucket instead of getting 429, and new-key admissions are bounded by a global sliding window (at most 10,000 new keys per window). Bucket scores are integer microseconds from Redis `TIME`; two scripts can read the same microsecond, in which case the tied buckets are equally stale and eviction picks deterministically by bucket key — the evicted owner is never denied, it gets a fresh bucket on its next request. Scores stay exact integers well beyond the maximum one-year window (the unit in the last place is 0.25 µs at the current epoch), so realistic windows of minutes to hours are unaffected by double-precision limits. IPv6 addresses share the bucket of their /64 prefix. That grouping is a tradeoff: a holder of a whole /48 can mint 65,536 distinct /64s and fill the table and the new-key window, which still denies genuinely brand-new keys until the window slides (evicted existing keys are never denied). `--rate-limit-ipv6-prefix-len 48` groups a whole /48 into one bucket, which stops that attack, but every host sharing the prefix then shares one bucket, so a busy /48 throttles its innocent users together; the default stays 64. Paths listed with `--rate-limit-exempt-path` (for example `/healthz`) skip rate limiting.

Multiple workers: `pynext start --workers 4` runs a supervisor whose worker processes share one listener. With `--port 0`, the assigned port remains stable across worker restarts. Workers have independent interpreters and event loops; memory caches and rate limits are per worker, while SQLite/Redis ISR and Redis rate limiting can be shared. The supervisor restarts failed workers and coordinates shutdown. An interrupted in-flight request fails and must be retried. CI tests shared-listener worker startup and restart on Linux, macOS, and Windows; signal-based supervisor tests run on Linux and macOS.

## Tests and troubleshooting

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

ASGI conformance is covered by `tests/test_asgi_conformance.py`: HTTP event
order, multi-chunk bodies and the 1 MiB size boundary, disconnects during
body reads and slow handlers, lifespan startup/shutdown including
`lifespan.startup.failed` / `lifespan.shutdown.failed` on failure, and
streaming response frame order. Sending the `.failed` messages on lifespan
failure is the behavior fixed alongside the suite.

The router and HTML serializer also have property-based tests beyond
hand-picked examples: hypothesis on the Python side
(`tests/test_property_router_render.py`, fixed seed, 40 examples to keep CI
fast) and proptest on the Rust side (unit tests in `crates/pynext-router` and
`crates/pynext-native`, 64 cases each), covering route-matching round-trips,
escaping with no leaks, void-tag handling, and rendering of arbitrary trees.
`embed` is on the blocked-tag list, so it cannot render and is excluded from
those tests.

After building the example, run the Chromium check with:

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

Run `uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4` for a small closed-loop smoke benchmark. It reports attempted and successful 2xx requests per second separately, error and status counts, p50/p95/p99 for all attempts and successes, response sizes, CPU and peak RSS. In the default mode the CPU and lifetime peak RSS cover both the server and the load generator; they are not server-only measurements. For a separately running local server, use `--base-url http://127.0.0.1:3000`. On Linux, add `--server-pid <PID>` to sample that process and its threads, excluding child workers. CI saves a bounded benchmark result measured in production mode (`--production`, using the built example) for each OS x deploy-mode (Axum/Uvicorn) matrix leg but does not yet enforce performance or memory regression thresholds. `--compare` runs the axum, uvicorn, and starlette loopback-HTTP groups with identical settings; a comparison recorded on 2026-11-08 (`/api/users`, 1000 requests, concurrency 10) is in `benchmarks/results-2026-11-08.json`. See `docs/BENCHMARKING.md` for the modes and caveats.

- If startup says sources changed, rerun `pynext build`.
- If esbuild is missing, run `npm ci` at the repository root or set `PYNEXT_ESBUILD` to an executable.
- On Windows, npm's esbuild wrapper runs through Node. If setting `PYNEXT_ESBUILD` directly, use the native `esbuild.exe`, not a `.cmd` or `.bat` file.
- If an SSG dynamic path returns 404, add it to `generate_static_params()` and rebuild.
- If hydration reports a mismatch, make server fallback and client `render` output deterministic and equivalent.
- If an ISR page looks different across workers, check the cache backend: the memory backend is per-process, and SQLite falls back to process-local copies if the shared store is unavailable.
