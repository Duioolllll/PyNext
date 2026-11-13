"""Regression tests for RequestMemo (track C, PR #26 finding P2-3).

P2-3: a computation started before ``clear()`` used to store its value
back into the memo when it finished, resurrecting the pre-clear value.
``clear()`` now bumps a generation counter and completions from an older
generation are discarded instead of stored.
"""

import asyncio

from pynext.request_memo import RequestMemo


def test_clear_invalidates_inflight_computation():
    """Mirror of the P2-3 repro: clear, then the old compute finishes.

    After the clear the memo must stay empty and the next read must
    compute a fresh value, not see the pre-clear one.
    """

    async def run():
        memo = RequestMemo()
        started, resume = asyncio.Event(), asyncio.Event()

        async def old():
            started.set()
            await resume.wait()
            return "old"

        pending = asyncio.create_task(memo.get_or_compute("key", old))
        await started.wait()
        memo.clear()
        assert memo.get("key") is None

        resume.set()
        assert await pending == "old"  # the awaiting caller still gets its value
        assert memo.get("key") is None  # ... but it must not come back into the memo

        async def new():
            return "new"

        assert await memo.get_or_compute("key", new) == "new"
        assert memo.get("key") == "new"

    asyncio.run(run())


def test_old_inflight_compute_does_not_clobber_post_clear_value():
    """A compute started after clear() must keep its stored value even
    when the pre-clear computation finishes afterwards."""

    async def run():
        memo = RequestMemo()
        started, resume = asyncio.Event(), asyncio.Event()

        async def old():
            started.set()
            await resume.wait()
            return "old"

        pending = asyncio.create_task(memo.get_or_compute("key", old))
        await started.wait()
        memo.clear()

        async def new():
            return "new"

        assert await memo.get_or_compute("key", new) == "new"
        assert memo.get("key") == "new"

        resume.set()
        assert await pending == "old"
        assert memo.get("key") == "new"

    asyncio.run(run())


def test_clear_drops_cached_value_and_recompute_stores():
    """Basic clear contract: cached values go away, later computes store."""

    async def run():
        memo = RequestMemo()
        assert await memo.get_or_compute("key", _return("first")) == "first"
        memo.clear()
        assert memo.get("key") is None
        assert await memo.get_or_compute("key", _return("second")) == "second"
        assert memo.get("key") == "second"

    asyncio.run(run())


def test_concurrent_callers_share_one_computation():
    """Unchanged behaviour guard: concurrent calls for the same key
    still share one in-flight computation."""

    async def run():
        memo = RequestMemo()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return "shared"

        first, second = await asyncio.gather(
            memo.get_or_compute("key", compute),
            memo.get_or_compute("key", compute),
        )
        assert first == second == "shared"
        assert calls == 1
        assert memo.get("key") == "shared"

    asyncio.run(run())


def _return(value):
    async def compute():
        return value

    return compute


def test_set_during_inflight_compute_is_not_clobbered_a6_l4():
    """A6-L4: a value set() while a computation is in flight must win.

    Before the fix, get_or_compute stored its result unconditionally on
    completion, so a slow computation overwrote a newer explicit set()
    (read-your-own-writes violation). The awaiting caller still gets the
    computed value; only the memo keeps the set value.
    """

    async def run():
        memo = RequestMemo()
        started, resume = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await resume.wait()
            return "STALE-COMPUTED"

        pending = asyncio.create_task(memo.get_or_compute("k", slow))
        await started.wait()
        memo.set("k", "FRESH-SET")
        assert memo.get("k") == "FRESH-SET"

        resume.set()
        assert await pending == "STALE-COMPUTED"
        assert memo.get("k") == "FRESH-SET"

    asyncio.run(run())


def test_invalidate_during_inflight_compute_is_not_resurrected_a6_l4():
    """Same generation guard as A6-L4, for invalidate(): a computation
    in flight when its key is invalidated must not store back."""

    async def run():
        memo = RequestMemo()
        started, resume = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await resume.wait()
            return "STALE-COMPUTED"

        pending = asyncio.create_task(memo.get_or_compute("k", slow))
        await started.wait()
        assert memo.invalidate("k") is True
        assert memo.get("k") is None

        resume.set()
        assert await pending == "STALE-COMPUTED"
        assert memo.get("k") is None

    asyncio.run(run())


def test_set_without_inflight_compute_leaves_other_computes_alone():
    """A set() that displaces no computation must not disturb an
    in-flight computation for another key."""

    async def run():
        memo = RequestMemo()
        started, resume = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await resume.wait()
            return "computed-j"

        pending = asyncio.create_task(memo.get_or_compute("j", slow))
        await started.wait()
        memo.set("k", "fresh-k")
        resume.set()
        assert await pending == "computed-j"
        assert memo.get("j") == "computed-j"
        assert memo.get("k") == "fresh-k"

    asyncio.run(run())


def test_memo_entries_are_bounded_a6_l5():
    """A6-L5: entries past MAX_ENTRIES evict the oldest first.

    A memo that never resets (memoize() outside a request scope) must
    not grow without bound.
    """
    memo = RequestMemo()
    for i in range(RequestMemo.MAX_ENTRIES + 50):
        memo.set(f"key-{i}", i)
    assert len(memo._values) == RequestMemo.MAX_ENTRIES
    assert memo.get("key-0") is None
    assert memo.get("key-49") is None
    assert memo.get("key-50") == 50
    assert memo.get(f"key-{RequestMemo.MAX_ENTRIES + 49}") == RequestMemo.MAX_ENTRIES + 49


def test_get_or_compute_respects_entry_bound_a6_l5():
    """A6-L5: a completed computation also evicts oldest first."""

    async def run():
        memo = RequestMemo()
        for i in range(RequestMemo.MAX_ENTRIES):
            memo.set(f"key-{i}", i)
        assert await memo.get_or_compute("new-key", _return("new")) == "new"
        assert len(memo._values) == RequestMemo.MAX_ENTRIES
        assert memo.get("key-0") is None
        assert memo.get("new-key") == "new"

    asyncio.run(run())


def test_memoize_outside_request_scope_is_bounded_a6_l5():
    """A6-L5: the review's repro — memoize() outside a request scope
    creates a memo that is never reset; its entries must still be
    bounded. Runs in a fresh context so the leaked memo cannot pollute
    other tests.
    """
    import contextvars

    from pynext.request_memo import current_memo, memoize_sync

    def run():
        for i in range(RequestMemo.MAX_ENTRIES + 10):
            memoize_sync(f"key-{i}", lambda i=i: i)
        memo = current_memo()
        assert memo is not None
        assert len(memo._values) == RequestMemo.MAX_ENTRIES
        assert memo.get("key-0") is None

    contextvars.Context().run(run)
