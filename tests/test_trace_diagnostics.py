"""Local slow-span trace file (F P1-4) and build diagnostics file (F P2-1/P2-2).

The trace file is off by default and records only ``pynext.request`` /
``pynext.render`` spans slower than the configured threshold, with route
patterns only — never raw URLs, headers or bodies. The diagnostics file
records only the framework version, the build stage, an optional failed
route *pattern* and anonymous timing; build-failure events are local only
and nothing is ever transmitted.
"""
import asyncio
import json
import time

import httpx
import pytest

from pynext import create_app
from pynext.build import BuildError, update_build_diagnostics, write_manifest
from pynext.observability import (
    _reset_local_trace_config,
    configure_local_trace,
    span,
)
import pynext.observability as observability


@pytest.fixture(autouse=True)
def _reset_trace(monkeypatch):
    monkeypatch.delenv("PYNEXT_TRACE_SPAN_THRESHOLD_MS", raising=False)
    monkeypatch.delenv("PYNEXT_TRACE_FILE", raising=False)
    _reset_local_trace_config()
    yield
    _reset_local_trace_config()


def _read_spans(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_diagnostics(root):
    return json.loads((root / ".pynext" / "diagnostics.json").read_text(encoding="utf-8"))


def _make_ssg_app(root):
    app = root / "app"
    (app).mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    return app


# -- local trace file ------------------------------------------------------

def test_local_trace_off_by_default(tmp_path):
    trace = tmp_path / "trace.jsonl"
    configure_local_trace(path=trace)
    with span("pynext.request", {"http.method": "GET", "http.route": "/"}):
        time.sleep(0.01)
    assert not trace.exists()


def test_local_trace_records_only_spans_slower_than_threshold(tmp_path):
    trace = tmp_path / "trace.jsonl"
    configure_local_trace(50, path=trace)
    with span("pynext.request", {"http.method": "GET", "http.route": "/"}):
        pass
    with span("pynext.render", {"http.route": "/"}):
        time.sleep(0.08)
    records = _read_spans(trace)
    assert len(records) == 1
    record = records[0]
    assert record["name"] == "pynext.render"
    assert record["duration_ms"] >= 80
    assert record["timestamp"].endswith("Z")
    assert record["attributes"] == {"http.route": "/"}
    assert record["error_type"] is None


def test_local_trace_zero_threshold_records_everything_known(tmp_path):
    trace = tmp_path / "trace.jsonl"
    configure_local_trace(0, path=trace)
    with span("pynext.request", {"http.method": "GET"}):
        pass
    with span("pynext.render", {"http.route": "/"}):
        pass
    with span("something.else"):
        pass
    names = [record["name"] for record in _read_spans(trace)]
    assert names == ["pynext.request", "pynext.render"]


def test_local_trace_records_error_type_and_propagates(tmp_path):
    trace = tmp_path / "trace.jsonl"
    configure_local_trace(0, path=trace)
    with pytest.raises(ValueError, match="boom"):
        with span("pynext.request", {"http.method": "GET"}):
            raise ValueError("boom")
    records = _read_spans(trace)
    assert len(records) == 1
    assert records[0]["error_type"] == "ValueError"


def test_local_trace_records_attributes_set_inside_span(tmp_path):
    trace = tmp_path / "trace.jsonl"
    configure_local_trace(0, path=trace)
    with span("pynext.request", {"http.method": "GET"}) as request_span:
        request_span.set_attribute("http.status_code", 200)
        request_span.set_attribute("http.route", "/blog/[slug]")
    record = _read_spans(trace)[0]
    assert record["attributes"]["http.status_code"] == 200
    assert record["attributes"]["http.route"] == "/blog/[slug]"


def test_local_trace_rotates_at_size_cap(tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b"x" * 300)
    monkeypatch.setattr(observability, "TRACE_FILE_MAX_BYTES", 200)
    configure_local_trace(0, path=trace)
    with span("pynext.request", {"http.method": "GET"}):
        pass
    records = _read_spans(trace)
    assert len(records) == 1
    assert records[0]["name"] == "pynext.request"
    assert trace.stat().st_size < 300


def test_local_trace_invalid_and_negative_threshold_env_are_off(tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("PYNEXT_TRACE_FILE", str(trace))
    for bad in ("not-a-number", "-5"):
        monkeypatch.setenv("PYNEXT_TRACE_SPAN_THRESHOLD_MS", bad)
        _reset_local_trace_config()
        with span("pynext.request", {"http.method": "GET"}):
            time.sleep(0.01)
    assert not trace.exists()


def test_local_trace_end_to_end_request(tmp_path, monkeypatch):
    _make_ssg_app(tmp_path)
    write_manifest(tmp_path)
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("PYNEXT_TRACE_SPAN_THRESHOLD_MS", "0")
    monkeypatch.setenv("PYNEXT_TRACE_FILE", str(trace))
    _reset_local_trace_config()
    app = create_app(tmp_path, production=True)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 200

    asyncio.run(run())
    records = _read_spans(trace)
    requests = [record for record in records if record["name"] == "pynext.request"]
    assert len(requests) == 1
    assert requests[0]["attributes"]["http.route"] == "/"
    assert requests[0]["attributes"]["http.status_code"] == 200


# -- build diagnostics file -------------------------------------------------

def test_successful_build_writes_done_diagnostics(tmp_path):
    _make_ssg_app(tmp_path)
    write_manifest(tmp_path)
    data = _read_diagnostics(tmp_path)
    assert data["stage"] == "done"
    assert data["pynext_version"] == "0.1.2"
    assert data["updated_at"].endswith("Z")
    assert isinstance(data["duration_ms"], float)
    assert "failed_route" not in data
    assert "events" not in data


def test_failed_discover_build_records_stage_and_anonymous_event(tmp_path):
    with pytest.raises(ValueError, match="App directory not found"):
        write_manifest(tmp_path)
    data = _read_diagnostics(tmp_path)
    assert data["stage"] == "discover"
    assert data["pynext_version"] == "0.1.2"
    assert "failed_route" not in data
    assert len(data["events"]) == 1
    event = data["events"][0]
    assert event == {
        "event": "build_failed",
        "stage": "discover",
        "duration_ms": event["duration_ms"],
        "timestamp": event["timestamp"],
    }
    assert isinstance(event["duration_ms"], float)
    raw = (tmp_path / ".pynext" / "diagnostics.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in raw
    assert "App directory not found" not in raw


def test_failed_render_build_records_failed_route_pattern(tmp_path):
    app = tmp_path / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): raise RuntimeError("boom-secret")\n'
    )
    with pytest.raises(BuildError, match="boom-secret"):
        write_manifest(tmp_path)
    data = _read_diagnostics(tmp_path)
    assert data["stage"] == "render"
    assert data["failed_route"] == "/"
    assert data["events"][0]["stage"] == "render"
    raw = (tmp_path / ".pynext" / "diagnostics.json").read_text(encoding="utf-8")
    assert "boom-secret" not in raw
    assert str(tmp_path) not in raw


def test_diagnostics_merges_and_caps_failure_events(tmp_path):
    _make_ssg_app(tmp_path)
    (tmp_path / ".pynext").mkdir()
    (tmp_path / ".pynext" / "diagnostics.json").write_text(
        json.dumps({"note": "keep-me", "events": []}), encoding="utf-8")
    for _ in range(55):
        update_build_diagnostics(tmp_path, "discover", duration_ms=1.5, event="build_failed")
    data = _read_diagnostics(tmp_path)
    assert data["note"] == "keep-me"
    assert len(data["events"]) == 50
    assert all(event["event"] == "build_failed" for event in data["events"])
    assert all(event["duration_ms"] == 1.5 for event in data["events"])


def test_failed_route_clears_on_later_successful_build(tmp_path):
    app = tmp_path / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): raise RuntimeError("boom")\n'
    )
    with pytest.raises(BuildError):
        write_manifest(tmp_path)
    assert _read_diagnostics(tmp_path)["failed_route"] == "/"
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    write_manifest(tmp_path)
    data = _read_diagnostics(tmp_path)
    assert data["stage"] == "done"
    assert "failed_route" not in data
    assert len(data["events"]) == 1  # failure history is kept


def test_diagnostics_skipped_for_symlinked_pynext_dir(tmp_path):
    _make_ssg_app(tmp_path)
    target = tmp_path / "real-pynext"
    target.mkdir()
    (tmp_path / ".pynext").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match=r"\.pynext must be a directory"):
        write_manifest(tmp_path)
    assert not (target / "diagnostics.json").exists()
