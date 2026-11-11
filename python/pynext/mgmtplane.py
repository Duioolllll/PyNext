"""Management plane (S-P1-32, Sanic lesson 3): fail-closed by default.

Sanic's Inspector looks safe because it is off by default, but turning it
on without a key leaves the management API unauthenticated
(``worker/inspector.py:80`` ``if self.api_key:``), and the key comparison
itself is a plain ``!=`` (``:85``). One config mistake then exposes the
whole management surface. PyNext does the opposite:

* The HTTP management surface (``/metrics`` Prometheus exposition, and
  optionally ``/readyz``) is **disabled by default**.
* Enabling it requires a key. Enabling it without a key **refuses to
  start** -- a loud error at startup, never a warning followed by an
  open endpoint.
* Key comparison is constant-time (``hmac.compare_digest``), never ``==``.
* The server binds ``127.0.0.1`` by default; binding ``0.0.0.0``/``::``
  logs a warning (see :func:`pynext.server.start_native`).

What counts as the management plane here
----------------------------------------
* ``/metrics`` -- the only data-exposing management endpoint PyNext
  serves over HTTP. It renders ``MetricsCollector.prometheus_text()``.
  There is no Inspector-equivalent HTTP surface in this codebase; any
  future inspector-style endpoint must be registered behind this same
  config and the same key check, not added as a bare route.
* ``/readyz`` -- a minimal readiness probe (``ready``/``not ready`` only,
  no data). It stays unauthenticated by default so load balancers can
  probe it, exactly like ``/healthz``. Operators who want it gated can
  set ``readyz_requires_key=True``; the gate is then enforced in
  :func:`pynext.server.start_native` before ``App.dispatch`` runs, so
  the readiness verdict itself still comes from the app.

Keys are never logged. The key value must come from configuration or the
``PYNEXT_MGMT_KEY`` environment variable -- never from a file checked
into the repo, and never printed in an error message.
"""
import hmac
import os
from dataclasses import dataclass

__all__ = ["ManagementPlaneConfig", "ManagementPlaneError"]


class ManagementPlaneError(ValueError):
    """Refusal to start: the management plane was requested without a key.

    A :class:`ValueError` subclass so configuration validation treats it
    like any other bad server config, with a message that names the fix
    (set a key, or leave the plane disabled) and never contains the key.
    """


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ManagementPlaneConfig:
    """Fail-closed configuration for the HTTP management plane.

    Defaults: everything off. ``key`` is required whenever anything is
    turned on; :meth:`validate` enforces that before the server binds.

    Environment overrides (read by :meth:`from_env`): ``PYNEXT_MGMT_ENABLED``,
    ``PYNEXT_MGMT_KEY``, ``PYNEXT_MGMT_GATE_READY``.
    """

    enabled: bool = False
    key: str | None = None
    metrics_path: str = "/metrics"
    readyz_requires_key: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            enabled=_env_bool("PYNEXT_MGMT_ENABLED", False),
            key=os.environ.get("PYNEXT_MGMT_KEY") or None,
            readyz_requires_key=_env_bool("PYNEXT_MGMT_GATE_READY", False),
        )

    def validate(self):
        """Refuse a configuration that would expose the plane unauthenticated.

        Raises :class:`ManagementPlaneError` when the plane (or the
        ``/readyz`` gate) is turned on without a usable key, or when the
        metrics path is not an absolute path. A disabled plane always
        validates, with or without a key.
        """
        if not self.enabled and not self.readyz_requires_key:
            return
        if not self.key or not self.key.strip():
            raise ManagementPlaneError(
                "management plane cannot start without a key: set "
                "ManagementPlaneConfig(key=...) or the PYNEXT_MGMT_KEY "
                "environment variable, or leave the management plane disabled"
            )
        if not isinstance(self.metrics_path, str) or not self.metrics_path.startswith("/"):
            raise ManagementPlaneError("metrics_path must be an absolute path such as '/metrics'")

    def check_key(self, headers):
        """Constant-time check of the management key in request headers.

        Accepts ``Authorization: Bearer <key>`` or ``X-PyNext-Mgmt-Key:
        <key>``. Any candidate that matches (constant-time
        ``hmac.compare_digest``) authorizes the request; a missing key,
        a missing header, or a non-matching candidate denies it. The
        comparison never uses ``==``/``!=`` (Sanic ``inspector.py:85``),
        so the check does not leak the key length or prefix through
        timing, and differing-length candidates are handled safely by
        ``compare_digest`` instead of short-circuiting.
        """
        if not self.key:
            return False
        expected = self.key.encode("utf-8")
        candidates = []
        for raw_value in _header_values(headers, "authorization"):
            scheme, _, token = raw_value.strip().partition(b" ")
            if scheme.lower() == b"bearer" and token:
                candidates.append(token.strip())
        for raw_value in _header_values(headers, "x-pynext-mgmt-key"):
            if raw_value.strip():
                candidates.append(raw_value.strip())
        return any(hmac.compare_digest(candidate, expected) for candidate in candidates)


def _header_values(headers, name):
    """Yield raw byte values of ``name`` (case-insensitive) from headers.

    ``headers`` is an iterable of ``(name, value)`` pairs whose items may
    be ``bytes`` or ``str`` (the native bridge passes bytes; the ASGI
    adapter passes str). Unknown encodings are dropped, never raised.
    """
    wanted = name.lower().encode("ascii")
    for key, value in headers or ():
        if isinstance(key, bytes):
            raw_key = key
        elif isinstance(key, str):
            raw_key = key.encode("ascii", "ignore")
        else:
            continue
        if raw_key.lower() != wanted:
            continue
        if isinstance(value, bytes):
            yield value
        elif isinstance(value, str):
            yield value.encode("utf-8", "ignore")
