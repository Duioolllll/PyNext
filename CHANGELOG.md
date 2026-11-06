# Changelog

All versions below are experimental. The feature-stage snapshots were reconstructed from preserved development source distributions when these releases were prepared; they are not claims that the tags were published at those earlier development times. The later malformed-query/JSON fix and timeout documentation/test were backported to the reconstructed snapshots.

## v0.4.0

- Add load, visible, and idle island activation, cancellation and fallback scheduling.
- Return HTTP 400 for malformed query UTF-8 and JSON through the ASGI and Axum adapters.
- Document synchronous handler timeout limitations; add regression coverage.
- Provide English, Korean, Japanese and Simplified Chinese manuals and a production roadmap.

## v0.3.0

- Add opt-in island hydration with server DOM comparison, mismatch recovery and cleanup.

## v0.2.0

- Add JavaScript/TypeScript island bundling and browser mounting.

## v0.1.0

- Introduce the Python/Rust runtime, file routing, buffered SSR, API handlers, ASGI and Axum.
- Add SSG, public file snapshots and process-local ISR caching.

Later production features, including distributed ISR, React/RSC, streaming, OpenTelemetry, standalone packaging and worker isolation, are not included. See [the roadmap](docs/PRODUCTION_ROADMAP.md).
