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


# ---------------------------------------------------------------------------
# Storage + request-memo layer (track C P1-1/P1-2, this commit):
# FetchCache over the ISR backends, resolve_fetch_policy, and the
# per-request memo. (The suite's module-level URL is FETCH_URL here
# so it does not overwrite the policy suite's URL above.)
# ---------------------------------------------------------------------------
"""Fetch/data cache + per-request memoization (track C P1-1/P1-2)."""
import asyncio
import contextvars
import http.server
import logging
import threading

import httpx
import pytest

from pynext import (FetchCache, FetchResult, create_app, fetch_cache_key,
                    fetch_cached, get_fetch_cache, memoize, memoize_sync,
                    resolve_fetch_policy)
from pynext.cache import MAX_CACHE_BODY_BYTES, ISRCache
from pynext.fetch_cache import urllib_fetcher
from pynext.request_memo import (RequestMemo, clear_request_memo, current_memo,
                                 ensure_request_memo, request_memo_scope)

FETCH_URL = "https://api.example.test/posts/42"


# --- cache keys -----------------------------------------------------------

def test_fetch_cache_key_is_stable_and_method_sensitive():
    key = fetch_cache_key(FETCH_URL)
    assert key == fetch_cache_key(FETCH_URL)
    assert key == fetch_cache_key(FETCH_URL + "#fragment")
    assert key != fetch_cache_key(FETCH_URL, method="POST")
    assert len(key) == 64


def test_fetch_cache_key_distinguishes_credentials_and_ignores_tracing_headers():
    """P1-1 regression: credential headers are part of the key, so two
    fetches with different credentials never share one entry. Only
    distributed tracing headers are excluded (Next.js
    ``FETCH_CACHE_KEY_EXCLUDED_HEADERS``)."""
    base = fetch_cache_key(FETCH_URL, headers=[("Accept", "application/json")])
    # Different credentials -> different keys (no cross-user serving).
    assert base != fetch_cache_key(
        FETCH_URL, headers=[("Accept", "application/json"),
                      ("Authorization", "Bearer x")])
    assert base != fetch_cache_key(
        FETCH_URL, headers=[("Accept", "application/json"),
                      ("Cookie", "s=1")])
    assert fetch_cache_key(
        FETCH_URL, headers=[("Authorization", "Bearer alice")]) != fetch_cache_key(
        FETCH_URL, headers=[("Authorization", "Bearer bob")])
    assert fetch_cache_key(
        FETCH_URL, headers=[("Cookie", "s=alice")]) != fetch_cache_key(
        FETCH_URL, headers=[("Cookie", "s=bob")])
    # Same credential -> same key (header order and case do not matter).
    assert fetch_cache_key(
        FETCH_URL, headers=[("X-B", "2"), ("Accept", "application/json"), ("X-A", "1"),
                      ("Authorization", "Bearer x")]) == fetch_cache_key(
        FETCH_URL, headers=[("authorization", "Bearer x"), ("X-A", "1"),
                      ("X-B", "2"), ("Accept", "application/json")])
    # Tracing headers still do not change the key.
    assert base == fetch_cache_key(
        FETCH_URL, headers=[("Accept", "application/json"),
                      ("traceparent", "00-abc-def-01")])
    assert base == fetch_cache_key(
        FETCH_URL, headers=[("Accept", "application/json"),
                      ("TraceState", "vendor=1")])
    # ...but a different non-credential header value does change the key.
    assert base != fetch_cache_key(
        FETCH_URL, headers=[("Accept", "text/html")])


def test_fetch_cache_key_tags_body_types_so_similar_bodies_differ():
    assert fetch_cache_key(FETCH_URL, method="POST", body="a,b") != fetch_cache_key(
        FETCH_URL, method="POST", body=b"a,b")
    # ["a", "b"] vs ["a,b"] must not collide (form-field tagging).
    assert fetch_cache_key(FETCH_URL, method="POST", body={"f": ["a", "b"]}) != fetch_cache_key(
        FETCH_URL, method="POST", body={"f": ["a,b"]})
    assert fetch_cache_key(FETCH_URL, method="POST", body={"b": "1", "a": "2"}) == fetch_cache_key(
        FETCH_URL, method="POST", body={"a": "2", "b": "1"})
    with pytest.raises(ValueError):
        fetch_cache_key("")
    with pytest.raises(TypeError):
        fetch_cache_key(FETCH_URL, method="POST", body=object())


# --- policy decision table -------------------------------------------------

def test_policy_explicit_revalidate_wins():
    assert resolve_fetch_policy(revalidate=60).revalidate == 60
    assert resolve_fetch_policy(revalidate=False).revalidate is False
    assert resolve_fetch_policy(revalidate=0) == resolve_fetch_policy(cache="no-store")
    assert resolve_fetch_policy(revalidate=0).cached is False
    # False (infinite) is not zero.
    assert resolve_fetch_policy(revalidate=False).cached is True


def test_policy_force_cache_without_revalidate_is_infinite():
    policy = resolve_fetch_policy(cache="force-cache")
    assert policy.revalidate is False and policy.cached is True


def test_policy_infinite_fresh_seconds_is_clamped_to_the_native_l1_cap():
    """P1-3 regression: an infinite policy resolves to a fresh window
    the native L1 PageCache accepts (7 days), not the 1-year
    ``MAX_CACHE_SECONDS`` the native ``insert`` rejects with
    ``ValueError: Invalid cache durations``. A finite revalidate
    longer than the cap is clamped the same way."""
    for policy in (resolve_fetch_policy(),
                   resolve_fetch_policy(cache="force-cache"),
                   resolve_fetch_policy(revalidate=False)):
        assert policy.revalidate is False
        assert policy.fresh_seconds == 604_800.0
    assert resolve_fetch_policy(revalidate=2_592_000).fresh_seconds == 604_800.0
    # Shorter windows are untouched.
    assert resolve_fetch_policy(revalidate=60).fresh_seconds == 60.0


def test_policy_no_store_is_zero():
    for cache in ("no-store", "no-cache"):
        policy = resolve_fetch_policy(cache=cache)
        assert policy.revalidate == 0 and policy.cached is False


def test_policy_conflicts_warn_and_drop_both(caplog):
    with caplog.at_level(logging.WARNING, logger="pynext"):
        policy = resolve_fetch_policy(cache="force-cache", revalidate=0)
    assert "Conflicting fetch cache options" in caplog.text
    # Both dropped: falls through to the default (infinite here).
    assert policy.revalidate is False
    with caplog.at_level(logging.WARNING, logger="pynext"):
        policy = resolve_fetch_policy(cache="no-store", revalidate=30)
    assert "Conflicting fetch cache options" in caplog.text
    assert policy.revalidate is False


def test_policy_auto_no_cache_on_auth_or_non_get():
    assert resolve_fetch_policy(
        headers=[("Authorization", "Bearer x")]).revalidate == 0
    assert resolve_fetch_policy(
        headers=[("Cookie", "s=1")]).revalidate == 0
    assert resolve_fetch_policy(method="POST").revalidate == 0
    # Build-time prerender disables auto-no-cache (Next.js patch-fetch).
    assert resolve_fetch_policy(
        headers=[("Authorization", "Bearer x")], build_time=True).cached is True
    assert resolve_fetch_policy(method="POST", build_time=True).cached is True


def test_policy_default_follows_default_revalidate():
    assert resolve_fetch_policy().revalidate is False
    assert resolve_fetch_policy(default_revalidate=120).revalidate == 120
    assert resolve_fetch_policy(default_revalidate=False).revalidate is False
    with pytest.raises(ValueError):
        resolve_fetch_policy(revalidate=-1)
    with pytest.raises(ValueError):
        resolve_fetch_policy(revalidate=True)
    with pytest.raises(ValueError):
        resolve_fetch_policy(cache="only-cache")


# --- request memo ----------------------------------------------------------

def test_request_memo_scope_isolation_and_clear():
    with request_memo_scope() as memo:
        assert current_memo() is memo
        memo.set("k", 1)
        assert memo.get("k") == 1
        assert memoize_sync("s", lambda: 7) == 7
        assert memo.get("s") == 7
        memo.invalidate("k")
        assert memo.get("k") is None
        clear_request_memo()
        assert len(memo) == 0
    assert current_memo() is None


def test_request_memo_get_or_compute_dedups_concurrent_calls():
    async def run():
        memo = RequestMemo()
        calls = []

        async def compute():
            calls.append(1)
            await asyncio.sleep(0.05)
            return "v"

        results = await asyncio.gather(
            *(memo.get_or_compute("k", compute) for _ in range(5)))
        assert results == ["v"] * 5
        assert len(calls) == 1

        # A failed computation is not cached: the next call recomputes.
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("boom")
            return "ok"

        with pytest.raises(RuntimeError):
            await memo.get_or_compute("f", flaky)
        assert await memo.get_or_compute("f", flaky) == "ok"
        assert len(attempts) == 2

    asyncio.run(run())


def test_ensure_request_memo_is_per_context():
    """An ISR generation task runs in a fresh context: it gets its own
    memo instead of the request's (and vice versa)."""
    async def run():
        with request_memo_scope() as outer:
            outer.set("k", "outer")
            seen = {}

            async def in_fresh_context():
                memo = ensure_request_memo()
                seen["memo"] = memo
                seen["value"] = memo.get("k")

            await asyncio.create_task(in_fresh_context(), context=contextvars.Context())
            assert seen["memo"] is not outer
            assert seen["value"] is None
            assert outer.get("k") == "outer"

    asyncio.run(run())


def test_memoize_outside_a_scope_creates_one_for_the_context():
    async def run():
        calls = []

        async def compute():
            calls.append(1)
            return 42

        assert await memoize(("db", 1), compute) == 42
        assert await memoize(("db", 1), compute) == 42
        assert len(calls) == 1

    asyncio.run(run())


# --- fetch cache over ISR backends ------------------------------------------

class _CountingFetcher:
    def __init__(self, status=200, body=b'{"ok":true}'):
        self.calls = 0
        self.status = status
        self.body = body

    async def __call__(self, url, *, method="GET", headers=(), body=None, **_kwargs):
        self.calls += 1
        return FetchResult(self.status, (("content-type", "application/json"),), self.body)


def _run(coro):
    return asyncio.run(coro)


def test_fetch_cache_stores_and_serves_without_refetching():
    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher()
            # A fresh memo scope per "request": the second scope must be
            # served from the persistent cache, not the request memo.
            with request_memo_scope():
                first = await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
            assert fetcher.calls == 1
            with request_memo_scope():
                second = await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
            assert first.body == b'{"ok":true}' and first.status == 200
            assert second == first
            assert fetcher.calls == 1
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_does_not_serve_one_users_authorised_response_to_another():
    """P1-1 regression: with an explicit ``revalidate``, Alice's cached
    response must not be served to Bob. Different credentials get
    different keys, so Bob's request misses and fetches upstream."""

    class _TokenFetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, url, *, method="GET", headers=(), body=None, **_kwargs):
            self.calls += 1
            pairs = headers.items() if hasattr(headers, "items") else headers
            token = dict((k.lower(), v) for k, v in pairs).get("authorization", "?")
            return FetchResult(200, (), f"data-for-{token}".encode())

    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _TokenFetcher()
            with request_memo_scope():
                alice = await fetch.get_or_fetch(
                    FETCH_URL, fetcher, revalidate=60,
                    headers={"Authorization": "Bearer alice"})
            assert alice.body == b"data-for-Bearer alice"
            assert fetcher.calls == 1
            with request_memo_scope():
                bob = await fetch.get_or_fetch(
                    FETCH_URL, fetcher, revalidate=60,
                    headers={"Authorization": "Bearer bob"})
            assert bob.body == b"data-for-Bearer bob"
            assert fetcher.calls == 2
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_infinite_policy_stores_without_value_error():
    """P1-3 regression: the default, force-cache and revalidate=False
    policies (all infinite) store through the native L1 without
    ``ValueError: Invalid cache durations`` and serve the second
    request from the cache."""
    async def run():
        for kwargs in ({}, {"cache": "force-cache"}, {"revalidate": False}):
            cache = ISRCache()
            try:
                fetch = FetchCache(cache)
                fetcher = _CountingFetcher()
                with request_memo_scope():
                    first = await fetch.get_or_fetch(FETCH_URL, fetcher, **kwargs)
                assert first.status == 200
                assert fetcher.calls == 1
                with request_memo_scope():
                    second = await fetch.get_or_fetch(FETCH_URL, fetcher, **kwargs)
                assert second == first
                assert fetcher.calls == 1
            finally:
                await cache.close()

    _run(run())


def test_fetch_cache_dedups_concurrent_requests():
    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher()

            async def slow(url, **kwargs):
                await asyncio.sleep(0.05)
                return await fetcher(url, **kwargs)

            results = await asyncio.gather(
                *(fetch.get_or_fetch(FETCH_URL, slow, revalidate=60) for _ in range(4)))
            assert all(r.body == b'{"ok":true}' for r in results)
            assert fetcher.calls == 1
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_expires_and_refetches():
    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher()
            with request_memo_scope():
                await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=0.2)
            assert fetcher.calls == 1
            await asyncio.sleep(0.3)
            # A fresh memo scope (a new request) must not see the old
            # memo, so the expired entry is refetched.
            with request_memo_scope():
                await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=0.2)
            assert fetcher.calls == 2
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_never_stores_non_2xx_but_still_memoizes():
    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher(status=500, body=b"err")
            with request_memo_scope():
                first = await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
                second = await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
            assert first.status == 500 and second.status == 500
            assert fetcher.calls == 1  # memoized within the request
            await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
            assert fetcher.calls == 2  # not persisted: refetched
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_no_store_never_persists_but_memoizes_in_request():
    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher()
            with request_memo_scope():
                await fetch.get_or_fetch(FETCH_URL, fetcher, cache="no-store")
                await fetch.get_or_fetch(FETCH_URL, fetcher, cache="no-store")
            assert fetcher.calls == 1
            await fetch.get_or_fetch(FETCH_URL, fetcher, cache="no-store")
            assert fetcher.calls == 2
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_invalidate_removes_persistent_and_memo_entries():
    async def run():
        cache = ISRCache()
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher()
            with request_memo_scope():
                await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
                assert await fetch.invalidate(FETCH_URL) is True
                await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
                assert await fetch.invalidate(FETCH_URL) is True
            assert fetcher.calls == 2
            assert await fetch.invalidate(FETCH_URL) is False
        finally:
            await cache.close()

    _run(run())


def test_fetch_cache_shared_sqlite_backend_survives_new_instance(tmp_path):
    async def run():
        path = tmp_path / "fetch.sqlite3"
        cache = ISRCache(cache_backend="sqlite", cache_path=path)
        try:
            fetch = FetchCache(cache)
            fetcher = _CountingFetcher()
            with request_memo_scope():
                await fetch.get_or_fetch(FETCH_URL, fetcher, revalidate=60)
            assert fetcher.calls == 1
        finally:
            await cache.close()
        cache2 = ISRCache(cache_backend="sqlite", cache_path=path)
        try:
            fetch2 = FetchCache(cache2)
            fetcher2 = _CountingFetcher()
            with request_memo_scope():
                result = await fetch2.get_or_fetch(FETCH_URL, fetcher2, revalidate=60)
            assert result.body == b'{"ok":true}'
            assert fetcher2.calls == 0
        finally:
            await cache2.close()

    _run(run())


def test_fetch_cached_requires_a_request_context():
    async def run():
        assert get_fetch_cache() is None
        with pytest.raises(RuntimeError, match="no fetch cache installed"):
            await fetch_cached(FETCH_URL, _CountingFetcher())

    _run(run())


# --- app integration ---------------------------------------------------------

def _write_project(root):
    app_dir = root / "app"
    (app_dir / "api" / "memo").mkdir(parents=True)
    (app_dir / "api" / "memo" / "route.py").write_text(
        "from pynext import JSONResponse, memoize, route\n"
        "calls = 0\n"
        "@route('GET')\n"
        "async def handler():\n"
        "    global calls\n"
        "    async def compute():\n"
        "        global calls\n"
        "        calls += 1\n"
        "        return calls\n"
        "    first = await memoize(('db', 1), compute)\n"
        "    second = await memoize(('db', 1), compute)\n"
        "    return JSONResponse({'first': first, 'second': second, 'calls': calls})\n"
    )
    (app_dir / "page.py").write_text(
        "from pynext import FetchResult, fetch_cached, h, page\n"
        "calls = 0\n"
        "async def fetcher(url, **kwargs):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return FetchResult(200, (), b'upstream-data')\n"
        "@page()\n"
        "async def Page():\n"
        "    first = await fetch_cached('https://api.example.test/data', fetcher, revalidate=60)\n"
        "    second = await fetch_cached('https://api.example.test/data', fetcher, revalidate=60)\n"
        "    return h('main', {}, f\"{first.body.decode()}|{second.body.decode()}|calls={calls}\")\n"
    )
    (app_dir / "layout.py").write_text(
        "from pynext import FetchResult, fetch_cached, h, layout\n"
        "async def _layout_fetcher(url, **kwargs):\n"
        "    return FetchResult(200, (), b'upstream-data')\n"
        "@layout\n"
        "async def Layout(children):\n"
        "    result = await fetch_cached('https://api.example.test/data', _layout_fetcher, revalidate=60)\n"
        "    return h('div', {}, result.body.decode(), children)\n"
    )


def test_app_memoize_dedups_within_a_request_and_resets_between(tmp_path):
    _write_project(tmp_path)
    app = create_app(tmp_path)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/api/memo")
            assert first.json() == {"first": 1, "second": 1, "calls": 1}
            second = await client.get("/api/memo")
            assert second.json() == {"first": 2, "second": 2, "calls": 2}

    _run(run())


def test_app_fetch_cached_shared_by_layout_and_page_and_persisted(tmp_path):
    _write_project(tmp_path)
    app = create_app(tmp_path)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/")
            assert first.status_code == 200
            assert "upstream-data|upstream-data|calls=1" in first.text
            second = await client.get("/")
            assert second.status_code == 200
            # The layout's fetcher and the page's fetcher both ran once
            # on the first request only; the second request hit the
            # persistent fetch cache (and the per-request memo).
            assert "upstream-data|upstream-data|calls=1" in second.text

    _run(run())


# --- urllib_fetcher body cap (A-3) -----------------------------------------

class _CappedBodyHandler(http.server.BaseHTTPRequestHandler):
    """Serves a fixed body with a configurable status (class attributes)."""

    body = b""
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def _local_http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CappedBodyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_urllib_fetcher_rejects_body_over_cap(_local_http_server):
    """A-3: a body over MAX_CACHE_BODY_BYTES must raise instead of being
    buffered whole — before the fix a 64 MiB upstream body was read
    fully into memory (RSS +~100 MiB) and only rejected afterwards."""
    _CappedBodyHandler.body = b"x" * (MAX_CACHE_BODY_BYTES + 1024)
    _CappedBodyHandler.status = 200
    with pytest.raises(ValueError, match="exceeds the .*byte cap"):
        asyncio.run(urllib_fetcher(_local_http_server))


def test_urllib_fetcher_accepts_body_at_cap(_local_http_server):
    """A-3: a body exactly at the cap still fetches normally."""
    _CappedBodyHandler.body = b"y" * MAX_CACHE_BODY_BYTES
    _CappedBodyHandler.status = 200
    result = asyncio.run(urllib_fetcher(_local_http_server))
    assert result.status == 200
    assert len(result.body) == MAX_CACHE_BODY_BYTES


def test_urllib_fetcher_caps_error_body(_local_http_server):
    """A-3: the HTTPError branch (exc.read) is capped the same way."""
    _CappedBodyHandler.body = b"z" * (MAX_CACHE_BODY_BYTES + 1024)
    _CappedBodyHandler.status = 500
    with pytest.raises(ValueError, match="exceeds the .*byte cap"):
        asyncio.run(urllib_fetcher(_local_http_server))
