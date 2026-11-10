"""Tests for the fetch / data cache key rules and policy decision table.

Covers ``pynext.fetch_cache`` (REPORT.md §2.2 C#1, tracks/c-caching.md
P1-1): Next.js ``generateCacheKey`` / ``generateSimpleCacheKey``
(``incremental-cache/index.ts:373-499``) and the ``patch-fetch.ts``
decision table (555-800). Pure policy — no network I/O, no storage,
no Redis needed.
"""

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from pynext.fetch_cache import (
    FETCH_CACHE_KEY_EXCLUDED_HEADERS,
    FETCH_CACHE_NAMESPACE,
    FetchCacheDecision,
    apply_fetch_revalidate,
    decide_fetch_cache,
    fetch_cache_store_key,
    generate_fetch_cache_key,
    generate_simple_fetch_cache_key,
)

URL = "https://example.com/api"


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def test_key_is_deterministic_sha256_hex():
    key = generate_fetch_cache_key(URL)
    assert key == generate_fetch_cache_key(URL)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_key_serialisation_vector():
    """Pin the exact serialised form: compact JSON, ``"v4"`` prefix,
    ``null`` for unset option fields, empty headers object and body."""
    expected_json = (
        '["v4","","https://example.com/api","GET",null,{},'
        "null,null,null,null,null,null,null,[]]"
    )
    expected = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    assert generate_fetch_cache_key(URL) == expected


def test_key_changes_with_url_method_prefix_and_options():
    base = generate_fetch_cache_key(URL)
    assert generate_fetch_cache_key("https://example.com/other") != base
    assert generate_fetch_cache_key(URL, method="POST") != base
    assert generate_fetch_cache_key(URL, fetch_cache_key_prefix="app") != base
    assert generate_fetch_cache_key(URL, cache="force-cache") != base
    assert generate_fetch_cache_key(URL, mode="cors") != base


def test_excluded_headers_do_not_change_key():
    base = generate_fetch_cache_key(URL, headers={"X-A": "1"})
    assert generate_fetch_cache_key(
        URL, headers={"X-A": "1", "traceparent": "00-abc-def-01"}
    ) == base
    assert generate_fetch_cache_key(
        URL, headers={"X-A": "1", "tracestate": "vendor=1"}
    ) == base
    # Exclusion is case-insensitive (header names are lower-cased).
    assert generate_fetch_cache_key(
        URL, headers={"X-A": "1", "TraceParent": "00-abc-def-01"}
    ) == base
    assert FETCH_CACHE_KEY_EXCLUDED_HEADERS == frozenset(
        {"traceparent", "tracestate"}
    )


def test_authorization_and_cookie_are_part_of_the_key():
    """Deliberate deviation from the track report's header list: the
    Next.js source excludes only traceparent/tracestate, so authorised
    fetches must not share one entry when force-cache overrides
    auto-no-cache."""
    base = generate_fetch_cache_key(URL)
    assert generate_fetch_cache_key(
        URL, headers={"Authorization": "Bearer x"}
    ) != base
    assert generate_fetch_cache_key(URL, headers={"Cookie": "s=1"}) != base
    assert generate_fetch_cache_key(
        URL, headers={"Authorization": "Bearer x"}
    ) != generate_fetch_cache_key(URL, headers={"Authorization": "Bearer y"})


def test_header_order_and_case_do_not_change_key():
    first = generate_fetch_cache_key(URL, headers={"B": "2", "A": "1"})
    assert generate_fetch_cache_key(URL, headers={"A": "1", "B": "2"}) == first
    assert generate_fetch_cache_key(URL, headers={"a": "1", "b": "2"}) == first
    # Mapping, pair sequence and bytes all normalise to the same key.
    assert generate_fetch_cache_key(URL, headers=[("A", "1"), ("B", "2")]) == first
    assert generate_fetch_cache_key(
        URL, headers=[(b"A", b"1"), (b"B", b"2")]
    ) == first
    # Duplicate names are joined with ", " (cookie uses "; "), not
    # overwritten — see test_duplicate_cookie_headers_produce_distinct_keys.
    assert generate_fetch_cache_key(
        URL, headers=[("A", "stale"), ("A", "1"), ("B", "2")]
    ) == generate_fetch_cache_key(URL, headers={"A": "stale, 1", "B": "2"})


def test_duplicate_cookie_headers_produce_distinct_keys():
    """Regression: duplicate header names kept only the last value, so
    ``cookie: session=A`` + ``cookie: theme=dark`` and ``cookie:
    session=B`` + ``cookie: theme=dark`` collapsed to the same key.
    HTTP/2 can split cookies across several headers, so this is a real
    collision. Duplicates are joined (", " generally, "; " for cookie)."""
    key_a = generate_fetch_cache_key(
        URL, headers=[("cookie", "session=A"), ("cookie", "theme=dark")])
    key_b = generate_fetch_cache_key(
        URL, headers=[("cookie", "session=B"), ("cookie", "theme=dark")])
    assert key_a != key_b
    # The joined form equals the single-header spelling.
    assert key_a == generate_fetch_cache_key(
        URL, headers={"cookie": "session=A; theme=dark"})
    # Non-cookie duplicates join with ", ".
    assert generate_fetch_cache_key(
        URL, headers=[("X-A", "1"), ("X-A", "2")]) == generate_fetch_cache_key(
        URL, headers={"X-A": "1, 2"})


def test_body_tagging_keeps_shapes_apart():
    assert generate_fetch_cache_key(URL, body="ab") != generate_fetch_cache_key(
        URL, body=b"ab"
    )
    assert generate_fetch_cache_key(URL, body=b"ab") == generate_fetch_cache_key(
        URL, body=bytearray(b"ab")
    )
    assert generate_fetch_cache_key(URL, body="ab") == generate_fetch_cache_key(
        URL, body="ab"
    )
    # Tagged per-field chunks: ["a", "b"] cannot collide with ["a,b"].
    assert generate_fetch_cache_key(
        URL, body={"x": "a", "y": "b"}
    ) != generate_fetch_cache_key(URL, body={"x": "a,b"})
    # Form field order does not matter (mappings serialise sorted).
    assert generate_fetch_cache_key(
        URL, body={"y": "b", "x": "a"}
    ) == generate_fetch_cache_key(URL, body={"x": "a", "y": "b"})
    # A file value switches the body type to multipart and differs
    # from the same content as plain text.
    assert generate_fetch_cache_key(
        URL, body={"f": ("up.txt", "text/plain", b"hi")}
    ) != generate_fetch_cache_key(URL, body={"f": "hi"})
    assert generate_fetch_cache_key(
        URL, body={"f": ("up.txt", "text/plain", b"hi")}
    ) == generate_fetch_cache_key(
        URL, body={"f": ("up.txt", "text/plain", bytearray(b"hi"))}
    )


def test_key_rejects_bad_inputs():
    with pytest.raises(TypeError):
        generate_fetch_cache_key(123)
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, method=7)
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, body=123)
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, body=["a"])
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, body={"a": 1})
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, body={"a": ("f", "text/plain")})
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, headers=123)
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, headers=[("A",)])
    with pytest.raises(TypeError):
        generate_fetch_cache_key(URL, headers={1: "x"})


def test_simple_key():
    expected_json = '["v4","","https://example.com/api"]'
    expected = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    assert generate_simple_fetch_cache_key(URL) == expected
    assert generate_simple_fetch_cache_key(URL) == generate_simple_fetch_cache_key(URL)
    assert generate_simple_fetch_cache_key("other") != generate_simple_fetch_cache_key(URL)
    assert generate_simple_fetch_cache_key(
        URL, fetch_cache_key_prefix="app"
    ) != generate_simple_fetch_cache_key(URL)
    with pytest.raises(TypeError):
        generate_simple_fetch_cache_key(123)


def test_fetch_cache_store_key_namespaces_fetch_entries():
    key = generate_fetch_cache_key(URL)
    assert fetch_cache_store_key(key) == FETCH_CACHE_NAMESPACE + key
    assert fetch_cache_store_key(key).startswith("\0fetch:")
    assert fetch_cache_store_key("abc") == "\0fetch:abc"
    with pytest.raises(ValueError):
        fetch_cache_store_key("")
    with pytest.raises(ValueError):
        fetch_cache_store_key(123)


# ---------------------------------------------------------------------------
# Decision table: explicit values and conflicts (patch-fetch.ts 555-618)
# ---------------------------------------------------------------------------


def test_plain_fetch_with_no_config_is_auto_no_cache():
    decision = decide_fetch_cache()
    assert decision.revalidate == 0
    assert decision.reason == "auto no cache"
    assert decision.cacheable is False
    assert decision.dynamic is False
    assert decision.route_revalidate is None
    assert decision.warnings == ()


def test_force_cache_without_revalidate_is_infinite():
    decision = decide_fetch_cache(cache="force-cache")
    assert decision.revalidate is None
    assert decision.cacheable is True
    assert decision.reason == "revalidate: false"


def test_explicit_opt_outs_are_zero():
    for kwargs in (
        {"cache": "no-store"},
        {"cache": "no-cache"},
        {"fetch_cache_mode": "force-no-store"},
        {"fetch_cache_mode": "only-no-store"},
    ):
        decision = decide_fetch_cache(**kwargs)
        assert decision.revalidate == 0, kwargs
        assert decision.cacheable is False, kwargs


def test_explicit_revalidate_and_propagation():
    decision = decide_fetch_cache(revalidate=60)
    assert decision.revalidate == 60
    assert decision.cacheable is True
    assert decision.reason == "revalidate: 60"
    # Shortest wins against the route; an unset route adopts it.
    assert decide_fetch_cache(revalidate=60, route_revalidate=120).route_revalidate == 60
    assert decide_fetch_cache(revalidate=60, route_revalidate=30).route_revalidate == 30
    assert decide_fetch_cache(revalidate=60, route_revalidate=None).route_revalidate == 60
    # Infinite never propagates.
    assert decide_fetch_cache(revalidate=False, route_revalidate=60).route_revalidate == 60


def test_revalidate_zero_makes_page_dynamic():
    decision = decide_fetch_cache(revalidate=0)
    assert decision.revalidate == 0
    assert decision.dynamic is True
    assert decision.route_revalidate == 0
    # force-static suppresses the dynamic marking.
    decision = decide_fetch_cache(revalidate=0, force_static=True)
    assert decision.dynamic is False
    assert decision.route_revalidate is None


def test_conflicting_options_warn_and_drop_both():
    decision = decide_fetch_cache(cache="force-cache", revalidate=0)
    assert decision.warnings == (
        'Specified "cache: force-cache" and "revalidate: 0", '
        "only one should be specified.",
    )
    assert decision.revalidate == 0
    assert decision.reason == "auto no cache"
    decision = decide_fetch_cache(cache="no-store", revalidate=60)
    assert decision.warnings == (
        'Specified "cache: no-store" and "revalidate: 60", '
        "only one should be specified.",
    )
    decision = decide_fetch_cache(cache="no-store", revalidate=False)
    assert decision.warnings == (
        'Specified "cache: no-store" and "revalidate: false", '
        "only one should be specified.",
    )


def test_non_conflicting_combinations_do_not_warn():
    assert decide_fetch_cache(cache="force-cache", revalidate=60).warnings == ()
    assert decide_fetch_cache(cache="no-store", revalidate=0).warnings == ()
    assert decide_fetch_cache(cache="force-cache", revalidate=60).revalidate == 60
    assert decide_fetch_cache(cache="no-store", revalidate=0).revalidate == 0
    assert decide_fetch_cache(cache="no-store", revalidate=0).dynamic is True


def test_invalid_options_and_revalidates_raise():
    with pytest.raises(ValueError):
        decide_fetch_cache(cache="reload")
    with pytest.raises(ValueError):
        decide_fetch_cache(cache="only-if-cached")
    with pytest.raises(ValueError):
        decide_fetch_cache(fetch_cache_mode="bogus")
    with pytest.raises(ValueError):
        decide_fetch_cache(revalidate=-1)
    with pytest.raises(ValueError):
        decide_fetch_cache(revalidate="60")
    with pytest.raises(ValueError):
        decide_fetch_cache(revalidate=float("nan"))
    with pytest.raises(ValueError):
        decide_fetch_cache(revalidate=float("inf"))
    with pytest.raises(ValueError):
        decide_fetch_cache(route_revalidate=-5)
    with pytest.raises(TypeError):
        decide_fetch_cache(method=123)
    with pytest.raises(TypeError):
        decide_fetch_cache(force_dynamic="yes")


# ---------------------------------------------------------------------------
# Decision table: auto-no-cache (patch-fetch.ts 620-686)
# ---------------------------------------------------------------------------


def test_auto_no_cache_from_auth_cookie_or_method_when_route_is_zero():
    for kwargs in (
        {"headers": {"Authorization": "Bearer x"}, "route_revalidate": 0},
        {"headers": {"Cookie": "s=1"}, "route_revalidate": 0},
        {"method": "POST", "route_revalidate": 0},
    ):
        decision = decide_fetch_cache(**kwargs)
        assert decision.revalidate == 0, kwargs
        assert decision.reason == "auto no cache", kwargs
        # Auto-no-cache never makes the page dynamic by itself.
        assert decision.dynamic is False, kwargs


def test_get_is_a_cacheable_method():
    """Regression: ``method.lower()`` was compared against the
    upper-case set ``{"GET", "HEAD"}``, so even GET was treated as an
    uncacheable method. A GET with revalidate=0 and route=0 makes the
    page dynamic (an explicit revalidate=0, not auto-no-cache)."""
    decision = decide_fetch_cache(revalidate=0, route_revalidate=0, method="GET")
    assert decision.revalidate == 0
    assert decision.dynamic is True
    assert decide_fetch_cache(
        revalidate=0, route_revalidate=0, method="HEAD").dynamic is True
    # POST stays uncacheable: auto-no-cache, never dynamic-making.
    assert decide_fetch_cache(
        revalidate=0, route_revalidate=0, method="POST").dynamic is False
    # Lower-case methods work too (the comparison is case-insensitive).
    assert decide_fetch_cache(
        revalidate=0, route_revalidate=0, method="get").dynamic is True


def test_no_explicit_config_is_auto_no_cache_unless_build_time():
    # Auth header / non-GET with an unset route still auto-no-caches
    # when there is no explicit config at all.
    assert decide_fetch_cache(headers={"Authorization": "Bearer x"}).revalidate == 0
    assert decide_fetch_cache(method="POST").revalidate == 0
    # Build-time prerendering is exempt so workers can share the cache.
    decision = decide_fetch_cache(is_build_time=True)
    assert decision.revalidate is None
    assert decision.reason == "auto cache"
    assert decide_fetch_cache(
        is_build_time=True, route_revalidate=60
    ).revalidate == 60


def test_explicit_force_cache_overrides_auto_no_cache():
    """The load-bearing case for keeping Authorization/Cookie in the
    key: an explicit force-cache must not share an entry with an
    auto-no-cache fetch, so authorised fetches stay distinguishable."""
    decision = decide_fetch_cache(
        cache="force-cache",
        headers={"Authorization": "Bearer x"},
        route_revalidate=0,
    )
    assert decision.revalidate is None
    assert decision.cacheable is True


def test_force_dynamic_with_no_config_is_zero():
    decision = decide_fetch_cache(force_dynamic=True)
    assert decision.revalidate == 0
    assert decision.reason == "revalidate: 0"
    # An explicit revalidate still wins over force-dynamic.
    assert decide_fetch_cache(force_dynamic=True, revalidate=60).revalidate == 60


def test_cache_default_is_treated_as_no_config():
    assert decide_fetch_cache(cache="default").revalidate == 0
    assert decide_fetch_cache(cache="default").reason == "auto no cache"


# ---------------------------------------------------------------------------
# Decision table: fetchCache page modes (patch-fetch.ts 720-800)
# ---------------------------------------------------------------------------


def test_fetch_cache_modes():
    assert decide_fetch_cache(fetch_cache_mode="force-no-store").reason == (
        "fetchCache = force-no-store"
    )
    assert decide_fetch_cache(fetch_cache_mode="default-cache").revalidate is None
    assert decide_fetch_cache(fetch_cache_mode="default-cache").reason == (
        "fetchCache = default-cache"
    )
    assert decide_fetch_cache(fetch_cache_mode="default-no-store").revalidate == 0
    assert decide_fetch_cache(fetch_cache_mode="default-no-store").reason == (
        "fetchCache = default-no-store"
    )
    # "auto" sets a page mode, so there *is* explicit config and the
    # auto-no-cache rule does not fire: it falls through to auto cache.
    decision = decide_fetch_cache(fetch_cache_mode="auto")
    assert decision.revalidate is None
    assert decision.reason == "auto cache"


def test_only_no_store_rejects_force_cache_but_not_plain_revalidate():
    with pytest.raises(ValueError):
        decide_fetch_cache(fetch_cache_mode="only-no-store", cache="force-cache")
    with pytest.raises(ValueError):
        decide_fetch_cache(
            fetch_cache_mode="only-no-store", cache="force-cache", revalidate=60
        )
    # The page opt-out zeroes an explicit revalidate before the mode
    # check runs, so this resolves to 0 instead of raising (same order
    # as patch-fetch.ts).
    decision = decide_fetch_cache(fetch_cache_mode="only-no-store", revalidate=60)
    assert decision.revalidate == 0
    assert decision.reason == "fetchCache = only-no-store"


def test_only_cache_rejects_no_store():
    with pytest.raises(ValueError):
        decide_fetch_cache(fetch_cache_mode="only-cache", cache="no-store")
    assert decide_fetch_cache(
        fetch_cache_mode="only-cache", cache="force-cache"
    ).revalidate is None


def test_force_cache_mode_promotes_unset_or_zero_to_infinite():
    assert decide_fetch_cache(fetch_cache_mode="force-cache").revalidate is None
    assert decide_fetch_cache(
        fetch_cache_mode="force-cache"
    ).reason == "fetchCache = force-cache"
    assert decide_fetch_cache(
        fetch_cache_mode="force-cache", revalidate=0
    ).revalidate is None
    # A positive revalidate is left alone.
    assert decide_fetch_cache(
        fetch_cache_mode="force-cache", revalidate=60
    ).revalidate == 60


def test_no_store_call_and_default_cache_interaction():
    decision = decide_fetch_cache(no_store=True)
    assert decision.revalidate == 0
    assert decision.reason == "noStore call"
    # default-cache loses to an explicit noStore call.
    decision = decide_fetch_cache(fetch_cache_mode="default-cache", no_store=True)
    assert decision.revalidate == 0
    assert decision.reason == "noStore call"


def test_decision_is_immutable():
    decision = decide_fetch_cache(revalidate=60)
    assert isinstance(decision, FetchCacheDecision)
    with pytest.raises(FrozenInstanceError):
        decision.revalidate = 0


# ---------------------------------------------------------------------------
# Downward propagation helper (patch-fetch.ts 795-864)
# ---------------------------------------------------------------------------


def test_apply_fetch_revalidate():
    assert apply_fetch_revalidate(None, 60, explicit=True) == (60.0, False)
    assert apply_fetch_revalidate(120.0, 60, explicit=True) == (60.0, False)
    assert apply_fetch_revalidate(30.0, 60, explicit=True) == (30.0, False)
    # Not explicit: defaults never propagate.
    assert apply_fetch_revalidate(60.0, 60, explicit=False) == (60.0, False)
    # Infinite never propagates.
    assert apply_fetch_revalidate(60.0, None, explicit=True) == (60.0, False)
    # Zero makes the page dynamic unless force-static or auto-no-cache.
    assert apply_fetch_revalidate(60.0, 0, explicit=True) == (0.0, True)
    assert apply_fetch_revalidate(60.0, 0, explicit=True, force_static=True) == (
        60.0,
        False,
    )
    assert apply_fetch_revalidate(60.0, 0, explicit=True, auto_no_cache=True) == (
        60.0,
        False,
    )
    with pytest.raises(ValueError):
        apply_fetch_revalidate(-1, 60)
    with pytest.raises(ValueError):
        apply_fetch_revalidate(60.0, -1)
