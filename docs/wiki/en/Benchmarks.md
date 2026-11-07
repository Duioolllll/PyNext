# Benchmarks and performance claims

[Home](Home.md) · [한국어](../ko/Benchmarks.md)

The bundled benchmark is a **regression/smoke tool**, not evidence of a general speed advantage over Next.js, FastAPI, Django, or other frameworks. See [the canonical benchmark methodology](../../BENCHMARKING.md).

## Prepare

~~~bash
uv sync --locked --extra dev
npm ci
uv run pynext build --dir examples/basic
~~~

## Run a bounded experiment

~~~bash
uv run python benchmarks/bench.py --mode axum --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode uvicorn --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode asgi --production --path /api/users --requests 1000 --concurrency 10
~~~

- `axum` and `uvicorn` use **real TCP loopback HTTP**, with both client and server in the same process.
- `asgi` uses `httpx.ASGITransport` **without HTTP networking**. Its throughput must **not** be compared directly to network modes.
- Output identifies `transport`, successful vs attempted requests/sec, HTTP and transport failures, p50/p95/p99 latencies, response size, CPU scope and lifetime peak RSS.

## Separate server process

Run `uv run pynext start --dir examples/basic --port 3000` in one terminal, then:

~~~bash
uv run python benchmarks/bench.py --base-url http://127.0.0.1:3000 --path /api/users --requests 5000 --concurrency 32 --warmup 100
~~~

On Linux, add `--server-pid PID` to sample **that PID**. It excludes child workers. The reported peak RSS is from the load generator, not the external server.

## Fair comparisons

Record hardware, OS, runtimes, worker count, TLS/HTTP mode, payloads, cache state, requests, concurrency, warmup and repeated samples. Compare equivalent route semantics and response bodies. Include successful throughput, error rate and p95/p99; larger attempted throughput with more errors is not a win.

CI's **40 requests at concurrency 4** per Axum/Uvicorn mode are correctness smoke tests, **not** a performance regression threshold. Historical cross-framework numbers discussed elsewhere are not independently reproduced or guaranteed by the repository.
