# Roadmap, releases, and contributing

[Home](Home.md) · [한국어](../ko/Roadmap-and-Contributing.md)

## Current release

The published experimental release used for this wiki is **[v0.6.0a2](https://github.com/Duioolllll/PyNext/releases/tag/v0.6.0a2)**. This documentation follows `main` at [aa256bf](https://github.com/Duioolllll/PyNext/commit/aa256bf2b03a175d96f9f084a3b8cfaeca883ceb), including benchmark/CI improvements merged after release preparation. Review [CHANGELOG](../../../CHANGELOG.md) and [release plan](../../RELEASE_PLAN.md) when updating docs.

## Implemented

- Rust trie routing, Axum HTTP engine and Python/ASGI integration
- Buffered SSR, build-time SSG and process-local ISR
- Public assets with ETags and single ranges
- Async API streaming
- Optional JS/TS islands with load/visible/idle/limited interaction activation
- Cross-platform CI and transport-labeled benchmarks

## Planned / not yet implemented

- Streaming page SSR; React/RSC compatibility is a separate significant design effort
- Shared/distributed ISR and worker coordination
- Per-app process isolation and more resilient worker lifecycle
- Production observability, rate limiting, security defaults and standalone packaging
- Better Rust/Python data transfer and systematic profiling
- **Exploratory Django integration** (such as ORM, auth, and admin interoperability) has been discussed as a potential direction, **but no integration exists or is promised**.

Consult the [production roadmap](../../PRODUCTION_ROADMAP.md) for design options and acceptance criteria, not release guarantees.

## Contributing

1. Fork and branch from current `main`; keep changes focused.
2. Use CPython 3.13, uv, Rust/Cargo and npm where needed.
3. Run `cargo fmt --all -- --check`, `cargo test --locked --workspace --all-features` and `uv run pytest -q`.
4. Run example build and Chromium smoke tests when touching web-facing behavior.
5. Add tests and update **English + Korean wiki pages** when introducing/changing public behavior.
6. Open a PR that clearly distinguishes actual implementation, verification results, performance limitations and future work.

Do not publish sensitive credentials, imply benchmark supremacy from smoke tests, or claim compatibility with APIs not implemented. Respect upstream [Next.js MIT attribution](../../../third_party/NEXTJS.md) and the [PyNext MIT license](../../../LICENSE).

[GitHub issues](https://github.com/Duioolllll/PyNext/issues) · [Pull requests](https://github.com/Duioolllll/PyNext/pulls)
