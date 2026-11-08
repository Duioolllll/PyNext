import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote_to_bytes

from .forms import parse_form


class BadRequest(Exception):
    """A request could not be parsed or failed application validation."""


#: Methods that never change server state and are exempt from CSRF checks.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

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

    CSRF protection is opt-in: either set ``csrf_enabled`` to require a
    valid token on every state-changing API request, or mark individual
    handlers with :func:`csrf_protect`. CORS denies every cross-origin
    request by default; list origins explicitly to allow them. CSP nonce
    support is off by default and only changes dynamically rendered pages.

    The A1 default security headers (HSTS, ``X-Content-Type-Options``,
    ``X-Frame-Options``, ``Referrer-Policy``, ``Permissions-Policy`` and the
    CSP below) are on by default and applied to every response by
    :func:`apply_security_headers`. HSTS is only *sent* in production
    (see :func:`security_headers`): it is ignored over plain HTTP anyway,
    and omitting it in development keeps ``pynext dev`` on
    ``http://127.0.0.1`` free of HSTS side effects. Set a field to ``None``
    to disable that header (``frame_options``, ``referrer_policy``,
    ``permissions_policy``); set ``hsts_enabled=False`` to disable HSTS.
    """

    csrf_enabled: bool = False
    csrf_cookie_name: str = "pynext_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    csrf_form_field: str = "csrf_token"
    csrf_cookie_secure: bool = False
    csrf_cookie_samesite: str = "Lax"
    cors_allowed_origins: tuple = ()
    cors_allowed_methods: tuple = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
    cors_allowed_headers: tuple = ("Content-Type", "Authorization", "X-CSRF-Token")
    cors_allow_credentials: bool = False
    cors_max_age: int = 600
    csp_nonce_enabled: bool = False
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

    def __post_init__(self):
        if not isinstance(self.csrf_enabled, bool):
            raise TypeError("csrf_enabled must be a bool")
        for name in ("csrf_cookie_name", "csrf_header_name", "csrf_form_field"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _TOKEN_NAME.fullmatch(value):
                raise ValueError(f"{name} must be a valid header/cookie token name")
        if self.csrf_cookie_samesite not in {"Lax", "Strict", "None"}:
            raise ValueError("csrf_cookie_samesite must be Lax, Strict or None")
        if not isinstance(self.csrf_cookie_secure, bool):
            raise TypeError("csrf_cookie_secure must be a bool")
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


#: The framework defaults: CSRF off (opt-in), CORS deny-all, CSP nonce off.
DEFAULT_SECURITY = SecurityConfig()


def new_csrf_token():
    """Return a fresh unpredictable CSRF token (double-submit pattern)."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response, token, *, config=DEFAULT_SECURITY):
    """Set the double-submit CSRF cookie on a response.

    The cookie is readable by same-origin JavaScript on purpose: the client
    reads it and sends the same value back in the ``X-CSRF-Token`` header (or
    the ``csrf_token`` form field), so it must not be ``HttpOnly``. It is
    ``SameSite=Lax`` by default; pass a config with ``csrf_cookie_secure=True``
    in production so it is only sent over HTTPS.
    """
    parts = [f"{config.csrf_cookie_name}={token}", "Path=/",
             f"SameSite={config.csrf_cookie_samesite}"]
    if config.csrf_cookie_secure:
        parts.append("Secure")
    response.headers.append((b"set-cookie", "; ".join(parts).encode("ascii")))
    return response


def _header_values(request, name):
    wanted = name.lower().encode("ascii")
    return [value for key, value in request.headers if key.lower() == wanted]


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


def validate_csrf(request, *, config=DEFAULT_SECURITY):
    """Check a session-bound token, or the double-submit token without sessions.

    GET, HEAD and OPTIONS are exempt. Tokens are compared in constant time.
    """
    if request.method.upper() in SAFE_METHODS:
        return True
    submitted = csrf_token_from_request(request, config=config)
    from .sessions import Session
    session = request.state.get("session")
    expected = session._csrf if isinstance(session, Session) else _cookie_value(request, config.csrf_cookie_name)
    if not expected or not submitted:
        return False
    try:
        return hmac.compare_digest(expected, submitted)
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
    """True when the Origin host matches the Host header.

    Same-origin requests are not cross-origin requests, so CORS never
    blocks them: a same-origin page may call the app's own API with custom
    headers (which triggers a preflight) even when no origin is configured.
    """
    values = _header_values(request, "host")
    if not values:
        return False
    host = values[0].decode("ascii", "ignore").strip().lower()
    match = re.fullmatch(r"https?://([^/]+)", origin)
    return match is not None and match.group(1).lower() == host


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

    @property
    def resources(self):
        """Named application resources managed by the server lifespan."""
        if self._app is None:
            raise RuntimeError("This request is not attached to an app")
        return self._app.resources

    async def revalidate(self, path):
        """Invalidate an ISR path in the current app's process-local cache."""
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
