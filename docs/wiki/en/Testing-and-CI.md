# Testing and CI

[Home](Home.md) · [한국어](../ko/Testing-and-CI.md)

## Local tests

~~~bash
uv sync --locked --extra dev
npm ci
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo test --locked --workspace --all-features
uv run pytest -q
uv run pynext build --dir examples/basic
uv run python tests/smoke_wheel.py
~~~

`tests/smoke_wheel.py` builds and tests an isolated wheel installation and should be run in a prepared environment. For the explicit distribution build workflow see [CI](../../../.github/workflows/ci.yml).

## Browser end-to-end checks

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

The browser suite covers islands, hydration, interactions, mismatch fallback, navigation behavior and CSP in an actual Chromium browser.

## CI matrix

GitHub Actions checks **Ubuntu, macOS and Windows** with Python 3.13 and 3.14, Node.js 20, Rust and uv. It runs format/lint, Rust/Python tests, production build, sdist/wheel packaging, isolated-wheel verification, Axum/Uvicorn HTTP smoke measurements, and browser checks. Dependencies are installed with `uv sync --locked` and `npm ci`; Rust checks use `--locked`.

Artifacts include platform distribution files and per-server benchmark JSON. A passing CI run **does not** prove production readiness, large-scale performance, or security auditing.

## Where to add coverage

- Rust routes/cache: `crates/pynext-router`, `crates/pynext-cache`.
- Python/ASGI, ISR, static responses, streaming: `tests/test_*.py`.
- Real native HTTP and overload behavior: `tests/test_native_admission.py` and `tests/test_streaming_native.py`.
- Browser behavior: `tests/browser/`.

Before opening a PR, include a focused regression test for any behavior fix and document intended compatibility changes. See [Roadmap and contributing](Roadmap-and-Contributing.md).
