"""Opt-in authorization markers for application-managed identities."""


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
    if not isinstance(permission, str) or not permission or len(permission) > 128:
        raise ValueError("permission must be a nonempty string of at most 128 characters")

    def decorate(fn):
        fn.__pynext_auth__ = True
        fn.__pynext_permissions__ = (*getattr(fn, "__pynext_permissions__", ()), permission)
        return fn
    return decorate


def check_authorization(request, fn):
    """Return 401/403, or None when the handler's policy is satisfied."""
    if not getattr(fn, "__pynext_auth__", False):
        return None
    from .http import Response
    from .sessions import Session

    session = request.state.get("session")
    if not isinstance(session, Session) or not isinstance(session.get("user_id"), str) or not session["user_id"]:
        return Response("Unauthorized", status=401, headers=[(b"cache-control", b"private, no-store")])
    permissions = session.get("permissions", [])
    if (not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions) or
            not all(item in permissions for item in getattr(fn, "__pynext_permissions__", ()))):
        return Response("Forbidden", status=403, headers=[(b"cache-control", b"private, no-store")])
    return None
