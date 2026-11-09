import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote_to_bytes

from .forms import parse_form
from .ws_origin import canonical_origin


class BadRequest(Exception):
    """A request could not be parsed or failed application validation."""


class RequestCancelled(RuntimeError):
    """The client disconnected or the request's execution deadline expired."""


#: Methods that never change server state and are exempt from CSRF checks.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: One process-wide fallback signing key for double-submit CSRF cookies.
#: Configs that do not set a key (and have no PYNEXT_CSRF_SIGNING_KEY)
#: share it, so a cookie issued with the framework default config
#: verifies against an app's own default config. Explicit keys and the
#: environment variable still override it per config. Workers are
#: separate processes, so each has its own fallback key — a lone worker
#: (or a single-process server) is unaffected, but a multi-worker
#: supervisor must share one key explicitly: see
#: :func:`ensure_shared_csrf_signing_key`, which the supervisor calls
#: before spawning workers so every worker signs with the same key.
_FALLBACK_CSRF_SIGNING_KEY = secrets.token_bytes(32)


def parse_csrf_signing_key(value, *, source="csrf_signing_key"):
    """Parse a CSRF signing key supplied as bytes or text.

    Accepted forms (``source`` names the setting in error messages,
    e.g. ``"csrf_signing_key"``, ``"PYNEXT_CSRF_SIGNING_KEY"`` or
    ``"--csrf-signing-key"``):

    - ``bytes`` / ``bytearray`` of at least 16 bytes, used as-is;
    - ``"hex:<hex>"`` — hex-decoded, at least 16 bytes after decoding;
    - ``"base64:<base64>"`` — standard base64-decoded, at least 16
      bytes after decoding;
    - any other ``str`` — UTF-8 encoded, at least 16 bytes.

    Surrounding whitespace is ignored. A key that is empty, too short,
    or has undecodable hex/base64 raises :class:`ValueError` (a value of
    the wrong type raises :class:`TypeError`): it never silently falls
    back to a random key, so a misconfigured deployment fails loudly at
    startup instead of issuing cookies no other worker can verify.
    """
    if isinstance(value, (bytes, bytearray)):
        key = bytes(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{source} must not be empty")
        if text.startswith("hex:"):
            try:
                key = bytes.fromhex(text[4:].strip())
            except ValueError as exc:
                raise ValueError(f"{source} is not valid hex after 'hex:'") from exc
        elif text.startswith("base64:"):
            try:
                key = base64.b64decode(text[7:].strip(), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"{source} is not valid base64 after 'base64:'") from exc
        else:
            key = text.encode("utf-8")
    else:
        raise TypeError(f"{source} must be str, bytes or None")
    if len(key) < 16:
        raise ValueError(f"{source} must be at least 16 bytes")
    return key


def ensure_shared_csrf_signing_key(explicit_key=None):
    """Return the one CSRF signing key every worker must share (finding #23).

    Precedence: an explicit key wins, then a set
    ``PYNEXT_CSRF_SIGNING_KEY`` (validated — an invalid value raises
    instead of falling back), and otherwise a fresh 32-byte key from
    :mod:`secrets`. The winning key is published back into the
    environment as ``PYNEXT_CSRF_SIGNING_KEY=hex:<hex>`` so workers
    spawned afterwards (``multiprocessing`` ``"spawn"`` re-imports this
    module per worker) inherit exactly the same key, and so every
    default :class:`SecurityConfig` built inside a worker agrees with
    the app's config. The key itself is never logged; only the fact
    that one key is shared is observable to callers.

    The supervisor calls this once before spawning workers; single-
    process servers do not call it, so their per-process fallback
    behaviour is unchanged.
    """
    if explicit_key is not None:
        key = parse_csrf_signing_key(explicit_key, source="csrf_signing_key")
    else:
        env_key = os.environ.get("PYNEXT_CSRF_SIGNING_KEY")
        if env_key is not None:
            key = parse_csrf_signing_key(env_key, source="PYNEXT_CSRF_SIGNING_KEY")
        else:
            key = secrets.token_bytes(32)
    os.environ["PYNEXT_CSRF_SIGNING_KEY"] = f"hex:{key.hex()}"
    return key

_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_TOKEN_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ORIGIN = re.compile(r"https?://[^/]+")
_CSP_NONCE = re.compile(r"[A-Za-z0-9+/=_-]+")
#: A CSP directive value is a space-separated source list: keywords
#: ('self', 'none', ...), hosts, or schemes. No semicolons (directive
#: separator), quotes outside keywords, or control characters.
_CSP_SOURCE_LIST = re.compile(r"[A-Za-z0-9\-_.*:/' ]+")
#: Referrer-Policy values from the spec; anything else is a typo, not a policy.
_REFERRER_POLICIES = frozenset({
    "no-referrer", "no-referrer-when-downgrade", "origin", "origin-when-cross-origin",
    "same-origin", "strict-origin", "strict-origin-when-cross-origin", "unsafe-url",
})
#: Permissions-Policy is `feature=(allowlist)` groups; reject separators and
#: control characters so a bad value cannot smuggle in another directive.
_PERMISSIONS_POLICY = re.compile(r"[A-Za-z0-9\-_()*,= ']+")


@dataclass
class SecurityConfig:
    """Security defaults for an app (spec v0.2 §11).

    CSRF protection is automatic when a session middleware is installed:
    ``csrf_enabled=None`` (the default) enables it for every state-changing
    API request once a ``SessionMiddleware`` is present, and leaves it
    off otherwise. Pass ``csrf_enabled=True`` to protect state-changing
    API routes even without sessions, or ``csrf_enabled=False`` to opt
    out explicitly (for example when a legacy client cannot send a token
    yet). Individual handlers can also be marked with :func:`csrf_protect`.

    Without sessions the double-submit cookie is HMAC-signed with
    ``csrf_signing_key`` so a subdomain that can set a cookie cannot
    inject its own token: an unsigned or foreign-signed cookie is
    rejected even when the submitted token matches it. The key defaults
    to ``PYNEXT_CSRF_SIGNING_KEY`` when set, and otherwise to a fresh
    per-process key. A key may be given as bytes, as a string of at
    least 16 bytes, or as ``"hex:<hex>"`` / ``"base64:<base64>"`` (see
    :func:`parse_csrf_signing_key`); an empty, too-short or undecodable
    key raises instead of silently falling back to a random key.
    Multi-worker deployments must share one key on every worker, or a
    cookie issued by one worker is rejected by the others — when the
    app is started with ``--workers N`` the supervisor generates one
    key (or adopts ``--csrf-signing-key`` / ``PYNEXT_CSRF_SIGNING_KEY``)
    and shares it with every worker, including replacements started
    after a crash or a rolling restart. With sessions the token is
    bound to the server-side session instead and the signing key is
    unused.

    The A1 default security headers (HSTS, ``X-Content-Type-Options``,
    ``X-Frame-Options``, ``Referrer-Policy``, ``Permissions-Policy`` and the
    CSP below) are on by default and applied to every response by
    :func:`apply_security_headers`. HSTS is only *sent* in production
    (see :func:`security_headers`): it is ignored over plain HTTP anyway,
    and omitting it in development keeps ``pynext dev`` on
    ``http://127.0.0.1`` free of HSTS side effects. Set a field to ``None``
    to disable that header (``frame_options``, ``referrer_policy``,
    ``permissions_policy``); set ``hsts_enabled=False`` to disable HSTS.

    CORS denies every cross-origin request by default; list origins
    explicitly to allow them. CSP nonce support is off by default and
    only changes dynamically rendered pages. The CSRF cookie is
    ``Secure`` by default: over plain ``http://`` (local development)
    pass ``csrf_cookie_secure=False`` to the app config and to
    :func:`set_csrf_cookie` (or use ``pynext dev``, which does that).

    WebSocket handshakes require a same-origin ``Origin`` by default.
    Additional origins can be listed in ``websocket_allowed_origins``;
    ``websocket_allow_missing_origin`` opts non-browser clients without
    the header in. Origin checking does not replace authentication.
    """

    csrf_enabled: bool | None = None
    csrf_cookie_name: str = "pynext_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    csrf_form_field: str = "csrf_token"
    csrf_cookie_secure: bool = True
    csrf_cookie_samesite: str = "Lax"
    cors_allowed_origins: tuple = ()
    cors_allowed_methods: tuple = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
    cors_allowed_headers: tuple = ("Content-Type", "Authorization", "X-CSRF-Token")
    cors_allow_credentials: bool = False
    cors_max_age: int = 600
    csp_nonce_enabled: bool = False
    csrf_signing_key: str | bytes | None = field(default=None, repr=False)
    csrf_origin_check: bool = True
    hsts_enabled: bool = True
    hsts_max_age: int = 31_536_000
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    frame_options: str | None = "DENY"
    referrer_policy: str | None = "strict-origin-when-cross-origin"
    permissions_policy: str | None = "camera=(), microphone=(), geolocation=()"
    csp_style_src: str = "'self'"
    csp_frame_ancestors: str = "'none'"
    csp_base_uri: str = "'self'"
    csp_form_action: str = "'self'"
    websocket_allowed_origins: tuple = ()
    websocket_allow_missing_origin: bool = False

    def __post_init__(self):
        if self.csrf_enabled is not None and not isinstance(self.csrf_enabled, bool):
            raise TypeError("csrf_enabled must be a bool or None")
        for name in ("csrf_cookie_name", "csrf_header_name", "csrf_form_field"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _TOKEN_NAME.fullmatch(value):
                raise ValueError(f"{name} must be a valid header/cookie token name")
        if self.csrf_cookie_samesite not in {"Lax", "Strict", "None"}:
            raise ValueError("csrf_cookie_samesite must be Lax, Strict or None")
        if not isinstance(self.csrf_cookie_secure, bool):
            raise TypeError("csrf_cookie_secure must be a bool")
        if not isinstance(self.csrf_origin_check, bool):
            raise TypeError("csrf_origin_check must be a bool")
        if not isinstance(self.websocket_allow_missing_origin, bool):
            raise TypeError("websocket_allow_missing_origin must be a bool")
        if isinstance(self.websocket_allowed_origins, (str, bytes)):
            raise TypeError("websocket_allowed_origins must be a sequence of origins")
        self.websocket_allowed_origins = tuple(self.websocket_allowed_origins)
        origin_keys = [canonical_origin(origin) for origin in self.websocket_allowed_origins]
        if any(origin is None for origin in origin_keys):
            raise ValueError("websocket_allowed_origins must contain valid HTTP(S) origins")
        key = self.csrf_signing_key
        if key is None:
            env_key = os.environ.get("PYNEXT_CSRF_SIGNING_KEY")
            if env_key is not None:
                # Invalid env values fail here (see
                # parse_csrf_signing_key): an empty, too-short or
                # undecodable key must never silently fall back to a
                # random key that no other worker shares.
                key = parse_csrf_signing_key(env_key, source="PYNEXT_CSRF_SIGNING_KEY")
            else:
                # Shared process-wide fallback key (see
                # _FALLBACK_CSRF_SIGNING_KEY): a cookie issued with the
                # framework default config must verify against an app's
                # own default config. Multi-worker deployments share one
                # key via ensure_shared_csrf_signing_key (see the class
                # docstring); this fallback is only for single-process
                # servers, where every config lives in one process.
                key = _FALLBACK_CSRF_SIGNING_KEY
        else:
            key = parse_csrf_signing_key(key, source="csrf_signing_key")
        self.csrf_signing_key = key
        self.cors_allowed_origins = tuple(self.cors_allowed_origins)
        self.cors_allowed_methods = tuple(self.cors_allowed_methods)
        self.cors_allowed_headers = tuple(self.cors_allowed_headers)
        for origin in self.cors_allowed_origins:
            if origin != "*" and (not isinstance(origin, str) or not _ORIGIN.fullmatch(origin)):
                raise ValueError(f"Invalid CORS origin: {origin!r}")
        for method in self.cors_allowed_methods:
            if method.upper() not in _KNOWN_METHODS:
                raise ValueError(f"Unsupported CORS method: {method!r}")
        for header in self.cors_allowed_headers:
            if not isinstance(header, str) or not _TOKEN_NAME.fullmatch(header):
                raise ValueError(f"Invalid CORS header name: {header!r}")
        if not isinstance(self.cors_allow_credentials, bool):
            raise TypeError("cors_allow_credentials must be a bool")
        if isinstance(self.cors_max_age, bool) or not isinstance(self.cors_max_age, int) or self.cors_max_age < 0:
            raise ValueError("cors_max_age must be a non-negative integer")
        if not isinstance(self.csp_nonce_enabled, bool):
            raise TypeError("csp_nonce_enabled must be a bool")
        if not isinstance(self.hsts_enabled, bool):
            raise TypeError("hsts_enabled must be a bool")
        if isinstance(self.hsts_max_age, bool) or not isinstance(self.hsts_max_age, int) or self.hsts_max_age < 0:
            raise ValueError("hsts_max_age must be a non-negative integer")
        if not isinstance(self.hsts_include_subdomains, bool):
            raise TypeError("hsts_include_subdomains must be a bool")
        if not isinstance(self.hsts_preload, bool):
            raise TypeError("hsts_preload must be a bool")
        if self.frame_options is not None:
            if not isinstance(self.frame_options, str) or self.frame_options.upper() not in {"DENY", "SAMEORIGIN"}:
                raise ValueError("frame_options must be 'DENY' or 'SAMEORIGIN'")
            self.frame_options = self.frame_options.upper()
        if self.referrer_policy is not None and (
                not isinstance(self.referrer_policy, str) or self.referrer_policy not in _REFERRER_POLICIES):
            raise ValueError(f"Invalid referrer_policy: {self.referrer_policy!r}")
        if self.permissions_policy is not None and (
                not isinstance(self.permissions_policy, str)
                or not _PERMISSIONS_POLICY.fullmatch(self.permissions_policy)):
            raise ValueError(f"Invalid permissions_policy: {self.permissions_policy!r}")
        for name in ("csp_style_src", "csp_frame_ancestors", "csp_base_uri", "csp_form_action"):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value.strip()
                    or not _CSP_SOURCE_LIST.fullmatch(value) or ";" in value):
                raise ValueError(f"{name} must be a space-separated CSP source list")


#: The framework defaults: CSRF auto (on with a session middleware, off
#: otherwise), CORS deny-all, CSP nonce off, CSRF cookie Secure.
DEFAULT_SECURITY = SecurityConfig()


def new_csrf_token():
    """Return a fresh unpredictable CSRF token (double-submit pattern)."""
    return secrets.token_urlsafe(32)


def sign_csrf_token(token, *, config=DEFAULT_SECURITY):
    """Return the signed double-submit cookie value for ``token``.

    The value is ``"<token>.<HMAC-SHA256 hex>"`` signed with the config's
    ``csrf_signing_key``. Clients send the raw ``token`` back in the
    header (or form field) and the signed value back as the cookie, so a
    subdomain that can plant an unsigned cookie cannot pass validation
    even if it knows the token.
    """
    signature = hmac.new(config.csrf_signing_key, token.encode("ascii"),
                         hashlib.sha256).hexdigest()
    return f"{token}.{signature}"


def _signed_csrf_token(cookie_value, *, config=DEFAULT_SECURITY):
    """Return the token inside a signed CSRF cookie value, or ``None``.

    Unsigned values (a bare token, a missing signature, a signature made
    with another key) are rejected: this is what stops subdomain cookie
    injection, where the attacker controls the cookie but not the key.
    """
    token, separator, signature = cookie_value.rpartition(".")
    if not separator or not token:
        return None
    expected = hmac.new(config.csrf_signing_key, token.encode("ascii"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    return token


def set_csrf_cookie(response, token, *, config=DEFAULT_SECURITY):
    """Set the double-submit CSRF cookie on a response.

    The cookie is readable by same-origin JavaScript on purpose: the client
    reads it and sends the same value back in the ``X-CSRF-Token`` header (or
    the ``csrf_token`` form field), so it must not be ``HttpOnly``. It is
    ``SameSite=Lax`` by default and ``Secure`` by default; pass a config
    with ``csrf_cookie_secure=False`` for plain-``http://`` local
    development. The cookie value is the HMAC-signed token (see
    :func:`sign_csrf_token`): send the cookie back exactly as issued and
    the raw token in the header — reconstructing the cookie from the
    token alone will not validate.
    """
    parts = [f"{config.csrf_cookie_name}={sign_csrf_token(token, config=config)}", "Path=/",
             f"SameSite={config.csrf_cookie_samesite}"]
    if config.csrf_cookie_secure:
        parts.append("Secure")
    response.headers.append((b"set-cookie", "; ".join(parts).encode("ascii")))
    return response


def _header_values(request, name):
    wanted = name.lower().encode("ascii")
    return [value for key, value in request.headers if key.lower() == wanted]


# Headers that are only ever set by the framework or its middleware, never
# by a client. Next.js `INTERNAL_HEADERS`
# (packages/next/src/server/lib/server-ipc/utils.ts:42-65) plus
# `x-middleware-refresh`, the framework-set middleware signal read back in
# `resolve-routes.ts:739-845` — 13 names in total. A client that sends one
# of these must not be able to steer routing, middleware, or any security
# decision that reads it, so both entry points
# (`App.__call__` for ASGI, `create_server` for the native transport)
# strip them before anything else sees the request. PyNext middleware
# communicates through ``Request``/``Response`` objects, so there is no
# `x-middleware-*` protocol to preserve — stripping is purely defensive,
# and a future internal signal belongs in this list first.
#
# Deliberately NOT stripped: `x-request-id` (echoed by design,
# `observability.begin_request_context`), `traceparent` (W3C tracing),
# and `x-forwarded-*` (only honored from trusted proxies,
# `proxy.ProxyTrust`).
INTERNAL_HEADERS = frozenset((
    b"x-middleware-rewrite",
    b"x-middleware-redirect",
    b"x-middleware-set-cookie",
    b"x-middleware-skip",
    b"x-middleware-override-headers",
    b"x-middleware-next",
    b"x-middleware-refresh",
    b"x-now-route-matches",
    b"x-matched-path",
    b"x-nextjs-data",
    b"x-next-internal-go-token",
    b"x-next-resume-state-length",
    b"next-resume",
))


def filter_internal_headers(headers):
    """Return ``headers`` without internal-only headers.

    Accepts the transport form — an iterable of ``(name, value)`` byte
    pairs (ASGI scope headers or the native transport's header list) —
    and returns a new tuple in the original order. Matching is
    case-insensitive; anything that is not an internal header (including
    duplicates) is preserved untouched.
    """
    return tuple(
        (name, value)
        for name, value in headers
        if (name.lower() if isinstance(name, bytes)
            else name.lower().encode("ascii", "ignore") if isinstance(name, str)
            else None) not in INTERNAL_HEADERS
    )


def _cookie_value(request, name):
    for value in _header_values(request, "cookie"):
        for part in value.split(b";"):
            key, separator, val = part.strip().partition(b"=")
            if separator and key.decode("ascii", "ignore") == name:
                return val.strip().decode("ascii", "ignore")
    return None


def csrf_token_from_request(request, *, config=DEFAULT_SECURITY):
    """Return the CSRF token a client submitted, or ``None``.

    The configured header wins; a ``csrf_token`` field in an
    URL-encoded or multipart form field is the fallback.
    """
    values = _header_values(request, config.csrf_header_name)
    if values:
        return values[0].decode("ascii", "ignore").strip() or None
    content_type = _header_values(request, "content-type")
    if len(content_type) == 1:
        try:
            fields = parse_form(content_type[0], request.body)
        except (UnicodeError, ValueError):
            return None
        submitted = fields.get(config.csrf_form_field)
        if submitted and isinstance(submitted[0], str):
            return submitted[0] or None
    return None


def _csrf_origin_allowed(request):
    """True when the request's origin signals do not show a cross-site call.

    A present ``Origin`` must match the ``Host`` header exactly (a
    malformed Origin, ``Origin: null`` or a missing Host all fail closed).
    Without an Origin, ``Sec-Fetch-Site`` must be ``same-origin`` or
    ``none`` (a non-browser client sends neither header and is allowed
    through to the token check). ``same-site`` is rejected too: a
    subdomain page is a different origin and must not drive state-changing
    API calls. Disable with ``SecurityConfig(csrf_origin_check=False)``
    when an app legitimately serves its API from another subdomain.
    """
    origins = _header_values(request, "origin")
    if origins:
        if len(origins) != 1:
            return False
        try:
            origin = origins[0].decode("ascii")
        except UnicodeDecodeError:
            return False
        return _is_same_origin(request, origin)
    fetch_site = _header_values(request, "sec-fetch-site")
    if fetch_site:
        return fetch_site[0].decode("ascii", "ignore").strip().lower() in {"same-origin", "none"}
    return True


def validate_csrf(request, *, config=DEFAULT_SECURITY):
    """Check the CSRF token, and the request's origin signals.

    GET, HEAD and OPTIONS are exempt. Otherwise the Origin /
    Sec-Fetch-Site check runs first (when ``csrf_origin_check`` is on),
    then the token: with a session the token must match the session-bound
    token, and without one the cookie must be a valid HMAC-signed
    double-submit value whose token matches the submitted header or form
    field. Tokens are compared in constant time.
    """
    if request.method.upper() in SAFE_METHODS:
        return True
    if config.csrf_origin_check and not _csrf_origin_allowed(request):
        return False
    submitted = csrf_token_from_request(request, config=config)
    from .sessions import Session
    session = request.state.get("session")
    if isinstance(session, Session):
        expected = session._csrf
        if not expected or not submitted:
            return False
        try:
            return hmac.compare_digest(expected, submitted)
        except TypeError:
            return False
    cookie = _cookie_value(request, config.csrf_cookie_name)
    token = _signed_csrf_token(cookie, config=config) if cookie else None
    if not token or not submitted:
        return False
    try:
        return hmac.compare_digest(token, submitted)
    except TypeError:
        return False


def csrf_protect(fn):
    """Mark an API handler as CSRF-protected (opt-in, per route).

    State-changing requests to the marked handler must carry a valid token
    (see :func:`validate_csrf`); missing or mismatched tokens get HTTP 403.
    Safe methods stay exempt. The cookie and header names come from the
    app's :class:`SecurityConfig`, so the decorator itself takes no options.
    """
    fn.__pynext_csrf__ = True
    return fn


def new_csp_nonce():
    """Return a fresh per-request CSP nonce."""
    return secrets.token_urlsafe(16)


def content_security_policy(nonce=None, *, config=DEFAULT_SECURITY):
    """Return the default CSP policy, with a nonce when one is given.

    Directives come from ``config`` so an app can tune them. The defaults
    are ``script-src 'self'`` (plus the nonce), ``style-src 'self'``,
    ``object-src 'none'``, ``base-uri 'self'``, ``form-action 'self'`` and
    ``frame-ancestors 'none'``. ``style-src 'self'`` is safe here because
    the renderer already rejects inline ``style`` attributes and ``<style>``
    tags (see ``components.py``), so no page depends on inline styles.
    """
    if nonce is not None and not _CSP_NONCE.fullmatch(nonce):
        raise ValueError("Invalid CSP nonce")
    script_src = "'self'" + (f" 'nonce-{nonce}'" if nonce is not None else "")
    return (f"script-src {script_src}; style-src {config.csp_style_src}"
            f"; object-src 'none'; base-uri {config.csp_base_uri}"
            f"; form-action {config.csp_form_action}"
            f"; frame-ancestors {config.csp_frame_ancestors}")


def security_headers(*, config=DEFAULT_SECURITY, nonce=None, production=True):
    """Return the A1 default security headers as ``(name, value)`` byte pairs.

    HSTS is only included for production: it is ignored over plain HTTP
    anyway, and omitting it in development keeps ``pynext dev`` on
    ``http://127.0.0.1`` free of HSTS side effects. Every other header is
    the same in both modes, so a header scan passes identically in dev and
    production except for HSTS.
    """
    headers = []
    if production and config.hsts_enabled:
        value = f"max-age={config.hsts_max_age}"
        if config.hsts_include_subdomains:
            value += "; includeSubDomains"
        if config.hsts_preload:
            value += "; preload"
        headers.append((b"strict-transport-security", value.encode("ascii")))
    headers.append((b"x-content-type-options", b"nosniff"))
    if config.frame_options is not None:
        headers.append((b"x-frame-options", config.frame_options.encode("ascii")))
    if config.referrer_policy is not None:
        headers.append((b"referrer-policy", config.referrer_policy.encode("ascii")))
    if config.permissions_policy is not None:
        headers.append((b"permissions-policy", config.permissions_policy.encode("ascii")))
    headers.append((b"content-security-policy",
                    content_security_policy(nonce, config=config).encode("ascii")))
    return headers


def apply_security_headers(response, *, config=DEFAULT_SECURITY, nonce=None, production=True):
    """Add the A1 default security headers to ``response`` (in place).

    A header an app or handler already set wins and is never overwritten,
    so explicit choices survive — except ``content-security-policy``:
    :class:`Response` always adds the default policy on construction, so an
    existing CSP is replaced when it differs from the configured one. A
    nonce already present in that CSP is preserved (dynamic pages render
    with a per-request nonce that :meth:`App.dispatch` cannot know), and a
    CSP a handler set explicitly is left alone. Idempotent: calling it
    twice, or on a response that already went through dispatch, changes
    nothing. Returns the response for call-site chaining.
    """
    wanted = dict(security_headers(config=config, nonce=nonce, production=production))
    present = {}
    for index, (key, _value) in enumerate(response.headers):
        present.setdefault(key, index)
    if b"content-security-policy" in present and not getattr(response, "_csp_explicit", False):
        index = present[b"content-security-policy"]
        existing = response.headers[index][1]
        if nonce is None:
            match = re.search(rb"'nonce-([A-Za-z0-9+/=_-]+)'", existing)
            if match:
                nonce = match.group(1).decode("ascii")
                wanted = dict(security_headers(config=config, nonce=nonce, production=production))
        if existing != wanted[b"content-security-policy"]:
            response.headers[index] = (b"content-security-policy", wanted[b"content-security-policy"])
    for key, value in wanted.items():
        if key not in present:
            response.headers.append((key, value))
    return response


def _request_origin(request):
    values = _header_values(request, "origin")
    return values[0].decode("ascii", "ignore").strip() if values else None


def _is_same_origin(request, origin):
    """Compare scheme, canonical host and effective port, using trusted identity.

    Same-origin requests are not cross-origin requests, so CORS never
    blocks them: a same-origin page may call the app's own API with custom
    headers (which triggers a preflight) even when no origin is configured.
    """
    values = _header_values(request, "host")
    if len(values) != 1:
        return False
    try:
        host = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    scheme = request.state.get("client_scheme", "http")
    host = request.state.get("client_host") or host
    parsed = canonical_origin(origin)
    return parsed is not None and parsed == canonical_origin(f"{scheme}://{host}")


def _cors_origin_allowed(origin, config):
    return "*" in config.cors_allowed_origins or origin in config.cors_allowed_origins


def _allow_origin_value(origin, config):
    # A wildcard is only safe without credentials; with credentials the
    # exact origin must be echoed back.
    if "*" in config.cors_allowed_origins and not config.cors_allow_credentials:
        return "*"
    return origin


def _add_vary(response, token):
    wanted = token.encode("ascii")
    for index, (key, value) in enumerate(response.headers):
        if key != b"vary":
            continue
        tokens = [part.strip() for part in value.split(b",")]
        if wanted.lower() not in [part.lower() for part in tokens]:
            response.headers[index] = (b"vary", value + b", " + wanted)
        return
    response.headers.append((b"vary", wanted))


def cors_preflight_response(request, *, config=DEFAULT_SECURITY):
    """Return the response for a CORS preflight request, or ``None``.

    ``None`` means this is not a preflight (no Origin, or no
    ``Access-Control-Request-Method``), or it is same-origin, so normal
    OPTIONS handling applies. A cross-origin preflight from a disallowed
    origin, or asking for a method or header outside the configured policy,
    gets HTTP 403. An allowed preflight gets 204 with ``Allow`` and the
    ``Access-Control-Allow-*`` headers.
    """
    if request.method.upper() != "OPTIONS":
        return None
    origin = _request_origin(request)
    requested_method = _header_values(request, "access-control-request-method")
    if origin is None or not requested_method or _is_same_origin(request, origin):
        return None
    method = requested_method[0].decode("ascii", "ignore").strip().upper()
    allowed_methods = {item.upper() for item in config.cors_allowed_methods}
    if not _cors_origin_allowed(origin, config) or method not in allowed_methods:
        return Response("Forbidden", status=403, headers=[(b"vary", b"Origin")])
    requested_headers = []
    for value in _header_values(request, "access-control-request-headers"):
        requested_headers.extend(
            part.strip() for part in value.decode("ascii", "ignore").split(",") if part.strip())
    allowed_headers = {item.lower() for item in config.cors_allowed_headers}
    if any(item.lower() not in allowed_headers for item in requested_headers):
        return Response("Forbidden", status=403, headers=[(b"vary", b"Origin")])
    headers = [
        (b"allow", ", ".join(sorted(allowed_methods)).encode("ascii")),
        (b"access-control-allow-origin", _allow_origin_value(origin, config).encode("ascii")),
        (b"access-control-allow-methods", ", ".join(sorted(allowed_methods)).encode("ascii")),
        (b"vary", b"Origin"),
    ]
    if requested_headers:
        headers.append((b"access-control-allow-headers",
                        ", ".join(sorted(set(requested_headers))).encode("ascii")))
    if config.cors_allow_credentials:
        headers.append((b"access-control-allow-credentials", b"true"))
    if config.cors_max_age:
        headers.append((b"access-control-max-age", str(config.cors_max_age).encode("ascii")))
    return Response(b"", status=204, headers=headers, media_type=None)


def apply_cors_headers(request, response, *, config=DEFAULT_SECURITY):
    """Add CORS headers for an actual (non-preflight) cross-origin request.

    Allowed origins get ``Access-Control-Allow-Origin`` (and
    ``Access-Control-Allow-Credentials`` when configured); disallowed
    origins get no CORS headers, so the browser refuses to expose the
    response to the calling page. Same-origin requests and requests without
    an Origin are untouched. Responses that carried an Origin always get
    ``Vary: Origin`` so caches do not mix the two cases.
    """
    origin = _request_origin(request)
    if origin is None or _is_same_origin(request, origin):
        return response
    _add_vary(response, "Origin")
    if not _cors_origin_allowed(origin, config):
        return response
    response.headers.append(
        (b"access-control-allow-origin", _allow_origin_value(origin, config).encode("ascii")))
    if config.cors_allow_credentials:
        response.headers.append((b"access-control-allow-credentials", b"true"))
    return response


class WebSocketDisconnect(Exception):
    """The peer closed the WebSocket connection (or the connection was lost)."""

    def __init__(self, code=1000, reason=""):
        super().__init__(f"WebSocket disconnected: {code}")
        self.code = code
        self.reason = reason


MAX_WS_MESSAGE = 65_536
_WS_CLOSE_RESERVED = {1005, 1006, 1015}


class WebSocket:
    """One WebSocket connection, independent of the transport (ASGI or Axum).

    ``receive`` is an async callable returning ``("text", str)`` or
    ``("bytes", bytes)`` and raising :class:`WebSocketDisconnect` when the
    peer disconnects. ``send`` is an async callable accepting one tuple:
    ``("accept",)``, ``("text", str)``, ``("bytes", bytes)`` or
    ``("close", code, reason)``. The transport translates those into its own
    frames/messages and enforces the per-message size limit.
    """

    def __init__(self, *, receive, send, path, params=None, headers=(), query_string=b"",
                 max_message_bytes=MAX_WS_MESSAGE, subprotocol=None, offered_subprotocols=()):
        self._receive_raw = receive
        self._send_raw = send
        self.path = path
        self.params = dict(params or {})
        self.headers = tuple(headers)
        self.query_string = bytes(query_string)
        self.max_message_bytes = max_message_bytes
        self.subprotocol = subprotocol
        self.offered_subprotocols = tuple(offered_subprotocols)
        self._accepted = False
        self._closed = False

    @property
    def accepted(self):
        return self._accepted

    @property
    def closed(self):
        return self._closed

    @property
    def query(self):
        try:
            query = self.query_string.decode("utf-8", errors="strict")
            unquote_to_bytes(query).decode("utf-8", errors="strict")
            return parse_qs(query, keep_blank_values=True, max_num_fields=1000)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BadRequest("Invalid query string") from exc

    async def accept(self):
        if self._accepted:
            raise RuntimeError("WebSocket is already accepted")
        if self._closed:
            raise RuntimeError("WebSocket is already closed")
        await self._send_raw(("accept",))
        self._accepted = True

    def _require_accepted(self):
        if not self._accepted:
            raise RuntimeError("WebSocket must be accepted before sending or receiving")
        if self._closed:
            raise RuntimeError("WebSocket is already closed")

    async def send_text(self, data):
        if not isinstance(data, str):
            raise TypeError("WebSocket text messages must be str")
        self._require_accepted()
        if len(data.encode("utf-8")) > self.max_message_bytes:
            raise ValueError("WebSocket message exceeds the configured limit")
        await self._send_raw(("text", data))

    async def send_bytes(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("WebSocket binary messages must be bytes")
        self._require_accepted()
        size = data.nbytes if isinstance(data, memoryview) else len(data)
        if size > self.max_message_bytes:
            raise ValueError("WebSocket message exceeds the configured limit")
        await self._send_raw(("bytes", bytes(data)))

    async def receive(self):
        self._require_accepted()
        return await self._receive_raw()

    async def receive_text(self):
        kind, data = await self.receive()
        if kind != "text":
            raise TypeError("Received a binary WebSocket message; use receive_bytes()")
        return data

    async def receive_bytes(self):
        kind, data = await self.receive()
        if kind != "bytes":
            raise TypeError("Received a text WebSocket message; use receive_text()")
        return data

    async def close(self, code=1000, reason=""):
        if not isinstance(code, int) or isinstance(code, bool) or not 1000 <= code <= 4999 or code in _WS_CLOSE_RESERVED:
            raise ValueError(f"Invalid WebSocket close code: {code!r}")
        if not isinstance(reason, str):
            raise TypeError("WebSocket close reason must be str")
        if len(reason.encode("utf-8")) > 123:
            raise ValueError("WebSocket close reason exceeds 123 bytes")
        if self._closed:
            return
        self._closed = True
        await self._send_raw(("close", code, reason))



def _reject_json_constant(_value):
    raise ValueError("Non-finite numbers are not valid JSON")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query_string: bytes = b""
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b""
    params: dict = field(default_factory=dict)
    _app: object = field(default=None, repr=False, compare=False)
    state: dict = field(default_factory=dict, compare=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @property
    def cancelled(self) -> bool:
        """Thread-safe cooperative cancellation signal; does not undo writes."""
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        """Raise RequestCancelled before starting a new side effect."""
        if self.cancelled:
            raise RequestCancelled("Request execution was cancelled")

    @property
    def resources(self):
        """Named application resources managed by the server lifespan."""
        if self._app is None:
            raise RuntimeError("This request is not attached to an app")
        return self._app.resources

    @property
    def security(self):
        """The app's :class:`SecurityConfig` (for issuing CSRF cookies, etc.)."""
        if self._app is None:
            raise RuntimeError("This request is not attached to an app")
        return self._app.security

    async def revalidate(self, path):
        """Invalidate an ISR path in the current app's process-local cache."""
        self.check_cancelled()
        if self._app is None:
            raise RuntimeError("This request is not attached to an app")
        return await self._app.revalidate(path)

    @property
    def query(self):
        try:
            query = self.query_string.decode("utf-8", errors="strict")
            unquote_to_bytes(query).decode("utf-8", errors="strict")
            return parse_qs(query, keep_blank_values=True, max_num_fields=1000)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BadRequest("Invalid query string") from exc

    async def json(self):
        try:
            return json.loads(self.body, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise BadRequest("Invalid JSON body") from exc

    async def form(self):
        """Parse a URL-encoded or multipart body as a mapping of value lists."""
        content_types = _header_values(self, "content-type")
        if len(content_types) != 1:
            raise BadRequest("Expected one form Content-Type")
        try:
            return parse_form(content_types[0], self.body)
        except (UnicodeError, ValueError) as exc:
            raise BadRequest("Invalid form body") from exc


class Response:
    def __init__(self, body=b"", *, status=200, headers=(), media_type="text/plain; charset=utf-8",
                 csp_nonce=None):
        if not 100 <= status <= 599:
            raise ValueError("Invalid HTTP status")
        if isinstance(body, str):
            self.body = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray, memoryview)):
            self.body = bytes(body)
        else:
            raise TypeError(f"Response body must be str or bytes, got {type(body).__name__}")
        self.status = status
        self.headers = [(bytes(k).lower(), bytes(v)) for k, v in headers]
        for key, value in self.headers:
            if (not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9a-z-]+", key) or
                    any(c < 32 or c == 127 for c in value)):
                raise ValueError("Invalid response header")
        if media_type:
            try:
                media_type_bytes = media_type.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("Invalid media type") from exc
            if any(c < 32 or c == 127 for c in media_type_bytes):
                raise ValueError("Invalid media type")
        if media_type and not any(k == b"content-type" for k, _ in self.headers):
            self.headers.append((b"content-type", media_type_bytes))
        # Remember whether the caller set CSP explicitly: apply_security_headers
        # replaces only the policy this constructor added itself.
        self._csp_explicit = any(k == b"content-security-policy" for k, _ in self.headers)
        if not self._csp_explicit:
            self.headers.append((b"content-security-policy",
                                 content_security_policy(csp_nonce).encode("ascii")))
        if status not in (204, 304) and not any(k == b"content-length" for k, _ in self.headers):
            self.headers.append((b"content-length", str(len(self.body)).encode("ascii")))


class JSONResponse(Response):
    def __init__(self, value, *, status=200, headers=()):
        super().__init__(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                         status=status, headers=headers, media_type="application/json; charset=utf-8")


MAX_STREAM_CHUNK = 1_048_576


class StreamingResponse(Response):
    """A response produced by an asynchronous iterable, one bounded chunk at a time."""

    def __init__(self, content, *, status=200, headers=(), media_type="application/octet-stream"):
        if status < 200 or status in (204, 304):
            raise ValueError("Streaming responses require a status that permits a body")
        headers = list(headers)
        if any(bytes(key).lower() in {b"content-length", b"transfer-encoding"} for key, _ in headers):
            raise ValueError("Streaming response framing is managed by the server")
        super().__init__(b"", status=status, headers=headers, media_type=media_type)
        self.headers = [(key, value) for key, value in self.headers if key != b"content-length"]
        self._iterator = aiter(content)
        self._closed = False
        self._head_only = False

    async def _next_chunk(self):
        if self._closed:
            return None
        try:
            chunk = await anext(self._iterator)
        except StopAsyncIteration:
            return None
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            chunk = bytes(chunk)
        else:
            raise TypeError(f"Streaming response chunks must be str or bytes, got {type(chunk).__name__}")
        if len(chunk) > MAX_STREAM_CHUNK:
            raise ValueError(f"Streaming response chunk exceeds {MAX_STREAM_CHUNK} bytes")
        return chunk

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()
