"""Small reproducible smoke benchmark, with a bounded cross-framework comparison.

Modes: ``axum`` (PyNext standalone, loopback HTTP), ``uvicorn`` (the same
PyNext ASGI app under Uvicorn, loopback HTTP), ``asgi`` (in-process
ASGITransport, not comparable with the HTTP modes), and ``starlette`` (the
same-feature Starlette reference app in benchmarks/starlette_app.py under
Uvicorn, loopback HTTP). ``--compare`` runs the three loopback-HTTP groups
(axum, uvicorn, starlette) with identical settings and prints one combined
result. Comparison groups use separate server processes; this remains a
smoke comparison for regressions, not a production throughput claim; record
the environment (see docs/BENCHMARKING.md) alongside any numbers quoted.
"""
import argparse
import asyncio
from collections import Counter
import importlib.metadata
import json
import math
import os
import platform
import socket
import statistics
import subprocess
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


async def _start_uvicorn_http(app):
    """Run the same ASGI app over real loopback HTTP rather than ASGITransport."""
    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=listener.getsockname()[1],
            loop="asyncio", lifespan="off", access_log=False, log_level="error",
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Uvicorn stopped before startup")
                    await asyncio.sleep(0.01)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        return server, task, listener
    except BaseException:
        listener.close()
        raise


async def run(mode, count, concurrency, path, production, *, warmup=10, base_url=None, server_pid=None):
    if server_pid is not None:
        if server_pid < 1 or base_url is None:
            raise ValueError("--server-pid requires a positive PID and --base-url")
        if urlsplit(base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("--server-pid requires a loopback --base-url")
        _linux_process_cpu(server_pid)

    app = None
    server = None
    uvicorn_server = uvicorn_task = uvicorn_socket = None
    if base_url:
        client = httpx.AsyncClient(base_url=base_url, timeout=30)
    elif mode == "starlette":
        # Same-feature reference app; the PyNext app is not built here and
        # --production does not apply to this group. Loaded by file path
        # because bench.py also runs as a script (no package import).
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "pynext_starlette_app",
            Path(__file__).resolve().parent / "starlette_app.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        uvicorn_server, uvicorn_task, uvicorn_socket = await _start_uvicorn_http(module.app)
        client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{uvicorn_socket.getsockname()[1]}", timeout=30)
    else:
        app = create_app(ROOT, production=production)
        if mode == "axum":
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=30)
        elif mode == "uvicorn":
            uvicorn_server, uvicorn_task, uvicorn_socket = await _start_uvicorn_http(app)
            client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{uvicorn_socket.getsockname()[1]}", timeout=30)
        elif mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            raise ValueError(f"Unsupported benchmark mode: {mode}")
    try:
        async with client:
            result = await measure(client, path, count, concurrency, warmup=warmup,
                                   server_pid=server_pid, external=bool(base_url))
        transport = ("external_http" if base_url else
                     "in_process_asgi" if mode == "asgi" else "loopback_http")
        return {"mode": "external" if base_url else mode, "transport": transport, "path": path,
                "production": None if base_url or mode == "starlette" else production,
                "framework": None if base_url else "starlette" if mode == "starlette" else "pynext",
                "python": platform.python_version(), "platform": platform.platform(), **result}
    finally:
        if uvicorn_task is not None:
            uvicorn_server.should_exit = True
            try:
                await asyncio.wait_for(uvicorn_task, timeout=15)
            finally:
                uvicorn_socket.close()
        if server is not None:
            await asyncio.to_thread(server.stop)
        if app is not None:
            await app.isr.close()


COMPARE_MODES = ("axum", "uvicorn", "starlette")


async def run_compare(count, concurrency, path, production, *, warmup=10):
    """Run the loopback-HTTP comparison groups with identical settings.

    The in-process ``asgi`` mode is deliberately excluded: it skips the
    network, so its numbers are not comparable with loopback HTTP.
    """
    import psutil

    groups = []
    for mode in COMPARE_MODES:
        command = [sys.executable, str(Path(__file__).with_name("server_process.py")), "--mode", mode]
        if production:
            command.append("--production")
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                       stderr=subprocess.DEVNULL)
        sampler = None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            if not line.startswith(b"PYNEXT_BENCH_READY "):
                raise RuntimeError(f"{mode} benchmark server did not start")
            address = line.decode("ascii").strip().split(" ", 1)[1]
            server_process = psutil.Process(process.pid)
            samples = []
            stop = asyncio.Event()

            async def sample_rss():
                while not stop.is_set():
                    try:
                        samples.append(server_process.memory_info().rss)
                    except psutil.Error:
                        return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.01)
                    except TimeoutError:
                        pass

            sampler = asyncio.create_task(sample_rss())
            try:
                async with httpx.AsyncClient(base_url=f"http://{address}", timeout=30, trust_env=False) as client:
                    result = await measure(client, path, count, concurrency, warmup=warmup,
                                           server_pid=process.pid if sys.platform == "linux" else None, external=True)
            finally:
                stop.set()
                await sampler
            groups.append({"mode": mode, "transport": "loopback_http", "path": path,
                           "production": None if mode == "starlette" else production,
                           "framework": "starlette" if mode == "starlette" else "pynext",
                           "python": platform.python_version(), "platform": platform.platform(), **result,
                           "process_peak_rss_bytes": max(samples) if samples else None,
                           "process_peak_rss_scope": "server_sampled_10ms",
                           "server_pid": process.pid})
        finally:
            if sampler is not None and not sampler.done():
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
    return {
        "comparison": "pynext-vs-starlette",
        "conditions": {
            "path": path,
            "requests": count,
            "concurrency": concurrency,
            "warmup": warmup,
            "production": production,
            "transport": "loopback_http (separate server processes)",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "uvicorn": importlib.metadata.version("uvicorn"),
            "starlette": importlib.metadata.version("starlette"),
            "httpx": importlib.metadata.version("httpx"),
        },
        "groups": groups,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("asgi", "axum", "uvicorn", "starlette"), default="axum",
                        help="asgi uses in-process ASGITransport; axum/uvicorn/starlette use loopback HTTP")
    parser.add_argument("--compare", action="store_true",
                        help="run axum, uvicorn and starlette with identical settings and print one combined result (ignores --mode)")
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
    if args.compare:
        print(json.dumps(asyncio.run(run_compare(args.requests, args.concurrency, args.path,
                                                 args.production, warmup=args.warmup)), indent=2))
        return
    print(json.dumps(asyncio.run(run(args.mode, args.requests, args.concurrency, args.path, args.production,
                                 warmup=args.warmup, base_url=args.base_url, server_pid=args.server_pid)), indent=2))


if __name__ == "__main__":
    main()
