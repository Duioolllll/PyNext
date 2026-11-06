"""Small reproducible smoke benchmark; not a cross-framework comparison."""
import argparse
import asyncio
from collections import Counter
import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pynext import create_app
from pynext.build import MANIFEST
from pynext.server import start_native

try:
    import resource
except ImportError:  # Windows
    resource = None

ROOT = Path(__file__).resolve().parents[1] / "examples" / "basic"


def _percentiles(samples):
    if not samples:
        return None
    ordered = sorted(samples)

    def percentile(value):
        return round(ordered[max(0, math.ceil(len(ordered) * value) - 1)], 3)

    return {"p50": round(statistics.median(ordered), 3), "p95": percentile(.95), "p99": percentile(.99)}


def _linux_process_cpu(pid):
    """Return CPU time and start ticks for one local Linux process."""
    if sys.platform != "linux":
        raise ValueError("--server-pid currently requires Linux")
    # The command name is parenthesized and may contain spaces or parentheses.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"), int(fields[19])


def _process_peak_rss_bytes():
    """Return lifetime peak resident memory where the OS reports known units."""
    if resource is None or sys.platform not in {"linux", "darwin"}:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value) * (1024 if sys.platform == "linux" else 1)


async def measure(client, path, count, concurrency, *, warmup=10, server_pid=None, external=False):
    """Measure all attempts and successful 2xx responses separately."""
    if count < 1 or concurrency < 1 or warmup < 0:
        raise ValueError("requests and concurrency must be positive; warmup must be non-negative")
    warmup_errors = 0
    for _ in range(warmup):
        try:
            response = await client.get(path)
            warmup_errors += not 200 <= response.status_code < 300
        except httpx.HTTPError:
            warmup_errors += 1

    queue = asyncio.Queue()
    for _ in range(count):
        queue.put_nowait(None)
    samples, successful_samples = [], []
    statuses, transport_errors = Counter(), Counter()
    response_bytes = 0

    async def worker():
        nonlocal response_bytes
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            started = time.perf_counter()
            success = False
            try:
                response = await client.get(path)
                statuses[str(response.status_code)] += 1
                response_bytes += len(response.content)
                success = 200 <= response.status_code < 300
            except httpx.HTTPError as exc:
                transport_errors[type(exc).__name__] += 1
            finally:
                latency = (time.perf_counter() - started) * 1000
                samples.append(latency)
                if success:
                    successful_samples.append(latency)

    server_cpu_before = _linux_process_cpu(server_pid) if server_pid is not None else None
    started, cpu_started = time.perf_counter(), time.process_time()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    cpu = time.process_time() - cpu_started
    server_cpu, server_cpu_error = None, None
    if server_cpu_before is not None:
        try:
            cpu_after, start_after = _linux_process_cpu(server_pid)
            if start_after != server_cpu_before[1]:
                server_cpu_error = "server process exited or PID was reused"
            else:
                server_cpu = cpu_after - server_cpu_before[0]
        except (OSError, ValueError, IndexError):
            server_cpu_error = "server process exited or CPU data was unavailable"

    successes = len(successful_samples)
    response_count = sum(statuses.values())
    status_classes = Counter()
    for code, number in statuses.items():
        status_classes[f"{int(code) // 100}xx"] += number
    return {
        "requests": count,
        "concurrency": concurrency,
        "warmup_requests": warmup,
        "warmup_errors": warmup_errors,
        "successful_requests": successes,
        "errors": count - successes,
        "error_rate": round((count - successes) / count, 6),
        "http_status_counts": dict(sorted(statuses.items())),
        "http_status_class_counts": dict(sorted(status_classes.items())),
        "transport_error_counts": dict(sorted(transport_errors.items())),
        "elapsed_seconds": round(elapsed, 3),
        "attempted_requests_per_second": round(count / elapsed, 2),
        "successful_requests_per_second": round(successes / elapsed, 2),
        "latency_ms": _percentiles(samples),
        "successful_latency_ms": _percentiles(successful_samples),
        "response_bytes": {"total": response_bytes,
                           "mean_per_response": round(response_bytes / response_count, 2) if response_count else None},
        "process_cpu_seconds": round(cpu, 3),
        "process_cpu_scope": "load_generator_only" if external else "load_generator_and_server",
        "process_peak_rss_bytes": _process_peak_rss_bytes(),
        "server_pid": server_pid,
        "server_cpu_seconds": round(server_cpu, 3) if server_cpu is not None else None,
        "server_cpu_percent_one_core": round(server_cpu / elapsed * 100, 2) if server_cpu is not None else None,
        "server_cpu_error": server_cpu_error,
    }


async def run(mode, count, concurrency, path, production, *, warmup=10, base_url=None, server_pid=None):
    if server_pid is not None:
        if server_pid < 1 or base_url is None:
            raise ValueError("--server-pid requires a positive PID and --base-url")
        if urlsplit(base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("--server-pid requires a loopback --base-url")
        _linux_process_cpu(server_pid)

    app = None
    server = None
    if base_url:
        client = httpx.AsyncClient(base_url=base_url, timeout=30)
    else:
        app = create_app(ROOT, production=production)
        if mode == "axum":
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=30)
        else:
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        async with client:
            result = await measure(client, path, count, concurrency, warmup=warmup,
                                   server_pid=server_pid, external=bool(base_url))
        return {"mode": "external" if base_url else mode, "path": path,
                "production": None if base_url else production,
                "python": platform.python_version(), "platform": platform.platform(), **result}
    finally:
        if server is not None:
            await asyncio.to_thread(server.stop)
        if app is not None:
            await app.isr.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("asgi", "axum"), default="axum")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--path", default="/")
    parser.add_argument("--production", action="store_true", help="use the build manifest")
    parser.add_argument("--base-url", help="benchmark an already-running HTTP server")
    parser.add_argument("--server-pid", type=int, help="sample CPU of a separate local Linux server process")
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.warmup < 0:
        parser.error("requests and concurrency must be positive; warmup must be non-negative")
    if args.production and not args.base_url and not (ROOT / MANIFEST).is_file():
        parser.error("no example build manifest; run `pynext build --dir examples/basic` first")
    if args.server_pid is not None:
        if args.server_pid < 1 or not args.base_url:
            parser.error("--server-pid requires a positive PID and --base-url")
        if sys.platform != "linux":
            parser.error("--server-pid currently requires Linux")
        if urlsplit(args.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            parser.error("--server-pid requires a loopback --base-url")
        try:
            _linux_process_cpu(args.server_pid)
        except (OSError, ValueError, IndexError) as exc:
            parser.error(f"cannot read server process CPU data: {exc}")
    print(json.dumps(asyncio.run(run(args.mode, args.requests, args.concurrency, args.path, args.production,
                                 warmup=args.warmup, base_url=args.base_url, server_pid=args.server_pid)), indent=2))


if __name__ == "__main__":
    main()
