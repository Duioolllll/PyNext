"""Server-side UI tree construction; no Python code is sent to browsers."""
import math
import json
import re

_ISLAND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_MAX_PROPS_BYTES = 16_384
_MODEL_PROP_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_MODEL_HTML_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{0,127}\Z")
_MAX_MODEL_BYTES = 16_384
_MAX_MODEL_NODES = 512

def _node(child):
    if isinstance(child, dict) and "kind" in child:
        return child
    if isinstance(child, (str, int, float)) and not isinstance(child, bool):
        return {"kind": "text", "value": str(child)}
    raise TypeError(f"Unsupported child type: {type(child).__name__}")


def h(tag, attrs=None, *children):
    if not isinstance(tag, str):
        raise TypeError(f"Element tag must be a string, got {type(tag).__name__}")
    if attrs is not None and not isinstance(attrs, dict):
        raise TypeError(f"Element attributes must be a dict, got {type(attrs).__name__}")
    return {"kind": "element", "tag": tag, "attrs": attrs or {},
            "children": [_node(child) for child in children if child is not None]}


def fragment(*children):
    return {"kind": "fragment", "children": [_node(child) for child in children if child is not None]}


def _validate_props(props):
    if not isinstance(props, dict):
        raise TypeError("Island props must be a JSON object")
    active = set()

    def visit(value, depth):
        if depth > 32:
            raise ValueError("Island props exceed maximum depth")
        if value is None or isinstance(value, (str, bool)):
            return
        if isinstance(value, int):
            if abs(value) > 9_007_199_254_740_991:
                raise ValueError("Island integer props must be exactly representable in JavaScript")
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Island numeric props must be finite")
            return
        if not isinstance(value, (list, dict)):
            raise TypeError("Island props must contain only JSON values")
        identity = id(value)
        if identity in active:
            raise ValueError("Island props cannot contain cycles")
        active.add(identity)
        try:
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise TypeError("Island prop keys must be strings")
                    visit(child, depth + 1)
            else:
                for child in value:
                    visit(child, depth + 1)
        finally:
            active.remove(identity)

    visit(props, 0)
    try:
        encoded = json.dumps(props, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Island props contain invalid Unicode") from exc
    if len(encoded) > _MAX_PROPS_BYTES:
        raise ValueError("Island props exceed 16384 bytes")


def model_prop(name):
    """A string-valued prop placeholder inside a shared island model."""
    if not isinstance(name, str) or not _MODEL_PROP_NAME.fullmatch(name):
        raise ValueError("Invalid shared model prop name")
    return {"kind": "prop", "name": name}


def _resolve_shared_model(model, props):
    try:
        encoded = json.dumps(model, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("Shared model must be acyclic JSON with valid Unicode") from exc
    if len(encoded) > _MAX_MODEL_BYTES:
        raise ValueError("Shared model exceeds 16384 bytes")
    count = 0

    def resolve(node, depth):
        nonlocal count
        count += 1
        if count > _MAX_MODEL_NODES or depth > 32 or not isinstance(node, dict):
            raise ValueError("Shared model exceeds its node or depth limit")
        kind = node.get("kind")
        if kind == "prop" and set(node) == {"kind", "name"}:
            name = node["name"]
            if (not isinstance(name, str) or not _MODEL_PROP_NAME.fullmatch(name) or
                    name not in props or not isinstance(props[name], str)):
                raise ValueError("Shared model props must refer to present string values")
            return {"kind": "text", "value": props[name]}
        if kind == "text" and set(node) == {"kind", "value"} and isinstance(node["value"], str):
            return {"kind": "text", "value": node["value"]}
        if kind == "fragment" and set(node) == {"kind", "children"} and isinstance(node["children"], list):
            return {"kind": "fragment", "children": [resolve(child, depth + 1) for child in node["children"]]}
        if kind == "element" and set(node) == {"kind", "tag", "attrs", "children"}:
            tag, attrs, children = node["tag"], node["attrs"], node["children"]
            if (not isinstance(tag, str) or not _MODEL_HTML_NAME.fullmatch(tag) or
                    tag.lower() in {"script", "style", "iframe", "object", "embed"} or
                    not isinstance(attrs, dict) or not isinstance(children, list)):
                raise ValueError("Invalid shared model element")
            if any(not isinstance(key, str) or not _MODEL_HTML_NAME.fullmatch(key) or
                   key.lower().startswith(("on", "data-pynext-")) or key.lower() in {"style", "srcdoc"} or
                   not (value is None or isinstance(value, (str, bool))) for key, value in attrs.items()):
                raise ValueError("Shared model attributes must be strings, booleans or None")
            for key, value in attrs.items():
                if key.lower() in {"href", "src", "action", "formaction", "xlink:href"} and isinstance(value, str):
                    clean = "".join(c for c in value if ord(c) > 32 and ord(c) != 127).lower()
                    if clean.startswith(("javascript:", "vbscript:", "data:")):
                        raise ValueError("Unsafe URL in shared model")
            return {"kind": "element", "tag": tag, "attrs": dict(attrs),
                    "children": [resolve(child, depth + 1) for child in children]}
        raise ValueError("Unsupported shared model node")

    return resolve(model, 0)


def island(name, *, props=None, fallback=None, hydrate=False, strategy="load", model=None):
    if not isinstance(name, str) or not _ISLAND_NAME.fullmatch(name):
        raise ValueError("Invalid island name")
    if not isinstance(hydrate, bool):
        raise TypeError("hydrate must be a boolean")
    if model is not None and (not hydrate or fallback is not None):
        raise ValueError("A shared model requires hydrate=True and cannot have a separate fallback")
    if hydrate and fallback is None and model is None:
        raise ValueError("A hydrated island requires server-rendered fallback content")
    if not isinstance(strategy, str) or strategy not in {"load", "visible", "idle", "interaction"}:
        raise ValueError("Island strategy must be 'load', 'visible', 'idle', or 'interaction'")
    if strategy == "interaction" and not hydrate:
        raise ValueError("Interaction strategy requires a hydrated island")
    props = {} if props is None else props
    _validate_props(props)
    result = {"kind": "island", "name": name, "props": props,
              "hydrate": hydrate, "strategy": strategy,
              "children": [] if fallback is None else [_node(fallback)]}
    if model is not None:
        result["model"] = model
        result["children"] = [_resolve_shared_model(model, props)]
    return result


def bind_islands(tree, manifest, *, include_runtime=True, dev_id=None):
    from .client import PREFIX

    def bind(node, inside_island=False, depth=0):
        if depth > 256 or not isinstance(node, dict):
            raise ValueError("Invalid or excessively deep render tree")
        kind = node.get("kind")
        if kind == "client_runtime":
            raise ValueError("Client runtime nodes are framework-internal")
        if kind == "island":
            if inside_island:
                raise ValueError("Nested islands are not supported")
            name = node.get("name")
            if not isinstance(name, str) or not _ISLAND_NAME.fullmatch(name) or name not in manifest["bundles"]:
                raise ValueError(f"Unknown island: {name!r}")
            props = node.get("props")
            _validate_props(props)
            children = node.get("children")
            if not isinstance(children, list):
                raise ValueError("Island children must be an array")
            hydrate = node.get("hydrate", False)
            if not isinstance(hydrate, bool):
                raise TypeError("hydrate must be a boolean")
            model = node.get("model")
            if model is not None:
                if not hydrate:
                    raise ValueError("A shared model requires hydrate=True")
                children = [_resolve_shared_model(model, props)]
            if hydrate and not children:
                raise ValueError("A hydrated island requires server-rendered fallback content")
            if hydrate and not manifest.get("hydration", {}).get(name, False):
                raise ValueError(f"Hydrated island {name!r} must export render and hydrate")
            strategy = node.get("strategy", "load")
            if not isinstance(strategy, str) or strategy not in {"load", "visible", "idle", "interaction"}:
                raise ValueError("Island strategy must be 'load', 'visible', 'idle', or 'interaction'")
            if strategy == "interaction" and not hydrate:
                raise ValueError("Interaction strategy requires a hydrated island")
            bound = [bind(child, True, depth + 1)[0] for child in children]
            result = {"kind": "island", "name": name, "props": props,
                      "module": PREFIX + manifest["bundles"][name] + ".js",
                      "hydrate": hydrate, "strategy": strategy, "children": bound}
            if model is not None:
                result["model"] = {"version": 1, "tree": model}
            return result, True
        result = dict(node)
        found = False
        if kind in {"element", "fragment"}:
            children = node.get("children")
            if not isinstance(children, list):
                raise ValueError("Node children must be an array")
            result["children"] = []
            for child in children:
                bound, child_found = bind(child, inside_island, depth + 1)
                result["children"].append(bound)
                found = found or child_found
        return result, found

    bound, found = bind(tree)
    if not include_runtime:
        return bound
    runtime = {"kind": "client_runtime", "src": PREFIX + manifest["runtime"] + ".js"}
    if dev_id is not None:
        runtime["dev_id"] = dev_id
    if bound.get("kind") == "element" and str(bound.get("tag", "")).lower() == "html":
        bodies = [child for child in bound["children"] if child.get("kind") == "element" and str(child.get("tag", "")).lower() == "body"]
        if len(bodies) != 1:
            if found:
                raise ValueError("An HTML document with islands must contain exactly one body")
            return {"kind": "fragment", "children": [bound, runtime]}
        bodies[0]["children"].append(runtime)
        return bound
    return {"kind": "fragment", "children": [bound, runtime]}


def _isr_policy(revalidate, stale):
    for name, value, allow_zero in (("revalidate", revalidate, False), ("stale", stale, True)):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                value < 0 or (not allow_zero and value == 0) or value > 604_800 or not math.isfinite(value)):
            raise ValueError(f"{name} must be a finite {'non-negative' if allow_zero else 'positive'} number of seconds, at most 604800")
    return {"revalidate": float(revalidate), "stale": float(stale)}


_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_\-./]{0,255}\Z")
_MAX_ISR_TAGS = 64
_MAX_TAG_EXPIRE = 604_800


def _validate_tag(tag):
    """Validate one ISR cache tag.

    Tags name the data a page depends on (for example ``"post:42"``), so
    one :func:`revalidate_tag` call can invalidate every page carrying
    the tag. A tag is a non-empty string of at most 256 characters from
    ``[A-Za-z0-9:_-./]`` starting with an alphanumeric character.
    """
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ValueError(
            f"Invalid ISR tag {tag!r}: tags must be 1-256 characters from [A-Za-z0-9:_-./]"
        )
    return tag


def _validate_tags(tags):
    """Validate the ``tags`` argument of :func:`page` and return a sorted tuple."""
    if tags is None:
        return ()
    if isinstance(tags, str) or not isinstance(tags, (tuple, list)) or len(tags) > _MAX_ISR_TAGS:
        raise ValueError(f"tags must be a list or tuple of at most {_MAX_ISR_TAGS} ISR tags")
    validated = [_validate_tag(tag) for tag in tags]
    if len(set(validated)) != len(validated):
        raise ValueError("Duplicate ISR tag")
    return tuple(sorted(validated))


def _validate_tag_expire(expire):
    """Validate the ``expire`` argument of ``revalidate_tag``.

    ``None`` means the tag expires immediately; otherwise ``expire`` is
    the number of seconds entries stay merely stale before they expire.
    """
    if expire is None:
        return None
    if (isinstance(expire, bool) or not isinstance(expire, (int, float)) or
            expire < 0 or expire > _MAX_TAG_EXPIRE or not math.isfinite(expire)):
        raise ValueError("expire must be None or a finite number of seconds from 0 to 604800")
    return float(expire)


def _isr_query_keys(keys):
    if not isinstance(keys, (tuple, list)) or len(keys) > 16:
        raise ValueError("query_keys must be a list or tuple of at most 16 names")
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key)
           for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Invalid or duplicate ISR query key")
    return sorted(keys)


def page(*, render="ssr", revalidate=None, stale=None, query_keys=(), tags=()):
    if render not in {"ssr", "ssg", "isr"}:
        raise ValueError("render must be 'ssr', 'ssg', or 'isr'")
    if render == "isr":
        policy = _isr_policy(revalidate, 60.0 if stale is None else stale)
        query_keys = _isr_query_keys(query_keys)
        tags = _validate_tags(tags)
    else:
        if revalidate is not None or stale is not None or query_keys or tags:
            raise ValueError("revalidate, stale, query_keys, and tags are only valid for ISR pages")
        policy = None
        tags = ()
    def decorate(fn):
        fn.__pynext_page__ = True
        fn.__pynext_render__ = render
        fn.__pynext_cache_policy__ = policy
        fn.__pynext_cache_query__ = query_keys
        fn.__pynext_cache_tags__ = tags
        return fn
    return decorate


def layout(fn):
    fn.__pynext_layout__ = True
    return fn
