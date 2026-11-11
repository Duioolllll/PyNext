"""C-P1-5 (adaptive/fair admission) and C-P1-7 (per-stage timeouts).

C-P1-5: the Rust slot wait is FIFO (tokio semaphore) and, when
``acquire_timeout_max_ms`` is set, adaptive — the effective timeout is
``clamp(2 * EWMA(slot hold), acquire_timeout_ms, max)``. ``--workers
auto`` resolves to one worker per CPU.

C-P1-7: body read / handler execution / response send are separate
stages. Handler overrun answers 504, admission overload answers 503
+ ``Retry-After``, a slow body answers 408, and each timeout is
counted under its stage.
"""
import asyncio
import os
import threading

import httpx
import pytest

from pynext import RateLimiter, Response, create_app
from pynext.admission import DecisionGate, DecisionUnavailable
from pynext.config import DEFAULT_CONFIG, ConfigError, load_config, resolve_workers
from pynext.observability import MetricsCollector, ObservabilityConfig
from pynext.server import start_native


def write_config(root, text):
    (root / "pynext.config.py").write_text(text, encoding="utf-8")


# -- config: --workers auto (C-P1-5) --------------------------------------


def test_resolve_workers_auto_uses_cpu_count(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert resolve_workers("auto") == 8
    assert resolve_workers("AUTO") == 8
    assert resolve_workers(" auto ") == 8
    assert resolve_workers(3) == 3


def test_resolve_workers_auto_is_clamped(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 128)
    assert resolve_workers("auto") == 64
    monkeypatch.setattr(os, "cpu_count", lambda: 0)
    assert resolve_workers("auto") == 1
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert resolve_workers("auto") == 1


def test_workers_auto_from_config_file_and_cli_override(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    write_config(tmp_path, "config = {'workers': 'auto'}\n")
    assert load_config(tmp_path, "start").values.workers == 4
    assert load_config(tmp_path, "start", cli_overrides={"workers": "auto"}).values.workers == 4
    assert load_config(tmp_path, "start", cli_overrides={"workers": 2}).values.workers == 2


def test_workers_invalid_string_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="'workers' must be an integer or 'auto'"):
        load_config(tmp_path, "start", cli_overrides={"workers": "many"})


# -- config: new timeout defaults (C-P1-5 / C-P1-7) ------------------------


def test_new_timeout_defaults():
    assert DEFAULT_CONFIG.acquire_timeout_max_ms is None
    assert DEFAULT_CONFIG.handler_timeout_ms == 30000
    assert DEFAULT_CONFIG.response_timeout_ms == 30000


def test_acquire_timeout_max_below_base_warns(tmp_path):
    write_config(tmp_path, "config = {'acquire_timeout_ms': 5000, 'acquire_timeout_max_ms': 1000}\n")
    resolved = load_config(tmp_path, "start")
    assert any("acquire_timeout_max_ms" in warning for warning in resolved.warnings)
    assert resolved.values.acquire_timeout_max_ms == 1000


# -- CLI -------------------------------------------------------------------


def test_cli_workers_auto_reaches_supervisor(tmp_path, monkeypatch):
    from pynext import cli
    import sys

    seen = []
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(cli, "run_supervisor", lambda config, workers: seen.append(workers) or 0)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path), "--workers", "auto"])
    assert cli.main() == 0
    assert seen == [4]


def test_cli_stage_and_adaptive_timeouts_reach_serve(tmp_path, monkeypatch):
    from pynext import cli
    import sys

    seen = []
    app = object()
    monkeypatch.setattr(cli, "create_app",
                        lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs: app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(cli, "serve", serve)
    # C-P0: `start` defaults to the supervisor path (workers=2); pin
    # single-process so the fake serve() is reached at all.
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--workers", "1",
                                      "--acquire-timeout-max-ms", "5000",
                                      "--handler-timeout-ms", "5000",
                                      "--response-timeout-ms", "7000"])
    cli.main()
    assert seen == [{"shutdown_timeout": 30.0, "acquire_timeout_max_ms": 5000,
                     "handler_timeout_ms": 5000, "response_timeout_ms": 7000}]


def test_cli_defaults_keep_old_serve_call_shape(tmp_path, monkeypatch):
    # The fake serve() has the historical fixed signature: new kwargs
    # must only be forwarded when they differ from the defaults.
    from pynext import cli
    import sys

    seen = []
    app = object()
    monkeypatch.setattr(cli, "create_app",
                        lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs: app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen.append((received_app, host, port, max_concurrent, acquire_timeout_ms,
                     body_capacity, body_timeout_ms, shutdown_timeout, max_waiting))

    monkeypatch.setattr(cli, "serve", serve)
    # C-P0: `start` defaults to the supervisor path (workers=2); pin
    # single-process so the fake serve() is reached at all. The expected
    # values are the C-P0 defaults (128/3000), not the pre-C-P0 64/1000.
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--workers", "1"])
    cli.main()
    assert seen == [(app, "127.0.0.1", 3000, 128, 3000, 16, 10000, 30, None)]


# -- DecisionGate stage naming (C-P1-7) -------------------------------------


def test_decision_gate_names_its_stage_in_errors():
    async def exercise():
        release = threading.Event()

        def slow():
            release.wait(5)
            return True

        gate = DecisionGate(capacity=1, timeout=5, name="rate-limit decision")
        first = asyncio.create_task(gate.check(slow))
        await asyncio.sleep(0.05)
        assert gate.inflight == 1
        with pytest.raises(DecisionUnavailable, match="rate-limit decision capacity exhausted"):
            await gate.check(lambda: True)
        release.set()
        assert await first is True

        deadline_gate = DecisionGate(capacity=1, timeout=0.05, name="rate-limit decision")
        with pytest.raises(DecisionUnavailable, match="rate-limit decision deadline exceeded"):
            await deadline_gate.check(lambda: threading.Event().wait(2) or True)

    asyncio.run(exercise())


def test_decision_gate_default_name_is_decision():
    gate = DecisionGate(capacity=1, timeout=0.01)
    assert gate.name == "decision"


# -- stage metrics (C-P1-7) -------------------------------------------------


def test_stage_timeouts_in_snapshot_and_prometheus():
    metrics = MetricsCollector(enabled=True)
    metrics.observe_stage_timeout("handler")
    metrics.observe_stage_timeout("handler")
    metrics.observe_stage_timeout("body")
    metrics.observe_stage_timeout("nope")  # unknown stages mint no series
    snap = metrics.snapshot()
    assert snap["stage_timeouts"] == {"body": 1, "handler": 2, "response": 0, "admission": 0}
    text = metrics.prometheus_text()
    assert 'pynext_stage_timeouts_total{stage="handler"} 2' in text
    assert 'pynext_stage_timeouts_total{stage="body"} 1' in text


# -- ASGI stages (C-P1-7) ---------------------------------------------------


def _asgi_request(app, *, path, method="GET", receive=None, body=b""):
    """Drive one ASGI HTTP request with a fake receive/send pair."""
    scope = {"type": "http", "path": path, "method": method, "scheme": "http",
             "client": ("127.0.0.1", 1234), "headers": [(b"host", b"localhost")]}
    sent = []

    async def default_receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    return app(scope, receive or default_receive, send), sent


def test_asgi_handler_timeout_answers_504(tmp_path):
    (tmp_path / "app" / "slow").mkdir(parents=True)
    (tmp_path / "app/slow/route.py").write_text(
        'import asyncio\nfrom pynext import Response, route\n@route("GET")\n'
        'async def get():\n    await asyncio.sleep(5)\n    return Response("too late")\n', encoding="utf-8")

    async def exercise():
        app = create_app(tmp_path, handler_timeout_ms=100,
                         observability=ObservabilityConfig(metrics=True))
        try:
            coro, sent = _asgi_request(app, path="/slow")
            await asyncio.wait_for(coro, 5)
            assert sent[0]["status"] == 504
            assert sent[0]["headers"] and sent[1]["body"] == b"Gateway Timeout"
            assert app.metrics.snapshot()["stage_timeouts"]["handler"] == 1
        finally:
            await app.shutdown()

    asyncio.run(exercise())


def test_asgi_body_timeout_answers_408(tmp_path):
    (tmp_path / "app" / "input").mkdir(parents=True)
    (tmp_path / "app/input/route.py").write_text(
        'from pynext import Response, route\n@route("POST")\n'
        'async def post(): return Response("ok")\n', encoding="utf-8")

    async def hanging_receive():
        await asyncio.sleep(30)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def exercise():
        app = create_app(tmp_path, body_timeout_ms=100,
                         observability=ObservabilityConfig(metrics=True))
        try:
            coro, sent = _asgi_request(app, path="/input", method="POST",
                                       receive=hanging_receive)
            await asyncio.wait_for(coro, 5)
            assert sent[0]["status"] == 408
            assert sent[1]["body"] == b"Request Timeout"
            assert app.metrics.snapshot()["stage_timeouts"]["body"] == 1
        finally:
            await app.shutdown()

    asyncio.run(exercise())


def test_asgi_response_timeout_counts_stage(tmp_path):
    (tmp_path / "app" / "ok").mkdir(parents=True)
    (tmp_path / "app/ok/route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("ok")\n', encoding="utf-8")

    async def exercise():
        app = create_app(tmp_path, response_timeout_ms=100,
                         observability=ObservabilityConfig(metrics=True))
        try:
            sent = []

            async def stalled_send(message):
                await asyncio.sleep(5)
                sent.append(message)

            scope = {"type": "http", "path": "/ok", "method": "GET", "scheme": "http",
                     "client": ("127.0.0.1", 1234), "headers": [(b"host", b"localhost")]}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            await asyncio.wait_for(app(scope, receive, stalled_send), 5)
            assert sent == []
            assert app.metrics.snapshot()["stage_timeouts"]["response"] == 1
        finally:
            await app.shutdown()

    asyncio.run(exercise())


def test_asgi_streaming_chunk_timeout_counts_stage(tmp_path):
    (tmp_path / "app" / "stream").mkdir(parents=True)
    (tmp_path / "app/stream/route.py").write_text(
        'import asyncio\nfrom pynext import StreamingResponse, route\n'
        '@route("GET")\nasync def get():\n'
        '    async def gen():\n        yield b"first"\n        await asyncio.sleep(5)\n        yield b"second"\n'
        '    return StreamingResponse(gen())\n', encoding="utf-8")

    async def exercise():
        app = create_app(tmp_path, response_timeout_ms=100,
                         observability=ObservabilityConfig(metrics=True))
        try:
            coro, sent = _asgi_request(app, path="/stream")
            await asyncio.wait_for(coro, 5)
            assert sent[0]["status"] == 200
            assert sent[1]["body"] == b"first" and sent[1]["more_body"] is True
            assert len(sent) == 2  # no terminating empty body: the stream aborted
            assert app.metrics.snapshot()["stage_timeouts"]["response"] == 1
        finally:
            await app.shutdown()

    asyncio.run(exercise())


def test_asgi_rate_limit_decision_timeout_fails_open(tmp_path, monkeypatch):
    # H-2 (S-P0-high) supersedes the C-P1-7 expectation that a slow
    # decision 503s: a decision that cannot be produced must fail OPEN
    # (a protection that cannot decide must not become a rejection
    # device), and is counted under rate_limit.fail_open — not under
    # stage_timeouts["admission"], which is reserved for decisions that
    # were produced but invalid (or a backend failure).
    (tmp_path / "app" / "input").mkdir(parents=True)
    (tmp_path / "app/input/route.py").write_text(
        'from pynext import Response, route\n@route("POST")\n'
        'async def post(): return Response("ok")\n', encoding="utf-8")
    release = threading.Event()

    class Slow(RateLimiter):
        def allow(self, key):
            release.wait(5)
            return True, None

    monkeypatch.setattr("pynext.app.ASGI_DECISION_TIMEOUT", 0.1)

    async def exercise():
        app = create_app(tmp_path, rate_limit=Slow(1, 60),
                         observability=ObservabilityConfig(metrics=True))
        try:
            coro, sent = _asgi_request(app, path="/input", method="POST")
            await asyncio.wait_for(coro, 5)
            assert sent[0]["status"] == 200
            assert app.metrics.snapshot()["rate_limit"]["fail_open"] == 1
            assert app.metrics.snapshot()["stage_timeouts"]["admission"] == 0
        finally:
            release.set()
            await app.wait_for_inflight(3)
            await app.shutdown()

    asyncio.run(exercise())


# -- native server (C-P1-5 / C-P1-7) ------------------------------------------


def _native_project(tmp_path):
    (tmp_path / "app" / "slow").mkdir(parents=True)
    (tmp_path / "app/slow/route.py").write_text(
        'import asyncio\nfrom pynext import Response, route\n@route("GET")\n'
        'async def get():\n    await asyncio.sleep(1)\n    return Response("slow")\n', encoding="utf-8")
    (tmp_path / "app" / "quick").mkdir(parents=True)
    (tmp_path / "app/quick/route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("quick")\n', encoding="utf-8")


def test_native_fixed_timeout_still_503s_waiter(tmp_path):
    _native_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=200)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                slow = asyncio.create_task(client.get("/slow"))
                await asyncio.sleep(0.1)
                response = await client.get("/quick")
                assert response.status_code == 503
                assert response.headers["retry-after"] == "1"
                assert (await slow).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()
            await app.isr.close()

    asyncio.run(run())


def test_native_adaptive_timeout_lets_waiter_survive(tmp_path):
    _native_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=200,
                              acquire_timeout_max_ms=5000)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                # Warm-up: one slow request seeds the hold-time EWMA
                # (~1000 ms), so the effective wait becomes 2 s.
                assert (await client.get("/slow")).status_code == 200
                slow = asyncio.create_task(client.get("/slow"))
                await asyncio.sleep(0.1)
                response = await client.get("/quick")
                assert response.status_code == 200
                assert (await slow).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()
            await app.isr.close()

    asyncio.run(run())


def test_native_slot_wait_is_fifo(tmp_path):
    (tmp_path / "app" / "block").mkdir(parents=True)
    (tmp_path / "app/block/route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("block")\n', encoding="utf-8")
    for name in ("a", "b", "c"):
        (tmp_path / "app" / name).mkdir(parents=True)
        (tmp_path / "app" / name / "route.py").write_text(
            'from pynext import Response, route\n@route("GET")\n'
            'async def get(): return Response("unset")\n', encoding="utf-8")
    order = []

    async def run():
        app = create_app(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            await release.wait()
            return Response("block")

        def make(name):
            async def handler():
                order.append(name)
                await asyncio.sleep(0.2)
                return Response(name)
            return handler

        app.entries["/block"]["GET"] = block
        for name in ("a", "b", "c"):
            app.entries[f"/{name}"]["GET"] = make(name)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, max_waiting=3, acquire_timeout_ms=5000)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                task = asyncio.create_task(client.get("/block"))
                await asyncio.wait_for(entered.wait(), 5)
                tasks = []
                for name in ("a", "b", "c"):
                    tasks.append(asyncio.create_task(client.get(f"/{name}")))
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.1)
                release.set()
                assert (await task).status_code == 200
                results = await asyncio.gather(*tasks)
                assert [response.text for response in results] == ["a", "b", "c"]
                assert order == ["a", "b", "c"]
        finally:
            release.set()
            await asyncio.to_thread(server.stop)
            await app.shutdown()
            await app.isr.close()

    asyncio.run(run())


def test_native_handler_timeout_is_504_not_503(tmp_path):
    _native_project(tmp_path)

    async def run():
        app = create_app(tmp_path, observability=ObservabilityConfig(metrics=True))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              handler_timeout_ms=200)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                response = await client.get("/slow")
                assert response.status_code == 504
                assert response.text == "Gateway Timeout"
            assert app.metrics.snapshot()["stage_timeouts"]["handler"] == 1
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()
            await app.isr.close()

    asyncio.run(run())


def test_start_native_rejects_inverted_adaptive_ceiling(tmp_path):
    _native_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            with pytest.raises(ValueError, match="acquire_timeout_max_ms must be at least acquire_timeout_ms"):
                start_native(app, asyncio.get_running_loop(), port=0,
                             acquire_timeout_ms=5000, acquire_timeout_max_ms=1000)
        finally:
            await app.shutdown()

    asyncio.run(run())
