"""``pynext.config.py`` loading, defaults and schema validation (Phase 0).

This module is the single source of truth for PyNext server settings.
Before it existed the same defaults lived in four places — the CLI
argument parser (``cli.py``), :func:`pynext.app.create_app`, :func:`pynext.server.serve`
and :class:`pynext.supervisor.WorkerConfig` — and ``pynext.config.py``
was a ghost: the dev watcher watched it and the manuals mentioned it,
but nothing ever loaded it.

Load pipeline (mirrors Next.js ``loadConfig``: file → defaults merge →
schema validation, in ``server/config.ts``):

1. Start from :data:`DEFAULT_CONFIG` (the frozen defaults, one copy).
2. Apply the project ``pynext.config.py`` file, if present — it must
   define a top-level ``config`` dict of plain data. The file is executed
   once at load time (like ``next.config.js``); it must not define hooks
   or mutate anything — settings are declarative data only.
3. Apply environment variables (``PYNEXT_*``) — they override the file.
4. Apply explicit command-line options — they override everything.

Validation follows the Next.js warning/fatal split (``config-schema.ts``
+ ``normalizeNextConfigZodErrors``): an unknown key is a warning and
loading continues, while a wrong type, an out-of-range value or a bad
security value (for example an invalid CSRF signing key) is a fatal
:class:`ConfigError` and startup fails. All schema errors are collected
and reported together, with the setting name and the source that set it.

The resolved settings are a frozen :class:`PyNextConfig`; the loader
returns a :class:`ResolvedConfig` carrying the values, any warnings, the
config file that was read (if any) and where each value came from.
"""
from __future__ import annotations

import difflib
import importlib.util
import math
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .http import _ORIGIN, canonical_origin, parse_csrf_signing_key

__all__ = [
    "CONFIG_FILENAME",
    "CONFIG_VARIABLE",
    "DEFAULT_CONFIG",
    "ConfigError",
    "PyNextConfig",
    "ResolvedConfig",
    "find_config_file",
    "load_config",
    "load_config_file",
]

CONFIG_FILENAME = "pynext.config.py"
CONFIG_VARIABLE = "config"

#: Phases a config can be loaded for (Next.js ``PHASE_*`` equivalent).
PHASES = ("dev", "build", "start")

#: Log levels accepted for ``log_level`` (normalised to upper case).
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Bounds shared with the CLI validators in ``cli.py`` and the native
#: server caps in ``server.py``; keep them in sync (``test_config.py``
#: checks the defaults, ``test_cli.py`` checks the CLI bounds).
_MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024


class ConfigError(ValueError):
    """A fatal configuration problem: startup must fail, not continue."""


@dataclass(frozen=True)
class PyNextConfig:
    """All server settings, with their defaults, in one place.

    Every default here is the value the CLI, :func:`create_app`,
    :func:`serve` and :class:`WorkerConfig` used to repeat separately;
    those call sites now read from :data:`DEFAULT_CONFIG` instead.
    ``None`` keeps its historical meaning per field: ``max_waiting``
    means "the native default", ``csrf_enabled`` / ``csrf_cookie_secure``
    mean "auto", ``structured_logs`` / ``metrics`` / ``tracing`` /
    ``log_level`` mean "decide from the environment".
    """

    # Listening.
    host: str = "127.0.0.1"
    port: int = 3000
    # Request admission and body limits (serve()).
    # C-P0-2: raised together with the multi-worker default (C-P0-1) —
    # the workers share one accept queue, so the per-process admission
    # budget and the wait for a slot are sized for all workers at once.
    max_concurrent: int = 128
    acquire_timeout_ms: int = 3000
    max_waiting: int | None = None
    body_capacity: int = 16
    body_timeout_ms: int = 10000
    # Connection limits (serve()).
    max_connections: int = 1024
    max_connections_per_ip: int = 128
    # Per-IP execution quota (serve()): at most this many requests from
    # one client IP may hold an execution slot at once (H-5). The global
    # pool is ``max_concurrent`` (64), so the default 16 is one quarter
    # of the pool: a single IP can no longer take all 64 slots with slow
    # requests and 503 every other user.
    max_concurrent_per_ip: int = 16
    header_read_timeout_ms: int = 10000
    body_min_rate_bytes_per_sec: int = 1024
    # Lifecycle.
    shutdown_timeout: float = 30.0
    # C-P0-1: two worker processes by default, so one worker crashing (or
    # a rolling restart) does not take the server down. The memory ISR
    # cache is per-process and is NOT shared between workers — use a
    # shared cache backend ('sqlite' or 'redis') to share ISR entries
    # across workers (see supervisor.py). Only the count changes here;
    # the supervisor still spawns `workers` processes on one listener.
    workers: int = 2
    # ISR cache.
    cache_backend: str = "memory"
    cache_path: str | None = None
    cache_failure_policy: str = "fail_open"
    # Rate limiting (H-1): on by default — 300 requests per client IP
    # per 60-second window. A default deployment used to expose no
    # framework-level throttle at all (``rate_limit=0``), so the only
    # defence was the 64 execution slots, which reject attackers and
    # legitimate users alike with 503. Set ``rate_limit=0`` to opt out
    # (``pynext start`` then warns that throttling is disabled).
    # Health probes are exempt by default so a load balancer can always
    # reach /healthz and /readyz.
    rate_limit: int = 300
    rate_limit_window: float = 60.0
    rate_limit_backend: str = "memory"
    rate_limit_ipv6_prefix_len: int = 64
    rate_limit_failure_policy: str = "fail_closed"
    rate_limit_exempt_paths: tuple = ("/healthz", "/readyz")
    trusted_proxies: tuple = ()
    # Security.
    csrf_enabled: bool | None = None
    csrf_signing_key: str | bytes | None = None
    csrf_cookie_secure: bool | None = None
    cors_allowed_origins: tuple = ()
    cors_allow_credentials: bool = False
    csp_nonce_enabled: bool = False
    websocket_max_message: int = 65_536
    websocket_allowed_origins: tuple = ()
    websocket_allow_missing_origin: bool = False
    # Observability (None = from the environment, as before).
    structured_logs: bool | None = None
    metrics: bool | None = None
    tracing: bool | None = None
    log_level: str | None = None


#: The single default source. Frozen: callers must not mutate it.
DEFAULT_CONFIG = PyNextConfig()


@dataclass(frozen=True)
class ResolvedConfig:
    """A loaded configuration: values plus load-time provenance."""

    values: PyNextConfig
    warnings: tuple = ()
    config_file: Path | None = None
    phase: str = "dev"
    #: ``{key: "default" | "env" | "file" | "cli"}`` — where each value came from.
    sources: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    def source_of(self, key):
        """Where ``key``'s value came from (``"default"`` if unset anywhere)."""
        return self.sources.get(key, "default")


@dataclass(frozen=True)
class _Spec:
    """One field's schema: kind, bounds and allowed values."""

    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple | None = None
    # When set, the minimum is exclusive (value must be > minimum).
    exclusive_minimum: bool = False


_SPECS = {
    "host": _Spec("str"),
    "port": _Spec("int", 0, 65535),
    "max_concurrent": _Spec("int", 1, 1000000),
    "acquire_timeout_ms": _Spec("int", 0, 86400000),
    "max_waiting": _Spec("int_or_none", 0, 1000000),
    "body_capacity": _Spec("int", 1, 1000000),
    "body_timeout_ms": _Spec("int", 0, 86400000),
    "max_connections": _Spec("int", 1, 1000000),
    "max_connections_per_ip": _Spec("int", 1, 1000000),
    "max_concurrent_per_ip": _Spec("int", 1, 1000000),
    "header_read_timeout_ms": _Spec("int", 0, 86400000),
    "body_min_rate_bytes_per_sec": _Spec("int", 0, 1000000000),
    "shutdown_timeout": _Spec("float", 0, 3600),
    "workers": _Spec("int", 1, 64),
    "cache_backend": _Spec("str", choices=("memory", "sqlite", "redis")),
    "cache_path": _Spec("str_or_none"),
    "cache_failure_policy": _Spec("str", choices=("fail_open", "fail_closed")),
    "rate_limit": _Spec("int", 0, None),
    "rate_limit_window": _Spec("float", 0, None, exclusive_minimum=True),
    "rate_limit_backend": _Spec("str", choices=("memory", "redis")),
    "rate_limit_ipv6_prefix_len": _Spec("int", 0, 128),
    "rate_limit_failure_policy": _Spec("str", choices=("fail_open", "fail_closed")),
    "rate_limit_exempt_paths": _Spec("str_tuple"),
    "trusted_proxies": _Spec("str_tuple"),
    "csrf_enabled": _Spec("bool_or_none"),
    "csrf_signing_key": _Spec("csrf_key"),
    "csrf_cookie_secure": _Spec("bool_or_none"),
    "cors_allowed_origins": _Spec("cors_origins"),
    "cors_allow_credentials": _Spec("bool"),
    "csp_nonce_enabled": _Spec("bool"),
    "websocket_max_message": _Spec("int", 1, _MAX_WS_MESSAGE_BYTES),
    "websocket_allowed_origins": _Spec("ws_origins"),
    "websocket_allow_missing_origin": _Spec("bool"),
    "structured_logs": _Spec("bool_or_none"),
    "metrics": _Spec("bool_or_none"),
    "tracing": _Spec("bool_or_none"),
    "log_level": _Spec("log_level"),
}

def _env_bool_value(value):
    """Parse an env-var bool the way :func:`observability._env_bool` does."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


#: Environment variables that feed settings (before the config file).
_ENV_VARS = (
    ("PYNEXT_STRUCTURED_LOGS", "structured_logs", _env_bool_value),
    ("PYNEXT_METRICS", "metrics", _env_bool_value),
    ("PYNEXT_TRACING", "tracing", _env_bool_value),
    ("PYNEXT_LOG_LEVEL", "log_level", str),
    ("PYNEXT_CSRF_SIGNING_KEY", "csrf_signing_key", str),
)


def _fail(source, key, problem, value=None):
    detail = f" (got {value!r})" if value is not None else ""
    raise ConfigError(f"{source}: {key!r} {problem}{detail}")


def _bound_text(value):
    """Render a bound without ``:g`` scientific notation (16777216, not 1.67772e+07)."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _check_bounds(source, key, value, spec):
    if spec.minimum is not None:
        if spec.exclusive_minimum:
            if value <= spec.minimum:
                _fail(source, key, f"must be a number > {_bound_text(spec.minimum)}", value)
        elif value < spec.minimum:
            _fail(source, key, f"must be >= {_bound_text(spec.minimum)}", value)
    if spec.maximum is not None and value > spec.maximum:
        _fail(source, key, f"must be <= {_bound_text(spec.maximum)}", value)


def _validate_value(key, value, *, source):
    """Validate one raw value; return the normalised value or raise :class:`ConfigError`."""
    spec = _SPECS[key]
    kind = spec.kind
    if kind == "bool":
        if type(value) is not bool:
            _fail(source, key, "must be a bool", value)
        return value
    if kind == "bool_or_none":
        if value is not None and type(value) is not bool:
            _fail(source, key, "must be a bool or None", value)
        return value
    if kind == "int":
        if type(value) is not int:
            _fail(source, key, "must be an integer", value)
        _check_bounds(source, key, value, spec)
        return value
    if kind == "int_or_none":
        if value is None:
            return None
        if type(value) is not int:
            _fail(source, key, "must be an integer or None", value)
        _check_bounds(source, key, value, spec)
        return value
    if kind == "float":
        if type(value) is bool or not isinstance(value, (int, float)):
            _fail(source, key, "must be a number", value)
        if not math.isfinite(value):
            _fail(source, key, "must be a finite number", value)
        _check_bounds(source, key, value, spec)
        return float(value)
    if kind == "str":
        if not isinstance(value, str):
            _fail(source, key, "must be a string", value)
        if spec.choices is not None and value not in spec.choices:
            _fail(source, key, f"must be one of {', '.join(spec.choices)}", value)
        if key == "host" and not value:
            _fail(source, key, "must not be empty", value)
        return value
    if kind == "str_or_none":
        if value is not None and not isinstance(value, str):
            _fail(source, key, "must be a string or None", value)
        return value
    if kind == "log_level":
        if not isinstance(value, str) or value.upper() not in _LOG_LEVELS:
            _fail(source, key, f"must be one of {', '.join(_LOG_LEVELS)}", value)
        return value.upper()
    if kind == "csrf_key":
        if value is None:
            return None
        try:
            parse_csrf_signing_key(value, source=key)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{source}: {exc}") from exc
        return value
    if kind in ("str_tuple", "cors_origins", "ws_origins"):
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            _fail(source, key, "must be a list or tuple of strings", value)
        for item in value:
            if not isinstance(item, str):
                _fail(source, key, "must be a list or tuple of strings", value)
            if kind == "cors_origins" and item != "*" and not _ORIGIN.fullmatch(item):
                _fail(source, key, f"has an invalid CORS origin {item!r}", value)
            if kind == "ws_origins" and canonical_origin(item) is None:
                _fail(source, key, f"has an invalid WebSocket origin {item!r}", value)
        return tuple(value)
    raise AssertionError(f"unknown spec kind {kind!r}")  # pragma: no cover


def _validate_config_dict(raw, *, source):
    """Validate a raw ``config`` dict; return ``(cleaned, warnings)``.

    Unknown keys warn (Next.js behaviour for unknown options) and are
    dropped; every other problem raises :class:`ConfigError`. All fatal
    problems are collected and reported in one error.
    """
    cleaned = {}
    warnings = []
    errors = []
    for key, value in raw.items():
        if key not in _SPECS:
            hint = difflib.get_close_matches(key, _SPECS, n=1, cutoff=0.6)
            suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
            warnings.append(f"{source}: unknown config key {key!r}; ignoring it{suggestion}")
            continue
        try:
            cleaned[key] = _validate_value(key, value, source=source)
        except ConfigError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigError("; ".join(errors))
    return cleaned, warnings


def _cross_check(values, *, source, workers_source="default"):
    """Warn about option combinations that are legal but surprising.

    The multi-worker memory-cache warning only fires when ``workers``
    was set explicitly (file/env/cli): with the multi-worker default
    (C-P0-1) it would otherwise warn on every startup even though the
    user changed nothing.

    One combination is fatal, not just surprising (H-4):
    ``cors_allowed_origins=["*"]`` with ``cors_allow_credentials=True``
    would echo any request Origin back with credentials allowed, so an
    arbitrary site could make credentialed (cookie-bearing) cross-origin
    requests. Refuse it at startup; list the origins explicitly instead.
    """
    if "*" in values.cors_allowed_origins and values.cors_allow_credentials:
        raise ConfigError(
            f"{source}: 'cors_allowed_origins' contains '*' together with "
            "'cors_allow_credentials=True'; that would allow any origin to "
            "make credentialed cross-origin requests. List the allowed "
            "origins explicitly instead of '*'.")
    warnings = []
    if values.cache_path is not None and values.cache_backend != "sqlite":
        warnings.append(
            f"{source}: 'cache_path' is only used with cache_backend 'sqlite'; "
            f"it will be ignored with backend {values.cache_backend!r}")
    if (values.workers > 1 and values.cache_backend == "memory"
            and workers_source != "default"):
        warnings.append(
            f"{source}: with workers={values.workers} and the memory ISR cache "
            "backend, each worker keeps its own ISR cache; use "
            "cache_backend 'sqlite' or 'redis' to share it")
    return warnings


def find_config_file(root):
    """Return the project's ``pynext.config.py`` path, or ``None``.

    Only the project root is searched: the project directory given to
    ``pynext dev`` / ``build`` / ``start`` is the config root.
    """
    candidate = Path(root).resolve() / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load_config_file(path):
    """Load one config file; return ``(raw_dict, warnings)``.

    The file must define a top-level ``config`` dict. A file without one
    warns and contributes nothing; a file that fails to import or whose
    ``config`` is not a dict is a fatal :class:`ConfigError`.
    """
    path = Path(path)
    module_name = f"_pynext_user_config_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"{path}: cannot load {CONFIG_FILENAME}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"{path}: failed to load {CONFIG_FILENAME}: "
            f"{exc.__class__.__name__}: {exc}") from exc
    finally:
        sys.modules.pop(module_name, None)
    if not hasattr(module, CONFIG_VARIABLE):
        return {}, [f"{path}: no top-level `config` dict found; ignoring the file"]
    raw = getattr(module, CONFIG_VARIABLE)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: `config` must be a dict, got {type(raw).__name__}")
    return raw, []


def load_config(root, phase, *, cli_overrides=None, env=None):
    """Resolve the effective configuration for ``phase``.

    Precedence: explicit CLI options > environment > config file >
    :data:`DEFAULT_CONFIG`. ``cli_overrides`` maps config keys to values
    the user set on the command line; ``env`` defaults to
    :data:`os.environ` (pass a dict in tests). Raises :class:`ConfigError`
    on any fatal validation problem.
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}, got {phase!r}")
    if env is None:
        env = os.environ
    merged = dict(vars(DEFAULT_CONFIG))
    sources = {key: "default" for key in _SPECS}
    warnings = []

    # Layers apply in increasing precedence: file, then environment,
    # then explicit CLI options — each later layer overwrites the earlier.
    config_file = find_config_file(root)
    if config_file is not None:
        raw, file_warnings = load_config_file(config_file)
        warnings.extend(file_warnings)
        cleaned, schema_warnings = _validate_config_dict(raw, source=str(config_file))
        warnings.extend(schema_warnings)
        for key, value in cleaned.items():
            merged[key] = value
            sources[key] = "file"

    for env_name, key, convert in _ENV_VARS:
        if env_name in env:
            merged[key] = _validate_value(key, convert(env[env_name]),
                                          source=f"environment variable {env_name}")
            sources[key] = "env"

    for key, value in (cli_overrides or {}).items():
        if key not in _SPECS:
            continue
        merged[key] = _validate_value(key, value, source=f"command-line option --{key.replace('_', '-')}")
        sources[key] = "cli"

    values = PyNextConfig(**merged)
    warnings.extend(_cross_check(values, source=str(config_file) if config_file else "config",
                                 workers_source=sources["workers"]))
    return ResolvedConfig(values=values, warnings=tuple(warnings),
                          config_file=config_file, phase=phase,
                          sources=MappingProxyType(sources))
