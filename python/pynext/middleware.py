"""Standard middleware results: ``next`` / ``rewrite`` / ``redirect`` / ``response``.

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
import re
from dataclasses import dataclass, field
from typing import Final, Literal

from .http import Request, Response

__all__ = [
    "MAX_MIDDLEWARE_REWRITES",
    "REDIRECT_STATUSES",
    "MiddlewareResult",
    "classify_response",
    "is_redirect_response",
    "redirect_response",
    "rewrite_request",
]

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
