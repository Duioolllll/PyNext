"""Observability (finding #45, spec v0.2 §12): structured logs, metrics, tracing."""
import asyncio
import io
import json
import logging
import sys
import types

import httpx
import pytest

from pathlib import Path

from pynext import create_app, ObservabilityConfig
from pynext.observability import (
    JsonFormatter,
    MetricsCollector,
    _reset_otel_cache,
    begin_request_context,
    is_tracing_available,
    parse_traceparent,
    span,
)
from pynext.server import start_native

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"


def _run(coro):
    return asyncio.run(coro)


# -- structured logs ------------------------------------------------------

def test_json_formatter_emits_one_object_per_line_and_drops_sensitive_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("pynext.test-observability")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("request", extra={
            "route": "/blog/[slug]", "status": 200, "latency_ms": 1.5,
            "authorization": "Bearer secret", "cookie": "session=abc",
            "password": "hunter2", "body": "sensitive payload",
        })
        line = stream.getvalue().strip()
    finally:
        logger.removeHandler(handler)
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert record["message"] == "request"
    assert record["route"] == "/blog/[slug]"
    assert record["status"] == 200
    assert record["latency_ms"] == 1.5
    assert "timestamp" in record and record["timestamp"].endswith("Z")
    for forbidden in ("authorization", "cookie", "password", "body", "secret", "Bearer"):
        assert forbidden not in line


def test_parse_traceparent_valid_and_invalid():
    assert parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") == (
        "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", "01")
    assert parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01") is None
    assert parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") is None
    assert parse_traceparent("not-a-traceparent") is None
    assert parse_traceparent(None) is None


def test_begin_request_context_echoes_valid_request_id_and_parses_traceparent():
    headers = [(b"x-request-id", b"req-123"),
               (b"traceparent", b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")]
    request_id, reset = begin_request_context(headers)
    try:
        from pynext.observability import get_request_id, get_trace_id
        assert request_id == "req-123"
        assert get_request_id() == "req-123"
        assert get_trace_id() == "4bf92f3577b34da6a3ce929d0e0e4736"
    finally:
        reset()
    from pynext.observability import get_request_id
    assert get_request_id() is None


def test_request_log_line_has_no_sensitive_values(tmp_path):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("pynext")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        async def run():
            app = create_app(EXAMPLE)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/api/users",
                    headers={"authorization": "Bearer top-secret",
                             "cookie": "session=super-secret",
                             "x-request-id": "log-check-1"})
                assert response.status_code == 200
                assert response.headers["x-request-id"] == "log-check-1"
        _run(run())
    finally:
        logger.removeHandler(handler)
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    access = [line for line in lines if line.get("message") == "request"]
    assert access, "expected a structured access log line"
    entry = access[-1]
    assert entry["request_id"] == "log-check-1"
    assert entry["route"] == "/api/users"
    assert entry["status"] == 200
    assert entry["latency_ms"] >= 0
    assert "top-secret" not in stream.getvalue()
    assert "super-secret" not in stream.getvalue()


def test_request_id_generated_when_absent_and_unique():
    async def run():
        app = create_app(EXAMPLE)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/")
            second = await client.get("/")
            assert first.headers["x-request-id"]
            assert second.headers["x-request-id"]
            assert first.headers["x-request-id"] != second.headers["x-request-id"]
    _run(run())


# -- metrics ---------------------------------------------------------------

def test_metrics_after_known_asgi_requests_use_route_patterns():
    async def run():
        app = create_app(EXAMPLE)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/blog/hello")).status_code == 200
            assert (await client.get("/blog/other-slug")).status_code == 200
            assert (await client.get("/missing/path")).status_code == 404
            assert (await client.get("/api/users")).status_code == 200
        return app.metrics.snapshot(), app.metrics.prometheus_text()
    snap, text = _run(run())
    assert snap["enabled"] is True
    assert snap["requests_total"] == 5
    routes = snap["routes"]
    # Labels use the route pattern, never the raw slug value.
    assert routes["/"]["requests"] == 1
    assert routes["/blog/[slug]"]["requests"] == 2
    assert routes["/blog/[slug]"]["status"] == {"200": 2}
    assert routes["__not_found__"]["status"] == {"404": 1}
    assert routes["/api/users"]["status"] == {"200": 1}
    assert "/blog/hello" not in routes and "/blog/other-slug" not in routes
    latency = routes["/"]["latency_ms"]
    assert latency["p50"] is not None and latency["p95"] is not None and latency["p99"] is not None
    assert routes["/"]["response_bytes"] > 0
    assert snap["python_ms"]["count"] == 5
    assert snap["rust_render_ms"]["count"] >= 3  # page renders only
    assert snap["island_bundles_total_bytes"] >= 0
    assert "pynext_requests_total" in text


def test_metrics_disabled_snapshot_reports_disabled():
    app = create_app(EXAMPLE, observability=ObservabilityConfig(metrics=False))
    assert app.metrics.snapshot() == {"enabled": False}
    assert app.metrics.prometheus_text().strip() == "# PyNext metrics disabled"


def test_metrics_prometheus_text():
    collector = MetricsCollector()
    collector.observe_request("/blog/[slug]", "GET", 200, 2.5, response_bytes=128)
    collector.observe_request("/blog/[slug]", "GET", 404, 1.0, response_bytes=9)
    collector.observe_cache("hit")
    collector.observe_cache("miss")
    collector.observe_regeneration("success")
    collector.observe_worker_start()
    text = collector.prometheus_text()
    assert 'pynext_requests_total{route="/blog/[slug]",status="200"} 1' in text
    assert 'pynext_requests_total{route="/blog/[slug]",status="404"} 1' in text
    assert 'pynext_cache_total{outcome="hit"} 1' in text
    assert 'pynext_cache_total{outcome="miss"} 1' in text
    assert 'pynext_regeneration_total{outcome="success"} 1' in text
    assert 'pynext_worker_total{outcome="starts"} 1' in text
    snap = collector.snapshot()
    assert snap["routes"]["/blog/[slug]"]["latency_ms"]["p50"] == 1.0
    assert snap["routes"]["/blog/[slug]"]["latency_ms"]["p99"] == 2.5


def test_native_server_records_queue_wait_and_worker_start():
    async def run():
        app = create_app(EXAMPLE)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            # trust_env=False: this VM's no_proxy contains bracketed IPv6
            # entries that httpx's proxy parser rejects (pre-existing env
            # issue; the untouched test_axum_end_to_end fails the same way).
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                         trust_env=False) as client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/api/users")).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
        return app.metrics.snapshot()
    snap = _run(run())
    assert snap["requests_total"] == 2
    assert snap["queue"]["wait_samples"] == 2
    assert snap["queue"]["waits_ms"]["p50"] is not None
    assert snap["queue"]["waits_ms"]["p50"] >= 0
    assert snap["workers"]["starts"] == 1


def test_isr_cache_metrics(tmp_path):
    directory = tmp_path / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'async def Page(params):\n'
        '    return h("p", {}, params["slug"])\n'
    )
    from pynext.build import write_manifest
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/blog/alpha")).status_code == 200  # miss + render
            assert (await client.get("/blog/alpha")).status_code == 200  # fresh hit
        return app.metrics.snapshot()
    snap = _run(run())
    assert snap["cache"]["miss"] == 1
    assert snap["cache"]["hit"] == 1
    assert snap["regeneration"]["success"] == 1


def test_cli_observability_flags_reach_create_app(tmp_path, monkeypatch):
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
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--no-metrics", "--tracing"])
    cli.main()
    config = seen["create_app"]["observability"]
    assert config.metrics is False
    assert config.tracing is True
    assert seen["serve"]["observability"] is config


def test_cli_without_observability_flags_keeps_old_call_shape(tmp_path, monkeypatch):
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
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path)])
    cli.main()
    # No observability kwarg leaks through when no flags are given; the
    # other kwargs are the merged defaults from the cache (#42) and
    # rate-limit (#49) options, plus the graceful-shutdown default (B6).
    # Since the P0 High fix the default rate limit is on (300/min), so
    # the CLI builds a limiter instead of passing None.
    from pynext import RateLimiter
    assert seen["create_app"]["cache_backend"] == "memory"
    assert seen["create_app"]["cache_path"] is None
    limiter = seen["create_app"]["rate_limit"]
    assert isinstance(limiter, RateLimiter) and limiter.limit == 300
    assert set(seen["create_app"]) == {"cache_backend", "cache_path", "rate_limit"}
    assert seen["serve"] == {"shutdown_timeout": 30}


def test_observability_config_from_env(monkeypatch):
    monkeypatch.setenv("PYNEXT_STRUCTURED_LOGS", "0")
    monkeypatch.setenv("PYNEXT_METRICS", "false")
    monkeypatch.setenv("PYNEXT_TRACING", "1")
    config = ObservabilityConfig.from_env()
    assert config.structured_logs is False
    assert config.metrics is False
    assert config.tracing is True
    monkeypatch.delenv("PYNEXT_STRUCTURED_LOGS")
    assert ObservabilityConfig.from_env().structured_logs is True


# -- tracing ---------------------------------------------------------------

def test_tracing_is_noop_without_opentelemetry(monkeypatch):
    import importlib
    original = importlib.import_module

    def unavailable(name, *args, **kwargs):
        if name == "opentelemetry.trace":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    _reset_otel_cache()
    try:
        assert is_tracing_available() is False
        with span("pynext.request", {"http.method": "GET"}, enabled=True) as active:
            active.set_attribute("http.status_code", 200)
    finally:
        _reset_otel_cache()


def test_request_without_traceparent_clears_inherited_otel_context():
    from pynext.observability import _otel_context_var

    outer = object()
    token = _otel_context_var.set(outer)
    try:
        _, reset = begin_request_context([])
        try:
            assert _otel_context_var.get() is None
        finally:
            reset()
        assert _otel_context_var.get() is outer
    finally:
        _otel_context_var.reset(token)


def test_span_creation_with_fake_opentelemetry(monkeypatch):
    recorded = []

    class FakeSpan:
        def __init__(self, name):
            self.name = name
            self.attributes = {}

        def set_attribute(self, key, value):
            self.attributes[key] = value

        def __enter__(self):
            recorded.append(self)
            return self

        def __exit__(self, *exc):
            return False

    class FakeTracer:
        def start_as_current_span(self, name, context=None):
            return FakeSpan(name)

    fake_trace = types.ModuleType("opentelemetry.trace")
    fake_trace.get_tracer = lambda name: FakeTracer()
    fake_otel = types.ModuleType("opentelemetry")
    fake_propagate = types.ModuleType("opentelemetry.propagate")
    fake_propagate.extract = lambda carrier: None
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)
    monkeypatch.setitem(sys.modules, "opentelemetry.propagate", fake_propagate)
    _reset_otel_cache()
    try:
        assert is_tracing_available() is True

        async def run():
            app = create_app(EXAMPLE, observability=ObservabilityConfig(tracing=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/")).status_code == 200
        _run(run())
        names = [item.name for item in recorded]
        assert "pynext.request" in names
        assert "pynext.render" in names
        request_span = next(item for item in recorded if item.name == "pynext.request")
        assert request_span.attributes["http.method"] == "GET"
        assert request_span.attributes["http.status_code"] == 200
        assert request_span.attributes["http.route"] == "/"
    finally:
        _reset_otel_cache()


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_real_opentelemetry_preserves_parentage_and_handler_errors(tmp_path, monkeypatch, mode):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda name: provider.get_tracer(name))
    _reset_otel_cache()
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

    async def run():
        app = create_app(tmp_path, observability=ObservabilityConfig(tracing=True))
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                response = await client.get("/", headers={
                    "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
                })
                assert response.status_code == 200
                assert (await client.get("/error")).status_code == 500
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    try:
        _run(run())
        spans = exporter.get_finished_spans()
        request_span = next(item for item in spans if item.name == "pynext.request"
                            and item.context.trace_id == int("0123456789abcdef0123456789abcdef", 16))
        render_span = next(item for item in spans if item.name == "pynext.render"
                           and item.context.trace_id == request_span.context.trace_id)
        assert request_span.parent.span_id == int("0123456789abcdef", 16)
        assert render_span.parent.span_id == request_span.context.span_id
        assert any(item.name == "pynext.request" and item.attributes.get("http.status_code") == 500
                   for item in spans)
    finally:
        provider.shutdown()
        _reset_otel_cache()
