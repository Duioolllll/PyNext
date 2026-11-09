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
