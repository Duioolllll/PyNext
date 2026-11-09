"""Structured logging, metrics and optional OpenTelemetry tracing (spec v0.2 §12).

Design notes
------------
* Structured logs are on by default, metrics are on by default, tracing is
  off by default and is a no-op unless the ``opentelemetry`` packages are
  installed *and* tracing is enabled. Request insights (recent slow/failed
  request snapshots) are on in development mode only, by default. Nothing
  here imports OpenTelemetry at module import time.
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
* C-P1-6 saturation metrics: every recorded queue wait also lands in a
  slot-wait histogram (``pynext_queue_wait_ms_bucket``), every bridge entry
  samples the in-flight depth into a queue-depth histogram, and the bridge
  samples the sync-worker gauge (``app._sync_active`` / ``app.sync_workers``,
  read under the app's in-flight lock). Together with the per-status request
  counts they let the next benchmark tell the 503 causes apart: Rust slot
  saturation shows up as 503s with short Python queue waits, GIL/loop
  serialisation as long queue waits with few busy sync workers, and sync
  worker saturation as ``sync_workers_active`` pinned at capacity.
* Secret scrubbing (S-P1-8 / S-P1-29, Django ``SafeExceptionReporterFilter``
  parity): :func:`scrub_post_parameters` masks POST parameters whose names
  match ``API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE``, and the
  ``@sensitive_variables`` / ``@sensitive_post_parameters`` decorators mark
  individual names for masking. :func:`scrub_text` applies the same key
  patterns (plus ``Bearer``/``Basic`` credentials) to free text, and the
  JSON formatter runs every log message, exception body and extra field
  through it, so ``log.exception`` can no longer leak a secret or raw user
  input that happens to contain one. Scrubbing is pattern-based, not a
  full allowlist: a bare secret with no key next to it is not recognised.
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
    "RequestInsights",
    "JsonFormatter",
    "configure_logging",
    "new_request_id",
    "get_request_id",
    "get_trace_id",
    "parse_traceparent",
    "begin_request_context",
    "span",
    "is_tracing_available",
    # Secret scrubbing (S-P1-8 / S-P1-29).
    "SECRET_MASK",
    "SENSITIVE_POST_PARAM_RE",
    "is_sensitive_key",
    "scrub_post_parameters",
    "scrub_text",
    "scrub_log_value",
    "scrub_locals",
    "sensitive_variables",
    "sensitive_post_parameters",
    # Saturation histograms (C-P1-6).
    "SLOT_WAIT_BUCKETS_MS",
    "QUEUE_DEPTH_BUCKETS",
]

log = logging.getLogger("pynext")

# Names that must never appear as fields in a structured log line.
SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "x-api-key", "api-key",
    "api_key", "password", "passwd", "token", "secret", "access-token",
    "refresh-token", "session", "sessionid", "csrf", "body", "headers",
    "query", "query_string", "raw_url", "url",
})

# -- Secret scrubbing (S-P1-8 / S-P1-29) ----------------------------------
# Django SafeExceptionReporterFilter parity (django/views/debug.py:113):
# POST parameter names matching this pattern are replaced with SECRET_MASK
# in error reports and logs. The same pattern drives the free-text scrubber
# below, extended with COOKIE/SESSION/CREDENTIAL so "key=value" text in an
# exception body is masked too.
SECRET_MASK = "********************"  # 20 asterisks, Django parity
SENSITIVE_POST_PARAM_RE = re.compile(
    r"API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE", re.IGNORECASE
)
# "key = value" / "key: 'value'" / '"key": "value"' — group 1 keeps the key
# and separator (and opening quote), the value is replaced by SECRET_MASK.
# The value stops at a quote, whitespace, comma, semicolon or brace so a
# trailing quote/comma survives the replacement. A "Bearer <token>" /
# "Basic <credentials>" value is consumed whole, so
# "Authorization: Bearer <token>" does not leak the token after "Bearer"
# is masked.
_KEY_VALUE_RE = re.compile(
    r"((?:\"|')?"
    r"[A-Za-z0-9_.\-]*?(?:API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE|COOKIE|SESSION|CREDENTIAL)[A-Za-z0-9_.\-]*"
    r"(?:\"|')?\s*[:=]\s*(?:\"|')?)"
    r"(?:(?:Bearer|Basic)\s+)?[^\"'\s,;}\]]+",
    re.IGNORECASE,
)
# "Bearer <token>" / "Basic <credentials>" with no key next to them.
_BEARER_RE = re.compile(
    r"((?:Bearer|Basic)\s+)[A-Za-z0-9_.\-~+/=]+", re.IGNORECASE
)

# Histogram bucket upper bounds (C-P1-6). Slot-wait buckets are
# milliseconds; queue-depth buckets are in-flight request counts. Values
# above the last bound land in the "+Inf" bucket.
SLOT_WAIT_BUCKETS_MS = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0,
                        1000.0, 2500.0, 5000.0, 10000.0)
QUEUE_DEPTH_BUCKETS = (0, 1, 2, 4, 8, 16, 32, 64, 128)


def is_sensitive_key(name):
    """True when a parameter/field name looks like it holds a secret."""
    text = str(name)
    return bool(SENSITIVE_POST_PARAM_RE.search(text)) or text.lower() in SENSITIVE_KEYS


def _scrub_mapping(mapping, extra_sensitive=()):
    extra = {str(item).lower() for item in extra_sensitive}
    scrubbed = {}
    for key, value in mapping.items():
        if str(key).lower() in extra or is_sensitive_key(key):
            scrubbed[key] = SECRET_MASK
        elif isinstance(value, dict):
            scrubbed[key] = _scrub_mapping(value, extra_sensitive)
        else:
            scrubbed[key] = value
    return scrubbed


def scrub_post_parameters(params, extra_sensitive=()):
    """Return a copy of POST parameters with secrets masked (S-P1-8).

    Any key matching ``API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE``
    (case-insensitive, substring) — or named in ``extra_sensitive`` (e.g. a
    view's ``@sensitive_post_parameters`` list) — has its value replaced by
    ``SECRET_MASK``. Nested mappings are scrubbed the same way. The input
    is never mutated. Non-mapping input is returned unchanged.
    """
    if not isinstance(params, dict):
        return params
    return _scrub_mapping(params, extra_sensitive)


def sensitive_variables(*names):
    """Mark local variables as sensitive (Django ``@sensitive_variables``).

    The decorator only records the names on the function
    (``__sensitive_variables__``); :func:`scrub_locals` applies the marking
    when an error report is built from the function's frame locals.
    """
    def decorator(func):
        func.__sensitive_variables__ = frozenset(names)
        return func
    return decorator


def sensitive_post_parameters(*names):
    """Mark POST parameters as sensitive (Django ``@sensitive_post_parameters``).

    Records the names on the function (``__sensitive_post_parameters__``);
    pass them as ``extra_sensitive`` to :func:`scrub_post_parameters`.
    """
    def decorator(func):
        func.__sensitive_post_parameters__ = frozenset(names)
        return func
    return decorator


def scrub_locals(func, local_vars):
    """Return a copy of ``local_vars`` with sensitive entries masked.

    Names declared via ``@sensitive_variables`` on ``func`` are masked, as
    are any names matching the sensitive-key pattern — so a handler that
    forgets the decorator still does not leak ``api_key`` from its frame.
    ``func`` may be None (pattern-only scrubbing). The input is not mutated.
    """
    if not isinstance(local_vars, dict):
        return local_vars
    declared = set()
    if func is not None:
        declared = {str(item).lower()
                    for item in getattr(func, "__sensitive_variables__", ())}
    scrubbed = {}
    for key, value in local_vars.items():
        if str(key).lower() in declared or is_sensitive_key(key):
            scrubbed[key] = SECRET_MASK
        else:
            scrubbed[key] = value
    return scrubbed


def scrub_text(text):
    """Mask secret values embedded in free text (S-P1-29).

    Replaces the value in ``key=value`` / ``key: value`` / JSON
    ``"key": "value"`` pairs whose key looks sensitive, and any
    ``Bearer``/``Basic`` credential. Anything else — including a bare secret
    with no key next to it — is left untouched, so callers must not log raw
    secrets in the first place. Non-string input is returned unchanged.
    """
    if not isinstance(text, str):
        return text
    scrubbed = _KEY_VALUE_RE.sub(lambda m: m.group(1) + SECRET_MASK, text)
    return _BEARER_RE.sub(lambda m: m.group(1) + SECRET_MASK, scrubbed)


def scrub_log_value(value, _depth=0):
    """Recursively scrub a structured-log extra value (S-P1-29).

    Mapping keys that look sensitive are masked wholesale; string leaves
    go through :func:`scrub_text`; anything else passes through. Depth is
    capped so a self-referential value cannot recurse forever (the leaf is
    replaced by its repr, scrubbed).
    """
    if _depth > 5:
        return scrub_text(repr(value)) if isinstance(value, str) else repr(value)
    if isinstance(value, dict):
        return {key: SECRET_MASK if is_sensitive_key(key)
                else scrub_log_value(item, _depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_log_value(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value

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
# Middleware/security decision for the current request (P2-2, §3.3 #18):
# set by ``App._run_middleware`` / ``App._rate_limit_denied`` so the access
# log line and the ``pynext.request`` span carry the decision. The values
# are bounded outcome words (never paths or user input), so they are safe
# as log fields and span attributes.
middleware_outcome_var = contextvars.ContextVar("pynext_middleware_outcome", default=None)
middleware_actor_var = contextvars.ContextVar("pynext_middleware_actor", default=None)
middleware_rewrote_path_var = contextvars.ContextVar("pynext_middleware_rewrote_path", default=None)
rate_limit_outcome_var = contextvars.ContextVar("pynext_rate_limit_outcome", default=None)
_otel_context_var = contextvars.ContextVar("pynext_otel_context", default=None)


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_tristate(name):
    """Parse an env var as True/False, or None when unset/unrecognized (auto)."""
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _env_int(name, default, minimum=None):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        return default
    return max(minimum, value) if minimum is not None else value


def _env_float(name):
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


@dataclass
class ObservabilityConfig:
    """Flags controlling structured logs, metrics and tracing.

    Defaults: structured logs on, metrics on, tracing off unless OpenTelemetry
    is installed and tracing is enabled. Environment overrides:
    ``PYNEXT_STRUCTURED_LOGS``, ``PYNEXT_METRICS``, ``PYNEXT_TRACING``,
    ``PYNEXT_LOG_LEVEL``.

    Request insights (recent slow/failed request snapshots, see
    :class:`RequestInsights`) are off unless enabled: ``request_insights``
    forces them on (``True``) or off (``False``); ``None`` (the default)
    means "auto" — on in development (``debug=True``) apps, off in
    production. Environment overrides: ``PYNEXT_REQUEST_INSIGHTS``
    (``1``/``0``), ``PYNEXT_REQUEST_INSIGHTS_MAX`` (ring-buffer size,
    default 100), ``PYNEXT_REQUEST_INSIGHTS_SLOW_MS`` (fixed slow
    threshold in ms; unset means the running p95 of recent latencies).
    """

    structured_logs: bool = True
    metrics: bool = True
    tracing: bool = False
    log_level: str = "INFO"
    request_insights: bool | None = None
    request_insights_max: int = 100
    request_insights_slow_ms: float | None = None

    @classmethod
    def from_env(cls):
        return cls(
            structured_logs=_env_bool("PYNEXT_STRUCTURED_LOGS", True),
            metrics=_env_bool("PYNEXT_METRICS", True),
            tracing=_env_bool("PYNEXT_TRACING", False),
            log_level=os.environ.get("PYNEXT_LOG_LEVEL", "INFO"),
            request_insights=_env_tristate("PYNEXT_REQUEST_INSIGHTS"),
            request_insights_max=_env_int("PYNEXT_REQUEST_INSIGHTS_MAX", 100, minimum=1),
            request_insights_slow_ms=_env_float("PYNEXT_REQUEST_INSIGHTS_SLOW_MS"),
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
        (middleware_outcome_var, middleware_outcome_var.set(None)),
        (middleware_actor_var, middleware_actor_var.set(None)),
        (middleware_rewrote_path_var, middleware_rewrote_path_var.set(None)),
        (rate_limit_outcome_var, rate_limit_outcome_var.set(None)),
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
    """One JSON object per line. Sensitive extra fields are dropped.

    S-P1-29: the message, the formatted exception body and every surviving
    extra field also pass through the secret scrubber, so a secret embedded
    in an exception message or in a nested extra value (``{"params":
    {"api_key": ...}}``) is masked instead of logged verbatim. Fields whose
    own name is sensitive are still dropped outright (see SENSITIVE_KEYS);
    the scrubber only handles secrets smuggled inside otherwise-safe
    fields.
    """

    def format(self, record):
        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_text(record.getMessage()),
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
            value = scrub_log_value(value)
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = scrub_text(repr(value))
            data[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            data["exception"] = scrub_text(self.formatException(record.exc_info))
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


def _bucket_index(value, boundaries):
    """Index of the first bucket whose upper bound is >= ``value``.

    ``len(boundaries)`` means the value belongs in the "+Inf" bucket.
    """
    import bisect
    return bisect.bisect_left(boundaries, value)


def _histogram_snapshot(counts, boundaries, total):
    """Cumulative Prometheus-style bucket counts plus count/sum."""
    cumulative = 0
    buckets = []
    for bound, count in zip(boundaries, counts):
        cumulative += count
        buckets.append({"le": bound, "count": cumulative})
    cumulative += counts[len(boundaries)] if len(counts) > len(boundaries) else 0
    buckets.append({"le": "+Inf", "count": cumulative})
    return {"buckets": buckets, "count": cumulative, "sum": round(total, 3)}


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
        # C-P1-6 saturation histograms. Slot waits are recorded once, in
        # observe_request (the dispatch finally block), which is the single
        # recording point for both the ASGI and the Axum paths — recording
        # them in server.py too would double-count. Queue depth is sampled
        # at bridge entry (observe_queue_enter); sync-worker activity is
        # sampled by server.py's bridge (observe_sync_workers).
        self._slot_wait_buckets = [0] * (len(SLOT_WAIT_BUCKETS_MS) + 1)
        self._slot_wait_total_ms = 0.0
        self._queue_depth_buckets = [0] * (len(QUEUE_DEPTH_BUCKETS) + 1)
        self._queue_depth_total = 0
        self._queue_depth_samples = 0
        self._sync_workers = {"active": 0, "capacity": None, "max_active": 0}
        self._queue_current = 0
        self._queue_max = 0
        self._python_ms_total = 0.0
        self._python_ms_count = 0
        self._rust_ms_total = 0.0
        self._rust_ms_count = 0
        self._cache = {"hit": 0, "miss": 0, "stale": 0, "busy": 0}
        self._regeneration = {"success": 0, "failure": 0, "started": 0}
        self._rate_limit = {"allowed": 0, "denied": 0, "exempt": 0, "error": 0, "fail_open": 0}
        self._middleware = {"continued": 0, "short_circuited": 0, "error": 0}
        self._workers = {"starts": 0, "timeouts": 0, "unavailable": 0}
        # H-2: "fail_open" counts rate-limit decisions that could not be
        # produced (decision gate full or deadline expired) and failed
        # open instead of 503.
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
                # C-P1-6: slot-wait histogram (single recording point; see
                # __init__). The deque above feeds percentiles, the buckets
                # feed the Prometheus histogram and the 503-cause split.
                wait = float(queue_wait_ms)
                self._slot_wait_buckets[_bucket_index(wait, SLOT_WAIT_BUCKETS_MS)] += 1
                self._slot_wait_total_ms += wait
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
            # C-P1-6: in-flight depth seen by the arriving request. This is
            # the queue-length distribution the next benchmark reads: a
            # depth histogram piled up at the top bucket while 503s climb
            # means the Rust execution slots are saturated before Python.
            self._queue_depth_buckets[_bucket_index(self._queue_current, QUEUE_DEPTH_BUCKETS)] += 1
            self._queue_depth_total += self._queue_current
            self._queue_depth_samples += 1

    def observe_queue_exit(self):
        if not self.enabled:
            return
        with self._lock:
            self._queue_current = max(0, self._queue_current - 1)

    def observe_sync_workers(self, active, capacity=None):
        """Record a sample of busy sync-handler workers (C-P1-6).

        Sampled, not exact: the bridge reads ``app._sync_active`` on
        request entry/exit, so ``max_active`` is a lower bound on the true
        peak — but a gauge pinned at ``capacity`` across samples is still
        an unambiguous sync-worker saturation signal, and it stays
        separable from long queue waits with idle sync workers (GIL/loop
        serialisation). ``capacity`` is the app's ``sync_workers`` limit.
        """
        if not self.enabled:
            return
        with self._lock:
            self._sync_workers["active"] = max(0, int(active))
            if capacity is not None:
                self._sync_workers["capacity"] = int(capacity)
            self._sync_workers["max_active"] = max(
                self._sync_workers["max_active"], self._sync_workers["active"])

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

    # -- middleware / security decisions ----------------------------------
    def observe_rate_limit(self, outcome):
        """Count one rate-limit admission decision.

        Outcomes: ``allowed`` (token consumed), ``denied`` (429),
        ``exempt`` (exempt path, no token charged), ``error`` (the
        decision itself failed, fail closed with 503). Observed at the
        decision on both transports, so a denied request is counted even
        though it never reaches dispatch — and an exempt 404 shows up as
        an ``exempt`` decision next to a 404 in the request counters,
        which is the shape a B-1 style bypass takes. Unknown outcomes
        are ignored so a typo can never corrupt the counters.
        """
        if not self.enabled or outcome not in self._rate_limit:
            return
        with self._lock:
            self._rate_limit[outcome] += 1

    def observe_middleware(self, outcome):
        """Count one middleware-chain outcome.

        Outcomes: ``continued`` (the chain reached the terminal),
        ``short_circuited`` (a middleware answered on its own), ``error``
        (a middleware raised or violated the chain contract). A failure
        in the handler behind the terminal still counts as ``continued``:
        the chain continued; the 500 is the handler's failure, not the
        middleware's. Unknown outcomes are ignored.
        """
        if not self.enabled or outcome not in self._middleware:
            return
        with self._lock:
            self._middleware[outcome] += 1

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
                    # C-P1-6: cumulative buckets; "le" is ms for waits,
                    # in-flight requests for depth.
                    "wait_histogram_ms": _histogram_snapshot(
                        self._slot_wait_buckets, SLOT_WAIT_BUCKETS_MS,
                        self._slot_wait_total_ms),
                    "depth_histogram": _histogram_snapshot(
                        self._queue_depth_buckets, QUEUE_DEPTH_BUCKETS,
                        float(self._queue_depth_total)),
                },
                "sync_workers": dict(self._sync_workers),
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
                "rate_limit": dict(self._rate_limit),
                "middleware": dict(self._middleware),
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
        lines += ["# HELP pynext_rate_limit_total Rate-limit admission decisions, by outcome.",
                  "# TYPE pynext_rate_limit_total counter"]
        for outcome, count in snap["rate_limit"].items():
            lines.append(f'pynext_rate_limit_total{{outcome="{outcome}"}} {count}')
        lines += ["# HELP pynext_middleware_total Middleware chain outcomes, by outcome.",
                  "# TYPE pynext_middleware_total counter"]
        for outcome, count in snap["middleware"].items():
            lines.append(f'pynext_middleware_total{{outcome="{outcome}"}} {count}')
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
                  "# HELP pynext_queue_wait_ms Slot/Python-loop wait before the handler starts, in milliseconds.",
                  "# TYPE pynext_queue_wait_ms histogram"]
        wait_hist = snap["queue"]["wait_histogram_ms"]
        for bucket in wait_hist["buckets"]:
            lines.append(f'pynext_queue_wait_ms_bucket{{le="{bucket["le"]}"}} {bucket["count"]}')
        lines.append(f'pynext_queue_wait_ms_sum {wait_hist["sum"]}')
        lines.append(f'pynext_queue_wait_ms_count {wait_hist["count"]}')
        lines += ["# HELP pynext_python_execution_queue_depth In-flight depth observed when a request entered the bridge.",
                  "# TYPE pynext_python_execution_queue_depth histogram"]
        depth_hist = snap["queue"]["depth_histogram"]
        for bucket in depth_hist["buckets"]:
            lines.append(f'pynext_python_execution_queue_depth_bucket{{le="{bucket["le"]}"}} {bucket["count"]}')
        lines.append(f'pynext_python_execution_queue_depth_sum {depth_hist["sum"]}')
        lines.append(f'pynext_python_execution_queue_depth_count {depth_hist["count"]}')
        sync_workers = snap["sync_workers"]
        lines += ["# HELP pynext_sync_workers_active Sync handler workers currently busy (sampled at bridge entry/exit).",
                  "# TYPE pynext_sync_workers_active gauge",
                  f'pynext_sync_workers_active {sync_workers["active"]}',
                  "# HELP pynext_sync_workers_max_active Maximum sync handler workers simultaneously busy since start (sampled).",
                  "# TYPE pynext_sync_workers_max_active gauge",
                  f'pynext_sync_workers_max_active {sync_workers["max_active"]}']
        if sync_workers["capacity"] is not None:
            lines += ["# HELP pynext_sync_workers_capacity Sync handler worker capacity.",
                      "# TYPE pynext_sync_workers_capacity gauge",
                      f'pynext_sync_workers_capacity {sync_workers["capacity"]}']
        lines += ["# HELP pynext_island_bundles_total_bytes Total island bundle bytes.",
                  "# TYPE pynext_island_bundles_total_bytes gauge",
                  f'pynext_island_bundles_total_bytes {snap["island_bundles_total_bytes"]}']
        return "\n".join(lines) + "\n"


class RequestInsights:
    """Bounded ring buffer of recent slow / failed request snapshots.

    Separate from :class:`MetricsCollector`: metrics aggregate, this keeps
    individual request snapshots so one slow request can be inspected
    without digging through logs. A request is kept when it is slow —
    slower than a fixed ``slow_threshold_ms`` when set, otherwise slower
    than the running p95 of recent latencies — or when it failed (status
    >= 500 or an ``error_type``). The buffer is a ``deque(maxlen=...)``
    and the p95 window is bounded too, so memory cannot grow without
    bound no matter how long the server runs. Snapshots record only the
    route *pattern*, never the raw URL, so no user data or credentials
    can leak in.

    In-process API only: there is deliberately no HTTP endpoint serving
    these snapshots (routes + latencies are internal debug data). Enabled
    in development mode by default; see :class:`ObservabilityConfig`.
    """

    DEFAULT_MAX_ENTRIES = 100
    _P95_WINDOW = 512
    _MIN_SAMPLES = 20

    def __init__(self, enabled=True, max_entries=DEFAULT_MAX_ENTRIES,
                 slow_threshold_ms=None):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if (slow_threshold_ms is not None and
                (isinstance(slow_threshold_ms, bool) or
                 not isinstance(slow_threshold_ms, (int, float)) or slow_threshold_ms < 0)):
            raise ValueError("slow_threshold_ms must be a non-negative number or None")
        self.enabled = enabled
        self.max_entries = max_entries
        self.slow_threshold_ms = slow_threshold_ms
        self._lock = threading.Lock()
        self._entries = deque(maxlen=max_entries)
        self._latencies = deque(maxlen=self._P95_WINDOW)
        self._observed = 0
        self._recorded = 0

    def _is_slow(self, latency_ms):
        if self.slow_threshold_ms is not None:
            return latency_ms >= self.slow_threshold_ms
        if len(self._latencies) < self._MIN_SAMPLES:
            # Not enough of a population yet to know what "slow" means;
            # only definite failures are kept during warm-up.
            return False
        return latency_ms > _percentiles(list(self._latencies))["p95"]

    def observe(self, *, request_id, method, route, status, latency_ms,
                python_ms=None, rust_ms=None, queue_wait_ms=None, error_type=None):
        """Record one finished request if it is slow or failed. No-op when disabled."""
        if not self.enabled:
            return
        with self._lock:
            self._observed += 1
            latency = float(latency_ms)
            self._latencies.append(latency)
            failed = status >= 500 or error_type is not None
            if not failed and not self._is_slow(latency):
                return
            self._recorded += 1
            self._entries.append({
                "request_id": request_id,
                "method": method,
                "route": route,
                "status": int(status),
                "latency_ms": round(latency, 3),
                "python_ms": round(float(python_ms), 3) if python_ms is not None else None,
                "rust_ms": round(float(rust_ms), 3) if rust_ms is not None else None,
                "queue_wait_ms": round(float(queue_wait_ms), 3) if queue_wait_ms is not None else None,
                "error_type": error_type,
                "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

    def clear(self):
        """Drop all kept snapshots and reset the counters / p95 window."""
        with self._lock:
            self._entries.clear()
            self._latencies.clear()
            self._observed = 0
            self._recorded = 0

    def snapshot(self):
        """Return the kept snapshots, oldest first, plus filter counters."""
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            threshold = self.slow_threshold_ms
            if threshold is None and len(self._latencies) >= self._MIN_SAMPLES:
                threshold = _percentiles(list(self._latencies))["p95"]
            return {
                "enabled": True,
                "max_entries": self.max_entries,
                "observed": self._observed,
                "recorded": self._recorded,
                "slow_threshold_ms": threshold,
                "requests": [dict(entry) for entry in self._entries],
            }


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
