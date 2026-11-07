# Rust / Python architecture

[Home](Home.md) · [한국어](../ko/Architecture.md)

PyNext has a Python application layer and native Rust extension compiled with **maturin/PyO3**.

~~~text
Client
  |
  v
Tokio + Hyper HTTP/1.1 or h2c
  |
  v
Axum handler and body admission (Rust)
  |
  +--> public files / SSG / fresh anonymous ISR --> HTTP response
  |
  v
execution semaphore and bounded wait queue
  |
  v
PyO3 bridge --> Python asyncio --> route dispatch
                                   |
                         +---------+---------+
                         |                   |
                     Page/UI tree          API Response
                         |                   |
                  JSON serialization      JSON / stream
                         |
                  Rust HTML renderer
                         |
                         v
                     HTTP response
~~~

## Source map

| Component | Source |
| --- | --- |
| Native server and renderer | [`crates/pynext-native/src/lib.rs`](../../../crates/pynext-native/src/lib.rs) |
| Rust trie router | [`crates/pynext-router/src/lib.rs`](../../../crates/pynext-router/src/lib.rs) |
| Native cache | [`crates/pynext-cache/src/lib.rs`](../../../crates/pynext-cache/src/lib.rs) |
| Python app, manifest, rendering | [`python/pynext/app.py`](../../../python/pynext/app.py) |
| Async bridge / ASGI | [`python/pynext/server.py`](../../../python/pynext/server.py) |
| Build and SSG | [`python/pynext/build.py`](../../../python/pynext/build.py) |
| JS/TS client runtime | [`python/pynext/browser/runtime.js`](../../../python/pynext/browser/runtime.js) |

## Performance boundary

For buffered page rendering, Python builds a node dictionary, serializes JSON, then Rust parses the JSON and escapes/serializes HTML. This path incurs **GIL, JSON, and copying overhead**; Rust networking does not make arbitrary Python handlers run without the GIL.

Synchronous Python handlers run in threads. Timeout/cancellation cannot forcibly stop synchronous work already executing, including database writes. Use idempotency and upstream timeouts.

## Supported vs planned

Implemented: file routing, buffered SSR/SSG/ISR, HTTP server, ASGI, API streaming, optional islands. Not implemented: React/RSC, streaming SSR, shared ISR across workers, per-app process isolation, standalone packaging, built-in OpenTelemetry.

See [HTTP and caching](HTTP-and-Caching.md) and [Roadmap and contributing](Roadmap-and-Contributing.md).
