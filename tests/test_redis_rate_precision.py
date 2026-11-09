"""Redis rate limiter: microsecond-tie claim and double-precision limits.

Review #4 #18: the Lua script claimed two scripts could never run in
the same microsecond, and the precision limit for huge windows was
undocumented. No Redis server is needed for these checks — they pin
the script wording and verify the arithmetic the documentation states.
"""
import datetime
import math

from pynext.redis_rate import _ALLOW


def test_script_no_longer_claims_microsecond_ties_are_impossible():
    assert "never run in the same microsecond" not in _ALLOW
    assert "never happens" not in _ALLOW
    # The replacement wording states the tie is possible and how it is
    # handled: deterministically, by member order, without denying.
    assert "still possible in principle" in _ALLOW
    assert "deterministically" in _ALLOW
    assert "never denied" in _ALLOW


def test_microsecond_scores_stay_exact_through_the_maximum_window():
    """Integer µs scores are exact from now through now + 1-year window.

    The review brief's figure of ~3.3e16 µs (ulp 8 µs) does not match
    the arithmetic: the epoch in microseconds is ~1.79e15, so now plus
    the maximum one-year window is ~1.83e15 µs with a 0.25 µs ulp —
    integer microseconds stay exact until 2^53 µs, around year 2255.
    """
    now_us = int(datetime.datetime(2026, 11, 9, tzinfo=datetime.timezone.utc).timestamp() * 1e6)
    max_window_us = 31_536_000 * 10**6  # the validated maximum window
    assert now_us + max_window_us < 2**53
    assert math.ulp(float(now_us + max_window_us)) == 0.25
    assert float(now_us + max_window_us) == now_us + max_window_us
    assert datetime.datetime.fromtimestamp(2**53 / 1e6, datetime.timezone.utc).year == 2255


def test_redis_rate_limiter_rejects_windows_over_one_year():
    """The one-year window cap the precision note relies on is enforced."""
    import pytest
    pytest.importorskip("redis")

    from pynext import RedisRateLimiter

    with pytest.raises(ValueError):
        RedisRateLimiter(10, 31_536_001, url="redis://127.0.0.1:6390/0",
                         namespace="precision-test")
    # Exactly one year is accepted (construction fails only on the URL
    # scheme here — the window itself passes validation).
    with pytest.raises(ValueError, match="Redis rate limiting requires a Redis URL"):
        RedisRateLimiter(10, 31_536_000, url="not-a-redis-url",
                         namespace="precision-test")
