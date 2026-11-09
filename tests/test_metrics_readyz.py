"""Phase 3 observability: /metrics endpoint, /readyz backend health,
span error recording, error counters, callsite-deduped error logs.

Dead-Redis cases use a closed localhost port (connection refused), so
they need no real Redis — but the Redis client library itself is still
required, hence the importorskip.
"""
import asyncio
import io
import json
import logging
import sys
import time
import types

import httpx
import pytest

from pathlib import Path

from pynext import (
    ObservabilityConfig,
    RateLimiter,
    RedisRateLimiter,
    SessionMiddleware,
    RedisSessionStore,
    Response,
    create_app,
)
from pynext.http import Request
from pynext.cache import ISRCache, SharedCacheUnavailable
from pynext.observability import (
    BackendHealth,
    DedupedErrorLogger,
    MetricsCollector,
    _reset_otel_cache,
    span,
)
from pynext.server import start_native

pytest.importorskip("redis")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"

# A Redis URL that nothing listens on: bind an ephemeral port and close
# it, so the port is free right now. A hardcoded "dead" port (e.g. 6399)
# is not safe — another test run on this machine may be using it.
def _dead_redis_url():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return f"redis://127.0.0.1:{sock.getsockname()[1]}/0"
    finally:
        sock.close()


DEAD_REDIS_URL = _dead_redis_url()


def _run(coro):
    return asyncio.run(coro)


def _write_error_app(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n'
    )
    error_dir = app_dir / "error"
    error_dir.mkdir()
    (error_dir / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ValueError("expected error")\n'
    )
    reset_dir = app_dir / "reset"
    reset_dir.mkdir()
    (reset_dir / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ConnectionResetError("client went away")\n'
    )
    return tmp_path


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


# -- /metrics ---------------------------------------------------------------

def test_metrics_endpoint_disabled_by_default():
    async def run():
        app = create_app(EXAMPLE)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/metrics")).status_code == 404
    _run(run())


def test_metrics_endpoint_serves_prometheus_text():
    async def run():
        app = create_app(EXAMPLE, observability=ObservabilityConfig(metrics_endpoint=True))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code == 200
            response = await client.get("/metrics")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
            assert 'pynext_requests_total{route="/",status="200"} 1' in response.text
            assert "pynext_errors_total" in response.text
            head = await client.head("/metrics")
            assert head.status_code == 200 and head.text == ""
            assert (await client.post("/metrics")).status_code == 405
    _run(run())


def test_metrics_endpoint_bypasses_rate_limit():
    async def run():
        app = create_app(EXAMPLE, rate_limit=RateLimiter(1, 60),
                         observability=ObservabilityConfig(metrics_endpoint=True))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/")).status_code == 429
            assert (await client.get("/metrics")).status_code == 200
    _run(run())


def test_metrics_endpoint_with_metrics_disabled_reports_disabled():
    async def run():
        app = create_app(EXAMPLE, observability=ObservabilityConfig(metrics=False, metrics_endpoint=True))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/metrics")
            assert response.status_code == 200
            assert response.text.strip() == "# PyNext metrics disabled"
    _run(run())


def test_metrics_endpoint_native():
    async def run():
        app = create_app(EXAMPLE, observability=ObservabilityConfig(metrics_endpoint=True))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                         trust_env=False) as client:
                assert (await client.get("/")).status_code == 200
                response = await client.get("/metrics")
                assert response.status_code == 200
                assert "pynext_requests_total" in response.text
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    _run(run())


def test_metrics_endpoint_disabled_native_is_404():
    async def run():
        app = create_app(EXAMPLE)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                         trust_env=False) as client:
                assert (await client.get("/metrics")).status_code == 404
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    _run(run())


# -- error counter ------------------------------------------------------------

def test_error_counter_counts_failures_by_type(tmp_path):
    _write_error_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/error")).status_code == 500
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/error")).status_code == 500
        return app.metrics.snapshot(), app.metrics.prometheus_text()
    snap, text = _run(run())
    assert snap["errors"]["total"] == 2
    assert snap["errors"]["by_type"] == {"/error": {"ValueError": 2}}
    assert 'pynext_errors_total{route="/error",error_type="ValueError",digest=""} 2' in text


def test_prometheus_label_values_are_escaped():
    collector = MetricsCollector()
    collector.observe_request('/a"b\\c', "GET", 200, 1.0)
    collector.observe_error('/a"b\\c', "ValueError")
    text = collector.prometheus_text()
    assert 'pynext_requests_total{route="/a\\"b\\\\c",status="200"} 1' in text
    assert 'pynext_errors_total{route="/a\\"b\\\\c",error_type="ValueError",digest=""} 1' in text


def test_degraded_counter_snapshot_and_prometheus():
    collector = MetricsCollector()
    collector.observe_degraded("isr-cache", "fail_closed", "refused")
    collector.observe_degraded("isr-cache", "fail_closed", "refused")
    collector.observe_degraded("rate-limit", "fail_open", "admitted")
    snap = collector.snapshot()
    assert snap["degraded"] == {"isr-cache": {"fail_closed": {"refused": 2}},
                                "rate-limit": {"fail_open": {"admitted": 1}}}
    text = collector.prometheus_text()
    assert 'pynext_degraded_total{component="isr-cache",policy="fail_closed",outcome="refused"} 2' in text
    assert 'pynext_degraded_total{component="rate-limit",policy="fail_open",outcome="admitted"} 1' in text


# -- span error recording -------------------------------------------------------

class _FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.exceptions = []
        self.status = None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def record_exception(self, exc):
        self.exceptions.append(exc)

    def set_status(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeStatus:
    def __init__(self, code, description=None):
        self.status_code = code
        self.description = description


class _FakeStatusCode:
    ERROR = "ERROR"


def _install_fake_otel(monkeypatch):
    recorded = []

    class FakeTracer:
        def start_as_current_span(self, name, context=None):
            span_obj = _FakeSpan(name)
            recorded.append(span_obj)
            return span_obj

    fake_trace = types.ModuleType("opentelemetry.trace")
    fake_trace.get_tracer = lambda name: FakeTracer()
    fake_trace.Status = _FakeStatus
    fake_trace.StatusCode = _FakeStatusCode
    fake_otel = types.ModuleType("opentelemetry")
    fake_propagate = types.ModuleType("opentelemetry.propagate")
    fake_propagate.extract = lambda carrier: None
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)
    monkeypatch.setitem(sys.modules, "opentelemetry.propagate", fake_propagate)
    _reset_otel_cache()
    return recorded


def test_span_records_error_with_fake_opentelemetry(tmp_path, monkeypatch):
    _write_error_app(tmp_path)
    recorded = _install_fake_otel(monkeypatch)
    try:
        async def run():
            app = create_app(tmp_path, observability=ObservabilityConfig(tracing=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/error")).status_code == 500
        _run(run())
        request_span = next(item for item in recorded if item.name == "pynext.request")
        assert request_span.attributes["error.type"] == "ValueError"
        assert request_span.attributes["http.status_code"] == 500
        assert len(request_span.exceptions) == 1
        assert isinstance(request_span.exceptions[0], ValueError)
        assert request_span.status is not None
        assert request_span.status.status_code == "ERROR"
        assert request_span.status.description == "ValueError"
    finally:
        _reset_otel_cache()


def test_span_does_not_record_cancellation(monkeypatch):
    recorded = _install_fake_otel(monkeypatch)
    try:
        with pytest.raises(asyncio.CancelledError):
            with span("pynext.request", enabled=True):
                raise asyncio.CancelledError()
        assert recorded[0].attributes.get("error.type") is None
        assert recorded[0].exceptions == []
    finally:
        _reset_otel_cache()


def test_span_records_error_with_real_opentelemetry(tmp_path, monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    _write_error_app(tmp_path)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda name: provider.get_tracer(name))
    _reset_otel_cache()
    try:
        async def run():
            app = create_app(tmp_path, observability=ObservabilityConfig(tracing=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/error")).status_code == 500
        _run(run())
        spans = exporter.get_finished_spans()
        request_span = next(item for item in spans if item.name == "pynext.request")
        assert request_span.attributes["error.type"] == "ValueError"
        assert request_span.status.status_code == StatusCode.ERROR
        assert any(event.name == "exception" for event in request_span.events)
    finally:
        _reset_otel_cache()


# -- callsite-deduped error logging --------------------------------------------

def _boom():
    raise ValueError("dedup boom")


def _boom_elsewhere():
    raise ValueError("dedup boom")


def _capture_logger(name):
    logger = logging.getLogger(name)
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, handler


def test_dedup_logger_groups_repeats_by_callsite():
    logger, handler = _capture_logger("pynext.test-dedup")
    dedup = DedupedErrorLogger(logger, window_s=60.0)
    try:
        for _ in range(3):
            try:
                _boom()
            except ValueError as exc:
                dedup.log_exception(exc, route="/error")
        try:
            _boom_elsewhere()
        except ValueError as exc:
            dedup.log_exception(exc, route="/error")
        error_records = [r for r in handler.records if r.levelno == logging.ERROR]
        assert len(error_records) == 2  # same callsite 3x -> once; other callsite -> once
        assert dedup.suppressed_counts  # the two swallowed repeats are counted
        assert sum(dedup.suppressed_counts.values()) == 2
    finally:
        logger.removeHandler(handler)


def test_dedup_logger_window_expiry_relogs_with_count():
    logger, handler = _capture_logger("pynext.test-dedup-window")
    dedup = DedupedErrorLogger(logger, window_s=0.05)
    try:
        for _ in range(2):
            try:
                _boom()
            except ValueError as exc:
                dedup.log_exception(exc, route="/error")
        time.sleep(0.07)
        try:
            _boom()
        except ValueError as exc:
            dedup.log_exception(exc, route="/error")
        error_records = [r for r in handler.records if r.levelno == logging.ERROR]
        assert len(error_records) == 2
        assert error_records[1].dedup_suppressed == 1
    finally:
        logger.removeHandler(handler)


def test_dedup_logger_window_zero_disables_grouping():
    logger, handler = _capture_logger("pynext.test-dedup-zero")
    dedup = DedupedErrorLogger(logger, window_s=0)
    try:
        for _ in range(2):
            try:
                _boom()
            except ValueError as exc:
                dedup.log_exception(exc, route="/error")
        assert len([r for r in handler.records if r.levelno == logging.ERROR]) == 2
    finally:
        logger.removeHandler(handler)


def test_known_errors_log_at_info_not_exception(tmp_path):
    _write_error_app(tmp_path)
    logger = logging.getLogger("pynext")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        async def run():
            app = create_app(tmp_path)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/reset")).status_code == 500
        _run(run())
    finally:
        logger.removeHandler(handler)
    failures = [r for r in handler.records if r.getMessage() == "Request failed"]
    assert failures == []
    known = [r for r in handler.records if r.getMessage() == "Request failed (known error)"]
    assert len(known) == 1
    assert known[0].levelno == logging.INFO
    assert known[0].error_type == "ConnectionResetError"


def test_repeated_500_logs_once_within_window(tmp_path):
    _write_error_app(tmp_path)
    logger = logging.getLogger("pynext")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        async def run():
            app = create_app(tmp_path)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/error")).status_code == 500
                assert (await client.get("/error")).status_code == 500
        _run(run())
    finally:
        logger.removeHandler(handler)
    failures = [r for r in handler.records
                if r.getMessage() == "Request failed" and r.levelno == logging.ERROR]
    assert len(failures) == 1
    assert failures[0].error_type == "ValueError"
    assert failures[0].route == "/error"


# -- /readyz backend health ----------------------------------------------------

def _request(app, method, path):
    return Request(method, path, b"", (), b"", _app=app)


def test_readyz_reflects_backend_health(tmp_path):
    _write_error_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            assert (await app.dispatch(_request(app, "GET", "/readyz"))).status == 503
            await app.startup()
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 200 and response.body == b"ready"
            app.backend_health.report("isr-cache", healthy=False, policy="fail_closed")
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 503
            assert b"unhealthy: isr-cache" in response.body
            app.backend_health.report("isr-cache", healthy=False, policy="fail_open")
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 200
            assert response.body == b"ready (degraded: isr-cache)"
            app.backend_health.report("isr-cache", healthy=True, policy="fail_closed")
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 200 and response.body == b"ready"
            app.begin_drain()
            assert (await app.dispatch(_request(app, "GET", "/readyz"))).status == 503
        finally:
            await app.shutdown()
    _run(run())


def test_backend_health_forgets_stale_reports():
    now = [1000.0]
    health = BackendHealth(ttl_s=10.0, clock=lambda: now[0])
    health.report("isr-cache", healthy=False, policy="fail_closed")
    assert health.unhealthy_fail_closed() == ["isr-cache"]
    now[0] += 11.0
    assert health.snapshot() == {}
    assert health.unhealthy_fail_closed() == []
    assert health.degraded() == []


def test_readyz_dead_redis_isr_fail_closed():
    async def run():
        app = create_app(EXAMPLE, cache_backend="redis", cache_url=DEAD_REDIS_URL,
                         cache_failure_policy="fail_closed")
        try:
            await app.startup()
            with pytest.raises(SharedCacheUnavailable):
                await app.isr._shared_entry("probe")
            snap = app.backend_health.snapshot()
            assert snap["isr-cache"]["healthy"] is False
            assert snap["isr-cache"]["policy"] == "fail_closed"
            assert app.metrics.snapshot()["degraded"] == {"isr-cache": {"fail_closed": {"refused": 1}}}
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 503
            assert b"unhealthy: isr-cache" in response.body
        finally:
            await app.shutdown()
    _run(run())


def test_readyz_dead_redis_isr_fail_open_is_degraded_not_down():
    async def run():
        app = create_app(EXAMPLE, cache_backend="redis", cache_url=DEAD_REDIS_URL,
                         cache_failure_policy="fail_open")
        try:
            await app.startup()
            # A dead shared store under fail_open: no exception escapes,
            # but the touch is still reported as an unhealthy backend.
            await app.isr._shared_entry("probe")
            snap = app.backend_health.snapshot()
            assert snap["isr-cache"]["healthy"] is False
            assert snap["isr-cache"]["policy"] == "fail_open"
            assert app.metrics.snapshot()["degraded"] == {"isr-cache": {"fail_open": {"degraded": 1}}}
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 200
            assert response.body == b"ready (degraded: isr-cache)"
        finally:
            await app.shutdown()
    _run(run())


def test_readyz_dead_redis_rate_limit_fail_closed():
    async def run():
        limiter = RedisRateLimiter(10, 60, url=DEAD_REDIS_URL, namespace="metrics-test")
        app = create_app(EXAMPLE, rate_limit=limiter)
        try:
            await app.startup()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/")).status_code == 503
                response = await client.get("/readyz")
                assert response.status_code == 503
                assert "unhealthy: rate-limit" in response.text
        finally:
            limiter.close()
            await app.shutdown()
    _run(run())


def test_readyz_dead_redis_rate_limit_fail_open_is_degraded():
    async def run():
        limiter = RedisRateLimiter(10, 60, url=DEAD_REDIS_URL, namespace="metrics-test-open",
                                   failure_policy="fail_open")
        app = create_app(EXAMPLE, rate_limit=limiter)
        try:
            await app.startup()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/")).status_code == 200
                response = await client.get("/readyz")
                assert response.status_code == 200
                assert response.text == "ready (degraded: rate-limit)"
        finally:
            limiter.close()
            await app.shutdown()
    _run(run())


def test_readyz_dead_redis_sessions_fail_closed():
    async def run():
        app = create_app(EXAMPLE)
        store = RedisSessionStore(url=DEAD_REDIS_URL)
        middleware = SessionMiddleware(store, purge_interval=None)
        try:
            await app.startup()
            token = "a" * 43
            request = Request("GET", "/", b"",
                              ((b"cookie", f"pynext_session={token}".encode("ascii")),),
                              b"", _app=app)

            async def call_next(_request):
                return Response("ok")

            response = await middleware(request, call_next)
            assert response.status == 503
            snap = app.backend_health.snapshot()
            assert snap["sessions"]["healthy"] is False
            assert snap["sessions"]["policy"] == "fail_closed"
            probe = await app.dispatch(_request(app, "GET", "/readyz"))
            assert probe.status == 503
            assert b"unhealthy: sessions" in probe.body
        finally:
            await app.shutdown()
            store.close()
    _run(run())


# -- CLI / env -------------------------------------------------------------------

def test_cli_metrics_endpoint_flag_reaches_create_app(tmp_path, monkeypatch):
    import sys
    from pynext import cli
    seen = {}

    def fake_create_app(path, production=False, **kwargs):
        seen["create_app"] = kwargs
        return object()

    async def fake_serve(app, host, port, max_concurrent, acquire_timeout_ms,
                         body_capacity, body_timeout_ms, **kwargs):
        seen["serve"] = kwargs

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "serve", fake_serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path), "--metrics-endpoint"])
    cli.main()
    assert seen["create_app"]["observability"].metrics_endpoint is True


def test_observability_config_from_env_metrics_endpoint(monkeypatch):
    monkeypatch.setenv("PYNEXT_METRICS_ENDPOINT", "1")
    monkeypatch.setenv("PYNEXT_LOG_DEDUP_WINDOW_S", "5")
    monkeypatch.setenv("PYNEXT_BACKEND_HEALTH_TTL_S", "42")
    config = ObservabilityConfig.from_env()
    assert config.metrics_endpoint is True
    assert config.log_dedup_window_s == 5.0
    assert config.backend_health_ttl_s == 42.0
    monkeypatch.setenv("PYNEXT_LOG_DEDUP_WINDOW_S", "not-a-number")
    assert ObservabilityConfig.from_env().log_dedup_window_s == 60.0
    monkeypatch.delenv("PYNEXT_METRICS_ENDPOINT")
    assert ObservabilityConfig.from_env().metrics_endpoint is False
