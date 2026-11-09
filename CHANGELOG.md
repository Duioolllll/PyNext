# Changelog

All versions below are experimental. The v0.1.0–v0.3.0 snapshots were reconstructed from preserved development source distributions when those releases were prepared; they are not claims that the tags were published at those earlier development times. The later malformed-query/JSON fix and timeout documentation/test were backported to the reconstructed snapshots.

## Unreleased

- Require a same-origin WebSocket `Origin` by default on ASGI and Axum before accepting the handshake. This changes behavior for non-browser clients that omit `Origin`: opt in with `SecurityConfig(websocket_allow_missing_origin=True)` or `--ws-allow-missing-origin`. Additional exact HTTP(S) origins can be configured with `websocket_allowed_origins` or repeatable `--ws-allowed-origin`; wildcard, `null`, malformed and duplicate origins are rejected. Forwarded host and scheme are honored only for configured trusted proxies. Origin checks do not replace application authentication.
- Add an optional, bounded Django ORM/form resource with lifecycle-managed worker cleanup; Django HTTP apps and templates remain out of scope.
- Serialize overlapping SQLite memory-database sessions and application/database lifecycle transitions, and reopen shared ISR stores after startup failure or shutdown.
- Add an optional SQLAlchemy async `Database` resource for SQLite and PostgreSQL, with lifespan-managed connection pools and explicit session and transaction contexts. Include PostgreSQL integration CI and an Alembic guide.
- Generate health endpoints and server-limit controls in the capability table, and align TLS roadmap wording with ADR-0001.
- Add a Redis session store (`RedisSessionStore`, `pynext-framework[redis]` or `PYNEXT_REDIS_URL`) sharing login, rotation and logout across nodes with TTL expiry and version-fenced writes; the session store interface (load/save/delete) is unchanged so external stores keep working.
- Run expired-session cleanup automatically: `SessionMiddleware` now purges expired sessions in a background task every hour by default (`purge_interval`, `None` disables it, `close()` stops it).
- Add `fail_open` / `fail_closed` failure policies for shared-backend outages (roadmap item B4): the ISR cache defaults to `fail_open` (local fallback, as before) and the Redis rate limiter defaults to `fail_closed` (503, as before); each can be flipped via `ISRCache(..., failure_policy=...)`, `RedisRateLimiter(..., failure_policy=...)`, `create_app(..., cache_failure_policy=...)`, or `--cache-failure-policy` / `--rate-limit-failure-policy`. Under `fail_closed`, an ISR page whose shared store cannot be consulted answers 503, and an invalidation that never reached the shared store raises instead of reporting success. Best-effort cache writes stay best-effort in both policies.
- Add `/healthz` (liveness) and `/readyz` (readiness: 200 only after startup and while not draining) health probes on both the Axum and ASGI servers. They bypass middleware, CORS/CSRF and rate limiting, and a user route or public asset at the same path never shadows them.
- Graceful shutdown (B6): SIGTERM/SIGINT to a single-process server stops advertising readiness, closes live WebSockets with a clean 1001 close, waits out in-flight HTTP requests and streaming bodies for up to `--shutdown-timeout` (default 30s, also bounding the native connection drain), then shuts resources down. The supervisor waits the same budget before SIGKILLing a worker, and SIGHUP performs a rolling restart that replaces workers one at a time — a replacement must report ready before the old worker drains — so a rolling restart under load serves no 5xx.
- Add a reproducible load baseline (`benchmarks/baseline.py`, roadmap C1): fixed scenarios (static, SSG, ISR hit, SSR, JSON API, SQLite DB query, streaming) with repeated measurements, p50/p95/p99/p99.9 latency, error rate, server CPU and sampled peak RSS, and recorded load-generator/server environments for separate-machine runs. The DB-query scenario uses a benchmark-only SQLite fixture app. `benchmarks/baseline-2026-11-08.json` is a same-machine smoke run, not a production claim.
- Report p99.9 latency in `benchmarks/bench.py` percentiles.
- Redesign the overload policy (roadmap C7): make the execution wait queue configurable with `--max-waiting` / `max_waiting` (default `min(--max-concurrent, 64)`; 0 waits for nobody), keep the default policy of bounded wait then 503, and return an accurate `Retry-After` — `ceil(acquire_timeout_ms / 1000)` seconds (at least 1) after a full wait, `1` for a full wait queue. Defaults are unchanged: the old hard-coded `min(max_concurrent, 64)` queue and `Retry-After: 1` at the default 1000 ms timeout behave exactly as before.

- Add a dedicated `Wheels` workflow (D1) that prebuilds binary wheels for
  Linux manylinux x86_64/aarch64, macOS universal2 and Windows x64 for
  Python 3.13/3.14, verifies each wheel in an isolated virtualenv with no
  Rust toolchain, and uploads the wheels as artifacts. See
  `docs/WHEEL_RELEASE.md`. CI still does not upload anything to PyPI;
  publishing stays a manual maintainer step.

The current release is **0.1.0** (top section). The `v0.7.0a1` section below is a release plan superseded by the 0.1.0 decision; its items were merged to `main` and are included in 0.1.0. The `v0.1.0` section at the bottom is the early reconstructed snapshot, not the current release.

## 0.1.0

- Add the A1 default security headers to every response (SSR, SSG, ISR, API, static, client assets, preflight, and error pages) on both the ASGI adapter and the native server, including the Rust fast path for static and fresh ISR responses: HSTS in production only (`max-age=31536000; includeSubDomains`, configurable, omitted in development so `pynext dev` on `http://127.0.0.1` is unaffected), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, and an extended CSP (`script-src 'self'` plus the per-request nonce when enabled, `style-src 'self'`, `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`, `frame-ancestors 'none'`). All of them are configurable on `SecurityConfig` (set a field to `None`/`False` to disable that header); an explicitly set response header still wins, and the per-request CSP nonce is preserved. See `security_headers()` and `apply_security_headers()`.
- Bound native-server connections (A4): a total TCP connection cap (default 1024) and a per-IP cap (default 128) enforced at accept time — excess connections are closed before any request is read, and a freed slot admits a new client. Slow request headers and idle keep-alive connections close after the header-read timeout (default 10s, configurable), and request bodies arriving slower than 1024 bytes/second on average after a 2-second grace window are rejected with 408 + `Retry-After: 1`. The limits are settable via `serve()`/`start_native()` and `--max-connections`, `--max-connections-per-ip`, `--header-read-timeout-ms`, `--body-min-rate-bytes-per-sec`; they apply to the native (Axum) server only and are enforced per worker process. h2c connections count toward the caps but have no idle timeout.
- Add Redis ISR storage with expiring leases, fenced writes and cross-worker invalidation; add Redis-backed shared rate limiting, including static and cached responses. Redis cache failures fall back locally; rate-limiter failures return 503.
- Add an ISR query-key allowlist and generation-based invalidation across all query variants and shared-cache workers.
- Stream the first ISR generation while caching only complete responses; allow asynchronous SSG chunks to be consumed during the build.
- Add WebSocket subprotocol negotiation, configurable message limits, bounded native queues and bounded process-local pub/sub.
- Share one listener across worker processes, including dynamically assigned ports retained across restarts; the Windows runtime path still requires CI validation.
- Extend the Python client DSL with short-circuiting chained comparisons and improve collection equality, membership and Unicode string handling.
- Add bounded shared island models for server rendering and browser hydration using the same node tree and string props.
- Replace updated JS/TS island modules in the development browser without a full page reload; reload documents after other source changes.
- Gracefully stop idle HTTP keep-alive connections during native server shutdown while allowing active requests to finish.
- Add a relocatable Linux standalone deployment directory containing the interpreter, installed dependencies and built application; compatible system libraries are still required.
- Load client navigation on pages without islands, bound prefetch concurrency and response bytes, and cache only explicitly public fresh responses; unsafe metadata changes fall back to full navigation.
- Add optional bounded local JPEG/PNG/WebP resizing to WebP via `pynext-framework[images]`.
- Use native file events for development reloads with polling fallback, and reuse the initially assigned port after `--port 0` reloads.
- Extend the supported CPython range and CI matrix to Python 3.14.
- Run each benchmark comparison server in a separate process and report its sampled peak RSS.
- Add async global HTTP middleware on ASGI and Axum, with request state and coverage of static, SSG, ISR and preflight requests. Register project middleware in `middleware.py` or pass it to `create_app`.
- Add bounded URL-encoded and multipart form parsing, in-memory `UploadFile`, development error pages, and a non-overwriting `pynext init` command.
- Add application resource lifespan hooks, SQLite-backed server sessions, session-bound CSRF tokens, authentication and permission markers, and security CLI flags. Applications remain responsible for verifying credentials.
- Validate SQLite ISR cache hits against the shared store and namespace shared entries by build, so another worker's invalidation is observed on the next lookup while the store is available. Local fallback during an outage may be stale.
- Verify real OpenTelemetry SDK tracing and parentage on both servers, preserve handler exceptions under tracing, and improve supervisor tests on Linux containers without the procfs `children` file.
- Fix cross-platform watcher paths and same-timestamp edits, SQLite ISR generation races and failure fallback, and exercise the correct Axum/Uvicorn browser server in CI.
- Match dynamic WebSocket routes on the native server using the same route priority as ASGI.
- Bound rate-limit bucket storage and select forwarded client addresses from the nearest untrusted proxy hop.
- Turn CSRF on automatically when a session middleware is installed (`SecurityConfig(csrf_enabled=None)` is now the default; pass `csrf_enabled=False` to opt out, `True` to protect without sessions). Double-submit cookies are HMAC-signed (key from `csrf_signing_key` or `PYNEXT_CSRF_SIGNING_KEY`, otherwise a process-wide fallback key, so multi-worker deployments must share one key), and `Origin`/`Sec-Fetch-Site` are checked on state-changing requests. The CSRF cookie is `Secure` by default; `pynext dev` sends it without `Secure` over plain HTTP, `pynext start` keeps `Secure`.
- Harden rate limiting against key exhaustion: IPv6 addresses share the bucket of their /64 prefix, a full bucket table evicts the stalest bucket instead of denying new keys, new-key admissions are bounded by a global sliding window, and paths listed with `--rate-limit-exempt-path` (for example `/healthz`) skip rate limiting on both adapters. The same policy applies to the shared Redis limiter.
- Unify forwarded-header handling in `pynext.proxy`: `X-Forwarded-For`, `Forwarded` (RFC 7239), `X-Forwarded-Proto` and `X-Forwarded-Host` are interpreted only from trusted proxies (none trusted means all are ignored), the resolved client IP/scheme/host is exposed to handlers as `request.state["client_ip"/"client_scheme"/"client_host"]`, and the rate limiter delegates to the same module.
- Document the architecture, observed limitations, and verification criteria.

## v0.7.0a1 (superseded plan — shipped in 0.1.0)

The 0.7.0a1 release plan was replaced by the 0.1.0 decision; the items below were merged to `main` and are included in 0.1.0.

- Add a shared SQLite ISR cache backend (`--cache-backend sqlite`, `create_app(..., cache_backend="sqlite", cache_path=...)`, default file `<project>/.pynext/isr-cache.sqlite3`) so multiple workers share ISR entries with no external service. Stale regeneration is single-flight per key through an expiring owner-token lease, shared writes are fenced by entry version so a slow generation cannot overwrite a newer result, failures preserve the old entry, and first-generation requests wait for the worker holding the lease. Responses with `Set-Cookie` or `Cache-Control: private`/`no-store` are never stored in the shared cache. The memory backend stays the default, and a Redis adapter is documented as a future option.
- Add streaming SSR: a page function may return an async iterable of UI nodes; the layout shell is sent first, each chunk is serialized and sent as produced, and the layout suffix closes the document. Buffered rendering stays the default, streaming is SSR-only, a disconnect closes the generator, and a mid-stream error ends the stream without changing its status. See `examples/basic/app/stream/page.py`.
- Make `pynext dev` watch `app/`, `islands/`, `public/` and `pynext.config.py` with a stdlib-only polling watcher and restart the server on the same port when a file changes. A broken edit prints its error and keeps the previous version serving until the next change fixes it.
- Add structured JSON logging (one object per line: timestamp, level, request id, trace id, route pattern, status, latency), request-id propagation with `x-request-id` echo, and an in-memory metrics collector (request counts/status by route pattern, latency p50/p95/p99, Python queue depth/wait, Python vs Rust render time, ISR cache hit/miss/stale and regeneration outcomes, worker starts/timeouts, response and island bundle sizes) with a JSON snapshot and Prometheus text exposition. Sensitive header/cookie/body values are never logged, and metric labels use route patterns, never raw URLs.
- Add optional OpenTelemetry tracing hooks (`pynext.request` / `pynext.render` spans, W3C traceparent/tracestate propagation). OpenTelemetry is an optional dependency: without the packages installed, tracing is a no-op.
- Add observability configuration via CLI flags (`--structured-logs`, `--metrics`, `--tracing` with `--no-*` forms), `PYNEXT_STRUCTURED_LOGS` / `PYNEXT_METRICS` / `PYNEXT_TRACING` / `PYNEXT_LOG_LEVEL` environment variables, and `ObservabilityConfig` on `create_app`. Defaults: structured logs on, metrics on, tracing off.
- Add WebSocket support (finding #46, spec §1.2): `@websocket` handlers with text/binary echo, close codes, disconnect cleanup and a 64 KiB message limit on both the ASGI adapter and the Axum standalone server, plus an echo example and raw-socket Axum coverage.
- Add an experimental Python client DSL compiler (finding #47, spec §7.3): `pynext.client_dsl.compile_client_dsl` compiles a fixed Python subset to JavaScript with compile errors for imports, reflection, file I/O and server-only names, plus Python/JS output-equivalence tests run through node.
- Add property-based tests for the router and HTML serializer (finding #48, spec §13): hypothesis on the Python side (fixed seed, 40 examples) and proptest as a Rust dev-dependency (64 cases per crate), covering route-matching round-trips, escaping, void tags, and arbitrary-tree rendering.
- Add opt-in per-process token-bucket rate limiting (--rate-limit, --rate-limit-window, --trusted-proxy; 429 with Retry-After) for dynamic requests on both adapters, keyed by client IP with forwarded headers honoured only from trusted proxies.
- Add optional catch-all routes and route groups (finding #50, spec §1.2 and §5.1): `[[...path]]` matches zero or more segments at the lowest priority, `(group)` directories organise files without appearing in the URL, and combining a required and an optional catch-all at the same level (or two groups mapping to one URL) fails the build.
- Add client-side navigation and prefetch (finding #51, spec §1.2): the client runtime intercepts same-origin link clicks on island pages, swaps the page body, updates history with `pushState`, and prefetches links on hover, focus, and viewport entry (at most 20 pages, 1 MiB each); any failure falls back to a full document navigation, with a Playwright smoke test covering navigation, history, hover prefetch, and the 404 fallback.
- Add a multi-worker supervisor (--workers, Unix-only, SO_REUSEPORT): a parent process spawns workers sharing one port, restarts crashed or stale-heartbeat workers, and propagates graceful shutdown; each worker keeps its own rate-limit buckets, and the ISR cache is per-process with the memory backend but shared with `--cache-backend sqlite`.
- Add an ASGI conformance test suite (finding #53, spec §13): HTTP event order, multi-chunk bodies and the 1 MiB size boundary, disconnects, lifespan startup/shutdown including failure paths, and streaming frame order. Fix lifespan startup/shutdown failures to send `lifespan.startup.failed` / `lifespan.shutdown.failed` instead of raising without a message.
- Extend the CI compatibility matrix (finding #54, spec §13): OS (Ubuntu/macOS/Windows) x Python 3.13 (the only version the project claims) x deploy mode (standalone Axum / Uvicorn-backed ASGI), with per-leg benchmark smoke measurements and browser smoke on all three OSes.
- Add ASGI-mode and Starlette comparison groups to the benchmark (finding #55, spec §14.1): a same-feature Starlette reference app (`benchmarks/starlette_app.py`), a `--mode starlette` loopback-HTTP group, and `--compare` for identical-settings runs of axum/uvicorn/starlette with p50/p95/p99, CPU, and memory per group. Recorded one comparison on 2026-11-08 (`benchmarks/results-2026-11-08.json`): 1000 requests at concurrency 10 on `/api/users`, all groups 0 errors (axum 281.21, uvicorn 215.95, starlette 227.06 successful req/s).
- Add a Uvicorn loopback HTTP benchmark mode alongside Axum, label direct ASGITransport results separately, and test both modes.
- Add opt-in CSRF protection for state-changing API routes (double-submit token via cookie plus header or form field, per-route `@csrf_protect` or app-wide `SecurityConfig(csrf_enabled=True)`; spec v0.2 §11).
- Add CORS configuration with deny-all by default (explicit origins/methods/headers, preflight handling returning Allow plus Access-Control-Allow-* headers, Vary: Origin, same-origin requests exempt; spec v0.2 §11).
- Add per-request CSP nonce support for dynamically rendered pages (`SecurityConfig(csp_nonce_enabled=True)`; nonce in the CSP header and on island runtime script tags, fresh on every response; spec v0.2 §11).
- Verify Python/Rust lockfiles in CI and save separate Axum/Uvicorn HTTP smoke measurements.

## v0.6.0a2

- Add an interaction-triggered hydration strategy with bounded replay of supported button clicks, plus browser coverage for navigation, mismatch, cleanup, and failures.
- Add async-iterable API streaming for ASGI and Axum, bounded chunks and native backpressure, HTTP/2 framing, disconnect cancellation, and a bounded execution wait queue.
- Add content-based ETags, conditional requests, and single byte ranges for public files, SSG pages, and island bundles on ASGI and Axum.
- Add process-local ISR path invalidation with protection against older in-flight generations overwriting newer cache entries.
- Verify installed wheels by building and serving the example, and save platform-specific distributions from CI.
- Follow the v0.6.0 version line; the 0.1.0a0 release line and the v0.1.0-alpha.prealpha tag plan are dropped (see docs/RELEASE_PLAN.md).
- Route with a trie (static > dynamic > catch-all, backtracking) instead of a linear scan, and wait up to a bounded timeout for a concurrency slot before returning 503 with Retry-After: 1.
- Make concurrency limits configurable (--max-concurrent, --acquire-timeout-ms, --body-capacity, --body-timeout-ms) with CLI range validation; limit body intake to 16 slots and apply a 10-second header-read deadline.
- Restore HTTP/2 prior-knowledge (h2c) support via the hyper auto builder while keeping the header-read timeout.
- Detect oversized bodies by counting bytes instead of relying on error wrapping, add cache-control to SSG responses, evict expired cache entries instead of refreshing them, and normalize router parameter names.
- Return a clean 400 for conflicting Content-Length headers (hyper handles the common case; the dispatch check covers the Transfer-Encoding-first edge case).
- Propagate native server startup failures to the caller instead of returning a dead server.
- Validate h() arguments and Response bodies at construction, reload directly loaded modules from fresh source, share one module per file regardless of letter case, warn when an ISR cache insert fails, and add native-path coverage for the 1000-field query limit.
- Fix the smoke-test port race with retries, measure the CI benchmark in production mode, and run browser smoke checks on macOS and Windows as well as Linux.

## v0.6.0a1

- Bound Axum request-body intake separately from Python execution, apply a 10-second body-read deadline, and allow static and fresh ISR responses while Python slots are busy.
- Load application modules into separate per-app packages and support relative imports. **Breaking:** the framework no longer prepends the project root to the process-wide `sys.path`; migrate project-local imports to relative imports or install the project as a package.
- Verify the server's actual CSP and example routes in browser CI, validate CLI ports, and improve benchmark guidance when the example has not been built.
- Add regression coverage for slow uploads, disconnects, saturated handlers, module isolation, CLI, and ASGI message boundaries.

## v0.5.0

- Separate attempted and successful request throughput, status and transport errors, and all-request versus success latency in the smoke benchmark.
- Add optional separate Linux server-process CPU sampling and label the shared-process CPU and peak RSS measurements.
- Expand CI to Linux, macOS and Windows; verify packaged wheels, record a bounded benchmark, and run Chromium smoke checks on Linux.
- Normalize equivalent module paths and run the npm esbuild wrapper safely on Windows.

## v0.4.0

- Add load, visible, and idle island activation, cancellation and fallback scheduling.
- Return HTTP 400 for malformed query UTF-8 and JSON through the ASGI and Axum adapters.
- Limit parsed query fields, validate response headers and render nodes, and add default CSP and cache-variation headers.
- Document synchronous handler timeout limitations; add regression coverage.
- Provide English, Korean, Japanese and Simplified Chinese manuals and a production roadmap.

## v0.3.0

- Add opt-in island hydration with server DOM comparison, mismatch recovery and cleanup.

## v0.2.0

- Add JavaScript/TypeScript island bundling and browser mounting.

## v0.1.0 (reconstructed early snapshot)

- Introduce the Python/Rust runtime, file routing, buffered SSR, API handlers, ASGI and Axum.
- Add SSG, public file snapshots and process-local ISR caching.

Later production features, including distributed ISR, React/RSC, streaming SSR, OpenTelemetry, standalone packaging and worker isolation, are not included. See [the roadmap](docs/PRODUCTION_ROADMAP.md).
