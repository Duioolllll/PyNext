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

from pynext import RequestInsights, create_app, ObservabilityConfig
from pynext.observability import (
    JsonFormatter,
    MetricsCollector,
    RequestInsights,
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


# -- saturation metrics (C-P1-6) ------------------------------------------

def test_slot_wait_histogram_buckets():
    collector = MetricsCollector()
    for wait_ms in (0.5, 3.0, 30.0, 300.0, 3000.0, 20000.0):
        collector.observe_request("/x", "GET", 200, 1.0, queue_wait_ms=wait_ms)
    hist = collector.snapshot()["queue"]["wait_histogram_ms"]
    assert hist["count"] == 6
    assert hist["sum"] == round(0.5 + 3.0 + 30.0 + 300.0 + 3000.0 + 20000.0, 3)
    buckets = {bucket["le"]: bucket["count"] for bucket in hist["buckets"]}
    assert buckets[1.0] == 1      # 0.5
    assert buckets[5.0] == 2      # +3.0
    assert buckets[50.0] == 3     # +30.0
    assert buckets[500.0] == 4    # +300.0
    assert buckets[5000.0] == 5   # +3000.0
    assert buckets["+Inf"] == 6   # +20000.0
    text = collector.prometheus_text()
    assert 'pynext_queue_wait_ms_bucket{le="1.0"} 1' in text
    assert 'pynext_queue_wait_ms_bucket{le="+Inf"} 6' in text
    assert "pynext_queue_wait_ms_count 6" in text


def test_queue_depth_histogram_samples_entry_depth():
    collector = MetricsCollector()
    collector.observe_queue_enter()  # depth 1
    collector.observe_queue_enter()  # depth 2
    collector.observe_queue_enter()  # depth 3
    collector.observe_queue_exit()
    collector.observe_queue_enter()  # depth 3 again
    snap = collector.snapshot()
    assert snap["queue"]["current"] == 3
    assert snap["queue"]["max"] == 3
    hist = snap["queue"]["depth_histogram"]
    assert hist["count"] == 4
    buckets = {bucket["le"]: bucket["count"] for bucket in hist["buckets"]}
    assert buckets[0] == 0
    assert buckets[1] == 1
    assert buckets[2] == 2
    assert buckets[4] == 4
    assert buckets["+Inf"] == 4
    assert 'pynext_python_execution_queue_depth_bucket{le="4"} 4' in collector.prometheus_text()


def test_sync_workers_gauge_tracks_active_capacity_and_max():
    collector = MetricsCollector()
    collector.observe_sync_workers(5, 32)
    snap = collector.snapshot()["sync_workers"]
    assert snap == {"active": 5, "capacity": 32, "max_active": 5}
    collector.observe_sync_workers(31, 32)
    collector.observe_sync_workers(2)  # capacity remembered, max kept
    snap = collector.snapshot()["sync_workers"]
    assert snap == {"active": 2, "capacity": 32, "max_active": 31}
    text = collector.prometheus_text()
    assert "pynext_sync_workers_active 2" in text
    assert "pynext_sync_workers_capacity 32" in text
    assert "pynext_sync_workers_max_active 31" in text
    disabled = MetricsCollector(enabled=False)
    disabled.observe_sync_workers(9, 32)
    assert disabled.snapshot() == {"enabled": False}


def test_native_server_samples_sync_workers_and_depth_histogram():
    async def run():
        app = create_app(EXAMPLE)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5,
                                         trust_env=False) as client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/api/users")).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
        return app.metrics.snapshot()
    snap = _run(run())
    # One depth sample per bridge entry, one wait sample per dispatch.
    assert snap["queue"]["depth_histogram"]["count"] == 2
    assert snap["queue"]["wait_histogram_ms"]["count"] == 2
    assert snap["queue"]["current"] == 0
    assert snap["sync_workers"]["capacity"] == 32
    assert snap["sync_workers"]["active"] == 0
    assert snap["sync_workers"]["max_active"] >= 0


# -- secret scrubbing (S-P1-8 / S-P1-29) -----------------------------------

def test_scrub_post_parameters_masks_sensitive_keys():
    from pynext.observability import SECRET_MASK, scrub_post_parameters
    params = {
        "username": "bob",
        "api_key": "sk-live-123",
        "API_TOKEN": "tok-456",
        "password": "hunter2",
        "x-auth-token": "tok-789",
        "HTTP_COOKIE": "session=abc",
        "signature": "sig-1",
        "nested": {"client_secret": "shh", "keep": "yes"},
    }
    scrubbed = scrub_post_parameters(params)
    assert scrubbed["username"] == "bob"
    assert scrubbed["nested"]["keep"] == "yes"
    for key in ("api_key", "API_TOKEN", "password", "x-auth-token",
                "HTTP_COOKIE", "signature"):
        assert scrubbed[key] == SECRET_MASK, key
    assert scrubbed["nested"]["client_secret"] == SECRET_MASK
    # The input mapping is not mutated.
    assert params["api_key"] == "sk-live-123"
    assert params["nested"]["client_secret"] == "shh"
    # Explicitly declared names are masked too, and non-mappings pass through.
    assert scrub_post_parameters(
        {"one_time_code": "123456"}, extra_sensitive=("one_time_code",)
    )["one_time_code"] == SECRET_MASK
    assert scrub_post_parameters(None) is None


def test_sensitive_decorators_and_scrub_locals():
    from pynext.observability import (
        SECRET_MASK, scrub_locals, sensitive_post_parameters, sensitive_variables,
    )

    @sensitive_variables("db_password")
    @sensitive_post_parameters("one_time_code")
    def handler(db_password, one_time_code, username):
        return db_password, one_time_code, username

    assert "db_password" in handler.__sensitive_variables__
    assert "one_time_code" in handler.__sensitive_post_parameters__
    scrubbed = scrub_locals(handler, {
        "db_password": "hunter2", "api_key": "sk-1",
        "username": "bob", "one_time_code": "123456",
    })
    assert scrubbed["db_password"] == SECRET_MASK
    assert scrubbed["api_key"] == SECRET_MASK  # pattern match, no decorator needed
    assert scrubbed["username"] == "bob"
    # A parameter declared sensitive for POST is a local like any other
    # only when the decorator says so; scrub_locals covers the
    # @sensitive_variables names, the POST list is honoured by
    # scrub_post_parameters instead.
    assert scrubbed["one_time_code"] == "123456"
    assert scrub_locals(None, {"token": "t", "ok": 1}) == {"token": SECRET_MASK, "ok": 1}


def test_scrub_text_masks_key_value_and_bearer_credentials():
    from pynext.observability import SECRET_MASK, scrub_text
    assert scrub_text("api_key=sk-live-123") == f"api_key={SECRET_MASK}"
    assert scrub_text('{"token": "tok-456", "n": 1}') == (
        f'{{"token": "{SECRET_MASK}", "n": 1}}')
    assert scrub_text("Authorization: Bearer tok-xyz-999") == (
        f"Authorization: {SECRET_MASK}")
    assert scrub_text("got Bearer tok-xyz-999 back") == f"got Bearer {SECRET_MASK} back"
    assert scrub_text("password: hunter2; user=bob") == (
        f"password: {SECRET_MASK}; user=bob")
    assert scrub_text("nothing secret here") == "nothing secret here"
    assert scrub_text(42) == 42


def test_json_formatter_scrubs_exception_body_and_nested_extras():
    from pynext.observability import SECRET_MASK
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("pynext.test-observability-scrub")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        try:
            raise ValueError("login failed: password=hunter2 for user bob; "
                             "upstream said Authorization: Bearer tok-xyz-999")
        except ValueError:
            logger.exception(
                "request failed: api_key=sk-live-abc123",
                extra={"params": {"api_key": "zzz", "q": "hello"},
                       "route": "/login"})
        line = stream.getvalue().strip()
    finally:
        logger.removeHandler(handler)
    record = json.loads(line)
    assert record["route"] == "/login"
    assert record["params"] == {"api_key": SECRET_MASK, "q": "hello"}
    assert record["message"] == f"request failed: api_key={SECRET_MASK}"
    assert "hunter2" not in line
    assert "tok-xyz-999" not in line
    assert "sk-live-abc123" not in line
    assert "zzz" not in line
    assert SECRET_MASK in line
    assert "bob" in line  # non-secret user input is preserved
    assert "ValueError: login failed" in record["exception"]


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
                                      "--workers", "1",
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
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--workers", "1"])
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


# -- request insights (P2-3) -------------------------------------------------

def test_request_insights_disabled_is_noop():
    insights = RequestInsights(enabled=False)
    insights.observe(request_id="r1", method="GET", route="/", status=500,
                     latency_ms=100.0, error_type="ValueError")
    assert insights.snapshot() == {"enabled": False}


def test_request_insights_records_slow_requests_over_fixed_threshold():
    insights = RequestInsights(slow_threshold_ms=10.0)
    insights.observe(request_id="fast", method="GET", route="/", status=200, latency_ms=1.0)
    insights.observe(request_id="at-threshold", method="GET", route="/", status=200, latency_ms=10.0)
    insights.observe(request_id="slow", method="GET", route="/blog/[slug]", status=200,
                     latency_ms=25.5, python_ms=20.0, rust_ms=5.5, queue_wait_ms=0.25)
    snap = insights.snapshot()
    assert snap["enabled"] is True
    assert snap["max_entries"] == 100
    assert snap["observed"] == 3
    assert snap["recorded"] == 2
    assert snap["slow_threshold_ms"] == 10.0
    assert [entry["request_id"] for entry in snap["requests"]] == ["at-threshold", "slow"]
    entry = snap["requests"][1]
    assert entry["method"] == "GET"
    assert entry["route"] == "/blog/[slug]"
    assert entry["status"] == 200
    assert entry["latency_ms"] == 25.5
    assert entry["python_ms"] == 20.0
    assert entry["rust_ms"] == 5.5
    assert entry["queue_wait_ms"] == 0.25
    assert entry["error_type"] is None
    assert entry["recorded_at"].endswith("Z")


def test_request_insights_records_failures_even_when_fast():
    insights = RequestInsights(slow_threshold_ms=1000.0)
    insights.observe(request_id="err", method="GET", route="/error", status=500,
                     latency_ms=1.0, error_type="ValueError")
    insights.observe(request_id="bad", method="GET", route="/", status=400,
                     latency_ms=1.0, error_type="BadRequest")
    insights.observe(request_id="ok", method="GET", route="/", status=200, latency_ms=1.0)
    snap = insights.snapshot()
    assert snap["observed"] == 3
    assert snap["recorded"] == 2
    assert [entry["request_id"] for entry in snap["requests"]] == ["err", "bad"]
    assert snap["requests"][0]["error_type"] == "ValueError"
    assert snap["requests"][1]["status"] == 400


def test_request_insights_dynamic_p95_threshold():
    insights = RequestInsights()
    for i in range(20):
        insights.observe(request_id=f"base-{i}", method="GET", route="/",
                         status=200, latency_ms=5.0)
    # p95 of twenty identical 5ms samples is 5ms: another 5ms request is
    # not slow, a 6ms one is.
    insights.observe(request_id="normal", method="GET", route="/", status=200, latency_ms=5.0)
    insights.observe(request_id="slow", method="GET", route="/", status=200, latency_ms=6.0)
    snap = insights.snapshot()
    assert snap["observed"] == 22
    assert snap["recorded"] == 1
    assert snap["requests"][0]["request_id"] == "slow"
    assert snap["slow_threshold_ms"] == 5.0


def test_request_insights_warm_up_keeps_only_failures():
    insights = RequestInsights()
    for i in range(5):
        insights.observe(request_id=f"fast-{i}", method="GET", route="/",
                         status=200, latency_ms=100.0)
    insights.observe(request_id="err", method="GET", route="/", status=500,
                     latency_ms=1.0, error_type="ValueError")
    snap = insights.snapshot()
    assert snap["observed"] == 6
    assert snap["recorded"] == 1
    assert snap["requests"][0]["request_id"] == "err"
    assert snap["slow_threshold_ms"] is None  # fewer than 20 samples


def test_request_insights_ring_buffer_is_bounded():
    insights = RequestInsights(max_entries=3, slow_threshold_ms=0.0)
    for i in range(5):
        insights.observe(request_id=f"r{i}", method="GET", route="/",
                         status=200, latency_ms=float(i))
    snap = insights.snapshot()
    assert snap["observed"] == 5
    assert snap["recorded"] == 5
    # Oldest first; the first two entries were evicted.
    assert [entry["request_id"] for entry in snap["requests"]] == ["r2", "r3", "r4"]


def test_request_insights_clear_resets_counters_and_window():
    insights = RequestInsights(slow_threshold_ms=0.0)
    insights.observe(request_id="r1", method="GET", route="/", status=200, latency_ms=1.0)
    insights.clear()
    snap = insights.snapshot()
    assert snap["observed"] == 0
    assert snap["recorded"] == 0
    assert snap["requests"] == []


def test_request_insights_snapshot_returns_copies():
    insights = RequestInsights(slow_threshold_ms=0.0)
    insights.observe(request_id="r1", method="GET", route="/", status=200, latency_ms=1.0)
    snap = insights.snapshot()
    snap["requests"][0]["status"] = 999
    snap["requests"].clear()
    assert insights.snapshot()["requests"][0]["status"] == 200


@pytest.mark.parametrize("bad", [0, -1, True, "10", 1.5])
def test_request_insights_rejects_bad_max_entries(bad):
    with pytest.raises(ValueError):
        RequestInsights(max_entries=bad)


@pytest.mark.parametrize("bad", [-1.0, True, "10"])
def test_request_insights_rejects_bad_slow_threshold(bad):
    with pytest.raises(ValueError):
        RequestInsights(slow_threshold_ms=bad)


def test_observability_config_request_insights_defaults():
    config = ObservabilityConfig()
    assert config.request_insights is None  # auto: on in debug, off in production
    assert config.request_insights_max == 100
    assert config.request_insights_slow_ms is None


def test_observability_config_request_insights_from_env(monkeypatch):
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS", "1")
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS_MAX", "50")
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS_SLOW_MS", "250")
    config = ObservabilityConfig.from_env()
    assert config.request_insights is True
    assert config.request_insights_max == 50
    assert config.request_insights_slow_ms == 250.0
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS", "0")
    assert ObservabilityConfig.from_env().request_insights is False
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS", "bogus")
    assert ObservabilityConfig.from_env().request_insights is None
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS_MAX", "bogus")
    assert ObservabilityConfig.from_env().request_insights_max == 100
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS_MAX", "0")
    assert ObservabilityConfig.from_env().request_insights_max == 1
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS_SLOW_MS", "-5")
    assert ObservabilityConfig.from_env().request_insights_slow_ms is None
    monkeypatch.setenv("PYNEXT_REQUEST_INSIGHTS_SLOW_MS", "bogus")
    assert ObservabilityConfig.from_env().request_insights_slow_ms is None


def test_app_request_insights_off_by_default():
    app = create_app(EXAMPLE)
    assert app.request_insights.enabled is False
    assert app.request_insights.snapshot() == {"enabled": False}


def test_app_request_insights_on_in_debug_and_records_requests():
    async def run():
        app = create_app(EXAMPLE, debug=True,
                         observability=ObservabilityConfig(request_insights_slow_ms=0.0))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            home = await client.get("/")
            assert home.status_code == 200
            assert (await client.get("/blog/hello")).status_code == 200
            assert (await client.get("/missing")).status_code == 404
            # No HTTP endpoint serves the snapshots (in-process API only).
            assert (await client.get("/_pynext/request-insights")).status_code == 404
        return app.request_insights.snapshot(), home.headers["x-request-id"]
    snap, home_request_id = _run(run())
    assert snap["enabled"] is True
    assert snap["observed"] == 4
    assert snap["recorded"] == 4
    # Route labels are patterns, never raw URLs.
    assert [entry["route"] for entry in snap["requests"]] == [
        "/", "/blog/[slug]", "__not_found__", "__client__"]
    assert snap["requests"][0]["request_id"] == home_request_id
    assert snap["requests"][0]["status"] == 200
    assert snap["requests"][2]["status"] == 404
    assert snap["requests"][2]["error_type"] is None


def test_app_request_insights_records_failed_request_with_error_type(tmp_path):
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
        app = create_app(tmp_path, observability=ObservabilityConfig(
            request_insights=True, request_insights_slow_ms=10000.0))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/error")).status_code == 500
            assert (await client.get("/")).status_code == 200
        return app.request_insights.snapshot()
    snap = _run(run())
    assert snap["observed"] == 2
    assert snap["recorded"] == 1
    (entry,) = snap["requests"]
    assert entry["route"] == "/error"
    assert entry["status"] == 500
    assert entry["error_type"] == "ValueError"


def test_app_configure_observability_updates_request_insights():
    app = create_app(EXAMPLE, debug=True)
    assert app.request_insights.enabled is True
    app.configure_observability(ObservabilityConfig(
        request_insights=False, request_insights_slow_ms=5.0))
    assert app.request_insights.enabled is False
    assert app.request_insights.slow_threshold_ms == 5.0
    assert app.request_insights.snapshot() == {"enabled": False}
    app.configure_observability(ObservabilityConfig())
    assert app.request_insights.enabled is True  # auto -> on in debug mode
