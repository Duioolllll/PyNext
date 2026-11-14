import inspect
import re
from urllib.parse import quote


def _segment_param(segment):
    """Parse one route segment.

    Returns (name, kind, prefix, suffix) where kind is "dynamic",
    "catch_all" or "optional", or None for a static segment. A dynamic
    parameter may carry a static prefix and/or suffix, as in
    ``v[version]`` or ``[name].json`` (Next.js PARAMETER_PATTERN in
    ``get-dynamic-param.ts``); catch-all parameters may not. This is
    the single segment grammar shared by build-time parsing
    (``pynext.build._segment_param`` delegates here) and public
    ``url_for()``.
    """
    if "[" not in segment:
        if "]" in segment:
            raise ValueError(f"Invalid route segment: {segment}")
        return None
    match = re.fullmatch(r"([^[]*)\[(\[)?(\.\.\.)?([A-Za-z0-9_-]+)\](\])?(.*)", segment)
    if match is None:
        raise ValueError(f"Invalid route segment: {segment}")
    prefix, double_open, dots, name, double_close, suffix = match.groups()
    if bool(double_open) != bool(double_close):
        raise ValueError(f"Invalid route segment: {segment}")
    if double_open and not dots:
        raise ValueError(f"Optional catch-all must use [[...name]]: {segment}")
    if "]" in prefix or "[" in suffix or "]" in suffix:
        raise ValueError(f"Invalid route segment: {segment}")
    kind = "optional" if double_open else ("catch_all" if dots else "dynamic")
    if kind != "dynamic" and (prefix or suffix):
        raise ValueError(f"Catch-all parameters cannot have a prefix or suffix: {segment}")
    return name, kind, prefix, suffix


def _build_path(pattern, params, *, encode):
    """Fill ``pattern`` with ``params`` and return the concrete path.

    With ``encode=True`` every value is percent-encoded per segment
    (``quote(value, safe='')``), so the result is safe to put in a URL.
    With ``encode=False`` values are inserted verbatim; that form is
    only for build-internal decoded paths (SSG manifest keys), which
    the request path decoder expects to see unencoded.
    """
    if not isinstance(pattern, str) or not pattern.startswith("/"):
        raise ValueError(f"Route pattern must start with '/': {pattern!r}")
    if pattern != "/" and pattern.endswith("/"):
        raise ValueError(f"Route pattern must not end with '/': {pattern!r}")
    segments = pattern.strip("/").split("/") if pattern != "/" else []
    keys = {}
    for index, segment in enumerate(segments):
        # A1-L1: validate the pattern the way the router does, instead of
        # silently producing a URL no route can ever match.
        if segment in ("", ".", ".."):
            raise ValueError(
                f"Route pattern must not contain empty, '.' or '..' segments: {pattern!r}")
        parsed = _segment_param(segment)
        if parsed:
            name, kind, _prefix, _suffix = parsed
            if kind in ("catch_all", "optional") and index != len(segments) - 1:
                raise ValueError(
                    f"Catch-all parameter {name!r} must be the last segment: {pattern!r}")
            if name in keys:
                raise ValueError(f"Duplicate parameter {name!r} in route pattern {pattern!r}")
            keys[name] = kind
    if not isinstance(params, dict) or set(params) != set(keys):
        raise ValueError(f"Parameters for {pattern} must contain exactly {sorted(keys)}")
    output = []
    for segment in segments:
        parsed = _segment_param(segment)
        if not parsed:
            output.append(segment)
            continue
        name, kind, prefix, suffix = parsed
        value = params[name]
        if kind == "catch_all":
            if not isinstance(value, list) or not value:
                raise ValueError(f"Catch-all parameter {name} must be a nonempty list")
            values = value
        elif kind == "optional":
            if not isinstance(value, list):
                raise ValueError(f"Optional catch-all parameter {name} must be a list")
            values = value
        else:
            values = [value]
        for part in values:
            if (not isinstance(part, str) or not part or part in {".", ".."} or
                    any(c in "/\\?#" or ord(c) < 32 or ord(c) == 127 for c in part)):
                raise ValueError(f"Invalid parameter value in {pattern}: {part!r}")
            try:
                part.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"Invalid Unicode parameter value in {pattern}") from exc
            output.append(prefix + (quote(part, safe="") if encode else part) + suffix)
    return "/" + "/".join(output)


def url_for(pattern, params, *, router=None, base_path=""):
    """Build the URL for ``pattern`` from ``params`` (reverse routing).

    This is the public form of the build-internal ``_materialize()``:
    the parameter names must match the pattern exactly, catch-all
    parameters take a list of segments (an optional catch-all takes an
    empty list for "no segments"), and every value is percent-encoded
    per segment, so a value can never smuggle a ``/``, ``?`` or ``#``
    into the generated URL. An optional catch-all with no values
    produces its parent path (``url_for("/docs/[[...path]]", {"path":
    []}) == "/docs"``). A dynamic parameter with no value, an unknown
    parameter, or a value containing ``/``, ``?``, ``#`` or a control
    character raises ``ValueError``.

    When ``router`` (a ``pynext._native.Router`` holding ``pattern``)
    is given, the generated URL is verified to match the pattern it
    was built from, mirroring the SSG self-check in ``build.py``.
    ``base_path`` is re-applied at the end, so links stay correct for
    apps served under a prefix.
    """
    path = _build_path(pattern, params, encode=True)
    if router is not None:
        match = router.matches(path)
        if match is None or match[0] != pattern:
            raise ValueError(f"Generated path {path} does not match {pattern}")
    return add_base_path(path, base_path)


def route(method):
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError(f"Unsupported HTTP method: {method}")
    def decorate(fn):
        fn.__pynext_method__ = method
        return fn
    return decorate


def state_changing_get(fn):
    """Mark a GET handler as knowingly state-changing (S-P1-23 acknowledgement).

    GET, HEAD and OPTIONS are exempt from CSRF checks, so a handler that
    changes state on GET is unprotected by construction. This marker
    only silences the development startup warning from
    :func:`pynext.app.check_get_idempotency` — it adds no CSRF
    protection whatsoever. Prefer moving the mutation to POST/PUT/PATCH/
    DELETE; use this only when the GET mutation is deliberate and the
    exposure has been reviewed.
    """
    fn.__pynext_state_changing_get__ = True
    return fn


def websocket(fn=None, *, protocols=()):
    """Mark an async function as the WebSocket handler for a route.py module.

    The handler receives a ``WebSocket`` (see ``pynext.http``) and must call
    ``await websocket.accept()`` before sending or receiving messages. A
    module may export at most one WebSocket handler; it may coexist with HTTP
    handlers in the same ``route.py``. Before the handshake, the upgrade
    runs the global middleware chain (including ``SessionMiddleware``) and
    the handler's ``@require_auth``/``@require_permission`` policy, so a
    denied upgrade never reaches this handler.
    """
    if (not isinstance(protocols, (list, tuple)) or len(protocols) > 32 or
            any(not isinstance(protocol, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", protocol)
                for protocol in protocols) or len(set(protocols)) != len(protocols)):
        raise ValueError("WebSocket protocols must be unique HTTP tokens")

    def decorate(handler):
        if not inspect.iscoroutinefunction(handler):
            raise ValueError("WebSocket handlers must be async functions")
        handler.__pynext_websocket__ = True
        handler.__pynext_websocket_protocols__ = tuple(protocols)
        return handler
    return decorate(fn) if fn is not None else decorate


#: Trailing-slash policies (Next.js ``trailingSlash`` equivalent).
#:
#: - ``"redirect"`` (default): a non-root path with a trailing slash
#:   answers 308 to the same path without it — the behaviour PyNext
#:   always had, now named instead of hardcoded.
#: - ``"ignore"``: no redirect; the trailing slash is stripped before
#:   matching, so ``/about/`` serves ``/about``.
TRAILING_SLASH_MODES = ("redirect", "ignore")


def normalize_base_path(value):
    """Validate and normalize a base path (Next.js ``basePath`` equivalent).

    Returns ``""`` for ``""`` and ``"/"`` (no prefix), otherwise the
    value unchanged. A base path must start with ``"/"``, must not end
    with ``"/"``, and must not contain empty, ``"."`` or ``".."``
    segments or characters that cannot appear in a literal URL prefix
    (``?``, ``#``, ``\\``, ``[``, ``]`` or control characters). The
    wrong type raises :class:`TypeError`; any other violation raises
    :class:`ValueError`, so a bad deployment fails at startup.
    """
    if not isinstance(value, str):
        raise TypeError("base_path must be a string")
    if value in ("", "/"):
        return ""
    if not value.startswith("/"):
        raise ValueError("base_path must start with '/'")
    if value.endswith("/"):
        raise ValueError("base_path must not end with '/'")
    if any(segment in ("", ".", "..") for segment in value.split("/")[1:]):
        raise ValueError("base_path must not contain empty, '.' or '..' segments")
    if any(ord(c) < 32 or ord(c) == 127 or c in "?#\\[]" for c in value):
        raise ValueError("base_path must not contain '?', '#', '\\', '[' or ']'")
    return value


def validate_trailing_slash(value):
    """Validate a trailing-slash policy, returning it unchanged."""
    if not isinstance(value, str):
        raise TypeError("trailing_slash must be a string")
    if value not in TRAILING_SLASH_MODES:
        raise ValueError("trailing_slash must be 'redirect' or 'ignore'")
    return value


def preprocess_pathname(path, *, base_path="", trailing_slash="redirect"):
    """Preprocess a decoded request path before matching.

    The ``getNextPathnameInfo()`` equivalent (Next.js
    ``shared/lib/router/utils/get-next-pathname-info.ts``), minus the
    i18n locale step, which PyNext has no roadmap for: strip the
    ``base_path`` prefix, then apply the trailing-slash policy.

    Returns the app-relative path, or ``None`` when the path lies
    outside ``base_path`` (the caller answers 404). ``"/app"`` maps to
    ``"/"``; a path that merely starts with the prefix text
    (``"/application"`` for ``base_path="/app"``) does not match — the
    prefix must end on a segment boundary.
    """
    base_path = normalize_base_path(base_path)
    trailing_slash = validate_trailing_slash(trailing_slash)
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must start with '/'")
    if base_path:
        if path == base_path:
            path = "/"
        elif path.startswith(base_path + "/"):
            path = path[len(base_path):]
        else:
            return None
    if trailing_slash == "ignore" and path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path


def add_base_path(path, base_path=""):
    """Re-apply a base path to an app-relative path (URL generation).

    The inverse of the prefix strip in :func:`preprocess_pathname`:
    ``add_base_path("/about", "/app")`` is ``"/app/about"`` and
    ``add_base_path("/", "/app")`` is ``"/app"``. With no base path the
    path is returned unchanged.
    """
    base_path = normalize_base_path(base_path)
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must start with '/'")
    if not base_path:
        return path
    return base_path if path == "/" else base_path + path


