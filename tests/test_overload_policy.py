"""Overload policy (roadmap C7): bounded wait, then reject.

The execution wait queue is configurable via ``max_waiting`` and the
503 ``Retry-After`` reflects the configured wait. Defaults are unchanged:
the queue holds ``min(max_concurrent, 64)`` waiters and a 503 after the
default 3000 ms wait carries ``Retry-After: 3``.

The concurrency sweep at the end is a measurement, not a pass/fail
throughput claim: it records success/5xx/p95 per level on this machine
so a policy change shows up as a number, while the structural assertions
(every response is 200 or 503, every 503 carries Retry-After) hold
whatever the load does.
"""
import asyncio
import math
import statistics
import time
from collections import Counter

import httpx

from pynext import create_app
from pynext.server import start_native

PAYLOAD = "x" * 10_240  # ~10 KiB JSON body, like the user-supplied benchmark


def _write_routes(tmp_path):
    wait = tmp_path / "app" / "api" / "wait"
    wait.mkdir(parents=True)
    (wait / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        'started = 0\nrelease = None\n'
        '@route("GET")\n'
        'async def get():\n'
        '    global started\n'
        '    started += 1\n'
        '    await release.wait()\n'
        '    return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    payload = tmp_path / "app" / "api" / "payload"
    payload.mkdir(parents=True)
    (payload / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\n'
        'def get(): return JSONResponse({"data": "x" * 10240})\n',
        encoding="utf-8",
    )


def _blocking_app(tmp_path):
    _write_routes(tmp_path)
    app = create_app(tmp_path)
    module = app._module("app/api/wait/route.py")
    module.release = asyncio.Event()
    return app, module


async def _wait_started(module, expected, timeout=10):
    async with asyncio.timeout(timeout):
        while module.started < expected:
            await asyncio.sleep(0.02)


def test_default_waiting_capacity_is_min_slots_64(tmp_path):
    """Default queue = min(slots, 64): with 2 slots, the 5th request 503s."""
    async def run():
        app, module = _blocking_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=2, acquire_timeout_ms=5000)
        try:
            async with httpx.AsyncClient(
                    base_url=f"http://{server.address}", timeout=10,
                    limits=httpx.Limits(max_connections=20)) as client:
                tasks = [asyncio.create_task(client.get("/api/wait")) for _ in range(2)]
                await _wait_started(module, 2)
                waiters = [asyncio.create_task(client.get("/api/wait")) for _ in range(2)]
                await asyncio.sleep(0.5)  # let both waiters take queue permits
                fifth = await client.get("/api/wait")
                assert fifth.status_code == 503
                assert fifth.headers["retry-after"] == "1"
                module.release.set()
                results = await asyncio.gather(*tasks, *waiters)
                assert all(r.status_code == 200 for r in results)
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_max_waiting_zero_rejects_without_waiting(tmp_path):
    async def run():
        app, module = _blocking_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=5000, max_waiting=0)
        try:
            async with httpx.AsyncClient(
                    base_url=f"http://{server.address}", timeout=10,
                    limits=httpx.Limits(max_connections=20)) as client:
                first = asyncio.create_task(client.get("/api/wait"))
                await _wait_started(module, 1)
                started = time.monotonic()
                second = await client.get("/api/wait")
                assert second.status_code == 503
                assert second.headers["retry-after"] == "1"
                # No waiting at all: the 503 must arrive well before the
                # 5 s acquire timeout would have expired.
                assert time.monotonic() - started < 2
                module.release.set()
                assert (await first).status_code == 200
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_max_waiting_large_burst_waits_and_succeeds(tmp_path):
    async def run():
        app, module = _blocking_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=5000, max_waiting=8)
        try:
            async with httpx.AsyncClient(
                    base_url=f"http://{server.address}", timeout=10,
                    limits=httpx.Limits(max_connections=20)) as client:
                first = asyncio.create_task(client.get("/api/wait"))
                await _wait_started(module, 1)

                async def release_soon():
                    await asyncio.sleep(0.3)
                    module.release.set()

                asyncio.create_task(release_soon())
                burst = await asyncio.gather(
                    *(client.get("/api/wait") for _ in range(8)))
                assert all(r.status_code == 200 for r in burst)
                assert (await first).status_code == 200
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_retry_after_matches_acquire_timeout(tmp_path):
    """A 503 after the full wait carries ceil(timeout) seconds, not a fixed 1."""
    async def run():
        app, module = _blocking_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=3000)
        try:
            async with httpx.AsyncClient(
                    base_url=f"http://{server.address}", timeout=10,
                    limits=httpx.Limits(max_connections=20)) as client:
                first = asyncio.create_task(client.get("/api/wait"))
                await _wait_started(module, 1)
                started = time.monotonic()
                second = await client.get("/api/wait")
                assert second.status_code == 503
                assert second.headers["retry-after"] == "3"
                assert time.monotonic() - started >= 2.5
                module.release.set()
                assert (await first).status_code == 200
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_retry_after_default_is_three(tmp_path):
    """Default acquire timeout is 3000 ms (C-P0-2), so a 503 after the full
    default wait carries ceil(3000/1000) = 3, not the old 1000 ms default's 1."""
    async def run():
        app, module = _blocking_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=1)
        try:
            async with httpx.AsyncClient(
                    base_url=f"http://{server.address}", timeout=10,
                    limits=httpx.Limits(max_connections=20)) as client:
                first = asyncio.create_task(client.get("/api/wait"))
                await _wait_started(module, 1)
                second = await client.get("/api/wait")
                assert second.status_code == 503
                assert second.headers["retry-after"] == "3"
                module.release.set()
                assert (await first).status_code == 200
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def _p95(samples):
    if not samples:
        return None
    ordered = sorted(samples)
    return round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3)


async def _sweep_level(client, count, concurrency):
    queue = asyncio.Queue()
    for _ in range(count):
        queue.put_nowait(None)
    statuses, latencies, ok_latencies = Counter(), [], []
    retry_after_missing = 0

    async def worker():
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            started = time.perf_counter()
            try:
                response = await client.get("/api/payload")
                statuses[str(response.status_code)] += 1
                if response.status_code == 503 and "retry-after" not in response.headers:
                    nonlocal retry_after_missing
                    retry_after_missing += 1
                success = 200 <= response.status_code < 300
            except httpx.HTTPError:
                statuses["transport-error"] += 1
                success = False
            finally:
                latency = (time.perf_counter() - started) * 1000
                latencies.append(latency)
                if success:
                    ok_latencies.append(latency)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    return {
        "concurrency": concurrency,
        "count": count,
        "success": statuses["200"],
        "statuses": dict(statuses),
        "retry_after_missing": retry_after_missing,
        "success_rps": round(statuses["200"] / elapsed, 1) if elapsed else 0,
        "p95_ms_all": _p95(latencies),
        "p95_ms_success": _p95(ok_latencies),
        "elapsed_s": round(elapsed, 3),
    }


def test_concurrency_sweep_10kb_json_records_results(tmp_path):
    """Sweep 1/50/200/500 against the fast 10 KiB JSON handler (defaults).

    Records success RPS / 5xx / p95 per level. Structural assertions only:
    every response is 200 or 503 and every 503 carries Retry-After.
    """
    async def run():
        _write_routes(tmp_path)
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(
                    base_url=f"http://{server.address}", timeout=30,
                    limits=httpx.Limits(max_connections=600,
                                        max_keepalive_connections=100)) as client:
                for _ in range(5):
                    assert (await client.get("/api/payload")).status_code == 200
                rows = []
                for concurrency in (1, 50, 200, 500):
                    row = await _sweep_level(client, 200, concurrency)
                    rows.append(row)
                    assert set(row["statuses"]) <= {"200", "503"}, row
                    assert row["retry_after_missing"] == 0, row
                print("\nconcurrency sweep (10 KiB JSON, defaults: slots=64, "
                      "queue=min(64, slots), acquire_timeout=1000ms):")
                for row in rows:
                    print(f"  c={row['concurrency']:<4} success={row['success']}/{row['count']} "
                          f"success_rps={row['success_rps']} "
                          f"p95_all={row['p95_ms_all']}ms "
                          f"p95_success={row['p95_ms_success']}ms "
                          f"statuses={row['statuses']}")
                assert rows[0]["success"] == 200, rows[0]
                assert rows[1]["success"] == 200, rows[1]
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
