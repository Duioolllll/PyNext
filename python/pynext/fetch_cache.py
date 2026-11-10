"""Fetch / data cache: cache-key rules and the cacheability decision table.

This module ports two pieces of Next.js fetch caching, and nothing else:

* the key generation of ``IncrementalCache.generateCacheKey`` /
  ``generateSimpleCacheKey``
  (``packages/next/src/server/lib/incremental-cache/index.ts:373-499``,
  HEAD ``0cf37690``), and
* the policy decision table of ``patch-fetch.ts`` (``555-800`` in the same
  checkout).

It is pure policy: no network I/O, no storage. A later fetch wrapper can
feed the key into the ISR shared backend under the ``\\0fetch:``
namespace (see :func:`fetch_cache_store_key`) and use
:func:`decide_fetch_cache` to decide whether a response may be stored at
all. What this module deliberately does **not** port is listed in the
analysis report §4: the ``fetchCache`` page-export modes are not
PyNext page configuration (only their *decision logic* is ported here),
``prefetch()`` / staged rendering is PPR-only, and tag validation
belongs to tag-based invalidation (REPORT §2.1 C#2), not to this table.

Key rule (Next.js ``generateCacheKey``)
---------------------------------------
The key is the SHA-256 hex digest of the JSON array::

    ["v4", fetch_cache_key_prefix, url, method, body_type, headers,
     mode, redirect, credentials, referrer, referrer_policy, integrity,
     cache, body_chunks]

``headers`` is the request headers with the excluded set removed, and
``body_chunks`` carries the body with a type tag per chunk so different
shapes cannot collide. Next.js ``hashString`` is SHA-256 hex as well
(``incremental-cache/index.ts:145-155``), so PyNext keys agree with
Next.js up to the documented deviations below; SHA-256 is stable
across processes, which matters once the key addresses a *shared*
backend.

Two deliberate deviations from the Next.js source, both documented here
because the track report (``tracks/c-caching.md`` P1-1) states them
differently:

* **Excluded headers are ``traceparent`` / ``tracestate`` only**
  (``packages/next/src/lib/constants.ts:25-28``), not ``Authorization`` /
  ``Cookie``. The track report's "헤더 제외 목록(Authorization, Cookie)"
  does not match the source. Keeping ``Authorization`` / ``Cookie`` in
  the key is load-bearing: an explicit ``force-cache`` overrides
  auto-no-cache, so two users' authorised fetches must not share one
  entry. Authorised fetches without an explicit cache setting never
  reach the key at all — auto-no-cache (step 5 of the table) resolves
  them to ``revalidate=0`` first.
* **Header order is normalised**: headers are lower-cased and sorted by
  name before serialisation. Next.js serialises in ``Headers`` iteration
  order; sorting makes the same logical request produce the same key
  regardless of how the caller ordered its headers. Duplicate names are
  combined (``", "``-joined, ``"; "`` for ``cookie``) so different
  values cannot collapse to one key.

Body tagging (Next.js body handling, same order of cases):

* ``str`` → ``["str:<body>"]``, ``body_type`` ``"text/plain;charset=UTF-8"``.
* ``bytes`` / ``bytearray`` / ``memoryview`` → ``["bytes:<hex>"]``.
* mapping (form fields) → one ``key:<name>`` chunk per field, then
  ``str:<value>`` for text values, ``bytes:<hex>`` for binary values,
  and ``"file", <name>, <type>, "bytes:<hex>"`` for a file value given
  as a ``(filename, content_type, data)`` tuple. ``body_type`` is
  ``"application/x-www-form-urlencoded;charset=UTF-8"``, or
  ``"multipart/form-data; boundary="`` when any value is a file — the
  same split Next.js makes between ``URLSearchParams`` and ``FormData``.
  Fields are serialised in sorted-name order: Next.js preserves
  ``FormData`` insertion order, PyNext sorts a mapping for
  determinism. The per-field tags are what keep ``["a", "b"]`` from
  colliding with ``["a,b"]``.
* anything else raises :class:`TypeError` (Next.js throws for
  unsupported body types).

Decision table (``patch-fetch.ts`` 555-800, in order)
-----------------------------------------------------
``revalidate`` below is ``None`` for "never revalidate" (Next.js
``revalidate: false`` / ``INFINITE_CACHE = 0xfffffffe``), ``0`` for
"do not cache", or a positive number of seconds. ``route_revalidate``
is the containing route's revalidate value (``None`` = unset).

1. **Conflict**: ``cache="force-cache"`` + ``revalidate=0``, or
   ``cache="no-store"`` + ``revalidate > 0`` or ``revalidate=False``
   → warn ``Specified "cache: ..." and "revalidate: ...", only one
   should be specified.`` and drop *both* options.
2. **force-cache default**: ``cache="force-cache"`` with no revalidate
   → ``revalidate=False`` (infinite).
3. **Explicit opt-out**: ``cache`` in ``{"no-cache", "no-store"}``, or
   page ``fetch_cache_mode`` in ``{"force-no-store", "only-no-store"}``,
   or ``force_dynamic`` with no cache setting at all → ``revalidate=0``.
4. **auto-no-cache**: (a) an ``Authorization`` / ``Cookie`` header is
   present or the method is not ``GET``/``HEAD``, *and* the route
   revalidate is ``0``; or (b) there is no explicit cache setting at
   all (no page mode, ``cache`` unset or ``"default"``, revalidate
   unset) and this is not a build-time prerender → ``revalidate=0``.
   Build-time prerendering is exempt from (b) so fetch results can
   still be shared between build workers.
5. **Page ``fetch_cache_mode``** (``export const fetchCache`` logic;
   PyNext has no page-level export yet, so this is a parameter for
   now): ``force-no-store`` → reason only; ``only-no-store`` raises
   :class:`ValueError` when the fetch is ``force-cache`` or its
   resolved revalidate is positive or infinite; ``only-cache`` raises
   when the fetch is ``no-store``; ``force-cache`` with no revalidate
   or ``revalidate=0`` → infinite; ``default-cache`` /
   ``default-no-store`` / ``auto`` / unset → nothing here.
6. **Final resolution** (only when revalidate is still unset):
   ``default-cache`` (and no ``no_store``) → infinite;
   ``default-no-store`` → ``0``; ``no_store`` → ``0``;
   auto-no-cache → ``0``; otherwise the route revalidate, or infinite
   when the route has none ("auto cache").
7. **Downward propagation**: an *explicitly* set fetch revalidate
   lowers the route revalidate (shortest wins); ``revalidate=0``
   makes the containing page dynamic — unless ``force_static`` is set
   or the ``0`` came from auto-no-cache, which Next.js explicitly
   does not treat as dynamic-making for ISR.

``revalidate=0`` therefore means two things at once: the fetch itself
is not cached, and (step 7) its page is dynamic. ``decide_fetch_cache``
reports both via :attr:`FetchCacheDecision.dynamic`.
"""
import hashlib
import json
import math
from dataclasses import dataclass

#: Version prefix in every fetch cache key (Next.js ``MAIN_KEY_PREFIX``).
FETCH_CACHE_KEY_PREFIX = "v4"

#: Headers removed from the key before hashing. Next.js
#: ``FETCH_CACHE_KEY_EXCLUDED_HEADERS`` (``lib/constants.ts:25-28``):
#: distributed-tracing headers must not affect fetch caching or
#: deduplication. ``Authorization`` / ``Cookie`` are intentionally NOT
#: here — see the module docstring.
FETCH_CACHE_KEY_EXCLUDED_HEADERS = frozenset({"traceparent", "tracestate"})

#: Namespace prefix for fetch entries stored in the ISR shared backend,
#: so fetch keys can never collide with page keys.
FETCH_CACHE_NAMESPACE = "\0fetch:"

#: ``cache`` option values ``patch-fetch.ts`` branches on. ``"reload"``
#: / ``"only-if-cached"`` are rejected: Next.js has no branch for them
#: either, and silently treating one as cacheable would be wrong.
FETCH_CACHE_OPTIONS = frozenset({"force-cache", "no-store", "no-cache", "default"})

#: Page-level ``fetchCache`` modes (``patch-fetch.ts`` 720-773).
FETCH_CACHE_MODES = frozenset({
    "force-no-store", "only-no-store", "only-cache", "force-cache",
    "default-cache", "default-no-store", "auto",
})

#: Methods auto-no-cache treats as cacheable (``patch-fetch.ts``:
#: ``!['get', 'head'].includes(method.toLowerCase() || 'get')``).
#: Lower-case, because :func:`decide_fetch_cache` compares
#: ``method.lower()`` against this set.
_CACHEABLE_METHODS = frozenset({"get", "head"})

_UNSET = object()


def _header_pairs(headers):
    """Normalise ``headers`` (mapping or ``(name, value)`` pairs) to a
    sorted ``{lower-name: value}`` dict with the excluded set removed.

    Names and values may be ``str`` or ``bytes`` (bytes decode as
    latin-1, the HTTP header byte encoding). Duplicate names are
    combined, not overwritten: values are joined with ``", "`` (RFC
    7230 §3.2.2), except ``cookie`` which is joined with ``"; "``
    (RFC 6265 §5.4) — otherwise two ``cookie`` headers carrying
    different sessions would collapse to the same cache key.
    Anything else raises :class:`TypeError`.
    """
    if headers is None:
        return {}
    if isinstance(headers, dict):
        items = headers.items()
    else:
        try:
            items = list(headers)
        except TypeError as exc:
            raise TypeError("headers must be a mapping or a sequence of (name, value) pairs") from exc
    result = {}
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError("headers must be a mapping or a sequence of (name, value) pairs")
        name, value = item
        if isinstance(name, (bytes, bytearray)):
            name = bytes(name).decode("latin-1")
        elif not isinstance(name, str):
            raise TypeError("header names must be str or bytes")
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("latin-1")
        elif not isinstance(value, str):
            raise TypeError("header values must be str or bytes")
        key = name.lower()
        if key in FETCH_CACHE_KEY_EXCLUDED_HEADERS:
            continue
        if key in result:
            separator = "; " if key == "cookie" else ", "
            result[key] = result[key] + separator + value
        else:
            result[key] = value
    return dict(sorted(result.items()))


def _tag_body(body):
    """Return ``(body_type, body_chunks)`` for ``body`` following the
    Next.js ``generateCacheKey`` body cases (see module docstring)."""
    if body is None:
        return None, []
    if isinstance(body, str):
        return "text/plain;charset=UTF-8", [f"str:{body}"]
    if isinstance(body, (bytes, bytearray, memoryview)):
        return None, [f"bytes:{bytes(body).hex()}"]
    if isinstance(body, dict):
        chunks = []
        body_type = "application/x-www-form-urlencoded;charset=UTF-8"
        for name in sorted(body):
            if not isinstance(name, str):
                raise TypeError("form field names must be str")
            value = body[name]
            chunks.append(f"key:{name}")
            if isinstance(value, str):
                chunks.append(f"str:{value}")
            elif isinstance(value, (bytes, bytearray, memoryview)):
                chunks.append(f"bytes:{bytes(value).hex()}")
            elif (isinstance(value, tuple) and len(value) == 3
                    and isinstance(value[0], str) and isinstance(value[1], str)
                    and isinstance(value[2], (bytes, bytearray, memoryview))):
                body_type = "multipart/form-data; boundary="
                chunks.extend(("file", value[0], value[1], f"bytes:{bytes(value[2]).hex()}"))
            else:
                raise TypeError(
                    "form field values must be str, bytes, or a (filename, content_type, data) tuple")
        return body_type, chunks
    raise TypeError(
        f"Unsupported fetch body type: {type(body).__name__} (expected str, bytes, or a form mapping)")


def generate_fetch_cache_key(url, method="GET", headers=(), body=None, *,
                             fetch_cache_key_prefix="", mode=None, redirect=None,
                             credentials=None, referrer=None, referrer_policy=None,
                             integrity=None, cache=None):
    """Return the cache key for one fetch, as a SHA-256 hex digest.

    Mirrors Next.js ``IncrementalCache.generateCacheKey``: the digest
    covers ``[prefix, fetch_cache_key_prefix, url, method, body_type,
    headers, mode, redirect, credentials, referrer, referrer_policy,
    integrity, cache, body_chunks]`` serialised as compact JSON.
    ``headers`` may be a mapping or ``(name, value)`` pairs; the
    excluded headers (``traceparent`` / ``tracestate``) are dropped and
    the rest are lower-cased and sorted, so header order does not
    change the key. ``body`` is tagged per :func:`_tag_body` (use the
    public behaviour: ``str`` / ``bytes`` / form mapping). ``method``
    is used as given (default ``"GET"``); ``None`` option fields stay
    ``null`` in the serialised array, exactly as in Next.js.
    """
    if not isinstance(url, str):
        raise TypeError("url must be str")
    if not isinstance(method, str):
        raise TypeError("method must be str")
    if not isinstance(fetch_cache_key_prefix, str):
        raise TypeError("fetch_cache_key_prefix must be str")
    headers = _header_pairs(headers)
    body_type, body_chunks = _tag_body(body)
    parts = [FETCH_CACHE_KEY_PREFIX, fetch_cache_key_prefix, url, method,
             body_type, headers, mode, redirect, credentials, referrer,
             referrer_policy, integrity, cache, body_chunks]
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generate_simple_fetch_cache_key(value, *, fetch_cache_key_prefix=""):
    """Return the simple cache key for ``value`` (Next.js
    ``generateSimpleCacheKey``): the SHA-256 hex digest of
    ``["v4", fetch_cache_key_prefix, value]``."""
    if not isinstance(value, str):
        raise TypeError("value must be str")
    if not isinstance(fetch_cache_key_prefix, str):
        raise TypeError("fetch_cache_key_prefix must be str")
    encoded = json.dumps([FETCH_CACHE_KEY_PREFIX, fetch_cache_key_prefix, value],
                         ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fetch_cache_store_key(cache_key):
    """Return the ISR-backend storage key for a fetch ``cache_key``.

    The ``\\0fetch:`` namespace keeps fetch entries out of the page-key
    space when the ISR shared backend is reused for fetch results.
    """
    if not isinstance(cache_key, str) or not cache_key:
        raise ValueError("cache_key must be a non-empty str")
    return FETCH_CACHE_NAMESPACE + cache_key


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_revalidate(value, *, route="/"):
    """Normalise a caller-supplied ``revalidate`` like Next.js
    ``validateRevalidate`` (``patch-fetch.ts:95-121``): ``False`` means
    infinite (returned as ``None``), a non-negative finite number is
    returned as a ``float``, and anything else raises
    :class:`ValueError` ("must be a non-negative number or false").
    ``None`` here means "not given" and is returned as the ``_UNSET``
    sentinel by the caller-facing wrapper — this helper itself rejects
    it, so callers must translate first.
    """
    if value is False:
        return None
    if _is_number(value) and math.isfinite(value) and value >= 0:
        return float(value)
    raise ValueError(
        f'Invalid revalidate value "{value}" on "{route}", must be a non-negative number or false')


def _normalize_revalidate(value, *, route="/"):
    """Translate a ``decide_fetch_cache`` ``revalidate`` argument to
    the internal form: ``_UNSET`` (not given), ``False`` (infinite),
    or a ``float``. ``None`` is "not given", matching the parameter
    default. ``False`` stays ``False`` here — it is only translated to
    the resolved infinite form (``None``) at final resolution; turning
    it into ``None`` early would make the conflict check
    (``current_revalidate > 0``) and the ``float(...)`` conversion
    below crash on it."""
    if value is None:
        return _UNSET
    if value is False:
        return False
    return _validate_revalidate(value, route=route)


def _fmt_revalidate(value):
    """Format a revalidate for messages the way ``patch-fetch.ts`` does
    (``false`` / ``0`` / ``60``). ``value`` is ``False`` (infinite) or
    a number."""
    if value is False:
        return "false"
    if value == int(value):
        return str(int(value))
    return str(value)


def _validate_route_revalidate(value):
    if value is None:
        return None
    if _is_number(value) and math.isfinite(value) and value >= 0:
        return float(value)
    raise ValueError("route_revalidate must be None or a non-negative finite number")


@dataclass(frozen=True)
class FetchCacheDecision:
    """The outcome of :func:`decide_fetch_cache`.

    ``revalidate`` is ``None`` for infinite (never revalidate),
    ``0`` for "do not cache", or a positive number of seconds.
    ``cacheable`` is ``revalidate != 0`` (Next.js
    ``isCacheableRevalidate`` — infinite counts as cacheable).
    ``dynamic`` is True when this fetch makes its containing page
    dynamic (resolved ``revalidate == 0`` without ``force_static`` and
    without auto-no-cache). ``route_revalidate`` is the route's
    revalidate after downward propagation (shortest wins); ``None``
    means the route has no revalidate. ``reason`` names the rule that
    decided the outcome (``patch-fetch.ts`` ``cacheReason``), and
    ``warnings`` carries conflict warnings — the decision is still
    usable when warnings are present.
    """
    revalidate: float | None
    reason: str
    warnings: tuple
    cacheable: bool
    dynamic: bool
    route_revalidate: float | None


def apply_fetch_revalidate(route_revalidate, fetch_revalidate, *, explicit=True,
                           force_static=False, auto_no_cache=False):
    """Apply one fetch's revalidate to its route (``patch-fetch.ts``
    795-864, "하위 전파"): the shortest explicitly set revalidate wins.

    ``fetch_revalidate`` is the *resolved* fetch revalidate (``None``
    = infinite, ``0``, or positive seconds); ``explicit`` is True only
    when the fetch call itself set a revalidate (Next.js compares
    ``originalFetchRevalidate === finalRevalidate`` — a value that
    only came from defaults never propagates). Returns
    ``(route_revalidate, dynamic)``: the route value afterwards, and
    whether the fetch makes the page dynamic (``revalidate == 0``
    without ``force_static`` and without auto-no-cache).

    PyNext extension, documented: when the route has no revalidate
    (``None``), an explicit fetch revalidate is adopted as the route's;
    Next.js only lowers a revalidate that is already set.
    """
    route = _validate_route_revalidate(route_revalidate)
    if fetch_revalidate is not None:
        fetch_revalidate = _validate_revalidate(fetch_revalidate, route="/")
    if not explicit or fetch_revalidate is None:
        return route, False
    if fetch_revalidate == 0:
        if force_static or auto_no_cache:
            return route, False
        return 0.0, True
    if route is None or fetch_revalidate < route:
        return fetch_revalidate, False
    return route, False


def decide_fetch_cache(*, cache=None, revalidate=None, fetch_cache_mode=None,
                       force_dynamic=False, force_static=False, is_build_time=False,
                       method="GET", headers=(), no_store=False, route_revalidate=None):
    """Decide whether one fetch may be cached, following the
    ``patch-fetch.ts`` decision table in order (see the module
    docstring for the numbered steps).

    Parameters mirror the Next.js inputs: ``cache`` is the fetch
    ``cache`` option (``"force-cache"`` / ``"no-store"`` / ``"no-cache"``
    / ``"default"`` / ``None``); ``revalidate`` is the fetch
    ``next.revalidate`` (``False`` = infinite, a non-negative number,
    or ``None`` = not given); ``fetch_cache_mode`` is the page-level
    ``fetchCache`` mode (PyNext has no page-level export yet — pass it
    explicitly when one exists); ``force_dynamic`` / ``force_static``
    are the segment-level dynamic/static settings; ``is_build_time``
    is True during build-time prerendering; ``method`` / ``headers``
    drive auto-no-cache; ``no_store`` is the ``unstable_noStore()``
    equivalent; ``route_revalidate`` is the containing route's
    revalidate (``None`` = unset).

    Returns a :class:`FetchCacheDecision`. Conflicting options produce
    a warning and are dropped, not an error — exactly like Next.js.
    Incompatible page modes (``only-no-store`` + ``force-cache``,
    ``only-cache`` + ``no-store``) raise :class:`ValueError`, as
    Next.js throws for them. Unknown ``cache`` / ``fetch_cache_mode``
    values and invalid ``revalidate`` values raise :class:`ValueError`.
    """
    if cache is not None and cache not in FETCH_CACHE_OPTIONS:
        raise ValueError(f"Unknown fetch cache option: {cache!r}")
    if fetch_cache_mode is not None and fetch_cache_mode not in FETCH_CACHE_MODES:
        raise ValueError(f"Unknown fetchCache mode: {fetch_cache_mode!r}")
    if not isinstance(method, str):
        raise TypeError("method must be str")
    if not isinstance(force_dynamic, bool) or not isinstance(force_static, bool):
        raise TypeError("force_dynamic and force_static must be bool")
    if not isinstance(is_build_time, bool):
        raise TypeError("is_build_time must be bool")
    if not isinstance(no_store, bool):
        raise TypeError("no_store must be bool")
    route = _validate_route_revalidate(route_revalidate)
    headers = _header_pairs(headers)

    # The raw revalidate the fetch call itself set (Next.js
    # ``originalFetchRevalidate``), kept for the propagation check in
    # step 7: only a value that survives unchanged propagates.
    raw_revalidate = _normalize_revalidate(revalidate)
    current_cache = cache
    current_revalidate = raw_revalidate

    warnings = []

    # 1. Conflict: warn and drop both options (patch-fetch.ts 560-569).
    if current_cache is not None and current_revalidate is not _UNSET:
        conflicting = (
            (current_cache == "force-cache" and current_revalidate == 0)
            or (current_cache == "no-store"
                and (current_revalidate is False or current_revalidate > 0))
        )
        if conflicting:
            warnings.append(
                f'Specified "cache: {current_cache}" and '
                f'"revalidate: {_fmt_revalidate(current_revalidate)}", '
                "only one should be specified.")
            current_cache = None
            current_revalidate = _UNSET

    # The explicit opt-out / force-dynamic checks read the values as
    # they stand after the conflict drop (patch-fetch.ts 571-602).
    has_explicit_opt_out = (
        current_cache in ("no-cache", "no-store")
        or fetch_cache_mode in ("force-no-store", "only-no-store")
    )
    no_config_and_force_dynamic = (
        fetch_cache_mode is None
        and current_cache is None
        and current_revalidate is _UNSET
        and force_dynamic
    )

    # 2. force-cache without a revalidate means infinite (603-612).
    # 3. An explicit opt-out (or force-dynamic with no config) means 0.
    if current_cache == "force-cache" and current_revalidate is _UNSET:
        current_revalidate = False
    elif has_explicit_opt_out or no_config_and_force_dynamic:
        current_revalidate = 0

    reason = ""
    if current_cache in ("no-cache", "no-store"):
        reason = f"cache: {current_cache}"

    # Validate the revalidate as it stands (patch-fetch.ts 614-618).
    if current_revalidate is _UNSET:
        final_revalidate = _UNSET
    elif current_revalidate is False:
        final_revalidate = None  # infinite
    else:
        final_revalidate = float(current_revalidate)

    # 4. auto-no-cache (patch-fetch.ts 620-686).
    has_uncacheable_header = "authorization" in headers or "cookie" in headers
    is_uncacheable_method = method.lower() not in _CACHEABLE_METHODS
    has_no_explicit_config = (
        fetch_cache_mode is None
        and (current_cache is None or current_cache == "default")
        and current_revalidate is _UNSET
    )
    auto_no_cache = bool(
        (has_uncacheable_header or is_uncacheable_method) and route == 0
    )
    is_implicit_build_time_cache = False
    if not auto_no_cache and has_no_explicit_config:
        # No automatic no-cache during build-time prerendering, so the
        # fetch cache can still be shared between build workers.
        if is_build_time:
            is_implicit_build_time_cache = True
        else:
            auto_no_cache = True

    # 5. Page fetchCache mode switch (patch-fetch.ts 720-773). The
    # only-no-store check reads the *resolved* revalidate, where
    # infinite counts as positive (Next.js INFINITE_CACHE > 0).
    resolved_positive_or_infinite = (
        final_revalidate is not _UNSET
        and (final_revalidate is None or final_revalidate > 0)
    )
    if fetch_cache_mode == "force-no-store":
        reason = "fetchCache = force-no-store"
    elif fetch_cache_mode == "only-no-store":
        if current_cache == "force-cache" or resolved_positive_or_infinite:
            raise ValueError(
                "cache: 'force-cache' used on fetch with "
                "'export const fetchCache = 'only-no-store''")
        reason = "fetchCache = only-no-store"
    elif fetch_cache_mode == "only-cache":
        if current_cache == "no-store":
            raise ValueError(
                "cache: 'no-store' used on fetch with "
                "'export const fetchCache = 'only-cache''")
    elif fetch_cache_mode == "force-cache":
        if final_revalidate is _UNSET or final_revalidate == 0:
            reason = "fetchCache = force-cache"
            final_revalidate = None  # infinite

    # 6. Final resolution when revalidate is still unset (774-800).
    if final_revalidate is _UNSET:
        if fetch_cache_mode == "default-cache" and not no_store:
            final_revalidate = None
            reason = "fetchCache = default-cache"
        elif fetch_cache_mode == "default-no-store":
            final_revalidate = 0.0
            reason = "fetchCache = default-no-store"
        elif no_store:
            final_revalidate = 0.0
            reason = "noStore call"
        elif auto_no_cache:
            final_revalidate = 0.0
            reason = "auto no cache"
        else:
            reason = "auto cache"
            final_revalidate = route  # None = infinite when unset
    elif not reason:
        reason = f"revalidate: {_fmt_revalidate(False if final_revalidate is None else final_revalidate)}"

    # 7. Downward propagation (patch-fetch.ts 795-864): only a fetch
    # revalidate that is unchanged from the raw call value propagates,
    # and only when it lowers the route value.
    explicit = (
        raw_revalidate is not _UNSET
        and raw_revalidate is not False
        and final_revalidate == raw_revalidate
    )
    # ``final_revalidate`` here is already the resolved form (None =
    # infinite); ``apply_fetch_revalidate`` takes the raw number form,
    # so translate back: infinite never propagates (Next.js
    # ``false !== INFINITE_CACHE``).
    propagated_route, dynamic = apply_fetch_revalidate(
        route,
        None if final_revalidate is None else final_revalidate,
        explicit=explicit,
        force_static=force_static,
        auto_no_cache=auto_no_cache,
    )

    return FetchCacheDecision(
        revalidate=final_revalidate,
        reason=reason,
        warnings=tuple(warnings),
        cacheable=final_revalidate != 0,
        dynamic=dynamic,
        route_revalidate=propagated_route,
    )
