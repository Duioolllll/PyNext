"""Deployment audit: ``pynext check --deploy`` (Django ``check --deploy`` pattern).

Django ships some thirty security checks (``django/core/checks/security/`` —
W001-W027, E023/E024/E026, E101/E102) that audit *configuration* for the
mistakes a framework's insecure-by-default settings invite: HSTS off, DEBUG
on, ``ALLOWED_HOSTS`` empty, cookies without ``Secure``, an empty
``SECRET_KEY``. They run at audit time (``manage.py check --deploy``), not
at startup, so a bad setting is reported before deployment instead of
blocking the server from starting.

PyNext has the same shape of trap — security headers and CSP are on by
default, but the CSRF signing key is per-process unless shared, there is
no Host allowlist at all (M-9), rate limiting defaults to off, and HTTP is
never redirected to HTTPS by the framework itself — so this module
provides the same kind of audit for a resolved :class:`PyNextConfig`:

* :func:`run_deploy_checks` runs every registered check function against
  a read-only :class:`DeploySettings` snapshot and returns a
  :class:`DeployReport` (errors / warnings / info, never the secret
  itself — a key is reported by source and byte length only).
* :func:`DeploySettings.from_resolved` builds that snapshot from a
  :class:`~pynext.config.ResolvedConfig` the way the CLI resolves it for
  ``pynext start`` (production posture: ``csrf_cookie_secure`` defaults
  on), plus the environment.
* The CLI wires it up as ``pynext check --deploy`` (and ``pynext doctor``,
  an alias that runs the same audit without the flag).

Checks are read-only: they inspect settings, they never change them, and
they never start a server. A check that finds nothing returns ``[]`` —
a safe setting passes silently, a risky one yields a message with a hint
naming the fix. Message ids mirror Django's where the check is a direct
port (``security.W004``, ``security.W018``, ...) and use fresh
``security.W1xx``/``security.E1xx`` ids for PyNext-only checks.
"""
from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import ResolvedConfig
from .http import DEFAULT_SECURITY, SecurityConfig, parse_csrf_signing_key, security_headers

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

__all__ = [
    "CHECKS",
    "CheckMessage",
    "DeployReport",
    "DeploySettings",
    "run_deploy_checks",
]

#: HSTS max-age Django requires for W004 (one year, in seconds).
_HSTS_MIN_MAX_AGE = 31_536_000
#: Minimum CSRF signing key length the roadmap asks for (L-4: 16 -> 32 bytes).
_MIN_CSRF_KEY_BYTES = 32
#: A session that lives longer than Django's default two weeks is a warning.
_SESSION_MAX_AGE_WARN = 14 * 24 * 3600
#: Minimum revalidate Bearer secret length (L-8).
_MIN_REVALIDATE_SECRET_LEN = 16
#: ``PYNEXT_DEBUG`` values that count as "debug on" for the audit.
_DEBUG_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class CheckMessage:
    """One finding from one check function (Django ``CheckMessage`` shape)."""

    id: str
    level: str  # "error" | "warning" | "info"
    message: str
    hint: str = ""

    def __post_init__(self):
        if self.level not in {"error", "warning", "info"}:
            raise ValueError(f"level must be 'error', 'warning' or 'info', got {self.level!r}")


@dataclass(frozen=True)
class DeploySettings:
    """A read-only snapshot of the deployment posture being audited.

    Defaults are the framework defaults, so ``DeploySettings()`` audits
    an out-of-the-box ``pynext start``. Fields the framework has no knob
    for keep their hardcoded effective value (the session cookie is
    always ``HttpOnly``; see :func:`check_session_cookie_httponly`) and
    the check that covers them says so instead of pretending a setting
    exists. ``csrf_signing_key_source`` is one of ``"default"`` (the
    per-process fallback key — see ``http._FALLBACK_CSRF_SIGNING_KEY``),
    ``"env"``, ``"file"`` or ``"cli"``; the key value itself is never
    stored here, only its byte length when one was configured.
    """

    debug: bool = False
    host: str = "127.0.0.1"
    workers: int = 1
    rate_limit: int = 0
    rate_limit_backend: str = "memory"
    cache_backend: str = "memory"
    cache_failure_policy: str = "fail_open"
    trusted_proxies: tuple = ()
    log_level: str | None = None
    metrics: bool | None = None
    # Signing secret (the SECRET_KEY analogue: the root of CSRF signing).
    csrf_signing_key_source: str = "default"
    csrf_signing_key_bytes: int | None = None
    # CSRF.
    csrf_enabled: bool | None = None
    csrf_cookie_secure: bool = True
    csrf_cookie_samesite: str = "Lax"
    csrf_origin_check: bool = True
    # CORS.
    cors_allowed_origins: tuple = ()
    cors_allow_credentials: bool = False
    # Security headers (SecurityConfig values; see http.SecurityConfig).
    hsts_enabled: bool = True
    hsts_max_age: int = _HSTS_MIN_MAX_AGE
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    frame_options: str | None = "DENY"
    referrer_policy: str | None = "strict-origin-when-cross-origin"
    permissions_policy: str | None = "camera=(), microphone=(), geolocation=()"
    csp_frame_ancestors: str = "'none'"
    csp_base_uri: str = "'self'"
    csp_form_action: str = "'self'"
    csp_nonce_enabled: bool = False
    # WebSocket.
    websocket_allowed_origins: tuple = ()
    websocket_allow_missing_origin: bool = False
    # Session cookie (SessionMiddleware defaults).
    session_cookie_secure: bool = True
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    session_max_age: int = 86400
    # Revalidate endpoint.
    revalidate_secret_set: bool = False
    revalidate_secret_length: int = 0
    # PyNext has no Host allowlist setting at all (M-9); always False
    # until S-P1-19 lands, so check_allowed_hosts always reports it.
    allowed_hosts_configured: bool = False

    @classmethod
    def from_resolved(cls, resolved: ResolvedConfig, *, debug: bool | None = None,
                      env: "Mapping[str, str] | None" = None) -> "DeploySettings":
        """Build the audited snapshot from a resolved config (production posture).

        ``resolved`` is the ``load_config(..., "start")`` result the CLI
        already computed, so file/env/CLI precedence is exactly what
        ``pynext start`` would use. ``debug`` defaults to the
        ``PYNEXT_DEBUG`` environment variable; pass it explicitly to
        audit a debug deployment. The effective security config is the
        one ``cli._security_from_config(values, production=True)`` would
        build (``csrf_cookie_secure`` defaults on for a deploy audit),
        falling back to :data:`DEFAULT_SECURITY` when nothing overrides it.
        """
        if env is None:
            env = os.environ
        values = resolved.values
        if debug is None:
            debug = env.get("PYNEXT_DEBUG", "").strip().lower() in _DEBUG_TRUTHY
        key_source = resolved.source_of("csrf_signing_key")
        key_bytes = None
        if key_source != "default" and values.csrf_signing_key is not None:
            key_bytes = len(parse_csrf_signing_key(values.csrf_signing_key,
                                                   source="csrf_signing_key"))
        security = _security_from_config(values)
        revalidate_secret = env.get("PYNEXT_REVALIDATE_SECRET")
        return cls(
            debug=debug,
            host=values.host,
            workers=values.workers,
            rate_limit=values.rate_limit,
            rate_limit_backend=values.rate_limit_backend,
            cache_backend=values.cache_backend,
            cache_failure_policy=values.cache_failure_policy,
            trusted_proxies=tuple(values.trusted_proxies),
            log_level=values.log_level,
            metrics=values.metrics,
            csrf_signing_key_source=key_source,
            csrf_signing_key_bytes=key_bytes,
            csrf_enabled=values.csrf_enabled,
            csrf_cookie_secure=security.csrf_cookie_secure,
            csrf_cookie_samesite=security.csrf_cookie_samesite,
            csrf_origin_check=security.csrf_origin_check,
            cors_allowed_origins=tuple(values.cors_allowed_origins),
            cors_allow_credentials=values.cors_allow_credentials,
            hsts_enabled=security.hsts_enabled,
            hsts_max_age=security.hsts_max_age,
            hsts_include_subdomains=security.hsts_include_subdomains,
            hsts_preload=security.hsts_preload,
            frame_options=security.frame_options,
            referrer_policy=security.referrer_policy,
            permissions_policy=security.permissions_policy,
            csp_frame_ancestors=security.csp_frame_ancestors,
            csp_base_uri=security.csp_base_uri,
            csp_form_action=security.csp_form_action,
            csp_nonce_enabled=values.csp_nonce_enabled,
            websocket_allowed_origins=tuple(values.websocket_allowed_origins),
            websocket_allow_missing_origin=values.websocket_allow_missing_origin,
            revalidate_secret_set=revalidate_secret is not None,
            revalidate_secret_length=len(revalidate_secret) if revalidate_secret else 0,
        )


def _security_from_config(values) -> SecurityConfig:
    """Mirror ``cli._security_from_config(values, production=True)``.

    Kept here (rather than importing the CLI) so this module stays
    importable without the CLI's server imports; the two must stay in
    sync — ``test_deploy_check`` checks the deploy audit against the
    security config the CLI actually builds.
    """
    if (values.csrf_enabled is None and values.csrf_signing_key is None
            and values.csrf_cookie_secure is None and not values.cors_allowed_origins
            and not values.cors_allow_credentials and not values.csp_nonce_enabled
            and not values.websocket_allowed_origins
            and not values.websocket_allow_missing_origin):
        return DEFAULT_SECURITY
    return SecurityConfig(
        csrf_enabled=values.csrf_enabled,
        csrf_signing_key=values.csrf_signing_key,
        csrf_cookie_secure=(values.csrf_cookie_secure if values.csrf_cookie_secure is not None
                            else True),
        cors_allowed_origins=tuple(values.cors_allowed_origins),
        cors_allow_credentials=values.cors_allow_credentials,
        websocket_allowed_origins=tuple(values.websocket_allowed_origins),
        websocket_allow_missing_origin=values.websocket_allow_missing_origin,
        csp_nonce_enabled=values.csp_nonce_enabled)


def _warn(id, message, hint=""):
    return [CheckMessage(id, "warning", message, hint)]


def _error(id, message, hint=""):
    return [CheckMessage(id, "error", message, hint)]


def _info(id, message, hint=""):
    return [CheckMessage(id, "info", message, hint)]


# ---------------------------------------------------------------------------
# Checks. Each takes DeploySettings and returns a list of CheckMessage
# (empty when the setting is safe). Ordered by Django id where ported.
# ---------------------------------------------------------------------------

def check_debug(settings: DeploySettings) -> list[CheckMessage]:
    """DEBUG must never be on in production (Django W018).

    ``create_app`` already refuses ``production + debug``, so a debug
    deployment cannot start — this check names the problem at audit
    time instead of letting the operator discover it at launch.
    """
    if settings.debug:
        return _error("security.W018",
                      "DEBUG is enabled for a production deployment.",
                      "Run the deployment without debug mode: debug pages expose "
                      "tracebacks, file paths and exception messages.")
    return []


def check_secret_key(settings: DeploySettings) -> list[CheckMessage]:
    """A signing secret must be configured explicitly (Django W025).

    PyNext has no ``SECRET_KEY`` setting; the analogue is the CSRF
    signing key, the root of CSRF signing. With no explicit key every
    process generates its own per-process fallback key, so CSRF cookies
    stop verifying after a restart and can never verify across workers.
    """
    if settings.csrf_signing_key_source == "default":
        return _warn("security.W025",
                     "No explicit CSRF signing key is configured, so each process "
                     "generates its own per-process key.",
                     "Set PYNEXT_CSRF_SIGNING_KEY or csrf_signing_key in "
                     "pynext.config.py to one shared 32-byte key (see "
                     "check_csrf_key_shared).")
    return []


def check_secret_key_strength(settings: DeploySettings) -> list[CheckMessage]:
    """The signing key should be at least 32 bytes (roadmap L-4).

    Config loading already rejects keys under 16 bytes; 16-31 bytes is
    legal but below the 32-byte minimum the roadmap asks for.
    """
    key_bytes = settings.csrf_signing_key_bytes
    if key_bytes is not None and key_bytes < _MIN_CSRF_KEY_BYTES:
        return _warn("security.W101",
                     f"The CSRF signing key is only {key_bytes} bytes; "
                     f"at least {_MIN_CSRF_KEY_BYTES} bytes are recommended.",
                     "Generate a fresh 32-byte key, e.g. with "
                     "secrets.token_hex(32), and share it via "
                     "PYNEXT_CSRF_SIGNING_KEY.")
    return []


def check_csrf_key_shared(settings: DeploySettings) -> list[CheckMessage]:
    """One CSRF signing key must be shared by every worker (Django lesson 2).

    The supervisor (``pynext start --workers N``) generates one key and
    shares it, but a deployment launched any other way (uvicorn /
    gunicorn ``--workers N``, containers) gives each worker its own
    per-process key, so a token issued by one worker fails on the next
    and requests fail with intermittent 403s (review #4, issue #23).
    """
    if settings.workers > 1 and settings.csrf_signing_key_source == "default":
        return _warn("security.W102",
                     f"Running with workers={settings.workers} but no shared "
                     "CSRF signing key is configured.",
                     "Set one PYNEXT_CSRF_SIGNING_KEY (or csrf_signing_key) "
                     "for every worker, or start through the supervisor "
                     "(pynext start --workers N), which shares one key.")
    return []


def check_allowed_hosts(settings: DeploySettings) -> list[CheckMessage]:
    """The Host header must be validated against an allowlist (Django W020).

    PyNext has no Host allowlist setting yet (M-9 / S-P1-19), so this
    always reports: an app that builds absolute URLs (password-reset
    links, redirects) from the Host header is open to Host injection
    until the allowlist lands.
    """
    if not settings.allowed_hosts_configured:
        return _warn("security.W020",
                     "No Host allowlist is configured: PyNext does not "
                     "validate the Host header yet (S-P1-19).",
                     "Do not build absolute URLs from the request Host "
                     "header; put the deployment behind a proxy that "
                     "rejects unknown Hosts until an allowlist exists.")
    return []


def check_hsts(settings: DeploySettings) -> list[CheckMessage]:
    """HSTS must be on with a long max-age (Django W004/W005)."""
    messages: list[CheckMessage] = []
    if not settings.hsts_enabled:
        messages.extend(_warn(
            "security.W004",
            "HSTS is disabled: browsers will not remember to use HTTPS.",
            "Keep hsts_enabled=True (the default) for production."))
    else:
        if settings.hsts_max_age < _HSTS_MIN_MAX_AGE:
            messages.extend(_warn(
                "security.W004",
                f"HSTS max-age is {settings.hsts_max_age} seconds, less than "
                f"one year ({_HSTS_MIN_MAX_AGE}).",
                "Set hsts_max_age to at least 31536000."))
        if not settings.hsts_include_subdomains:
            messages.extend(_warn(
                "security.W005",
                "HSTS does not include subdomains.",
                "Keep hsts_include_subdomains=True (the default) so "
                "subdomains are covered too."))
    if not settings.hsts_preload:
        messages.extend(_info(
            "security.W103",
            "HSTS preload is off (opt-in).",
            "Preload is only useful once the domain is submitted to the "
            "HSTS preload list; it is not required for HSTS itself."))
    return messages


def check_ssl_redirect(settings: DeploySettings) -> list[CheckMessage]:
    """Plain HTTP should be redirected to HTTPS (Django W008).

    PyNext has no built-in HTTP->HTTPS redirect (S-P2-7), so a
    deployment that serves plain HTTP alongside HTTPS must redirect at
    the proxy — this always reports until the redirect option lands.
    """
    return _warn("security.W008",
                 "PyNext does not redirect HTTP to HTTPS by itself.",
                 "Terminate TLS at the proxy / load balancer and redirect "
                 "plain-HTTP requests to HTTPS there.")


def check_frame_options(settings: DeploySettings) -> list[CheckMessage]:
    """A framing policy must be set (Django W019)."""
    if settings.frame_options is None:
        return _warn("security.W019",
                     "X-Frame-Options is disabled: the site can be framed.",
                     "Keep frame_options='DENY' (the default).")
    return []


def check_referrer_policy(settings: DeploySettings) -> list[CheckMessage]:
    """A Referrer-Policy must be set (Django W011 analogue)."""
    if settings.referrer_policy is None:
        return _warn("security.W011",
                     "Referrer-Policy is disabled: full URLs (including "
                     "query strings) may leak to other origins.",
                     "Keep referrer_policy='strict-origin-when-cross-origin' "
                     "(the default).")
    return []


def check_permissions_policy(settings: DeploySettings) -> list[CheckMessage]:
    """A Permissions-Policy must restrict powerful features."""
    if settings.permissions_policy is None:
        return _warn("security.W104",
                     "Permissions-Policy is disabled: camera, microphone "
                     "and geolocation are left to the browser default.",
                     "Keep permissions_policy set (the default disables "
                     "camera, microphone and geolocation).")
    return []


def check_content_security_policy(settings: DeploySettings) -> list[CheckMessage]:
    """CSP frame/base/form directives must stay restrictive."""
    messages: list[CheckMessage] = []
    for name, value in (("csp_frame_ancestors", settings.csp_frame_ancestors),
                        ("csp_base_uri", settings.csp_base_uri),
                        ("csp_form_action", settings.csp_form_action)):
        if "*" in value or "data:" in value:
            messages.extend(_warn(
                "security.W105",
                f"CSP {name} is permissive ({value!r}).",
                "Keep frame-ancestors 'none' and base-uri / form-action "
                "'self' (the defaults)."))
    return messages


def check_effective_security_headers(settings: DeploySettings) -> list[CheckMessage]:
    """The headers a production response actually carries must be complete.

    Unlike the field checks above, this one builds the real header list
    with :func:`http.security_headers` and verifies what a client would
    see: HSTS in production, ``nosniff`` always, a framing header and a
    CSP. A custom SecurityConfig that weakens a header is caught here
    even if the individual field still looks plausible.
    """
    security = SecurityConfig(
        hsts_enabled=settings.hsts_enabled, hsts_max_age=settings.hsts_max_age,
        hsts_include_subdomains=settings.hsts_include_subdomains,
        hsts_preload=settings.hsts_preload, frame_options=settings.frame_options,
        referrer_policy=settings.referrer_policy,
        permissions_policy=settings.permissions_policy,
        csp_frame_ancestors=settings.csp_frame_ancestors,
        csp_base_uri=settings.csp_base_uri, csp_form_action=settings.csp_form_action,
        csrf_cookie_secure=settings.csrf_cookie_secure,
        csrf_cookie_samesite=settings.csrf_cookie_samesite,
        # Dummy key: header rendering never signs anything, and this
        # keeps the check independent of the environment's key.
        csrf_signing_key="x" * _MIN_CSRF_KEY_BYTES,
    )
    seen = {name.decode("ascii").lower() for name, _ in
            security_headers(config=security, production=True)}
    messages: list[CheckMessage] = []
    if settings.hsts_enabled and "strict-transport-security" not in seen:
        messages.extend(_warn("security.W004",
                              "HSTS is enabled but no Strict-Transport-Security "
                              "header is emitted for production responses.",
                              "Check the SecurityConfig HSTS settings."))
    if "x-content-type-options" not in seen:
        messages.extend(_warn("security.W106",
                              "No X-Content-Type-Options: nosniff header is emitted.",
                              "The framework always sends nosniff; a custom "
                              "header setup may have removed it."))
    if "content-security-policy" not in seen:
        messages.extend(_warn("security.W106",
                              "No Content-Security-Policy header is emitted.",
                              "Keep the default CSP (the framework always sends one)."))
    return messages


def check_session_cookie_secure(settings: DeploySettings) -> list[CheckMessage]:
    """The session cookie must be Secure (Django W010)."""
    if not settings.session_cookie_secure:
        return _warn("security.W010",
                     "The session cookie is sent without Secure: it can be "
                     "sent over plain HTTP and stolen on the network.",
                     "Keep the session cookie Secure (the default) in production.")
    return []


def check_session_cookie_httponly(settings: DeploySettings) -> list[CheckMessage]:
    """The session cookie must be HttpOnly (Django W013).

    PyNext has no knob for this — :meth:`sessions.SessionMiddleware`
    always appends ``HttpOnly`` — so the check passes on the framework
    default and only fires if a deployment ever configures otherwise.
    """
    if not settings.session_cookie_httponly:
        return _warn("security.W013",
                     "The session cookie is readable from JavaScript.",
                     "Keep the session cookie HttpOnly (the framework default).")
    return []


def check_session_cookie_samesite(settings: DeploySettings) -> list[CheckMessage]:
    """Session SameSite=None requires Secure (Django W015 / E-series)."""
    if settings.session_cookie_samesite == "None" and not settings.session_cookie_secure:
        return _error("security.E107",
                      "The session cookie has SameSite=None without Secure: "
                      "browsers reject it and the setting is incoherent.",
                      "Set SameSite to Lax or Strict, or enable Secure.")
    if settings.session_cookie_samesite == "None":
        return _warn("security.W015",
                     "The session cookie has SameSite=None: it is sent on "
                     "cross-site requests.",
                     "Prefer SameSite=Lax (the default) unless cross-site "
                     "cookie use is genuinely required.")
    return []


def check_session_max_age(settings: DeploySettings) -> list[CheckMessage]:
    """Sessions should expire, and not live for weeks (Django lesson; S-P1-9)."""
    messages: list[CheckMessage] = []
    if settings.session_max_age > _SESSION_MAX_AGE_WARN:
        messages.extend(_warn(
            "security.W108",
            f"Session max-age is {settings.session_max_age} seconds "
            f"(over two weeks): a stolen session stays valid for that long.",
            "Keep session max-age short (the default is 86400 seconds, "
            "one day)."))
    messages.extend(_info(
        "security.W109",
        "Sessions have no absolute lifetime or idle timeout yet (S-P1-9): "
        "a session stays valid until max-age even if unused.",
        "Rotate sessions on login and keep max-age short until absolute "
        "lifetime / idle timeout land."))
    return messages


def check_csrf_cookie_secure(settings: DeploySettings) -> list[CheckMessage]:
    """The CSRF cookie must be Secure (Django W016)."""
    if not settings.csrf_cookie_secure:
        return _warn("security.W016",
                     "The CSRF cookie is sent without Secure: it can be "
                     "sent over plain HTTP.",
                     "Keep csrf_cookie_secure=True in production (the "
                     "default for pynext start).")
    return []


def check_csrf_cookie_samesite(settings: DeploySettings) -> list[CheckMessage]:
    """CSRF SameSite=None requires Secure (Django E102; roadmap L-2)."""
    if settings.csrf_cookie_samesite == "None" and not settings.csrf_cookie_secure:
        return _error("security.E102",
                      "The CSRF cookie has SameSite=None without Secure: "
                      "browsers reject it and CSRF protection breaks.",
                      "Set SameSite to Lax or Strict, or enable Secure.")
    if settings.csrf_cookie_samesite == "None":
        return _warn("security.W110",
                     "The CSRF cookie has SameSite=None: it is sent on "
                     "cross-site requests.",
                     "Prefer SameSite=Lax (the default).")
    return []


def check_csrf_cookie_httponly(settings: DeploySettings) -> list[CheckMessage]:
    """The CSRF cookie is readable by design — note it, don't warn (Django).

    Double-submit CSRF needs JavaScript to read the cookie and send it
    back as a header, so ``HttpOnly`` must stay off here (Django's
    ``CSRF_COOKIE_HTTPONLY = False`` default is the same deliberate
    choice). An XSS hole would expose the token either way, so this is
    an info note, not a warning.
    """
    return _info("security.W111",
                 "The CSRF cookie is readable from JavaScript by design "
                 "(double-submit: JS sends it back as a header).",
                 "Do not rely on the CSRF cookie alone: keep XSS defences "
                 "(CSP, escaping) in place.")


def check_csrf_enabled(settings: DeploySettings) -> list[CheckMessage]:
    """CSRF protection must not be switched off for a deployment."""
    if settings.csrf_enabled is False:
        return _warn("security.W112",
                     "CSRF protection is explicitly disabled.",
                     "Remove csrf_enabled=False (auto: on whenever a session "
                     "middleware is installed) unless a legacy client "
                     "genuinely cannot send tokens yet.")
    if settings.csrf_enabled is None:
        return _info("security.W113",
                     "CSRF protection is automatic: on when a session "
                     "middleware is installed, off otherwise.",
                     "Confirm the production app installs a session "
                     "middleware, or set csrf_enabled=True.")
    return []


def check_csrf_origin_check(settings: DeploySettings) -> list[CheckMessage]:
    """Origin checking is part of CSRF defence (Django lesson 2)."""
    if not settings.csrf_origin_check:
        return _warn("security.W114",
                     "CSRF Origin checking is disabled: token-only "
                     "validation is weaker against subdomain attacks.",
                     "Keep csrf_origin_check=True (the default).")
    return []


def check_cors(settings: DeploySettings) -> list[CheckMessage]:
    """``*`` origins with credentials is a critical misconfiguration (H-4)."""
    origins = settings.cors_allowed_origins
    if "*" in origins and settings.cors_allow_credentials:
        return _error("security.E115",
                      "CORS allows any origin ('*') with credentials: any "
                      "site can make credentialed cross-origin requests.",
                      "List explicit origins when cors_allow_credentials is on.")
    if "*" in origins:
        return _warn("security.W115",
                     "CORS allows any origin ('*'): any site can read "
                     "cross-origin responses.",
                     "List the explicit origins the app needs instead of '*'.")
    return []


def check_cors_origins(settings: DeploySettings) -> list[CheckMessage]:
    """CORS origins should be HTTPS in production."""
    http_origins = [o for o in settings.cors_allowed_origins
                    if o != "*" and o.startswith("http://")]
    if http_origins:
        return _warn("security.W116",
                     "CORS allows plain-HTTP origins: "
                     f"{', '.join(http_origins)}.",
                     "Allow only https:// origins in production (http:// is "
                     "for local development).")
    return []


def check_rate_limit(settings: DeploySettings) -> list[CheckMessage]:
    """Rate limiting should be on for a deployment (S-P0-8 / H-1)."""
    if settings.rate_limit < 1:
        return _warn("security.W117",
                     "Rate limiting is off (rate_limit=0): the deployment "
                     "has no throttle against brute force or scraping.",
                     "Set rate_limit (requests per client per window) for "
                     "production; login paths need a stricter bucket.")
    return []


def check_rate_limit_backend(settings: DeploySettings) -> list[CheckMessage]:
    """A memory rate-limit backend does not share across workers (S-P1-4)."""
    if settings.workers > 1 and settings.rate_limit_backend == "memory" and settings.rate_limit >= 1:
        return _warn("security.W118",
                     f"Running with workers={settings.workers} but the "
                     "in-memory rate-limit backend keeps a separate bucket "
                     "per worker.",
                     "Use rate_limit_backend='redis' so limits hold across workers.")
    return []


def check_workers_cache(settings: DeploySettings) -> list[CheckMessage]:
    """Multi-worker deployments need a shared ISR cache backend."""
    if settings.workers > 1 and settings.cache_backend == "memory":
        return _warn("security.W119",
                     f"Running with workers={settings.workers} and the memory "
                     "ISR cache backend: each worker keeps its own cache.",
                     "Use cache_backend 'sqlite' or 'redis' to share the ISR cache.")
    return []


def check_host_binding(settings: DeploySettings) -> list[CheckMessage]:
    """Debug + an external bind exposes debug pages (S-P1-28 / M-19)."""
    if settings.debug and settings.host not in {"127.0.0.1", "localhost", "::1"}:
        return _error("security.E120",
                      f"Debug mode is on while bound to {settings.host}: debug "
                      "error pages (tracebacks, file paths) are exposed externally.",
                      "Bind debug servers to 127.0.0.1 only, or turn debug off.")
    if settings.host in {"0.0.0.0", "::"}:
        return _warn("security.W120",
                     f"The server binds all interfaces ({settings.host}).",
                     "Make sure the deployment sits behind a firewall / TLS "
                     "terminating proxy and is not directly reachable.")
    return []


def check_websocket_origins(settings: DeploySettings) -> list[CheckMessage]:
    """WebSocket handshakes should require an Origin (S-P0-7)."""
    messages: list[CheckMessage] = []
    if settings.websocket_allow_missing_origin:
        messages.extend(_warn(
            "security.W121",
            "WebSocket clients may omit Origin: non-browser clients can "
            "connect without an origin check.",
            "Keep websocket_allow_missing_origin=False (the default) unless "
            "non-browser clients genuinely need it."))
    http_origins = [o for o in settings.websocket_allowed_origins
                    if o.startswith("http://")]
    if http_origins:
        messages.extend(_info(
            "security.W122",
            "WebSocket origins include plain-HTTP origins: "
            f"{', '.join(http_origins)}.",
            "Allow only https:// origins in production."))
    return messages


def check_trusted_proxies(settings: DeploySettings) -> list[CheckMessage]:
    """Trusting every proxy lets anyone spoof forwarded headers (S-P2-3)."""
    bad = []
    for entry in settings.trusted_proxies:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            if entry.strip() in {"*", "0.0.0.0/0", "::/0"}:
                bad.append(entry)
            continue
        if network.prefixlen == 0:
            bad.append(entry)
    if bad:
        return _error("security.E123",
                      "Trusted proxies include a catch-all "
                      f"({', '.join(bad)}): any client can spoof "
                      "X-Forwarded-For / X-Forwarded-Proto.",
                      "Trust only the deployment's own proxy IPs / networks.")
    return []


def check_proxy_ssl_header(settings: DeploySettings) -> list[CheckMessage]:
    """A trusted proxy must strip client-supplied forwarded headers (S-P2-3)."""
    if settings.trusted_proxies:
        return _info("security.W124",
                     "Forwarded headers are trusted from "
                     f"{', '.join(settings.trusted_proxies)}: the proxy must "
                     "strip client-supplied X-Forwarded-* headers, or a "
                     "client can fake HTTPS / its own IP.",
                     "Confirm the proxy overwrites X-Forwarded-Proto and "
                     "X-Forwarded-For instead of appending to them.")
    return []


def check_log_level(settings: DeploySettings) -> list[CheckMessage]:
    """DEBUG logging in production is verbose and may leak details."""
    if settings.log_level is not None and settings.log_level.upper() == "DEBUG":
        return _warn("security.W125",
                     "Log level is DEBUG in production: verbose logs may "
                     "record request details and secrets.",
                     "Use log_level INFO (or WARNING) for production.")
    return []


def check_metrics_exposure(settings: DeploySettings) -> list[CheckMessage]:
    """Metrics collection on an external bind needs an exposure check (S-P1-32)."""
    if settings.metrics and settings.host in {"0.0.0.0", "::"}:
        return _warn("security.W126",
                     "Metrics collection is on while bound to all interfaces: "
                     "confirm the metrics endpoint is not publicly reachable.",
                     "Bind metrics to loopback, or require a key for the "
                     "management plane (S-P1-32).")
    return []


def check_revalidate_secret(settings: DeploySettings) -> list[CheckMessage]:
    """The revalidate Bearer secret must be long enough (roadmap L-8)."""
    if settings.revalidate_secret_set and settings.revalidate_secret_length < _MIN_REVALIDATE_SECRET_LEN:
        return _warn("security.W127",
                     "The revalidate secret is shorter than "
                     f"{_MIN_REVALIDATE_SECRET_LEN} characters.",
                     "Use a long random secret for PYNEXT_REVALIDATE_SECRET; "
                     "a short one is guessable.")
    if not settings.revalidate_secret_set:
        return _info("security.W128",
                     "No revalidate secret is set: POST /_pynext/revalidate "
                     "is disabled (404).",
                     "That is the safe default; set PYNEXT_REVALIDATE_SECRET "
                     "only if on-demand revalidation is needed.")
    return []


def check_cache_failure_policy(settings: DeploySettings) -> list[CheckMessage]:
    """fail_open ISR caching serves stale content on backend outage."""
    if settings.cache_failure_policy == "fail_open":
        return _info("security.W129",
                     "ISR cache failure policy is fail_open: a backend "
                     "outage serves stale cached pages instead of 503s.",
                     "That is the default trade-off (availability over "
                     "freshness); use fail_closed if stale content is "
                     "unacceptable.")
    return []


def check_csp_nonce(settings: DeploySettings) -> list[CheckMessage]:
    """CSP nonces are opt-in; dynamic pages use a static policy otherwise."""
    if not settings.csp_nonce_enabled:
        return _info("security.W130",
                     "CSP nonces are off: dynamic pages use the static CSP.",
                     "Enable csp_nonce_enabled if dynamic pages need "
                     "per-request script nonces.")
    return []


#: Every deploy check, in run order. Add new checks here (and only here)
#: so ``run_deploy_checks`` picks them up.
CHECKS = (
    check_debug,
    check_secret_key,
    check_secret_key_strength,
    check_csrf_key_shared,
    check_allowed_hosts,
    check_hsts,
    check_ssl_redirect,
    check_frame_options,
    check_referrer_policy,
    check_permissions_policy,
    check_content_security_policy,
    check_effective_security_headers,
    check_session_cookie_secure,
    check_session_cookie_httponly,
    check_session_cookie_samesite,
    check_session_max_age,
    check_csrf_cookie_secure,
    check_csrf_cookie_samesite,
    check_csrf_cookie_httponly,
    check_csrf_enabled,
    check_csrf_origin_check,
    check_cors,
    check_cors_origins,
    check_rate_limit,
    check_rate_limit_backend,
    check_workers_cache,
    check_host_binding,
    check_websocket_origins,
    check_trusted_proxies,
    check_proxy_ssl_header,
    check_log_level,
    check_metrics_exposure,
    check_revalidate_secret,
    check_cache_failure_policy,
    check_csp_nonce,
)


@dataclass(frozen=True)
class DeployReport:
    """The outcome of :func:`run_deploy_checks` (read-only)."""

    messages: tuple = field(default_factory=tuple)

    @property
    def errors(self) -> tuple:
        return tuple(m for m in self.messages if m.level == "error")

    @property
    def warnings(self) -> tuple:
        return tuple(m for m in self.messages if m.level == "warning")

    @property
    def infos(self) -> tuple:
        return tuple(m for m in self.messages if m.level == "info")

    @property
    def ok(self) -> bool:
        """True when no error was found (warnings do not fail the audit)."""
        return not self.errors

    def summary(self) -> str:
        return (f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), "
                f"{len(self.infos)} info note(s)")

    def format_text(self) -> str:
        """Human-readable report (Django ``check`` output shape)."""
        lines = ["Deployment checks:"]
        for level, title in (("error", "Errors"), ("warning", "Warnings"), ("info", "Info")):
            found = [m for m in self.messages if m.level == level]
            if not found:
                continue
            lines.append(f"{title} ({len(found)}):")
            for message in found:
                lines.append(f"  {message.id}: {message.message}")
                if message.hint:
                    lines.append(f"    Hint: {message.hint}")
        if not self.messages:
            lines.append("  No issues found.")
        lines.append(f"System check identified {self.summary()}.")
        return "\n".join(lines)

    def format_json(self) -> str:
        """Machine-readable report. Never contains a secret value — only
        ids, levels, messages and hints (a key is reported by source and
        byte length, never by value)."""
        return json.dumps({
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "ok": self.ok,
            "messages": [
                {"id": m.id, "level": m.level, "message": m.message, "hint": m.hint}
                for m in self.messages
            ],
        }, indent=2)


def run_deploy_checks(settings: DeploySettings | None = None,
                      checks: "Sequence" = CHECKS) -> DeployReport:
    """Run every registered deploy check against ``settings``.

    ``settings`` defaults to :class:`DeploySettings` (the framework
    defaults). Checks never mutate settings and never touch the network
    or the filesystem — this is a pure audit of configuration.
    """
    if settings is None:
        settings = DeploySettings()
    messages: list[CheckMessage] = []
    for check in checks:
        messages.extend(check(settings))
    return DeployReport(messages=tuple(messages))
