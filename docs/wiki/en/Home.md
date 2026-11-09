# PyNext Wiki

**PyNext 0.1.2 — experimental Python + Rust full-stack framework**

[한국어](../ko/Home.md) · [Source](https://github.com/Duioolllll/PyNext) · [Release](https://github.com/Duioolllll/PyNext/releases/tag/v0.1.2) · [License](../../../LICENSE)

PyNext is an independent, Next.js-inspired experiment in **file-based Python pages and APIs** combined with a **Rust router, HTML renderer, and Axum/Tokio HTTP engine**. It is **not Next.js, React, or React Server Components compatible**.

> **Experimental software:** CPython 3.13 and 3.14 are verified. Do not expose this prototype directly to the public internet or execute untrusted app code.

## Explore

| Start | Build applications | Understand internals |
| --- | --- | --- |
| [Getting started](Getting-Started.md) | [Routing](Routing.md) | [Architecture](Architecture.md) |
| [Components and layouts](Components-and-Layouts.md) | [SSR / SSG / ISR](Rendering.md) | [HTTP and caching](HTTP-and-Caching.md) |
| [API and streaming](API-Routes-and-Streaming.md) | [Islands and hydration](Islands-and-Hydration.md) | [Benchmarks](Benchmarks.md) |
| [Testing and CI](Testing-and-CI.md) | [Deployment and security](Deployment-and-Security.md) | [Troubleshooting](Troubleshooting.md) |

For project status, implementation limits, and **proposed** Django integration, see [Roadmap and contributing](Roadmap-and-Contributing.md).

## At a glance

- **Routes:** `app/page.py`, `app/blog/[slug]/page.py`, `app/api/users/route.py`.
- **Rendering:** buffered and streaming Python-to-Rust SSR; build-time SSG; ISR with process-local memory or shared SQLite/Redis backends.
- **Interactive UI:** optional JS/TS islands bundled with esbuild, with load/visible/idle/limited interaction hydration.
- **HTTP:** native Axum/Hyper HTTP/1.1 and h2c, ASGI adapter, bounded API streaming.
- **Testing:** Python, Rust, Chromium, Windows/macOS/Linux CI.

This documentation is based on the repository's `main` branch at commit [d8560f8](https://github.com/Duioolllll/PyNext/commit/d8560f828a13e6ae5bb82bf4496fed6b4d45ecb8), not on unimplemented roadmap items.
