"""Server-side UI tree construction; no Python code is sent to browsers."""
import math

def _node(child):
    if isinstance(child, dict) and "kind" in child:
        return child
    if isinstance(child, (str, int, float)) and not isinstance(child, bool):
        return {"kind": "text", "value": str(child)}
    raise TypeError(f"Unsupported child type: {type(child).__name__}")


def h(tag, attrs=None, *children):
    return {"kind": "element", "tag": tag, "attrs": attrs or {},
            "children": [_node(child) for child in children if child is not None]}


def fragment(*children):
    return {"kind": "fragment", "children": [_node(child) for child in children if child is not None]}


def page(*, render="ssr", revalidate=None, stale=None):
    if render not in {"ssr", "ssg", "isr"}:
        raise ValueError("render must be 'ssr', 'ssg', or 'isr'")
    if render == "isr":
        if stale is None:
            stale = 60.0
        for name, value, allow_zero in (("revalidate", revalidate, False), ("stale", stale, True)):
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    value < 0 or (not allow_zero and value == 0) or value > 604_800 or not math.isfinite(value)):
                raise ValueError(f"{name} must be a finite {'non-negative' if allow_zero else 'positive'} number of seconds, at most 604800")
        policy = {"revalidate": float(revalidate), "stale": float(stale)}
    else:
        if revalidate is not None or stale is not None:
            raise ValueError("revalidate and stale are only valid for ISR pages")
        policy = None
    def decorate(fn):
        fn.__pynext_page__ = True
        fn.__pynext_render__ = render
        fn.__pynext_cache_policy__ = policy
        return fn
    return decorate


def layout(fn):
    fn.__pynext_layout__ = True
    return fn
