# Roadmap, releases, and contributing

[Home](Home.md) · [한국어](../ko/Roadmap-and-Contributing.md)

## Current release

The published experimental release used for this wiki is **[0.1.0](https://github.com/Duioolllll/PyNext/releases/tag/0.1.0v)**. This documentation follows `main` at [d8560f8](https://github.com/Duioolllll/PyNext/commit/d8560f828a13e6ae5bb82bf4496fed6b4d45ecb8), including benchmark/CI improvements merged after release preparation. Review [CHANGELOG](../../../CHANGELOG.md) and [release plan](../../RELEASE_PLAN.md) when updating docs.

## Implemented

- Rust trie routing, Axum HTTP engine and Python/ASGI integration
- Buffered and streaming SSR, build-time SSG, ISR with process-local memory or shared SQLite/Redis backends
- Public assets with ETags and single ranges
- Async API streaming and WebSocket
- Optional JS/TS islands with load/visible/idle/interaction activation, client-side navigation and island HMR
- Global HTTP middleware, SQLite sessions with session-bound CSRF, multipart uploads, `pynext init`, development error pages and a Linux standalone directory
- Structured logging, metrics and OpenTelemetry tracing (real SDK)
- Cross-platform CI and transport-labeled benchmarks

The full capability list is generated from code: [FEATURES](../../FEATURES.md).

## Planned / not yet implemented

- React/RSC compatibility is a separate significant design effort and an explicit non-goal for 1.0
- Security-default hardening (see [roadmap](../../ROADMAP.md))
- DB and authentication reference apps (credential verification stays the application's responsibility)
- Better Rust/Python data transfer and systematic profiling
- **Exploratory Django integration** (such as ORM, auth, and admin interoperability) has been discussed as a potential direction, **but no integration exists or is promised**.

Consult the [production roadmap](../../PRODUCTION_ROADMAP.md) for design options and acceptance criteria, not release guarantees.

## Contributing

1. Fork and branch from current `main`; keep changes focused.
2. Use CPython 3.13 or 3.14, uv, Rust/Cargo and npm where needed.
3. Run `cargo fmt --all -- --check`, `cargo test --locked --workspace --all-features` and `uv run pytest -q`.
4. Run example build and Chromium smoke tests when touching web-facing behavior.
5. Add tests and update **English + Korean wiki pages** when introducing/changing public behavior.
6. Open a PR that clearly distinguishes actual implementation, verification results, performance limitations and future work.

Do not publish sensitive credentials, imply benchmark supremacy from smoke tests, or claim compatibility with APIs not implemented. Respect upstream [Next.js MIT attribution](../../../third_party/NEXTJS.md) and the [PyNext MIT license](../../../LICENSE).

[GitHub issues](https://github.com/Duioolllll/PyNext/issues) · [Pull requests](https://github.com/Duioolllll/PyNext/pulls)
