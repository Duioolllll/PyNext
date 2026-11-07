# Changelog

All versions below are experimental. The v0.1.0–v0.3.0 snapshots were reconstructed from preserved development source distributions when those releases were prepared; they are not claims that the tags were published at those earlier development times. The later malformed-query/JSON fix and timeout documentation/test were backported to the reconstructed snapshots.

## Unreleased

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

## v0.1.0

- Introduce the Python/Rust runtime, file routing, buffered SSR, API handlers, ASGI and Axum.
- Add SSG, public file snapshots and process-local ISR caching.

Later production features, including distributed ISR, React/RSC, streaming SSR, OpenTelemetry, standalone packaging and worker isolation, are not included. See [the roadmap](docs/PRODUCTION_ROADMAP.md).
