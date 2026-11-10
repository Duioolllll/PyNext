"""Conditional middleware scopes (Next.js-style ``has``/``missing`` matchers).

PyNext middleware is a global chain: every registered middleware runs for
every request. :func:`scoped` attaches a condition to one middleware so it
runs only when the condition holds, following Next.js
``build/segment-config/middleware/middleware-config.ts`` (``RouteHasSchema``)
and ``shared/lib/router/utils/prepare-destination.ts`` (``matchHas``):

- ``source`` is a path pattern in PyNext route syntax (``/api/[...rest]``,
  ``/admin/[id]``, ``/docs/[[...path]]``), compiled to a regex once at
  registration. A middleware with no ``source`` applies to every path.
- ``has`` is a list of conditions that must *all* hold; ``missing`` is a
  list of conditions that must *none* hold. Each condition is
  ``{"type": "header" | "query" | "cookie" | "host", "key": ..., "value": ...}``
  (``host`` takes a ``value`` and no ``key``). With no ``value`` the
  condition is an existence check; with a ``value`` it is a full-match
  regular expression (Next.js ``^value$`` semantics).
- Conditions are compiled once at registration; an invalid pattern, key or
  regex raises at startup instead of failing silently per request. Value
  regexes are length-capped so a scope cannot smuggle in a ReDoS-prone
  pattern unnoticed.

Execution is fail-closed: the source is matched against the decoded
request path *and*, when the transport recorded one, the raw
(still-encoded) request target, so an encoded path cannot dodge a scoped
middleware (Next.js does the same OR for middleware execution in
``resolve-routes.ts``; exemptions must never use it). A scope that cannot
be evaluated (for example a malformed query string) runs the middleware
rather than skipping it. The WebSocket pre-flight runs the same chain, so
a scoped middleware applies to upgrades exactly as it does to HTTP.

Standard middleware results: ``next`` / ``rewrite`` / ``redirect`` / ``response``.

A PyNext middleware is an async callable ``(request, call_next)``. Before
this module, its result had only two shapes: call ``call_next`` and return
what comes back, or return its own :class:`~pynext.http.Response`. That
left the other two outcomes implicit — an internal rewrite was
indistinguishable from "continue", and a redirect was just a response
that happened to carry a ``Location`` header. This module gives all four
outcomes one explicit representation, :class:`MiddlewareResult`, mirroring
the ``NextResponse`` outcomes in Next.js (``next`` / ``rewrite`` /
``redirect`` / the response itself; see ``adapter.ts`` and
``resolve-routes.ts:700-780`` in the analysis report, track D P2-1):

- ``next`` — continue with the request unchanged:
  ``await call_next(request)``, or ``return MiddlewareResult.next(request)``.
- ``rewrite`` — continue with a different internal path (and optionally a
  different query string), with no round-trip to the client:
  ``await call_next(rewrite_request(request, "/new"))``, or
  ``return MiddlewareResult.rewrite(rewrite_request(request, "/new"))``.
- ``redirect`` — answer with a 3xx response carrying a ``Location``
  header: ``return MiddlewareResult.redirect("/new")`` (default 307), or
  return ``redirect_response("/new")`` directly. No further middleware
  runs and the handler never sees the request.
- ``response`` — answer with any other response of the middleware's own
  making: ``return Response(...)``, or
  ``return MiddlewareResult.response(Response(...))``. This short-circuits
  the chain exactly like a redirect does.

The implicit forms keep working: returning a plain ``Response`` is still
a ``response`` (or a ``redirect`` when it is 3xx with a ``Location``
header — see :func:`classify_response`), and calling ``call_next`` with a
request whose path or query string differs is still a rewrite. The
explicit forms exist so middleware can state its intent, and so the
framework — not each middleware — owns the rewrite rules below.

Rewrite rules (enforced by :meth:`App._run_middleware`, not by callers):

- **Normalization.** A rewritten path is validated with the same
  ``_decode_path`` the transports use at the edge, so a rewrite cannot
  smuggle in an encoded separator (``%2F``/``%5C``/``%00``), a malformed
  escape, a ``//`` or backslash, or a dot segment. ``?`` and ``#`` are
  rejected in the path itself: the query belongs in ``query_string``.
  The stored path is the decoded/normalized one, exactly as an
  edge-decoded request path would be.
- **Re-dispatch.** Routing, ``@require_auth``/``@require_permission``
  and CSRF are all evaluated against the *rewritten* request, because
  they run in ``_dispatch`` after the middleware chain. The final
  matched pattern is recorded in ``route_var`` as usual, so the access
  log and metrics label the request by the route that actually served
  it, not the one the client asked for. ``request.state`` is shared
  with the rewritten request, so values a middleware stored (session,
  identity, ``client_ip``) survive the rewrite; route ``params`` are
  re-derived from the rewritten path.
- **Admission is decided once.** Rate limiting is charged (or exempted)
  before middleware runs, on the original path, on both transports.
  A rewrite neither consumes a second token nor re-evaluates the
  exemption. Middleware is trusted app code, so a rewrite from an
  exempt path to a protected route is the app's own choice — the same
  as a middleware answering the request itself — and routing, auth and
  CSRF above still apply to the rewritten path.
- **Loop cap.** At most :data:`MAX_MIDDLEWARE_REWRITES` rewrites run per
  request (5, matching Next.js's ``handleIndex > 5`` limit on
  re-invoked requests). The sixth rewrite fails closed with a 500 (503
  for a WebSocket pre-flight), never an unbounded loop. Each middleware
  still calls ``call_next`` at most once.

WebSocket pre-flights run the same chain around a synthetic ``GET``:
a rewrite there still reaches the terminal (it only changes the request
the authorization check sees), while a redirect or any other response
denies the upgrade, exactly like a short-circuit does today.
"""

import dataclasses
import inspect
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import parse_qs, unquote_to_bytes

from .http import Request, Response

__all__ = ["MiddlewareScope", "RouteHas", "ScopedMiddleware", "scoped",
           "MAX_MIDDLEWARE_REWRITES", "REDIRECT_STATUSES", "MiddlewareResult",
           "classify_response", "is_redirect_response", "redirect_response",
           "rewrite_request"]

log = logging.getLogger("pynext.middleware")

#: Next.js ``SourceSchema`` caps a matcher at 4096 characters.
_MAX_SOURCE_LENGTH = 4096
#: Bound on a ``has``/``missing`` value regex: compiled once at startup,
#: but a long pattern is still a ReDoS and review burden.
_MAX_HAS_VALUE_LENGTH = 1024
_HAS_TYPES = frozenset({"header", "query", "cookie", "host"})
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SEGMENT = re.compile(r"\[(\[)?(\.\.\.)?([A-Za-z0-9_-]+)\](\])?")


def _cookies(request):
    """Parse ``Cookie`` headers into a name -> first-value mapping."""
    cookies = {}
    for key, value in request.headers:
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, val = part.split("=", 1)
            name = name.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            cookies.setdefault(name, val)
    return cookies


def _query(request):
    """Parse the query string, mirroring :attr:`Request.query` strictness.

    Invalid UTF-8 (including malformed percent-decoded UTF-8) and more
    than 1,000 fields raise :class:`ValueError`; the caller treats that as
    unevaluatable and runs the middleware (fail-closed).
    """
    text = request.query_string.decode("utf-8", errors="strict")
    unquote_to_bytes(text).decode("utf-8", errors="strict")
    return parse_qs(text, keep_blank_values=True, max_num_fields=1000)


def _host(request):
    """Return the request host without port, lowercased, else ``None``."""
    for key, value in request.headers:
        if key.lower() != b"host":
            continue
        host = value.decode("latin-1").strip()
        if not host:
            return None
        if host.startswith("["):
            end = host.find("]")
            return host[1:end].lower() if end > 0 else host.lower()
        return host.split(":", 1)[0].lower()
    return None


def _compile_source(source):
    """Compile a ``source`` path pattern to a regex.

    Same segment grammar as :func:`pynext.build._segment_param`:
    ``[name]`` matches one segment, ``[...name]`` the rest, and
    ``[[...name]]`` the rest or nothing. Route groups (``(group)``) and
    slots (``@slot``) are not URL segments and are rejected, as are
    ``%``/``?``/``#`` (they can never occur in a decoded routing path).
    """
    if source is None:
        return None
    if not isinstance(source, str):
        raise TypeError("middleware source must be a string")
    if len(source) > _MAX_SOURCE_LENGTH:
        raise ValueError("middleware source exceeds 4096 characters")
    if not source.startswith("/"):
        raise ValueError(f"middleware source must start with '/': {source!r}")
    if (any(c in "%?#\\" or ord(c) < 32 or ord(c) == 127 for c in source)
            or (source != "/" and source.endswith("/")) or "//" in source):
        raise ValueError(f"Invalid middleware source: {source!r}")
    segments = [] if source == "/" else source.strip("/").split("/")
    names = set()
    parts = []
    for index, segment in enumerate(segments):
        if segment.startswith("(") or segment.startswith("@"):
            raise ValueError(f"Invalid middleware source segment: {segment!r}")
        parsed = _SEGMENT.fullmatch(segment) if segment.startswith("[") else None
        if segment.startswith("[") and parsed is None:
            raise ValueError(f"Invalid middleware source segment: {segment!r}")
        if parsed is None:
            parts.append("/" + re.escape(segment))
            continue
        double_open, dots, name, double_close = parsed.groups()
        if bool(double_open) != bool(double_close):
            raise ValueError(f"Invalid middleware source segment: {segment!r}")
        if double_open and not dots:
            raise ValueError(f"Optional catch-all must use [[...name]]: {segment!r}")
        kind = "optional" if double_open else ("catch_all" if dots else "dynamic")
        if name in names:
            raise ValueError(f"Duplicate parameter {name!r} in middleware source: {source!r}")
        names.add(name)
        if kind != "dynamic" and index != len(segments) - 1:
            raise ValueError(f"Catch-all must be the last segment: {segment!r}")
        if kind == "dynamic":
            parts.append("/[^/]+")
        elif kind == "catch_all":
            parts.append("/.+")
        else:  # optional catch-all: this segment, or nothing at all
            parts.append("(?:/.*)?")
    if not parts:
        return re.compile("^/$")
    return re.compile("^" + "".join(parts) + "$")


@dataclass(frozen=True)
class RouteHas:
    """One ``has``/``missing`` condition (Next.js ``RouteHas``).

    ``type`` is ``"header"``, ``"query"``, ``"cookie"`` or ``"host"``.
    ``key`` names the header/query/cookie (``"host"`` takes no key).
    With ``value`` unset (or empty) the condition is an existence check;
    with ``value`` set it is a full-match regex, as in Next.js ``matchHas``.
    Named groups in the regex are accepted but not exposed: PyNext has no
    rewrite destination for them to flow into.
    """

    type: str
    key: str | None = None
    value: str | None = None
    _regex: re.Pattern | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.type, str) or self.type not in _HAS_TYPES:
            raise ValueError(f"has/missing type must be one of {sorted(_HAS_TYPES)}: {self.type!r}")
        if self.type == "host":
            if self.key is not None:
                raise ValueError("host condition takes no key")
            if not self.value:
                raise ValueError("host condition requires a value")
        else:
            if not isinstance(self.key, str) or not self.key:
                raise ValueError(f"{self.type} condition requires a key")
            if self.type == "header" and not _TOKEN.fullmatch(self.key):
                raise ValueError(f"Invalid header name: {self.key!r}")
            if self.type == "cookie" and not _TOKEN.fullmatch(self.key):
                raise ValueError(f"Invalid cookie name: {self.key!r}")
            if (self.type == "query" and
                    any(c in "&=#?" or ord(c) < 32 or ord(c) == 127 for c in self.key)):
                raise ValueError(f"Invalid query key: {self.key!r}")
        regex = None
        value = self.value
        if value is not None:
            if not isinstance(value, str):
                raise TypeError("has/missing value must be a string")
            if value == "":
                # Next.js treats an empty value as "no value": existence.
                object.__setattr__(self, "value", None)
                value = None
            elif len(value) > _MAX_HAS_VALUE_LENGTH:
                raise ValueError("has/missing value exceeds 1024 characters")
            else:
                try:
                    regex = re.compile(value)
                except re.error as exc:
                    raise ValueError(f"Invalid has/missing value regex: {value!r}") from exc
        if self.type == "header":
            object.__setattr__(self, "key", self.key.lower())
        object.__setattr__(self, "_regex", regex)

    def matches(self, request: "Request") -> bool:
        """Return whether this condition holds for ``request``."""
        if self.type == "header":
            wanted = self.key.encode("ascii")
            values = [value.decode("latin-1") for key, value in request.headers
                      if key.lower() == wanted]
            if self._regex is None:
                return any(values)
            return any(self._regex.fullmatch(value) for value in values if value)
        if self.type == "cookie":
            value = _cookies(request).get(self.key)
        elif self.type == "query":
            values = _query(request).get(self.key)
            value = values[-1] if values else None
        else:  # host
            value = _host(request)
        if self._regex is None:
            return bool(value)
        return bool(value) and self._regex.fullmatch(value) is not None


def _coerce_conditions(value, name):
    """Normalize ``has``/``missing`` input to a tuple of :class:`RouteHas`.

    Accepts a single condition, or an iterable of conditions, each a
    :class:`RouteHas` or a ``{"type": ..., "key": ..., "value": ...}`` dict.
    """
    if value is None:
        return ()
    if isinstance(value, (dict, RouteHas)):
        value = (value,)
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{name} must be a condition or an iterable of conditions")
    items = []
    for item in value:
        if isinstance(item, RouteHas):
            items.append(item)
        elif isinstance(item, dict):
            items.append(RouteHas(**item))
        else:
            raise TypeError(f"{name} conditions must be RouteHas or dict, got {type(item).__name__}")
    return tuple(items)


@dataclass(frozen=True)
class MiddlewareScope:
    """A compiled conditional scope for one middleware.

    All ``has`` conditions must hold and no ``missing`` condition may hold
    for the middleware to run; ``source``, when set, must match the request
    path. An empty scope (no ``source``, ``has`` or ``missing``) matches
    every request, which is the same as an unscoped middleware.
    """

    source: str | None = None
    has: tuple = field(default_factory=tuple)
    missing: tuple = field(default_factory=tuple)
    _source_re: re.Pattern | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "has", _coerce_conditions(self.has, "has"))
        object.__setattr__(self, "missing", _coerce_conditions(self.missing, "missing"))
        object.__setattr__(self, "_source_re", _compile_source(self.source))

    def _matches_source(self, request: "Request") -> bool:
        if self._source_re.fullmatch(request.path):
            return True
        # Fail-closed OR: also try the raw (still-encoded) target the
        # transport recorded. An encoded path must not dodge the scope.
        raw = getattr(request, "raw_path", None)
        if not raw:
            return False
        try:
            decoded = unquote_to_bytes(raw).decode("utf-8", errors="strict")
        except (UnicodeError, ValueError, TypeError):
            decoded = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return decoded != request.path and self._source_re.fullmatch(decoded) is not None

    def matches(self, request: "Request") -> bool:
        """Return whether the middleware should run for ``request``.

        A scope that cannot be evaluated (for example a malformed query
        string) matches: execution is fail-closed, so the middleware runs
        rather than being silently skipped.
        """
        try:
            if self._source_re is not None and not self._matches_source(request):
                return False
            return (all(item.matches(request) for item in self.has)
                    and not any(item.matches(request) for item in self.missing))
        except Exception:
            log.debug("middleware scope evaluation failed; running middleware", exc_info=True)
            return True


class ScopedMiddleware:
    """A middleware callable that runs only when its scope matches.

    Out of scope it calls ``call_next`` unchanged, so the rest of the chain
    never sees it; in scope it delegates to the wrapped middleware. This
    keeps the skip transparent to neighbouring middleware (including the
    ``call_next``-once guard) and lets the WebSocket pre-flight, which runs
    the same chain, share the scope with no extra code.
    """

    def __init__(self, middleware, scope):
        if not (inspect.iscoroutinefunction(middleware) or
                inspect.iscoroutinefunction(getattr(middleware, "__call__", None))):
            raise TypeError("middleware entries must be async callables")
        if not isinstance(scope, MiddlewareScope):
            raise TypeError("scope must be a MiddlewareScope")
        self.middleware = middleware
        self.scope = scope

    async def __call__(self, request, call_next):
        if not self.scope.matches(request):
            return await call_next(request)
        return await self.middleware(request, call_next)

    def __repr__(self):
        return f"ScopedMiddleware({self.middleware!r}, scope={self.scope!r})"


def scoped(middleware, *, source=None, has=(), missing=()):
    """Wrap ``middleware`` so it runs only when the scope matches.

    ``source`` is a path pattern in PyNext route syntax (``/api/[...rest]``,
    ``/admin/[id]``, ``/docs/[[...path]]``); ``has``/``missing`` are
    conditions (``RouteHas`` or ``{"type", "key", "value"}`` dicts, or a
    single such condition). Invalid patterns, keys or value regexes raise
    :class:`ValueError` here, at registration time, so a bad scope fails
    startup instead of silently never (or always) running.
    """
    scope = MiddlewareScope(source=source,
                            has=_coerce_conditions(has, "has"),
                            missing=_coerce_conditions(missing, "missing"))
    return ScopedMiddleware(middleware, scope)


# ---------------------------------------------------------------------------
# Standard middleware results (``next`` / ``rewrite`` / ``redirect`` /
# ``response``); see the module docstring.
# ---------------------------------------------------------------------------

#: Maximum rewrites per request (Next.js ``handleIndex > 5`` parity).
MAX_MIDDLEWARE_REWRITES: Final = 5

#: Statuses a redirect response may carry.
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

#: The four middleware outcomes.
MiddlewareKind = Literal["next", "rewrite", "redirect", "response"]

_ABSOLUTE_URL = re.compile(r"https?://[^\s/$.?#].[^\s]*", re.IGNORECASE)


def _validate_location(location: str) -> str:
    """Return ``location`` when it is a safe redirect target, else raise."""
    if not isinstance(location, str) or not location:
        raise ValueError("redirect location must be a non-empty string")
    if any(ord(c) < 32 or ord(c) == 127 or c == " " for c in location):
        raise ValueError("redirect location must not contain control characters or spaces")
    if location.startswith("/") and not location.startswith("//"):
        return location
    if _ABSOLUTE_URL.fullmatch(location):
        return location
    raise ValueError(
        "redirect location must be an absolute path ('/new') or an absolute "
        "http(s) URL; scheme-relative ('//host/...') and other schemes are rejected"
    )


def is_redirect_response(response: Response) -> bool:
    """True when ``response`` is a 3xx response carrying a ``Location`` header."""
    if not isinstance(response, Response):
        return False
    if not 300 <= response.status < 400:
        return False
    return any(key == b"location" for key, _ in response.headers)


def classify_response(response: Response) -> MiddlewareKind:
    """Classify a middleware's plain ``Response`` as ``"redirect"`` or ``"response"``.

    This is the rule the framework applies to the implicit form: a 3xx
    response with a ``Location`` header is a redirect, anything else is
    a short-circuit response. It never inspects the body.
    """
    if not isinstance(response, Response):
        raise TypeError("middleware must return a Response")
    return "redirect" if is_redirect_response(response) else "response"


def redirect_response(location: str, status: int = 307) -> Response:
    """Build the ``Response`` for a redirect outcome.

    ``status`` defaults to 307 (method-preserving, the Next.js
    ``NextResponse.redirect`` default) and must be one of
    :data:`REDIRECT_STATUSES`. ``location`` is an absolute path or an
    absolute http(s) URL; scheme-relative and non-http(s) targets are
    rejected so a middleware cannot be tricked into an open redirect.
    """
    _validate_location(location)
    if status not in REDIRECT_STATUSES:
        raise ValueError(f"redirect status must be one of {sorted(REDIRECT_STATUSES)}")
    return Response("", status=status, headers=[(b"location", location.encode("ascii"))])


def rewrite_request(request: Request, path: str,
                    query_string: str | bytes | bytearray | memoryview | None = None) -> Request:
    """Build the request a ``rewrite`` outcome continues with.

    Copies method, headers, body, ``state`` (shared, not copied — values
    a middleware stored survive the rewrite) and app/cancellation
    handles from ``request``; ``params`` are cleared because
    :meth:`App._dispatch` re-derives them from the new path. ``path``
    must be an absolute path with no ``?`` or ``#`` (the query belongs
    in ``query_string``); the full edge normalization (percent-decoding,
    encoded-separator rejection) is applied by the framework when the
    rewrite runs, so a bad path fails closed there even if this helper
    accepted its shape.
    """
    if not isinstance(request, Request):
        raise TypeError("rewrite_request requires a Request")
    if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("rewrite path must be an absolute path without '?' or '#'")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("rewrite path must not contain control characters")
    if query_string is None:
        query = request.query_string
    elif isinstance(query_string, str):
        query = query_string.encode("utf-8")
    elif isinstance(query_string, (bytes, bytearray, memoryview)):
        query = bytes(query_string)
    else:
        raise TypeError("query_string must be str or bytes")
    return dataclasses.replace(request, path=path, query_string=query, params={})


@dataclass(frozen=True)
class MiddlewareResult:
    """One of the four middleware outcomes, stated explicitly.

    ``kind`` is ``"next"``, ``"rewrite"``, ``"redirect"`` or
    ``"response"``. ``next``/``rewrite`` carry the request to continue
    with in :attr:`request`; ``redirect``/``response`` carry the
    short-circuit response in :attr:`response`. Build instances with the
    classmethods (:meth:`next`, :meth:`rewrite`, :meth:`redirect`,
    :meth:`response`) rather than the constructor: they validate the
    pairing, so an outcome can never claim to be a redirect without a
    redirect response, or a rewrite without a request.
    """

    kind: MiddlewareKind
    request: Request | None = field(default=None)
    response: Response | None = field(default=None)

    def __post_init__(self) -> None:
        if self.kind not in ("next", "rewrite", "redirect", "response"):
            raise ValueError(f"invalid middleware result kind: {self.kind!r}")
        if self.kind in ("next", "rewrite"):
            if not isinstance(self.request, Request):
                raise TypeError(f"middleware result {self.kind!r} requires a Request")
            if self.response is not None:
                raise ValueError(f"middleware result {self.kind!r} must not carry a response")
        else:
            if not isinstance(self.response, Response):
                raise TypeError(f"middleware result {self.kind!r} requires a Response")
            if self.request is not None:
                raise ValueError(f"middleware result {self.kind!r} must not carry a request")
            if self.kind == "redirect" and not is_redirect_response(self.response):
                raise ValueError("a redirect result requires a 3xx response with a Location header")

    @property
    def is_terminal(self) -> bool:
        """True for ``redirect``/``response``: no further middleware runs."""
        return self.kind in ("redirect", "response")

    @property
    def is_continue(self) -> bool:
        """True for ``next``/``rewrite``: the chain continues."""
        return self.kind in ("next", "rewrite")

    @classmethod
    def next(cls, request: Request) -> "MiddlewareResult":
        """Continue with ``request`` unchanged (``await call_next(request)``)."""
        return cls("next", request=request)

    @classmethod
    def rewrite(cls, request: Request) -> "MiddlewareResult":
        """Continue with a rewritten ``request`` (see :func:`rewrite_request`).

        The request's path (or query string) should differ from the one
        the middleware received; an unchanged request is simply ``next``.
        """
        return cls("rewrite", request=request)

    @classmethod
    def redirect(cls, location: str, status: int = 307) -> "MiddlewareResult":
        """Answer with a redirect to ``location`` (see :func:`redirect_response`)."""
        return cls("redirect", response=redirect_response(location, status=status))


# NOTE: ``MiddlewareResult.response`` is attached here, not declared in the
# class body. A classmethod named ``response`` inside the body would rebind
# the ``response`` field's default before ``@dataclass`` builds
# ``__init__`` (the generated default became the bound classmethod), so
# ``MiddlewareResult.next(...)`` / ``.rewrite(...)`` — which rely on the
# ``response=None`` default — raised ``ValueError``. Attaching it after
# the dataclass is built keeps the default ``None`` and the public
# ``MiddlewareResult.response(...)`` factory. ``setattr`` (rather than a
# plain assignment) keeps type checkers from reading the classmethod as a
# value for the ``Response | None`` field.
def _middleware_result_response(cls, response: Response) -> MiddlewareResult:
    """Answer with ``response`` directly, short-circuiting the chain.

    A 3xx response with a ``Location`` header is classified as a
    ``redirect`` outcome, anything else as ``response`` — the same
    rule :func:`classify_response` applies to a plain returned
    ``Response``.
    """
    return cls(classify_response(response), response=response)


setattr(MiddlewareResult, "response", classmethod(_middleware_result_response))
