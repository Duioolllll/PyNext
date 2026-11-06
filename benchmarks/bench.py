"""Small reproducible smoke benchmark; not a cross-framework comparison."""
import argparse
import asyncio
import json
import math
import platform
import statistics
import time
from pathlib import Path

import httpx
from pynext import create_app
from pynext.server import start_native

ROOT = Path(__file__).resolve().parents[1] / "examples" / "basic"


async def run(mode, count, concurrency, path, production):
    app = create_app(ROOT, production=production)
    server = None
    if mode == "axum":
        server = start_native(app, asyncio.get_running_loop(), port=0)
        client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=30)
    else:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        async with client:
            for _ in range(10):
                result = await client.get(path)
                result.raise_for_status()
            queue = asyncio.Queue()
            for _ in range(count):
                queue.put_nowait(None)
            samples, failures = [], []

            async def worker():
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    start = time.perf_counter()
                    try:
                        response = await client.get(path)
                        if response.status_code != 200:
                            failures.append(response.status_code)
                    except httpx.HTTPError as exc:
                        failures.append(type(exc).__name__)
                    samples.append((time.perf_counter() - start) * 1000)

            start, cpu = time.perf_counter(), time.process_time()
            await asyncio.gather(*(worker() for _ in range(concurrency)))
            elapsed, cpu = time.perf_counter() - start, time.process_time() - cpu
            ordered = sorted(samples)
            def p(n):
                return round(ordered[max(0, math.ceil(len(ordered) * n) - 1)], 3)
            return {"mode": mode, "path": path, "production": production,
                    "python": platform.python_version(), "platform": platform.platform(),
                    "requests": count, "concurrency": concurrency, "errors": len(failures),
                    "elapsed_seconds": round(elapsed, 3), "process_cpu_seconds": round(cpu, 3),
                    "requests_per_second": round(count / elapsed, 2),
                    "latency_ms": {"p50": round(statistics.median(ordered), 3), "p95": p(.95), "p99": p(.99)}}
    finally:
        if server is not None:
            await asyncio.to_thread(server.stop)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("asgi", "axum"), default="axum")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--path", default="/")
    parser.add_argument("--production", action="store_true", help="use the build manifest")
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests and concurrency must be positive")
    print(json.dumps(asyncio.run(run(args.mode, args.requests, args.concurrency, args.path, args.production)), indent=2))


if __name__ == "__main__":
    main()
