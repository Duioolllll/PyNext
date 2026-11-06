def route(method):
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError(f"Unsupported HTTP method: {method}")
    def decorate(fn):
        fn.__pynext_method__ = method
        return fn
    return decorate
