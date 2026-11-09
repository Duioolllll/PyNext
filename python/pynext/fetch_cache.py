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


# ---------------------------------------------------------------------------
# Storage layer (track C P1-1): persistent fetch cache over the ISR
# backends, request-memo integration, and the stdlib fetcher. This
# lives in the same module as the policy layer above; the two layers
# keep separate key/policy helpers (``fetch_cache_key`` /
# ``resolve_fetch_policy`` here vs ``generate_fetch_cache_key`` /
# ``decide_fetch_cache`` above) because they were built on different
# branches — unifying them is a follow-up, not this rebase.
# ---------------------------------------------------------------------------
import asyncio
import base64
import binascii
import contextvars
import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

from .cache import MAX_CACHE_BODY_BYTES, MAX_CACHE_SECONDS, ISRCache, SharedCacheUnavailable
from .failure_policy import FAIL_CLOSED
from .request_memo import ensure_request_memo

__all__ = [
    # Storage layer (this commit's track C P1-1).
    "FetchCache",
    "FetchPolicy",
    "FetchResult",
    "fetch_cache_key",
    "fetch_cache_scope",
    "fetch_cached",
    "get_fetch_cache",
    "resolve_fetch_policy",
    "urllib_fetcher",
    # Policy layer (HEAD: key rules + decision table).
    "FETCH_CACHE_KEY_EXCLUDED_HEADERS",
    "FETCH_CACHE_KEY_PREFIX",
    "FETCH_CACHE_MODES",
    "FETCH_CACHE_NAMESPACE",
    "FETCH_CACHE_OPTIONS",
    "FetchCacheDecision",
    "apply_fetch_revalidate",
    "decide_fetch_cache",
    "fetch_cache_store_key",
    "generate_fetch_cache_key",
    "generate_simple_fetch_cache_key",
]

log = logging.getLogger("pynext")

# The fetch cache for the request/render in progress. Installed by
# App.dispatch for the whole request and by App.render_page for renders
# that run in a fresh task context (ISR generation), so page functions —
# which cannot take a ``request`` argument — can still call
# fetch_cached() without holding an App reference.
_fetch_cache_var = contextvars.ContextVar("pynext_fetch_cache", default=None)


@contextmanager
def fetch_cache_scope(cache):
    """Install ``cache`` as the current fetch cache; reset it on exit."""
    token = _fetch_cache_var.set(cache)
    try:
        yield cache
    finally:
        _fetch_cache_var.reset(token)


def get_fetch_cache():
    """Return the current fetch cache, or ``None`` outside a request."""
    return _fetch_cache_var.get()


async def fetch_cached(url, fetcher, **kwargs):
    """Fetch ``url`` through the current request's fetch cache.

    Same arguments as :meth:`FetchCache.get_or_fetch`. Raises
    :class:`RuntimeError` outside a request/render (no cache installed).
    """
    cache = _fetch_cache_var.get()
    if cache is None:
        raise RuntimeError("fetch_cached() requires a request context (no fetch cache installed)")
    return await cache.get_or_fetch(url, fetcher, **kwargs)

_FETCH_PREFIX = "\0fetch:"
_EXCLUDED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "set-cookie"})
_METHOD_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


@dataclass(frozen=True)
class FetchResult:
    """One fetched response: status, headers and body bytes."""

    status: int
    headers: tuple = ()
    body: bytes = b""


@dataclass(frozen=True)
class FetchPolicy:
    """Resolved fetch caching policy.

    ``revalidate`` is seconds (``0`` = never store, ``False`` =
    infinite). ``cached`` is False exactly when ``revalidate == 0``.
    """

    revalidate: float | bool = False
    cached: bool = True

    @property
    def fresh_seconds(self):
        """``revalidate`` mapped onto what the backends can store."""
        if self.revalidate is False:
            return MAX_CACHE_SECONDS
        return float(self.revalidate)


def _is_zero(value):
    """True for a numeric zero revalidate — but not for ``False``,
    which means infinite (``False == 0`` in Python)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == 0)


def _coerce_headers(headers):
    if headers is None:
        return []
    if hasattr(headers, "items"):
        headers = headers.items()
    coerced = []
    for name, value in headers:
        if isinstance(name, (bytes, bytearray)):
            name = bytes(name).decode("latin-1")
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("latin-1")
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("fetch headers must be str or bytes pairs")
        coerced.append((name.strip().lower(), value.strip()))
    return coerced


def _validate_method(method):
    if not isinstance(method, str) or not _METHOD_TOKEN.fullmatch(method):
        raise ValueError(f"Invalid fetch method: {method!r}")
    return method.upper()


def _encode_body(body):
    """Tag a body by type so ``["a", "b"]`` and ``["a,b"]`` cannot
    collide (Next.js ``generateCacheKey`` does the same)."""
    if body is None:
        return ["none"]
    if isinstance(body, str):
        data = body.encode("utf-8")
        return ["str", len(data), hashlib.sha256(data).hexdigest()]
    if isinstance(body, (bytes, bytearray, memoryview)):
        data = bytes(body)
        return ["bytes", len(data), hashlib.sha256(data).hexdigest()]
    if isinstance(body, dict):
        fields = []
        for key in sorted(body, key=str):
            value = body[key]
            values = value if isinstance(value, (list, tuple)) else [value]
            fields.append([str(key), [_encode_body(item) for item in values]])
        return ["form", fields]
    raise TypeError(f"Unsupported fetch body type: {type(body).__name__}")


def fetch_cache_key(url, *, method="GET", headers=(), body=None):
    """Return the hex cache key for one fetch.

    The key hashes ``[url (fragment stripped), method, headers, body]``.
    ``Authorization``, ``Cookie``, ``Proxy-Authorization`` and
    ``Set-Cookie`` headers are excluded: a cached response must never
    be served to a different credential. Header order does not matter;
    the body is type-tagged (see :func:`_encode_body`).
    """
    if not isinstance(url, str) or not url:
        raise ValueError("fetch URL must be a non-empty string")
    method = _validate_method(method)
    url = url.split("#", 1)[0]
    kept = sorted(
        (name, value) for name, value in _coerce_headers(headers)
        if name not in _EXCLUDED_HEADERS
    )
    canonical = json.dumps(
        ["fetch-v1", method, url, kept, _encode_body(body)],
        ensure_ascii=True, separators=(",", ":"), sort_keys=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_fetch_revalidate(value, *, source="revalidate"):
    if value is False:
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source} must be a non-negative number or False")
    if value != value or value < 0 or value == float("inf"):
        raise ValueError(f"{source} must be a non-negative number or False")
    return value


def resolve_fetch_policy(*, cache=None, revalidate=_UNSET, method="GET",
                         headers=(), default_revalidate=None, build_time=False):
    """Resolve caching for one fetch from the decision table.

    ``cache`` is ``"force-cache"``, ``"no-store"`` or ``"no-cache"``;
    ``revalidate`` is seconds or ``False`` (infinite). Conflicting
    explicit options warn and are both dropped. ``default_revalidate``
    is the fallback when nothing is explicit (a page's ISR
    ``revalidate`` when the caller knows it, else infinite).
    """
    if cache is not None and cache not in {"force-cache", "no-store", "no-cache"}:
        raise ValueError(f"Unknown fetch cache mode: {cache!r}")
    explicit_revalidate = revalidate is not _UNSET
    if explicit_revalidate:
        revalidate = _validate_fetch_revalidate(revalidate)
    if default_revalidate is not None:
        default_revalidate = _validate_fetch_revalidate(
            default_revalidate, source="default_revalidate")
    conflict = (
        (cache == "force-cache" and _is_zero(revalidate))
        or (cache in {"no-store", "no-cache"} and revalidate is not _UNSET
            and (revalidate is False or revalidate > 0))
    )
    if conflict:
        log.warning("Conflicting fetch cache options cache=%r revalidate=%r; ignoring both",
                    cache, revalidate)
        cache, revalidate, explicit_revalidate = None, _UNSET, False
    if explicit_revalidate:
        return FetchPolicy(revalidate, cached=not _is_zero(revalidate))
    if cache == "force-cache":
        return FetchPolicy(False, True)
    if cache in {"no-store", "no-cache"}:
        return FetchPolicy(0, False)
    method = _validate_method(method)
    names = {name for name, _ in _coerce_headers(headers)}
    if not build_time and (names & {"authorization", "cookie"}
                           or method not in {"GET", "HEAD"}):
        return FetchPolicy(0, False)
    fallback = False if default_revalidate is None else default_revalidate
    return FetchPolicy(fallback, True)


def _serialize(result):
    payload = {
        "status": result.status,
        "headers": [[name, value] for name, value in result.headers],
        "body": base64.b64encode(result.body).decode("ascii"),
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _deserialize(data):
    try:
        payload = json.loads(data.decode("ascii"))
        status = payload["status"]
        headers = tuple((str(k), str(v)) for k, v in payload["headers"])
        body = base64.b64decode(payload["body"], validate=True)
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as exc:
        raise ValueError("Corrupt fetch cache entry") from exc
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("Corrupt fetch cache entry")
    return FetchResult(status, headers, body)


async def urllib_fetcher(url, *, method="GET", headers=(), body=None, timeout=10.0):
    """Fetch ``url`` with the stdlib and return a :class:`FetchResult`.

    Only ``http``/``https`` URLs are allowed. HTTP error statuses are
    returned as results, not raised; transport errors propagate.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"urllib_fetcher only supports http(s) URLs: {url!r}")
    if isinstance(body, dict):
        data = urllib.parse.urlencode(body, doseq=True).encode("ascii")
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif body is None:
        data = None
    else:
        data = bytes(body)

    def _do():
        request = urllib.request.Request(url, data=data, method=method)
        for name, value in _coerce_headers(headers):
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return FetchResult(response.status,
                                   tuple((k.lower(), v) for k, v in response.headers.items()),
                                   response.read())
        except urllib.error.HTTPError as exc:
            return FetchResult(exc.code,
                               tuple((k.lower(), v) for k, v in exc.headers.items()),
                               exc.read())

    return await asyncio.to_thread(_do)


class FetchCache:
    """Persistent fetch cache over an :class:`ISRCache`.

    ``isr_cache`` supplies the backends (memory / SQLite / Redis); fetch
    entries live under ``\\0fetch:`` keys so they never collide with page
    entries. The same request memo as :mod:`pynext.request_memo` dedups
    repeated fetches inside one request, including ``no-store`` ones.
    """

    def __init__(self, isr_cache):
        if not isinstance(isr_cache, ISRCache):
            raise TypeError("FetchCache requires an ISRCache")
        self._isr = isr_cache
        self._inflight = {}
        self._tasks = set()

    def _storage_key(self, key):
        return self._isr.namespace + _FETCH_PREFIX + key

    def _memo_key(self, key):
        return ("pynext.fetch", key)

    async def _shared_lookup(self, key):
        """Return a fresh shared entry body, ``None`` on miss/expired,
        or the shared-unavailable sentinel path (``None`` under
        fail_open, raise under fail_closed)."""
        shared = self._isr.shared
        if shared is None:
            return None
        try:
            entry = await asyncio.to_thread(shared.lookup_entry, self._storage_key(key))
        except Exception as exc:
            if self._isr.failure_policy == FAIL_CLOSED:
                raise SharedCacheUnavailable("Shared fetch cache lookup failed") from exc
            log.warning("Shared fetch cache lookup failed for %s", key, exc_info=True)
            return None
        if entry is not None and entry.status == "fresh":
            return entry.body
        return None

    async def _shared_store(self, key, data, fresh_seconds):
        shared = self._isr.shared
        if shared is None:
            return
        try:
            await asyncio.to_thread(
                shared.insert_fenced, self._storage_key(key), data,
                float(fresh_seconds), 0.0, None)
        except Exception:
            log.warning("Shared fetch cache insert failed for %s", key, exc_info=True)

    async def _shared_remove(self, key):
        shared = self._isr.shared
        if shared is None:
            return False
        try:
            return await asyncio.to_thread(shared.remove, self._storage_key(key))
        except Exception:
            log.warning("Shared fetch cache remove failed for %s", key, exc_info=True)
            return False

    async def _fetch_once(self, key, url, fetcher, fetcher_kwargs, policy):
        memo = ensure_request_memo()
        cached = memo.get(self._memo_key(key))
        if cached is not None:
            return cached
        if policy.cached:
            storage_key = self._storage_key(key)
            status, body = self._isr.store.lookup(storage_key)
            if status == "fresh":
                try:
                    result = _deserialize(body)
                except ValueError:
                    result = None
                if result is not None:
                    memo.set(self._memo_key(key), result)
                    return result
            shared_body = await self._shared_lookup(key)
            if shared_body is not None:
                try:
                    result = _deserialize(shared_body)
                except ValueError:
                    result = None
                if result is not None:
                    self._isr.store.insert(storage_key, shared_body,
                                           policy.fresh_seconds, 0.0)
                    memo.set(self._memo_key(key), result)
                    return result
        result = await fetcher(url, **fetcher_kwargs)
        if not isinstance(result, FetchResult):
            raise TypeError("fetcher must return a FetchResult")
        if policy.cached and 200 <= result.status <= 299:
            data = _serialize(result)
            if len(data) <= MAX_CACHE_BODY_BYTES:
                storage_key = self._storage_key(key)
                self._isr.store.insert(storage_key, data, policy.fresh_seconds, 0.0)
                await self._shared_store(key, data, policy.fresh_seconds)
            else:
                log.warning("Fetch cache entry for %s exceeds the %d-byte cap; served but not cached",
                            key, MAX_CACHE_BODY_BYTES)
        memo.set(self._memo_key(key), result)
        return result

    async def get_or_fetch(self, url, fetcher, *, method="GET", headers=(),
                           body=None, cache=None, revalidate=_UNSET,
                           default_revalidate=None, build_time=False, **fetcher_kwargs):
        """Fetch ``url`` (via async ``fetcher(url, ...)``), cached per
        the resolved policy.

        ``fetcher`` is an async callable returning a
        :class:`FetchResult` — pass :func:`urllib_fetcher` for real
        HTTP, or a stub in tests. Extra keyword arguments are passed
        through to the fetcher (e.g. ``timeout=`` for
        :func:`urllib_fetcher`). Concurrent calls for the same key in
        this process share one upstream fetch.
        """
        key = fetch_cache_key(url, method=method, headers=headers, body=body)
        policy = resolve_fetch_policy(cache=cache, revalidate=revalidate, method=method,
                                      headers=headers, default_revalidate=default_revalidate,
                                      build_time=build_time)
        memo = ensure_request_memo()
        cached = memo.get(self._memo_key(key))
        if cached is not None:
            return cached
        task = self._inflight.get(key)
        if task is None:
            fetcher_kwargs = {k: v for k, v in fetcher_kwargs.items()
                              if k not in {"method", "headers", "body"}}
            task = asyncio.ensure_future(
                self._fetch_once(key, url, fetcher,
                                 {"method": method, "headers": headers, "body": body,
                                  **fetcher_kwargs},
                                 policy))
            self._inflight[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

            def _done(finished):
                if self._inflight.get(key) is finished:
                    self._inflight.pop(key, None)
                if not finished.cancelled() and finished.exception() is not None:
                    log.warning("Fetch failed for %s", url, exc_info=finished.exception())

            task.add_done_callback(_done)
        try:
            return await asyncio.shield(task)
        except BaseException:
            # Only retire the slot when the shared fetch itself finished;
            # a cancelled waiter must not orphan a still-running fetch.
            if task.done() and self._inflight.get(key) is task:
                self._inflight.pop(key, None)
            raise

    async def invalidate(self, url, *, method="GET", headers=(), body=None):
        """Remove one fetch entry (persistent + request memo).

        Returns True when a persistent entry or a memo entry was removed.
        """
        key = fetch_cache_key(url, method=method, headers=headers, body=body)
        memo = ensure_request_memo()
        removed_memo = memo.invalidate(self._memo_key(key))
        removed_local = self._isr.store.remove(self._storage_key(key))
        removed_shared = await self._shared_remove(key)
        return bool(removed_memo or removed_local or removed_shared)
