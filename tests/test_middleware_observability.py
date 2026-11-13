"""Middleware/security decision observability (P2-2, REPORT §3.3 #18).

The middleware chain outcome and the rate-limit admission decision are
recorded as counters (``pynext_middleware_total`` /
``pynext_rate_limit_total``), as fields on the structured access log line,
and as attributes on the ``pynext.request`` span — on both transports.
A denied request never reaches dispatch, so its rate-limit counter is its
only record; an exempt 404 shows up as an ``exempt`` decision next to a
404 in the request counters, which is the shape a B-1 style bypass takes.
"""
import asyncio
import io
import json
import logging
import sys
import types
from dataclasses import replace

import httpx
import pytest

from pynext import ObservabilityConfig, RateLimiter, Response, create_app
from pynext.observability import (
    JsonFormatter,
    MetricsCollector,
    _reset_otel_cache,
    begin_request_context,
)
from pynext.server import start_native


async def block_middleware(request, call_next):
    if request.path == "/blocked":
        return Response("blocked", status=403)
    return await call_next(request)


async def raise_middleware(request, call_next):
    if request.path == "/boom":
        raise ValueError("middleware boom")
    return await call_next(request)


async def rewrite_middleware(request, call_next):
    if request.path == "/old":
        request = replace(request, path="/target")
    return await call_next(request)


async def strip_headers_middleware(request, call_next):
    # Drops every header (including traceparent) before the terminal.
    return await call_next(replace(request, headers=()))


async def block_ws_middleware(request, call_next):
    if request.path == "/ws":
        return Response("blocked", status=403)
    return await call_next(request)


async def pass_through_middleware(request, call_next):
    request.state["seen_by_middleware"] = True
    return await call_next(request)


def _project(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "home")\n'
    )
    target = app_dir / "target"
    target.mkdir()
    (target / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "target")\n'
    )
    ws_dir = app_dir / "ws"
    ws_dir.mkdir()
    (ws_dir / "route.py").write_text(
        'from pynext import websocket\n@websocket\n'
        'async def handler(websocket):\n'
        '    await websocket.accept()\n'
        '    await websocket.send_text("hi")\n'
    )
    return tmp_path


class _LogCapture:
    """Attach a JsonFormatter handler to the ``pynext`` logger."""

    def __init__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(JsonFormatter())
        self.logger = logging.getLogger("pynext")
        self._level = self.logger.level

    def __enter__(self):
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._level)
        return False

    def lines(self, message=None):
        entries = [json.loads(line) for line in self.stream.getvalue().splitlines()
                   if line.strip()]
        if message is not None:
            entries = [entry for entry in entries if entry.get("message") == message]
        return entries


def _run(coro):
    return asyncio.run(coro)


# -- counters ---------------------------------------------------------------

def test_collector_decision_counters():
    collector = MetricsCollector()
    collector.observe_rate_limit("allowed")
    collector.observe_rate_limit("denied")
    collector.observe_rate_limit("exempt")
    collector.observe_rate_limit("error")
    collector.observe_rate_limit("bogus")  # unknown outcomes are ignored
    collector.observe_middleware("continued")
    collector.observe_middleware("short_circuited")
    collector.observe_middleware("error")
    collector.observe_middleware("bogus")
    snap = collector.snapshot()
    assert snap["rate_limit"] == {"allowed": 1, "denied": 1, "exempt": 1, "error": 1, "fail_open": 0}
    assert snap["middleware"] == {"continued": 1, "short_circuited": 1, "error": 1}
    text = collector.prometheus_text()
    assert 'pynext_rate_limit_total{outcome="allowed"} 1' in text
    assert 'pynext_rate_limit_total{outcome="denied"} 1' in text
    assert 'pynext_rate_limit_total{outcome="exempt"} 1' in text
    assert 'pynext_rate_limit_total{outcome="error"} 1' in text
    assert 'pynext_middleware_total{outcome="continued"} 1' in text
    assert 'pynext_middleware_total{outcome="short_circuited"} 1' in text
    assert 'pynext_middleware_total{outcome="error"} 1' in text
    assert "bogus" not in text

    disabled = MetricsCollector(enabled=False)
    disabled.observe_rate_limit("allowed")
    disabled.observe_middleware("continued")
    assert disabled.snapshot() == {"enabled": False}


# -- middleware outcomes: log line + counters (ASGI) ------------------------

def test_asgi_middleware_outcomes_in_log_and_counters(tmp_path):
    _project(tmp_path)
    app = create_app(tmp_path, middleware=[rewrite_middleware, block_middleware,
                                           raise_middleware])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/old")).status_code == 200
            assert (await client.get("/blocked")).status_code == 403
            assert (await client.get("/boom")).status_code == 500
        await app.isr.close()

    with _LogCapture() as capture:
        _run(run())
    access = capture.lines("request")
    assert len(access) == 4
    home, rewritten, blocked, boom = access
    assert home["route"] == "/" and home["status"] == 200
    assert home["middleware"] == "continued"
    assert home["middleware_actor"] is None
    assert home["middleware_rewrote_path"] is None
    assert rewritten["route"] == "/target" and rewritten["status"] == 200
    assert rewritten["middleware"] == "continued"
    assert rewritten["middleware_rewrote_path"] is True
    assert blocked["route"] == "__unmatched__" and blocked["status"] == 403
    assert blocked["middleware"] == "short_circuited"
    assert blocked["middleware_actor"] == "block_middleware"
    assert boom["route"] == "__unmatched__" and boom["status"] == 500
    assert boom["middleware"] == "error"
    assert boom["middleware_actor"] == "raise_middleware"
    assert app.metrics.snapshot()["middleware"] == {
        "continued": 2, "short_circuited": 1, "error": 1}


def test_handler_error_is_not_a_middleware_error(tmp_path):
    _project(tmp_path)
    error_dir = tmp_path / "app" / "error"
    error_dir.mkdir()
    (error_dir / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ValueError("handler boom")\n'
    )
    app = create_app(tmp_path, middleware=[pass_through_middleware])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.get("/error")).status_code == 500
        await app.isr.close()

    with _LogCapture() as capture:
        _run(run())
    access = capture.lines("request")
    assert len(access) == 1
    # The terminal ran, so the chain continued; the handler's own failure
    # is a 500 in the log, not a middleware outcome.
    assert access[0]["middleware"] == "continued"
    assert access[0]["middleware_actor"] is None
    assert app.metrics.snapshot()["middleware"] == {
        "continued": 1, "short_circuited": 0, "error": 0}


# -- rate-limit outcomes: log line + counters (ASGI) ------------------------

def test_asgi_rate_limit_outcomes_in_log_and_counters(tmp_path):
    _project(tmp_path)
    app = create_app(tmp_path, rate_limit=RateLimiter(1, 60, exempt_paths=("/open",)))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.get("/open")).status_code == 404  # exempt + unmatched
            assert (await client.get("/")).status_code == 200     # allowed
            assert (await client.get("/")).status_code == 429     # denied
            assert (await client.get("/open")).status_code == 404  # exempt, no token
        await app.isr.close()

    with _LogCapture() as capture:
        _run(run())
    access = capture.lines("request")
    # The denied request never reaches dispatch, so it has no access line.
    assert len(access) == 3
    assert access[0]["route"] == "__not_found__" and access[0]["status"] == 404
    assert access[0]["rate_limit"] == "exempt"
    assert access[1]["route"] == "/" and access[1]["rate_limit"] == "allowed"
    assert access[2]["rate_limit"] == "exempt"
    snap = app.metrics.snapshot()
    assert snap["rate_limit"] == {"allowed": 1, "denied": 1, "exempt": 2, "error": 0, "fail_open": 0}
    # The B-1 detection shape: exempt decisions next to 404s.
    assert snap["routes"]["__not_found__"]["status"] == {"404": 2}


def test_asgi_rate_limit_decision_failure_is_counted_as_error(tmp_path):
    _project(tmp_path)
    app = create_app(tmp_path, rate_limit=RateLimiter(10, 60))

    class _FailingGate:
        # A generic failure (not DecisionUnavailable): the decision gate
        # could not decide because the limiter itself broke. H-2 fails
        # open only on DecisionUnavailable (gate full / deadline); any
        # other failure fails closed and is counted as an error.
        async def check(self, callback):
            raise RuntimeError("limiter backend failed")

    app._asgi_admission = _FailingGate()

    async def run():
        denied = await app._rate_limit_denied(
            {"client": ("127.0.0.1", 1234), "headers": []}, "/")
        assert denied is not None and denied.status == 503
        await app.isr.close()

    _run(run())
    assert app.metrics.snapshot()["rate_limit"] == {
        "allowed": 0, "denied": 0, "exempt": 0, "error": 1, "fail_open": 0}


# -- trace context survives middleware header rewrites ----------------------

def test_traceparent_survives_middleware_header_rewrite(tmp_path):
    _project(tmp_path)
    app = create_app(tmp_path, middleware=[strip_headers_middleware])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/", headers={
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
            assert response.status_code == 200
        await app.isr.close()

    with _LogCapture() as capture:
        _run(run())
    access = capture.lines("request")
    assert len(access) == 1
    # The trace id comes from the transport entry, not from the headers
    # the middleware hands to the terminal.
    assert access[0]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert access[0]["middleware"] == "continued"


# -- WebSocket pre-flight: no access log line, so it logs its own ----------

def test_websocket_prefilter_middleware_denial_is_observed(tmp_path):
    _project(tmp_path)
    blocked = create_app(tmp_path, middleware=[block_ws_middleware])
    allowed = create_app(tmp_path, middleware=[pass_through_middleware])

    async def run():
        _, reset = begin_request_context([])
        try:
            denied = await blocked._authorize_websocket("/ws", b"", ())
            assert denied is not None and denied.status == 403
        finally:
            reset()
        _, reset = begin_request_context([])
        try:
            assert await allowed._authorize_websocket("/ws", b"", ()) is None
        finally:
            reset()

    with _LogCapture() as capture:
        _run(run())
    try:
        assert blocked.metrics.snapshot()["middleware"] == {
            "continued": 0, "short_circuited": 1, "error": 0}
        assert allowed.metrics.snapshot()["middleware"] == {
            "continued": 1, "short_circuited": 0, "error": 0}
        denials = capture.lines("websocket authorization denied by middleware")
        assert len(denials) == 1
        assert denials[0]["middleware"] == "short_circuited"
        assert denials[0]["middleware_actor"] == "block_ws_middleware"
        assert denials[0]["status"] == 403
    finally:
        _run(blocked.isr.close())
        _run(allowed.isr.close())


# -- both transports --------------------------------------------------------

@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_middleware_and_rate_limit_counters_on_both_transports(tmp_path, mode):
    _project(tmp_path)
    app = create_app(tmp_path, middleware=[block_middleware],
                     rate_limit=RateLimiter(10, 60, exempt_paths=("/open",)))

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/blocked")).status_code == 403
                assert (await client.get("/open")).status_code == 404
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())
    snap = app.metrics.snapshot()
    assert snap["middleware"] == {"continued": 2, "short_circuited": 1, "error": 0}
    assert snap["rate_limit"] == {"allowed": 2, "denied": 0, "exempt": 1, "error": 0, "fail_open": 0}
    text = app.metrics.prometheus_text()
    assert 'pynext_middleware_total{outcome="short_circuited"} 1' in text
    assert 'pynext_rate_limit_total{outcome="exempt"} 1' in text


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_rate_limit_denied_is_counted_on_both_transports(tmp_path, mode):
    _project(tmp_path)
    app = create_app(tmp_path, rate_limit=RateLimiter(1, 60, exempt_paths=("/open",)))

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/")).status_code == 429
                assert (await client.get("/open")).status_code == 404
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())
    assert app.metrics.snapshot()["rate_limit"] == {
        "allowed": 1, "denied": 1, "exempt": 1, "error": 0, "fail_open": 0}


# -- spans ------------------------------------------------------------------

def test_request_span_carries_middleware_and_rate_limit_outcomes(tmp_path, monkeypatch):
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
        _project(tmp_path)
        app = create_app(tmp_path, middleware=[block_middleware],
                         rate_limit=RateLimiter(10, 60),
                         observability=ObservabilityConfig(tracing=True))

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/blocked")).status_code == 403
                assert (await client.get("/")).status_code == 200
            await app.isr.close()

        _run(run())
        request_spans = [item for item in recorded if item.name == "pynext.request"]
        assert len(request_spans) == 2
        blocked_span = next(item for item in request_spans
                            if item.attributes.get("http.status_code") == 403)
        assert blocked_span.attributes["middleware.outcome"] == "short_circuited"
        assert blocked_span.attributes["middleware.actor"] == "block_middleware"
        assert blocked_span.attributes["rate_limit.outcome"] == "allowed"
        ok_span = next(item for item in request_spans
                       if item.attributes.get("http.status_code") == 200)
        assert ok_span.attributes["middleware.outcome"] == "continued"
        assert ok_span.attributes["rate_limit.outcome"] == "allowed"
        assert "middleware.actor" not in ok_span.attributes
    finally:
        _reset_otel_cache()
