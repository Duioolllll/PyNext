"""Structured logging, metrics and optional OpenTelemetry tracing (spec v0.2 §12).

Design notes
------------
* Structured logs are on by default, metrics are on by default, tracing is
  off by default and is a no-op unless the ``opentelemetry`` packages are
  installed *and* tracing is enabled. Nothing here imports OpenTelemetry at
  module import time.
* Sensitive values are never logged by default: the JSON formatter drops
  extra fields whose names look like credentials/headers/cookies/bodies, and
  the request log line records only the route *pattern* (never the raw URL),
  method, status, latency and sizes. Never pass header, cookie, body or
  ``Authorization`` values to the logger.
* Metric labels use route patterns (``/blog/[slug]``), never raw URLs or
  user IDs, so cardinality stays bounded by the application's route table.
  The same rule applies to error digests: only the fixed well-known
  digests (``PYNEXT_HTTP_ERROR_FALLBACK;404`` and friends, ``PYNEXT_REDIRECT``)
  become Prometheus label values. Computed digests (a hash of message +
  stack) are aggregated in the in-process snapshot only, capped at 256
  distinct digests with the rest folded into an ``__overflow__`` bucket,
  and are emitted in Prometheus under an empty ``digest`` label.
* Native (Rust) fresh ISR hits are served without entering Python, so they
  are invisible to the Python metrics below. That gap is documented in the
  manuals; only ISR outcomes that reach :meth:`App.dispatch` are counted.
* ISR regeneration runs in a detached context (the cache creates its render
  task with a fresh ``contextvars.Context`` so request-local state cannot
  leak into a publicly shared render), so its Rust render time is not
  attributed to the dispatching request's Python/Rust split; the request
  still records the cache outcome and regeneration result.
* Queue wait is measured on the Axum path only: the bridge records when a
  request is submitted to the Python loop and the handler records when it
  starts running. The ASGI path has no such queue, so its queue wait is 0.
"""
import contextlib
import contextvars
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

__all__ = [
    "ObservabilityConfig",
    "MetricsCollector",
    "JsonFormatter",
    "configure_logging",
    "new_request_id",
    "get_request_id",
    "get_trace_id",
    "parse_traceparent",
    "begin_request_context",
    "span",
    "is_tracing_available",
]

log = logging.getLogger("pynext")

# Names that must never appear as fields in a structured log line.
SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "x-api-key", "api-key",
    "api_key", "password", "passwd", "token", "secret", "access-token",
    "refresh-token", "session", "sessionid", "csrf", "body", "headers",
    "query", "query_string", "raw_url", "url",
})

_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")

request_id_var = contextvars.ContextVar("pynext_request_id", default=None)
trace_id_var = contextvars.ContextVar("pynext_trace_id", default=None)
trace_span_id_var = contextvars.ContextVar("pynext_trace_span_id", default=None)
route_var = contextvars.ContextVar("pynext_route", default=None)
queue_wait_var = contextvars.ContextVar("pynext_queue_wait_ms", default=None)
render_ms_var = contextvars.ContextVar("pynext_render_ms", default=None)
_otel_context_var = contextvars.ContextVar("pynext_otel_context", default=None)


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ObservabilityConfig:
    """Flags controlling structured logs, metrics and tracing.

    Defaults: structured logs on, metrics on, tracing off unless OpenTelemetry
    is installed and tracing is enabled. Environment overrides:
    ``PYNEXT_STRUCTURED_LOGS``, ``PYNEXT_METRICS``, ``PYNEXT_TRACING``,
    ``PYNEXT_LOG_LEVEL``.
    """

    structured_logs: bool = True
    metrics: bool = True
    tracing: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls):
        return cls(
            structured_logs=_env_bool("PYNEXT_STRUCTURED_LOGS", True),
            metrics=_env_bool("PYNEXT_METRICS", True),
            tracing=_env_bool("PYNEXT_TRACING", False),
            log_level=os.environ.get("PYNEXT_LOG_LEVEL", "INFO"),
        )


def new_request_id():
    return uuid.uuid4().hex


def get_request_id():
    return request_id_var.get()


def get_trace_id():
    return trace_id_var.get()


def parse_traceparent(value):
    """Parse a W3C ``traceparent`` header. Returns (trace_id, span_id, flags) or None."""
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if match is None:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id, span_id, flags


def _header_value(headers, name):
    wanted = name.lower().encode("ascii")
    for key, value in headers or ():
        if isinstance(key, bytes) and key.lower() == wanted:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return None
        if isinstance(key, str) and key.lower() == name.lower():
            return value if isinstance(value, str) else None
    return None


def begin_request_context(headers):
    """Set request-id / trace contextvars from incoming headers.

    Returns a reset callable (tokens) the caller must invoke when the
    request finishes. The request id echoes a valid ``X-Request-Id`` header
    and is otherwise freshly generated; it is also returned so the transport
    can attach it to the response.
    """
    incoming = _header_value(headers, "x-request-id")
    request_id = incoming if incoming and _REQUEST_ID_RE.match(incoming) else new_request_id()
    traceparent = _header_value(headers, "traceparent")
    parsed = parse_traceparent(traceparent) if traceparent else None
    tokens = (
        (request_id_var, request_id_var.set(request_id)),
        (trace_id_var, trace_id_var.set(parsed[0] if parsed else None)),
        (trace_span_id_var, trace_span_id_var.set(parsed[1] if parsed else None)),
        (route_var, route_var.set(None)),
        (queue_wait_var, queue_wait_var.set(None)),
        (render_ms_var, render_ms_var.set(None)),
    )
    context = _extract_otel_context(headers) if parsed is not None else None
    # Clear any inherited request context when this request has no valid
    # parent, and restore the caller's context at the end of the request.
    otel_token = _otel_context_var.set(context)

    def reset():
        for var, token in reversed(tokens):
            try:
                var.reset(token)
            except ValueError:
                pass
        try:
            _otel_context_var.reset(otel_token)
        except ValueError:
            pass

    return request_id, reset


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Sensitive extra fields are dropped."""

    def format(self, record):
        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            data["request_id"] = request_id
        trace_id = trace_id_var.get()
        if trace_id:
            data["trace_id"] = trace_id
        route = route_var.get()
        if route:
            data["route"] = route
        for key, value in vars(record).items():
            if key in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
            }:
                continue
            if key.lower() in SENSITIVE_KEYS:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            data[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=repr)


def configure_logging(config=None):
    """Install the structured (or plain) handler on the ``pynext`` logger once."""
    config = config or ObservabilityConfig.from_env()
    logger = logging.getLogger("pynext")
    if getattr(logger, "_pynext_observability_configured", False):
        return logger
    handler = logging.StreamHandler()
    if config.structured_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    except Exception:
        logger.setLevel(logging.INFO)
    logger._pynext_observability_configured = True
    return logger


def _percentiles(samples):
    if not samples:
        return {"p50": None, "p95": None, "p99": None, "avg": None}
    ordered = sorted(samples)
    count = len(ordered)

    def at(quantile):
        # Nearest-rank percentile.
        import math
        rank = math.ceil(count * quantile)
        return ordered[max(0, min(count - 1, rank - 1))]

    return {
        "p50": round(at(0.50), 3),
        "p95": round(at(0.95), 3),
        "p99": round(at(0.99), 3),
        "avg": round(sum(ordered) / count, 3),
    }


class MetricsCollector:
    """Cheap in-memory metrics. All observe methods are no-ops when disabled.

    Latency samples are kept in a bounded deque (2048 per route) so a
    long-running server cannot grow memory without bound.
    """

    _MAX_SAMPLES = 2048

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._requests = {}  # route -> {"count", "status": Counter-ish, "latency": deque, "bytes": int}
        self._queue_waits = deque(maxlen=self._MAX_SAMPLES)
        self._queue_current = 0
        self._queue_max = 0
        self._python_ms_total = 0.0
        self._python_ms_count = 0
        self._rust_ms_total = 0.0
        self._rust_ms_count = 0
        self._cache = {"hit": 0, "miss": 0, "stale": 0, "busy": 0}
        self._regeneration = {"success": 0, "failure": 0, "started": 0}
        self._workers = {"starts": 0, "timeouts": 0, "unavailable": 0}
        # H-2: rate-limit decisions that could not be produced (decision
        # gate full or deadline expired) and failed open instead of 503.
        self._rate_limit = {"fail_open": 0}
        # Per-stage timeout counts (C-P1-7): which request stage ran out
        # of its budget — body read, handler execution, response sending,
        # or admission (rate-limit decision gate). A handler timeout
        # answers 504 and an admission timeout answers 503, so the
        # stages must stay separable here too, not folded into one
        # "timeouts" number.
        self._stage_timeouts = {"body": 0, "handler": 0, "response": 0, "admission": 0}
        self._bundles = {}  # client asset path -> size in bytes
        # Error counts, joint key (route, error_type, digest); digest is ""
        # when the error has none. Computed digests are capped (see
        # observe_error) so a distinct traceback per request cannot grow
        # this dict without bound.
        self._errors = {}
        self._error_digests = set()

    # -- errors -----------------------------------------------------------
    def observe_error(self, route, error_type, digest=None):
        """Count one error, by exception type and (when known) digest.

        ``digest`` is the well-known signal digest for control-flow errors
        (``PYNEXT_HTTP_ERROR_FALLBACK;404`` ...) or the computed digest of a
        real failure. Signal errors are counted too — they are error
        responses, just not failures — and their distinct ``error_type``
        label keeps them separable from 500s. Computed digests are capped
        at 256 distinct values; further distinct digests fold into the
        ``__overflow__`` bucket. A digest is never a raw URL or message.
        """
        if not self.enabled:
            return
        digest = digest or ""
        with self._lock:
            if (digest and not digest.startswith("PYNEXT_")
                    and digest not in self._error_digests):
                if len(self._error_digests) >= 256:
                    digest = "__overflow__"
                else:
                    self._error_digests.add(digest)
            key = (route, error_type, digest)
            self._errors[key] = self._errors.get(key, 0) + 1

    # -- requests ---------------------------------------------------------
    def observe_request(self, route, method, status, latency_ms,
                        response_bytes=0, queue_wait_ms=None,
                        python_ms=None, rust_ms=None):
        if not self.enabled:
            return
        with self._lock:
            entry = self._requests.setdefault(route, {
                "count": 0, "status": {}, "latency": deque(maxlen=self._MAX_SAMPLES),
                "bytes": 0, "methods": {},
            })
            entry["count"] += 1
            entry["status"][str(status)] = entry["status"].get(str(status), 0) + 1
            entry["methods"][method] = entry["methods"].get(method, 0) + 1
            entry["latency"].append(float(latency_ms))
            entry["bytes"] += int(response_bytes or 0)
            if queue_wait_ms is not None:
                self._queue_waits.append(float(queue_wait_ms))
            if python_ms is not None:
                self._python_ms_total += float(python_ms)
                self._python_ms_count += 1
            if rust_ms is not None:
                self._rust_ms_total += float(rust_ms)
                self._rust_ms_count += 1

    def observe_render(self, rust_ms):
        if not self.enabled:
            return
        with self._lock:
            self._rust_ms_total += float(rust_ms)
            self._rust_ms_count += 1

    # -- queue / workers --------------------------------------------------
    def observe_queue_enter(self):
        if not self.enabled:
            return
        with self._lock:
            self._queue_current += 1
            self._queue_max = max(self._queue_max, self._queue_current)

    def observe_queue_exit(self):
        if not self.enabled:
            return
        with self._lock:
            self._queue_current = max(0, self._queue_current - 1)

    def observe_worker_start(self):
        if not self.enabled:
            return
        with self._lock:
            self._workers["starts"] += 1

    def observe_worker_timeout(self, kind="timeout"):
        if not self.enabled:
            return
        with self._lock:
            if kind == "timeout":
                self._workers["timeouts"] += 1
            else:
                self._workers["unavailable"] += 1

    def observe_stage_timeout(self, stage):
        """Count one stage timeout (C-P1-7).

        ``stage`` is one of ``"body"`` (body read, 408), ``"handler"``
        (handler execution, 504), ``"response"`` (response sending,
        aborted stream) or ``"admission"`` (rate-limit decision gate,
        503). Unknown stages are ignored so a typo cannot mint a new
        metric series.
        """
        if not self.enabled:
            return
        with self._lock:
            if stage in self._stage_timeouts:
                self._stage_timeouts[stage] += 1

    # -- cache ------------------------------------------------------------
    def observe_cache(self, outcome):
        # The Rust page store reports "fresh"; the counter key is "hit".
        outcome = {"fresh": "hit"}.get(outcome, outcome)
        if not self.enabled or outcome not in self._cache:
            return
        with self._lock:
            self._cache[outcome] += 1

    def observe_regeneration(self, outcome):
        if not self.enabled or outcome not in self._regeneration:
            return
        with self._lock:
            self._regeneration[outcome] += 1

    def observe_rate_limit_fail_open(self):
        """Count one rate-limit decision that failed open (H-2)."""
        if not self.enabled:
            return
        with self._lock:
            self._rate_limit["fail_open"] += 1

    # -- sizes ------------------------------------------------------------
    def observe_island_bundle(self, path, size_bytes):
        if not self.enabled:
            return
        with self._lock:
            self._bundles[path] = int(size_bytes)

    # -- snapshots --------------------------------------------------------
    def snapshot(self):
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            routes = {}
            total_requests = 0
            total_bytes = 0
            for route, entry in sorted(self._requests.items()):
                samples = list(entry["latency"])
                routes[route] = {
                    "requests": entry["count"],
                    "status": dict(sorted(entry["status"].items())),
                    "methods": dict(sorted(entry["methods"].items())),
                    "latency_ms": _percentiles(samples),
                    "response_bytes": entry["bytes"],
                }
                total_requests += entry["count"]
                total_bytes += entry["bytes"]
            bundle_sizes = dict(sorted(self._bundles.items()))
            errors_by_type = {}
            errors_by_digest = {}
            for (route, error_type, digest), count in sorted(self._errors.items()):
                errors_by_type.setdefault(route, {})[error_type] = (
                    errors_by_type.setdefault(route, {}).get(error_type, 0) + count)
                if digest:
                    errors_by_digest.setdefault(route, {})[digest] = (
                        errors_by_digest.setdefault(route, {}).get(digest, 0) + count)
            return {
                "enabled": True,
                "requests_total": total_requests,
                "response_bytes_total": total_bytes,
                "routes": routes,
                "errors": {"by_type": errors_by_type, "by_digest": errors_by_digest},
                "queue": {
                    "current": self._queue_current,
                    "max": self._queue_max,
                    "waits_ms": _percentiles(list(self._queue_waits)),
                    "wait_samples": len(self._queue_waits),
                },
                "python_ms": {
                    "total": round(self._python_ms_total, 3),
                    "count": self._python_ms_count,
                    "avg": round(self._python_ms_total / self._python_ms_count, 3) if self._python_ms_count else None,
                },
                "rust_render_ms": {
                    "total": round(self._rust_ms_total, 3),
                    "count": self._rust_ms_count,
                    "avg": round(self._rust_ms_total / self._rust_ms_count, 3) if self._rust_ms_count else None,
                },
                "cache": dict(self._cache),
                "regeneration": dict(self._regeneration),
                "workers": dict(self._workers),
                "rate_limit": dict(self._rate_limit),
                "stage_timeouts": dict(self._stage_timeouts),
                "island_bundles": bundle_sizes,
                "island_bundles_total_bytes": sum(bundle_sizes.values()),
            }

    def prometheus_text(self):
        """Minimal Prometheus text exposition (counters/gauges only)."""
        snap = self.snapshot()
        if not snap.get("enabled"):
            return "# PyNext metrics disabled\n"
        lines = ["# HELP pynext_requests_total Requests handled, by route and status.",
                 "# TYPE pynext_requests_total counter"]
        for route, entry in snap["routes"].items():
            for status, count in entry["status"].items():
                lines.append(f'pynext_requests_total{{route="{route}",status="{status}"}} {count}')
        lines += ["# HELP pynext_response_bytes_total Response bytes sent, by route.",
                  "# TYPE pynext_response_bytes_total counter"]
        for route, entry in snap["routes"].items():
            lines.append(f'pynext_response_bytes_total{{route="{route}"}} {entry["response_bytes"]}')
        lines += ["# HELP pynext_request_latency_ms Request latency percentile, by route.",
                  "# TYPE pynext_request_latency_ms gauge"]
        for route, entry in snap["routes"].items():
            for name, value in entry["latency_ms"].items():
                if name != "avg" and value is not None:
                    lines.append(f'pynext_request_latency_ms{{route="{route}",quantile="{name}"}} {value}')
        lines += ["# HELP pynext_cache_total ISR cache outcomes.",
                  "# TYPE pynext_cache_total counter"]
        for outcome, count in snap["cache"].items():
            lines.append(f'pynext_cache_total{{outcome="{outcome}"}} {count}')
        lines += ["# HELP pynext_regeneration_total ISR regeneration outcomes.",
                  "# TYPE pynext_regeneration_total counter"]
        for outcome, count in snap["regeneration"].items():
            lines.append(f'pynext_regeneration_total{{outcome="{outcome}"}} {count}')
        lines += ["# HELP pynext_worker_total Worker starts, timeouts and unavailability.",
                  "# TYPE pynext_worker_total counter"]
        for outcome, count in snap["workers"].items():
            lines.append(f'pynext_worker_total{{outcome="{outcome}"}} {count}')
        lines += ["# HELP pynext_rate_limit_fail_open_total Rate-limit decisions that could not be produced and failed open (H-2).",
                  "# TYPE pynext_rate_limit_fail_open_total counter",
                  f'pynext_rate_limit_fail_open_total {snap["rate_limit"]["fail_open"]}']
        lines += ["# HELP pynext_stage_timeouts_total Request-stage timeouts, by stage.",
                  "# TYPE pynext_stage_timeouts_total counter"]
        for stage, count in snap["stage_timeouts"].items():
            lines.append(f'pynext_stage_timeouts_total{{stage="{stage}"}} {count}')
        lines += ["# HELP pynext_errors_total Errors, by route, exception type and digest.",
                  "# TYPE pynext_errors_total counter"]
        # Emit from the joint (route, error_type, digest) counts, not the
        # snapshot marginals: a digest belongs to the error type that raised
        # it. Well-known signal digests get their own series; computed
        # digests are folded into digest="" so one traceback per request
        # cannot mint a new series (see module docstring).
        with self._lock:
            joint = sorted(self._errors.items())
        folded = {}
        for (route, error_type, digest), count in joint:
            if digest.startswith("PYNEXT_"):
                lines.append(
                    f'pynext_errors_total{{route="{route}",error_type="{error_type}",'
                    f'digest="{digest}"}} {count}')
            else:
                key = (route, error_type)
                folded[key] = folded.get(key, 0) + count
        for (route, error_type), count in sorted(folded.items()):
            lines.append(
                f'pynext_errors_total{{route="{route}",error_type="{error_type}",'
                f'digest=""}} {count}')
        lines += ["# HELP pynext_python_execution_queue_length Current Python execution queue depth.",
                  "# TYPE pynext_python_execution_queue_length gauge",
                  f'pynext_python_execution_queue_length {snap["queue"]["current"]}',
                  "# HELP pynext_island_bundles_total_bytes Total island bundle bytes.",
                  "# TYPE pynext_island_bundles_total_bytes gauge",
                  f'pynext_island_bundles_total_bytes {snap["island_bundles_total_bytes"]}']
        return "\n".join(lines) + "\n"


# -- OpenTelemetry (optional) ---------------------------------------------
_otel_trace = None
_otel_loaded = False


def _load_otel():
    global _otel_trace, _otel_loaded
    if _otel_loaded:
        return _otel_trace
    _otel_loaded = True
    try:
        import importlib
        _otel_trace = importlib.import_module("opentelemetry.trace")
    except Exception:
        _otel_trace = None
    return _otel_trace


def _reset_otel_cache():
    """Test hook: forget a previously probed OpenTelemetry import."""
    global _otel_trace, _otel_loaded
    _otel_trace, _otel_loaded = None, False


def is_tracing_available():
    return _load_otel() is not None


def _extract_otel_context(headers):
    try:
        propagate = __import__("importlib").import_module("opentelemetry.propagate")
    except Exception:
        return None
    try:
        carrier = {}
        for key, value in headers or ():
            k = key.decode("ascii") if isinstance(key, bytes) else str(key)
            v = value.decode("ascii") if isinstance(value, bytes) else str(value)
            carrier[k.lower()] = v
        return propagate.extract(carrier)
    except Exception:
        return None


class _Span:
    """Uniform wrapper: a real OTel span when available, else a no-op."""

    def __init__(self, real=None):
        self._real = real

    def set_attribute(self, key, value):
        if self._real is not None:
            try:
                self._real.set_attribute(key, value)
            except Exception:
                pass

    def __enter__(self):
        if self._real is not None:
            try:
                self._real.__enter__()
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        if self._real is not None:
            try:
                return self._real.__exit__(*exc)
            except Exception:
                pass
        return False


@contextlib.contextmanager
def span(name, attributes=None, *, enabled=True):
    """Create a span named ``name`` when tracing is enabled and OTel is installed.

    Otherwise this is a no-op that still yields a span-like object, so
    callers never need to branch on availability.
    """
    trace = _load_otel() if enabled else None
    if trace is None:
        yield _Span()
        return
    try:
        tracer = trace.get_tracer("pynext")
        context = _otel_context_var.get()
        get_current = getattr(trace, "get_current_span", None)
        if name != "pynext.request" and get_current is not None and get_current().get_span_context().is_valid:
            context = None
        cm = tracer.start_as_current_span(name, context=context) if context is not None else tracer.start_as_current_span(name)
        real = cm.__enter__()
    except Exception:
        yield _Span()
        return
    wrapped = _Span(real)
    for key, value in (attributes or {}).items():
        wrapped.set_attribute(key, value)
    try:
        yield wrapped
    except BaseException:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:
            pass
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
