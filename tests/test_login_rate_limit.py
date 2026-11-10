"""Login rate limiting (ROADMAP S-P1-6): 5 consecutive failures from one
(IP, account) pair block that pair for 15 minutes; a success resets the
count. Tests use injected monotonic times so block expiry is
deterministic — no real 15-minute waits.
"""

import threading

import pytest

from pynext.auth import LOGIN_BLOCK_SECONDS, LOGIN_MAX_FAILURES, LoginRateLimiter

IP = "203.0.113.7"
OTHER_IP = "198.51.100.2"
ACCOUNT = "alice@example.com"


def test_five_failures_then_blocked_on_the_sixth():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES - 1):
        assert limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt) is None
        assert not limiter.is_blocked(IP, ACCOUNT, now=1000.0 + attempt)
    # The 5th failure trips the block; the 6th attempt is refused.
    assert limiter.record_failure(IP, ACCOUNT, now=1004.0) == LOGIN_BLOCK_SECONDS
    assert limiter.is_blocked(IP, ACCOUNT, now=1005.0)
    assert limiter.retry_after(IP, ACCOUNT, now=1005.0) == LOGIN_BLOCK_SECONDS - 1
    blocked = limiter.check_login(IP, ACCOUNT, now=1005.0)
    assert blocked is not None
    assert blocked.status == 429
    assert dict(blocked.headers)[b"retry-after"] == b"899"
    # A failure recorded while blocked does not extend the block.
    assert limiter.record_failure(IP, ACCOUNT, now=1010.0) == LOGIN_BLOCK_SECONDS - 6
    assert limiter.retry_after(IP, ACCOUNT, now=1010.0) == LOGIN_BLOCK_SECONDS - 6


def test_block_returns_retry_after_from_the_tripping_failure():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES - 1):
        assert limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt) is None
    assert limiter.record_failure(IP, ACCOUNT, now=1004.0) == LOGIN_BLOCK_SECONDS


def test_block_expires_after_fifteen_minutes_and_attempts_work_again():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES):
        limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt)
    assert limiter.is_blocked(IP, ACCOUNT, now=1004.0 + LOGIN_BLOCK_SECONDS - 1)
    # Exactly at the block's end the pair may try again.
    assert not limiter.is_blocked(IP, ACCOUNT, now=1004.0 + LOGIN_BLOCK_SECONDS)
    assert limiter.check_login(IP, ACCOUNT, now=1004.0 + LOGIN_BLOCK_SECONDS) is None
    assert limiter.retry_after(IP, ACCOUNT, now=1004.0 + LOGIN_BLOCK_SECONDS) is None
    # And a failure after expiry starts a fresh count, not a re-block.
    assert limiter.record_failure(IP, ACCOUNT, now=1004.0 + LOGIN_BLOCK_SECONDS) is None
    assert limiter.stats()["blocked"] == 0


def test_success_resets_the_failure_count():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES - 1):
        limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt)
    limiter.record_success(IP, ACCOUNT)
    assert not limiter.is_blocked(IP, ACCOUNT, now=1010.0)
    # Four more failures after the reset still do not block.
    for attempt in range(LOGIN_MAX_FAILURES - 1):
        assert limiter.record_failure(IP, ACCOUNT, now=1010.0 + attempt) is None
    assert not limiter.is_blocked(IP, ACCOUNT, now=1020.0)
    # The fifth consecutive failure after the reset does block.
    assert limiter.record_failure(IP, ACCOUNT, now=1020.0) == LOGIN_BLOCK_SECONDS


def test_normal_login_is_never_blocked():
    limiter = LoginRateLimiter()
    for attempt in range(50):
        assert limiter.check_login(IP, ACCOUNT, now=1000.0 + attempt) is None
        limiter.record_success(IP, ACCOUNT)
    assert limiter.stats() == {"keys": 0, "max_keys": 10_000, "blocked": 0}


def test_block_is_scoped_to_the_ip_and_account_pair():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES):
        limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt)
    # Same account, different IP: not blocked.
    assert not limiter.is_blocked(OTHER_IP, ACCOUNT, now=1010.0)
    assert limiter.check_login(OTHER_IP, ACCOUNT, now=1010.0) is None
    # Same IP, different account: not blocked.
    assert not limiter.is_blocked(IP, "bob@example.com", now=1010.0)
    # The blocked pair stays blocked.
    assert limiter.is_blocked(IP, ACCOUNT, now=1010.0)


def test_account_key_is_case_insensitive_and_trims_whitespace():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES):
        limiter.record_failure(IP, "  Alice@Example.com ", now=1000.0 + attempt)
    assert limiter.is_blocked(IP, "alice@example.com", now=1010.0)
    assert limiter.is_blocked(IP, "ALICE@EXAMPLE.COM", now=1010.0)


def test_stale_failures_do_not_count_after_idle_window():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES - 1):
        limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt)
    # One failure after a full idle window starts a fresh count.
    assert limiter.record_failure(IP, ACCOUNT, now=1000.0 + LOGIN_BLOCK_SECONDS + 100) is None
    assert not limiter.is_blocked(IP, ACCOUNT, now=1000.0 + LOGIN_BLOCK_SECONDS + 100)


def test_bounded_table_evicts_stalest_unblocked_pair_first():
    limiter = LoginRateLimiter(max_keys=3)
    limiter.record_failure("10.0.0.1", "a", now=1000.0)
    limiter.record_failure("10.0.0.2", "b", now=1001.0)
    limiter.record_failure("10.0.0.3", "c", now=1002.0)
    # A fourth pair evicts the stalest (10.0.0.1/a), not a blocked one.
    limiter.record_failure("10.0.0.4", "d", now=1003.0)
    assert limiter.stats()["keys"] == 3
    assert limiter.retry_after("10.0.0.1", "a", now=1003.0) is None
    assert limiter.retry_after("10.0.0.2", "b", now=1003.0) is None


def test_concurrent_failures_cannot_slip_under_the_limit():
    limiter = LoginRateLimiter()
    results = []

    def fail_many():
        for _ in range(10):
            results.append(limiter.record_failure(IP, ACCOUNT))

    threads = [threading.Thread(target=fail_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Exactly the four pre-trip failures report None; every failure
    # after the trip reports the block instead of slipping through.
    assert sum(1 for item in results if item is None) == LOGIN_MAX_FAILURES - 1
    assert all(item is None or item >= 1 for item in results)
    assert limiter.is_blocked(IP, ACCOUNT)
    assert limiter.stats()["blocked"] == 1


@pytest.mark.parametrize("max_failures,block_seconds", [(0, 900), (-1, 900), (1.5, 900),
                                                        ("5", 900), (5, 0), (5, -1),
                                                        (5, float("inf")), (5, float("nan"))])
def test_limiter_rejects_invalid_config(max_failures, block_seconds):
    with pytest.raises(ValueError):
        LoginRateLimiter(max_failures, block_seconds)


def test_custom_limits_are_honoured():
    limiter = LoginRateLimiter(max_failures=2, block_seconds=60)
    assert limiter.record_failure(IP, ACCOUNT, now=1000.0) is None
    assert limiter.record_failure(IP, ACCOUNT, now=1001.0) == 60
    assert limiter.is_blocked(IP, ACCOUNT, now=1030.0)
    assert not limiter.is_blocked(IP, ACCOUNT, now=1061.0)


def test_check_login_response_is_not_cacheable():
    limiter = LoginRateLimiter()
    for attempt in range(LOGIN_MAX_FAILURES):
        limiter.record_failure(IP, ACCOUNT, now=1000.0 + attempt)
    blocked = limiter.check_login(IP, ACCOUNT, now=1010.0)
    assert dict(blocked.headers)[b"cache-control"] == b"private, no-store"
    assert limiter.check_login(IP, ACCOUNT, now=1010.0).body == b"Too Many Login Attempts"
