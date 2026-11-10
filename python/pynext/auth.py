"""Opt-in authorization markers for application-managed identities.

Authorization is declared at registration time and enforced at exactly
one place per entry point (S-P1-33, FastAPI lesson 2): the dispatcher
(:meth:`pynext.app.App.dispatch`) and the WebSocket pre-flight
(:meth:`pynext.app.App._authorize_websocket`) both decide from the
policy snapshotted when the route was registered
(:func:`authorization_policy`), never from the handler's live
attributes — so a decorator lost to wrapping, or a handler replaced
after registration, cannot silently drop the check. The default is
deny: a route that declares permissions or scopes without a usable
identity is refused, and a 403 never says which permission or scope
was missing.
"""

from dataclasses import dataclass


def _validate_token_name(value, label):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{label} must be a nonempty string of at most 128 characters")
    return value


@dataclass(frozen=True)
class AuthorizationPolicy:
    """The authorization policy for one route handler, snapshotted at registration.

    ``requires_auth`` is True when the handler was marked with
    :func:`require_auth`, :func:`require_permission` or
    :func:`require_scope`. ``permissions`` and ``scopes`` are the
    required names, in declaration order; an unmarked handler has an
    empty policy and is public.
    """

    requires_auth: bool = False
    permissions: tuple = ()
    scopes: tuple = ()


def authorization_policy(fn):
    """Snapshot ``fn``'s authorization markers into an :class:`AuthorizationPolicy`.

    Called once when the route is registered. A handler that declares
    permissions or scopes without the auth flag is a configuration
    error and fails loudly here, at startup — it must never silently
    become public because a marker was set without its flag. Names are
    validated (nonempty strings of at most 128 characters) and must be
    given as a tuple/list, never a bare string (whose characters would
    otherwise be read as separate names).
    """
    permissions = getattr(fn, "__pynext_permissions__", ())
    scopes = getattr(fn, "__pynext_scopes__", ())
    requires_auth = bool(getattr(fn, "__pynext_auth__", False))
    if (permissions or scopes) and not requires_auth:
        raise ValueError(
            f"{getattr(fn, '__qualname__', fn)!r} declares permissions/scopes "
            "without @require_auth/@require_permission/@require_scope; "
            "refusing to register it as public")
    for label, names in (("permission", permissions), ("scope", scopes)):
        if isinstance(names, str) or not isinstance(names, (tuple, list)):
            raise ValueError(f"{label} names on {getattr(fn, '__qualname__', fn)!r} "
                             "must be a tuple/list of strings")
        for name in names:
            _validate_token_name(name, label)
    return AuthorizationPolicy(requires_auth=requires_auth,
                               permissions=tuple(permissions), scopes=tuple(scopes))


def require_auth(fn):
    """Require a nonempty string ``user_id`` in the server-side session.

    Applies to HTTP handlers and WebSocket handlers alike: a WebSocket
    upgrade is rejected before the handshake when the session has no
    ``user_id``.
    """
    fn.__pynext_auth__ = True
    return fn


def require_permission(permission):
    """Require a permission from the server-side session's permissions list."""
    _validate_token_name(permission, "permission")

    def decorate(fn):
        fn.__pynext_auth__ = True
        fn.__pynext_permissions__ = (*getattr(fn, "__pynext_permissions__", ()), permission)
        return fn
    return decorate


def require_scope(*scopes):
    """Require one or more scopes from the server-side session's scopes list.

    The scopes are declared on the handler at registration time and
    enforced by the dispatcher (and the WebSocket pre-flight) against
    ``session["scopes"]`` — the handler itself never checks them, so a
    forgotten check cannot turn a bare token into full access (S-P1-33).
    Every listed scope must be present; a session without a ``scopes``
    list, or with a malformed one, is denied. Declaring a scope also
    implies :func:`require_auth`.
    """
    if not scopes:
        raise ValueError("require_scope needs at least one scope")
    for scope in scopes:
        _validate_token_name(scope, "scope")

    def decorate(fn):
        fn.__pynext_auth__ = True
        fn.__pynext_scopes__ = (*getattr(fn, "__pynext_scopes__", ()), *scopes)
        return fn
    return decorate


def check_authorization(request, fn, *, policy=None):
    """Return 401/403, or None when the handler's policy is satisfied.

    Session fixation (H-6) is not decided here: this runs before the
    handler, so it cannot see the login transition — there is no
    rotation to check yet at this point. The guarantee lives in
    :class:`pynext.sessions.SessionMiddleware`, which rotates the
    session id whenever a request authenticates a previously anonymous
    (or different-user) session, even when the login handler forgot
    ``rotate_on_login()`` — so by the time any later request reaches
    this check with a ``user_id``, its id is one issued after
    authentication, never one fixed before login.

    The decision is made from ``policy`` — the snapshot taken at
    registration (see :func:`authorization_policy`) — and defaults to
    deriving that snapshot from ``fn`` when no policy is given, so
    callers that predate the snapshot keep working. The default is
    deny: an unmarked handler is public, but any marked handler without
    a usable identity (no session, no ``user_id``), with a malformed
    permissions/scopes list, or missing even one required permission or
    scope is refused. The 403 body is a bare ``"Forbidden"``: it never
    says which permission or scope was missing.
    """
    if policy is None:
        policy = authorization_policy(fn)
    if not policy.requires_auth:
        return None
    from .http import Response
    from .sessions import Session

    session = request.state.get("session")
    if not isinstance(session, Session) or not isinstance(session.get("user_id"), str) or not session["user_id"]:
        return Response("Unauthorized", status=401, headers=[(b"cache-control", b"private, no-store")])
    permissions = session.get("permissions", [])
    if (not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions) or
            not all(item in permissions for item in policy.permissions)):
        return Response("Forbidden", status=403, headers=[(b"cache-control", b"private, no-store")])
    scopes = session.get("scopes", [])
    if (not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes) or
            not all(item in scopes for item in policy.scopes)):
        return Response("Forbidden", status=403, headers=[(b"cache-control", b"private, no-store")])
    return None
