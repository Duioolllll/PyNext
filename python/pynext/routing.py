import inspect


def route(method):
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError(f"Unsupported HTTP method: {method}")
    def decorate(fn):
        fn.__pynext_method__ = method
        return fn
    return decorate


def websocket(fn):
    """Mark an async function as the WebSocket handler for a route.py module.

    The handler receives a ``WebSocket`` (see ``pynext.http``) and must call
    ``await websocket.accept()`` before sending or receiving messages. A
    module may export at most one WebSocket handler; it may coexist with HTTP
    handlers in the same ``route.py``.
    """
    if not inspect.iscoroutinefunction(fn):
        raise ValueError("WebSocket handlers must be async functions")
    fn.__pynext_websocket__ = True
    return fn
