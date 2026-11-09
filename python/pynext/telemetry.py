"""Anonymous, local-only build telemetry.

Only one event exists: ``build_failed``. It records *that* a build failed,
in which build stage, and how long the build ran -- nothing else. In
particular it never records project names, paths, user code, error messages,
exception types, or route information, so an event cannot identify a project
or leak its contents.

Nothing is ever transmitted anywhere: this module contains no network code
at all (no sockets, no HTTP client, no subprocess that could phone home).
Events are appended to ``<project>/.pynext/telemetry.jsonl`` as JSON Lines
for local inspection only.

Recording is best-effort and must never break a build: every public
function in this module swallows its own errors. Set the
``PYNEXT_TELEMETRY_DISABLED`` environment variable to ``1``/``true``/``yes``
to turn local recording off.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

EVENT_BUILD_FAILED = "build_failed"

# Build stages, in order. "unknown" is the fallback for a stage value this
# module does not recognise (defensive; callers only pass the three below).
STAGES = ("discover", "render", "write")
MAX_EVENTS = 100
TELEMETRY_FILENAME = "telemetry.jsonl"


def telemetry_enabled():
    """Local recording is on unless PYNEXT_TELEMETRY_DISABLED is truthy."""
    return os.environ.get("PYNEXT_TELEMETRY_DISABLED", "").strip().lower() not in ("1", "true", "yes")


def _pynext_version():
    try:
        from importlib.metadata import version
        return version("pynext-framework")
    except Exception:
        return "unknown"


def build_failed_event(stage, duration_ms):
    """Build the anonymous ``build_failed`` event dict.

    ``stage`` is coerced to one of ``discover``/``render``/``write``
    (anything else becomes ``"unknown"``); ``duration_ms`` is an integer
    number of milliseconds. No caller-supplied text is ever included.
    """
    if stage not in STAGES:
        stage = "unknown"
    try:
        duration = max(0, int(duration_ms))
    except (TypeError, ValueError):
        duration = 0
    return {
        "event": EVENT_BUILD_FAILED,
        "stage": stage,
        "duration_ms": duration,
        "pynext_version": _pynext_version(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def telemetry_path(root):
    """Path of the local telemetry file for a project root."""
    return Path(root) / ".pynext" / TELEMETRY_FILENAME


def read_events(root):
    """Return the recorded events for a project root (newest last).

    Corrupt lines are skipped and any I/O problem yields an empty list:
    reading telemetry must never raise.
    """
    try:
        lines = telemetry_path(root).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == EVENT_BUILD_FAILED:
            events.append(event)
    return events


def record_build_failed(root, stage, duration_ms):
    """Append one anonymous ``build_failed`` event for a project root.

    Keeps only the most recent ``MAX_EVENTS`` events so the file stays
    bounded. Never raises: if recording fails for any reason (disabled,
    unwritable directory, ...), the failure is silently ignored so the
    build itself is unaffected.
    """
    if not telemetry_enabled():
        return
    try:
        event = build_failed_event(stage, duration_ms)
        path = telemetry_path(root)
        # Never write through a symlink: a symlinked .pynext directory or
        # telemetry file means the build output location is not trusted
        # (build.py rejects symlinked output dirs outright), so skip
        # recording instead of writing outside the project.
        if path.is_symlink() or path.parent.is_symlink():
            return
        if path.parent.exists() and not path.parent.is_dir():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        events = read_events(root)
        events.append(event)
        events = events[-MAX_EVENTS:]
        contents = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(contents, encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    except Exception:
        # Telemetry must never break a build; ignore every recording error.
        # The build's own exception (if any) still propagates from the caller.
        pass
