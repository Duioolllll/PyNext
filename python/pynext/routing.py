import inspect
import re


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


def url_for(pattern, params, *, base_path=""):
    """Build the URL for a route pattern (reverse routing).

    Materializes ``pattern`` with ``params`` (see
    ``pynext.build._materialize`` for the parameter rules) and then
    re-applies ``base_path``, so links stay correct for apps served
    under a prefix. With no base path this is plain materialization.
    """
    from .build import _materialize

    return add_base_path(_materialize(pattern, params), base_path)
