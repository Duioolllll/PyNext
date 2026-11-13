"""Local-only anonymous build-failure telemetry (REPORT.md §2.3 P2, track F P2-2).

Only the "build failed" event exists, it is anonymous (stage + duration
only -- no paths, error messages, or user code), and nothing is ever
transmitted: events are only appended to <project>/.pynext/telemetry.jsonl.
"""
import json

import pytest

from pynext import telemetry
from pynext.build import write_manifest
from pynext.telemetry import EVENT_BUILD_FAILED, MAX_EVENTS, build_failed_event, read_events, record_build_failed


def test_event_schema_is_anonymous_only():
    event = build_failed_event("render", 123)
    assert set(event) == {"event", "stage", "duration_ms", "pynext_version", "timestamp"}
    assert event["event"] == EVENT_BUILD_FAILED
    assert event["stage"] == "render"
    assert event["duration_ms"] == 123
    # No field may carry user-derived content.
    assert "my-secret-project" not in json.dumps(event)
    assert "/" not in event["stage"]


def test_unknown_stage_and_bad_duration_are_coerced():
    assert build_failed_event("bogus-stage", 5)["stage"] == "unknown"
    assert build_failed_event("discover", -10)["duration_ms"] == 0
    assert build_failed_event("write", "not-a-number")["duration_ms"] == 0


def test_record_appends_and_reads_back(tmp_path):
    record_build_failed(tmp_path, "discover", 42)
    record_build_failed(tmp_path, "render", 7)
    events = read_events(tmp_path)
    assert [event["stage"] for event in events] == ["discover", "render"]
    assert events[0]["duration_ms"] == 42
    # One JSON object per line, parseable as written.
    lines = (tmp_path / ".pynext" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["event"] == EVENT_BUILD_FAILED for line in lines)


def test_record_keeps_only_the_most_recent_events(tmp_path):
    for _ in range(MAX_EVENTS + 5):
        record_build_failed(tmp_path, "write", 1)
    events = read_events(tmp_path)
    assert len(events) == MAX_EVENTS
    assert all(event["stage"] == "write" for event in events)


def test_record_is_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNEXT_TELEMETRY_DISABLED", "1")
    record_build_failed(tmp_path, "discover", 1)
    assert not (tmp_path / ".pynext" / "telemetry.jsonl").exists()
    assert read_events(tmp_path) == []


def test_record_never_raises(tmp_path, monkeypatch):
    # Root is a file, not a directory: recording must fail silently.
    target = tmp_path / "not-a-dir"
    target.write_text("x")
    record_build_failed(target, "discover", 1)
    # And a read-only .pynext directory must not raise either.
    locked = tmp_path / ".pynext"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        record_build_failed(tmp_path, "render", 1)
    finally:
        locked.chmod(0o700)


def test_record_refuses_to_write_through_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / ".pynext"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Directory symlinks unavailable")
    record_build_failed(tmp_path, "discover", 1)
    assert list(outside.iterdir()) == []
    assert read_events(tmp_path) == []


def test_read_events_skips_corrupt_lines(tmp_path):
    path = tmp_path / ".pynext" / "telemetry.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"event": "build_failed", "stage": "discover", "duration_ms": 1}\n'
                    'not json at all\n'
                    '{"event": "something-else"}\n')
    events = read_events(tmp_path)
    assert len(events) == 1 and events[0]["stage"] == "discover"


def test_read_events_missing_file_is_empty(tmp_path):
    assert read_events(tmp_path) == []


def _make_broken_app(root, page_source):
    app = root / "app"
    (app).mkdir(parents=True)
    (app / "page.py").write_text(page_source, encoding="utf-8")


def test_failed_build_records_discover_stage(tmp_path):
    # No app/ directory at all: discovery fails before any rendering.
    with pytest.raises(ValueError):
        write_manifest(tmp_path)
    events = read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["event"] == EVENT_BUILD_FAILED
    assert events[0]["stage"] == "discover"
    assert events[0]["duration_ms"] >= 0
    # The event must not contain the project path or the error message.
    assert str(tmp_path) not in json.dumps(events)
    assert "App directory" not in json.dumps(events)


def test_failed_build_records_render_stage(tmp_path):
    # Integrated build: render failures are aggregated into a staged
    # BuildError whose message preserves the original error text.
    from pynext.build import BuildError

    _make_broken_app(tmp_path, 'raise RuntimeError("boom-secret-message")\n')
    with pytest.raises(BuildError, match="boom-secret-message"):
        write_manifest(tmp_path)
    events = read_events(tmp_path)
    assert len(events) == 1 and events[0]["stage"] == "render"
    assert "boom-secret-message" not in json.dumps(events)


def test_failed_build_records_write_stage(tmp_path, monkeypatch):
    from pynext import build as build_module

    _make_broken_app(
        tmp_path,
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n',
    )
    real_atomic_write = build_module._atomic_write

    def failing_write(path, contents):
        if path.name == "manifest.json":
            raise OSError("disk full (simulated)")
        return real_atomic_write(path, contents)

    monkeypatch.setattr(build_module, "_atomic_write", failing_write)
    # Integrated build: the write failure is wrapped in a staged
    # BuildError whose message preserves the original error text.
    from pynext.build import BuildError

    with pytest.raises(BuildError, match="disk full"):
        write_manifest(tmp_path)
    events = read_events(tmp_path)
    assert len(events) == 1 and events[0]["stage"] == "write"
    assert "disk full" not in json.dumps(events)


def test_successful_build_records_nothing(tmp_path):
    _make_broken_app(
        tmp_path,
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n',
    )
    write_manifest(tmp_path)
    assert read_events(tmp_path) == []
    assert not (tmp_path / ".pynext" / "telemetry.jsonl").exists()


def test_build_error_still_propagates_unchanged(tmp_path):
    with pytest.raises(ValueError, match="App directory not found"):
        write_manifest(tmp_path)
    # Telemetry recorded the failure but did not swallow or alter it.
    assert read_events(tmp_path)[0]["stage"] == "discover"


def test_module_contains_no_network_code():
    import ast

    source = open(telemetry.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"socket", "urllib", "requests", "httpx", "http"})
    assert "telemetry.nextjs.org" not in source
