"""Startup-compiled matchers for middleware scope and per-path headers.

Next.js compiles middleware matchers and custom-route headers at build
time — the matcher schema is zod-validated (``middleware-config.ts``)
and a violation fails the build (``get-page-static-info.ts:526-545``),
while header rules are validated by ``checkHeader`` and compiled into
regex routes by ``buildCustomRoute`` (``load-custom-routes.ts``,
``filesystem.ts:594-602``). A bad matcher therefore never survives to
run silently at request time.

PyNext does the same at app startup. :func:`compile_matcher`,
:func:`compile_scope` and :func:`compile_header_rules` validate and
compile their input once, when the app is created; anything invalid
raises :class:`ValueError` (or :class:`TypeError` for a wrong shape) and
app creation — and therefore server startup — fails instead of the
rule quietly never matching.

Matcher syntax reuses the route-pattern segments PyNext apps already
know (see :func:`pynext.build._segment_param`):

- ``/`` — the root path only;
- literal segments (``/api/users``);
- ``[name]`` — exactly one non-empty segment;
- ``[...name]`` — one or more segments (last segment only);
- ``[[...name]]`` — zero or more segments (last segment only);
- a trailing ``/*`` — zero or more segments (``/api/*`` matches
  ``/api`` itself and everything below it).

A matcher is always evaluated against the single decoded routing path
(``Request.path``) — the same value the router sees — so there is no
second, differently-normalized path for a scope check to disagree with.
Patterns containing ``%``, ``?`` or ``#`` are rejected at startup:
such a pattern could never match a decoded routing path, so accepting
it would only create a rule that silently never fires.

``has`` / ``missing`` conditions follow Next.js ``RouteHasSchema``
semantics (``middleware-config.ts:13-33``, ``matchHas`` in
``prepare-destination.ts:48-125``): ``type`` is ``header`` | ``query`` |
``cookie`` | ``host``; ``key`` names the header/query/cookie (absent for
``host``); ``value`` is optional — without it the condition is a
presence check, with it the value must fully match the given regular
expression. A ``missing`` condition matches exactly when the same
``has`` condition would not. Condition value regexes are compiled once
at startup; a value that does not compile is a startup error.

Per-path header rules are applied after routing, before the default
security headers: the first matching rule wins per header name, and a
header the handler already set always wins over a rule. The one
exception is ``content-security-policy``: :class:`pynext.http.Response`
always adds a default policy on construction, so a rule that sets CSP
replaces that default (and marks it explicit so
:func:`pynext.http.apply_security_headers` leaves it alone); a CSP a
handler set explicitly still wins over a rule.
"""

import inspect
import re
from dataclasses import dataclass, field

__all__ = ["Matcher", "HasCondition", "Scope", "HeaderRule", "ScopedMiddleware",
           "compile_matcher", "compile_has_condition", "compile_scope",
           "compile_header_rules", "compile_middleware_entries", "scoped_middleware"]

#: Next.js caps a matcher source at 4096 characters
#: (``middleware-config.ts`` ``SourceSchema``); same cap here.
MAX_MATCHER_LENGTH = 4096
#: A ``has``/``missing`` value is a regex compiled at startup; cap its
#: length so a pathological pattern cannot be smuggled in unnoticed.
MAX_HAS_VALUE_LENGTH = 1024
#: A single per-path header value must fit in one header line.
MAX_HEADER_VALUE_LENGTH = 8192
#: A sanity cap on the number of header rules per app.
MAX_HEADER_RULES = 1024

_TOKEN_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_PARAM_NAME = re.compile(r"[A-Za-z0-9_-]+")
_QUERY_KEY = re.compile(r"[A-Za-z0-9_.\-]+")
_SEGMENT_PARAM = re.compile(r"\[(\[)?(\.\.\.)?([A-Za-z0-9_-]+)\](\])?")
_HAS_TYPES = frozenset({"header", "query", "cookie", "host"})
#: Framing headers are owned by the server; a rule must not set them
#: (``StreamingResponse`` rejects them for the same reason).
_FRAMING_HEADERS = frozenset({"content-length", "transfer-encoding"})


@dataclass(frozen=True)
class Matcher:
    """A matcher source compiled to a regex at startup."""

    source: str
    regex: re.Pattern = field(repr=False)
    param_names: tuple = ()

    def matches(self, path):
        """Return the captured params dict, or ``None`` when no match."""
        match = self.regex.match(path)
        if match is None:
            return None
        return {name: match.group(name) for name in self.param_names}


def compile_matcher(source):
    """Validate ``source`` and compile it to a :class:`Matcher`.

    Raises :class:`ValueError` for any source that could never match a
    decoded routing path (wrong shape, ``%``/``?``/``#``, a catch-all
    that is not the last segment, a duplicated parameter name, ...).
    """
    if not isinstance(source, str):
        raise TypeError("matcher source must be a str")
    if not source.startswith("/"):
        raise ValueError(f"matcher source must start with '/': {source!r}")
    if len(source) > MAX_MATCHER_LENGTH:
        raise ValueError(f"matcher source exceeds {MAX_MATCHER_LENGTH} characters: {source!r}")
    if len(source) > 1 and source.endswith("/"):
        raise ValueError(f"matcher source must not end with '/': {source!r}")
    if "//" in source or "\\" in source:
        raise ValueError(f"matcher source must not contain '//' or backslashes: {source!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in source):
        raise ValueError(f"matcher source must not contain control characters: {source!r}")
    if "%" in source or "?" in source or "#" in source:
        raise ValueError(
            f"matcher source must not contain '%', '?' or '#': {source!r} "
            "(a decoded routing path never contains them, so this pattern could never match)")
    if source == "/":
        return Matcher(source, re.compile(r"\A/\Z"))
    segments = source.strip("/").split("/")
    parts = [r"\A"]
    names = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "*":
            if not last:
                raise ValueError(f"'*' is only allowed as the last segment: {source!r}")
            # Trailing /* matches zero or more segments: /api/* matches
            # /api itself as well as /api/users/1.
            parts.append(r"(?:/.*)?")
            continue
        if segment.startswith("["):
            parsed = _SEGMENT_PARAM.fullmatch(segment)
            if parsed is None:
                raise ValueError(f"Invalid matcher segment: {segment!r} in {source!r}")
            double_open, dots, name, double_close = parsed.groups()
            if bool(double_open) != bool(double_close):
                raise ValueError(f"Invalid matcher segment: {segment!r} in {source!r}")
            if double_open and not dots:
                raise ValueError(f"Optional catch-all must use [[...name]]: {segment!r} in {source!r}")
            if name in names:
                raise ValueError(f"Duplicate matcher parameter {name!r} in {source!r}")
            names.append(name)
            if double_open:
                if not last:
                    raise ValueError(f"Optional catch-all must be the last segment: {source!r}")
                parts.append(rf"(?:/(?P<{name}>.*))?")
            elif dots:
                if not last:
                    raise ValueError(f"Catch-all must be the last segment: {source!r}")
                parts.append(rf"/(?P<{name}>.+)")
            else:
                parts.append(rf"/(?P<{name}>[^/]+)")
            continue
        if segment in (".", ".."):
            raise ValueError(f"matcher source must not contain '.' or '..' segments: {source!r}")
        if segment.startswith("(") or segment.endswith(")"):
            raise ValueError(
                f"matcher source must not contain route groups: {source!r} "
                "(groups organise files; they never appear in a URL)")
        if any(c in segment for c in "[]{}*"):
            raise ValueError(f"Invalid matcher segment: {segment!r} in {source!r}")
        if not segment:
            raise ValueError(f"matcher source must not contain empty segments: {source!r}")
        parts.append("/" + re.escape(segment))
    parts.append(r"\Z")
    try:
        regex = re.compile("".join(parts))
    except re.error as exc:
        raise ValueError(f"Invalid matcher source {source!r}: {exc}") from exc
    return Matcher(source, regex, tuple(names))


def _compile_has_value(condition_type, value):
    if not isinstance(value, str):
        raise TypeError(f"has/missing value for {condition_type!r} must be a str")
    if not value:
        raise ValueError(f"has/missing value for {condition_type!r} must not be empty")
    if len(value) > MAX_HAS_VALUE_LENGTH:
        raise ValueError(
            f"has/missing value for {condition_type!r} exceeds {MAX_HAS_VALUE_LENGTH} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"has/missing value for {condition_type!r} must not contain control characters")
    text = value.lower() if condition_type == "host" else value
    try:
        return re.compile(text)
    except re.error as exc:
        raise ValueError(f"Invalid has/missing value regex {value!r}: {exc}") from exc


@dataclass(frozen=True)
class HasCondition:
    """One ``has``/``missing`` condition, compiled at startup.

    ``type`` is ``header`` | ``query`` | ``cookie`` | ``host``; ``key``
    is ``None`` for ``host``. When ``value`` (the raw regex text) is
    ``None`` the condition is a presence check; otherwise the candidate
    must fully match the compiled regex.
    """

    type: str
    key: str | None
    value: str | None = None
    _regex: re.Pattern | None = field(default=None, repr=False, compare=False)

    def matches(self, *, headers=(), query=None, cookies=None, host=""):
        """True when this condition holds for the given request data.

        ``headers`` is a tuple of ``(name, value)`` byte pairs, ``query``
        maps names to value lists (``parse_qs`` shape), ``cookies`` maps
        names to values, and ``host`` is the request host without a port.
        """
        candidate = None
        if self.type == "header":
            wanted = self.key.encode("ascii")
            for name, value in headers:
                if name.lower() == wanted:
                    candidate = value.decode("latin-1")
                    break
            if candidate is None:
                return False
        elif self.type == "query":
            values = (query or {}).get(self.key)
            if not values:
                return False
            candidate = values[0]
        elif self.type == "cookie":
            candidate = (cookies or {}).get(self.key)
            if candidate is None:
                return False
        else:  # host
            candidate = host or ""
            if not candidate:
                return False
        if self._regex is None:
            return True
        return self._regex.fullmatch(candidate) is not None


def compile_has_condition(spec):
    """Validate one ``has``/``missing`` dict and compile it.

    ``spec`` is ``{"type": ..., "key": ..., "value": ...}``; ``key`` is
    required for ``header``/``query``/``cookie`` and forbidden for
    ``host``; ``value`` is optional. Unknown keys are rejected so a
    typo (``{"typo": ...}``) fails at startup instead of being ignored.
    """
    if not isinstance(spec, dict):
        raise TypeError("has/missing conditions must be dicts")
    unknown = set(spec) - {"type", "key", "value"}
    if unknown:
        raise ValueError(f"Unknown has/missing condition keys: {sorted(unknown)}")
    condition_type = spec.get("type")
    if condition_type not in _HAS_TYPES:
        raise ValueError(f"has/missing type must be one of {sorted(_HAS_TYPES)}: {condition_type!r}")
    key = spec.get("key")
    value = spec.get("value")
    if condition_type == "host":
        if key is not None:
            raise ValueError("has/missing condition of type 'host' must not have a key")
    else:
        if not isinstance(key, str):
            raise TypeError(f"has/missing condition of type {condition_type!r} requires a str key")
        pattern = _TOKEN_NAME if condition_type in ("header", "cookie") else _QUERY_KEY
        if not pattern.fullmatch(key):
            raise ValueError(f"Invalid has/missing key for {condition_type!r}: {key!r}")
        key = key.lower() if condition_type == "header" else key
    regex = None
    if value is not None:
        regex = _compile_has_value(condition_type, value)
    return HasCondition(condition_type, key, value, regex)


def _compile_conditions(specs, name):
    if isinstance(specs, dict):
        specs = (specs,)
    if not isinstance(specs, (list, tuple)):
        raise TypeError(f"{name} must be a list or tuple of condition dicts")
    return tuple(compile_has_condition(spec) for spec in specs)


def _parse_cookies(headers):
    cookies = {}
    for name, value in headers:
        if name.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            key, separator, val = part.strip().partition("=")
            if separator and key not in cookies:
                cookies[key] = val.strip()
    return cookies


def _request_host(request):
    host = request.state.get("client_host") if isinstance(request.state, dict) else None
    if not host:
        for name, value in request.headers:
            if name.lower() == b"host":
                host = value.decode("latin-1")
                break
    if not host:
        return ""
    host = host.strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end > 0 else ""
    return host.split(":", 1)[0].lower()


@dataclass(frozen=True)
class Scope:
    """A compiled middleware scope: path matcher + has/missing conditions.

    ``matcher`` is ``None`` for an unscoped (global) middleware. All
    ``has`` conditions must hold and no ``missing`` condition may hold
    for the scope to match. Matching uses the decoded routing path only.
    """

    matcher: Matcher | None
    has: tuple = ()
    missing: tuple = ()

    def matches(self, path, *, headers=(), query=None, cookies=None, host=""):
        if self.matcher is not None and self.matcher.matches(path) is None:
            return False
        if any(not condition.matches(headers=headers, query=query, cookies=cookies, host=host)
               for condition in self.has):
            return False
        return not any(condition.matches(headers=headers, query=query, cookies=cookies, host=host)
                       for condition in self.missing)

    def matches_request(self, request):
        """True when this scope matches ``request`` (a ``Request``)."""
        try:
            query = request.query
        except Exception:
            # An unparseable query string means no query condition can
            # hold (so a `missing` query condition does hold); header,
            # cookie and host conditions are unaffected.
            query = {}
        return self.matches(request.path, headers=request.headers, query=query,
                            cookies=_parse_cookies(request.headers),
                            host=_request_host(request))


def compile_scope(match=None, has=(), missing=()):
    """Compile a middleware scope; ``match=None`` means "every path"."""
    matcher = compile_matcher(match) if match is not None else None
    return Scope(matcher, _compile_conditions(has, "has"), _compile_conditions(missing, "missing"))


def _is_async_callable(value):
    return (inspect.iscoroutinefunction(value) or
            inspect.iscoroutinefunction(getattr(value, "__call__", None)))


class ScopedMiddleware:
    """A middleware callable wrapped with a compiled :class:`Scope`.

    Created by :func:`scoped_middleware`; :class:`pynext.app.App`
    unwraps it at startup and skips the middleware for requests whose
    scope does not match. Calling it directly runs the wrapped
    middleware unchanged (the scope check lives in the app, not here,
    so a scoped middleware used outside an app behaves globally).
    """

    def __init__(self, middleware, scope):
        self.middleware = middleware
        self.scope = scope

    async def __call__(self, request, call_next):
        return await self.middleware(request, call_next)

    def __repr__(self):
        return f"ScopedMiddleware({self.middleware!r}, scope={self.scope!r})"


def scoped_middleware(middleware=None, *, match=None, has=(), missing=()):
    """Scope a middleware to the requests its conditions match.

    Usable as a wrapper — ``scoped_middleware(auth, match="/api/*")`` —
    or as a decorator — ``@scoped_middleware(match="/api/*")``. The
    scope is compiled immediately, so an invalid matcher or condition
    fails here (at import/startup time) instead of at request time.
    An unscoped middleware keeps running on every request, exactly as
    before; a scoped one is skipped (the chain continues past it) when
    its scope does not match.
    """
    scope = compile_scope(match=match, has=has, missing=missing)

    def wrap(fn):
        if not _is_async_callable(fn):
            raise TypeError("middleware entries must be async callables")
        return ScopedMiddleware(fn, scope)

    return wrap(middleware) if middleware is not None else wrap


@dataclass(frozen=True)
class HeaderRule:
    """One compiled per-path header rule."""

    source: str
    matcher: Matcher
    headers: tuple = ()
    has: tuple = ()
    missing: tuple = ()

    def matches(self, path, *, headers=(), query=None, cookies=None, host=""):
        if self.matcher.matches(path) is None:
            return False
        if any(not condition.matches(headers=headers, query=query, cookies=cookies, host=host)
               for condition in self.has):
            return False
        return not any(condition.matches(headers=headers, query=query, cookies=cookies, host=host)
                       for condition in self.missing)

    def matches_request(self, request):
        """True when this rule applies to ``request`` (a ``Request``)."""
        try:
            query = request.query
        except Exception:
            query = {}
        return self.matches(request.path, headers=request.headers, query=query,
                            cookies=_parse_cookies(request.headers),
                            host=_request_host(request))


def _compile_rule_headers(headers, source):
    if not isinstance(headers, dict):
        raise TypeError(f"headers for {source!r} must be a dict of header name to value")
    if not headers:
        raise ValueError(f"headers for {source!r} must not be empty")
    compiled = []
    seen = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not _TOKEN_NAME.fullmatch(name):
            raise ValueError(f"Invalid header name {name!r} in rule for {source!r}")
        lowered = name.lower()
        if lowered in seen:
            raise ValueError(f"Duplicate header {name!r} in rule for {source!r}")
        seen.add(lowered)
        if lowered in _FRAMING_HEADERS:
            raise ValueError(
                f"Header {name!r} in rule for {source!r} is framing owned by the server")
        if isinstance(value, str):
            try:
                raw = value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"Header {name!r} in rule for {source!r} must be ASCII") from exc
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            raise TypeError(f"Header {name!r} in rule for {source!r} must be str or bytes")
        if len(raw) > MAX_HEADER_VALUE_LENGTH:
            raise ValueError(
                f"Header {name!r} in rule for {source!r} exceeds {MAX_HEADER_VALUE_LENGTH} bytes")
        if any(c < 32 or c == 127 for c in raw):
            raise ValueError(
                f"Header {name!r} in rule for {source!r} must not contain control characters")
        compiled.append((lowered.encode("ascii"), raw))
    return tuple(compiled)


def compile_header_rules(rules):
    """Validate and compile per-path header rules.

    Each rule is either a dict — ``{"source": "/api/*", "headers":
    {"Cache-Control": "no-store"}, "has": [...], "missing": [...]}``
    (``has``/``missing`` optional) — or a ``(source, headers)`` tuple.
    Rules apply in order; the first matching rule wins per header name.
    Returns a tuple of :class:`HeaderRule`. Raises :class:`ValueError`
    (or :class:`TypeError` for a wrong shape) on any invalid rule.
    """
    if rules is None:
        return ()
    if not isinstance(rules, (list, tuple)):
        raise TypeError("headers must be a list or tuple of rules")
    if len(rules) > MAX_HEADER_RULES:
        raise ValueError(f"at most {MAX_HEADER_RULES} header rules are allowed")
    compiled = []
    for rule in rules:
        if isinstance(rule, dict):
            unknown = set(rule) - {"source", "headers", "has", "missing"}
            if unknown:
                raise ValueError(f"Unknown header rule keys: {sorted(unknown)}")
            if "source" not in rule or "headers" not in rule:
                raise ValueError("header rules require 'source' and 'headers'")
            source, headers = rule["source"], rule["headers"]
            has = _compile_conditions(rule.get("has", ()), "has")
            missing = _compile_conditions(rule.get("missing", ()), "missing")
        elif isinstance(rule, (list, tuple)) and len(rule) == 2:
            source, headers = rule
            has, missing = (), ()
        else:
            raise TypeError(
                "header rules must be dicts or (source, headers) tuples")
        matcher = compile_matcher(source)
        compiled.append(HeaderRule(source, matcher, _compile_rule_headers(headers, source),
                                   has, missing))
    return tuple(compiled)


def compile_middleware_entries(entries):
    """Compile a middleware list into ``(callable, Scope | None)`` pairs.

    Plain async callables stay global (``Scope`` ``None``);
    :class:`ScopedMiddleware` entries keep their compiled scope.
    Raises :class:`TypeError` for a non-callable entry or a sync
    callable, and :class:`ValueError` for an invalid scope (the scope
    itself is already compiled by :func:`scoped_middleware`, so this is
    defense in depth for hand-built wrappers).
    """
    if entries is None:
        return ()
    if not isinstance(entries, (list, tuple)):
        raise TypeError("middleware must be a list or tuple")
    compiled = []
    for item in entries:
        if isinstance(item, ScopedMiddleware):
            fn, scope = item.middleware, item.scope
            if not isinstance(scope, Scope):
                raise TypeError("scoped middleware scope must be a Scope")
        else:
            fn, scope = item, None
        if not _is_async_callable(fn):
            raise TypeError("middleware entries must be async callables")
        compiled.append((fn, scope))
    return tuple(compiled)
