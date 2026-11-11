"""Per-request memoization (track C P1-2).

Next.js keeps a Resume Data Cache (``server/resume-data-cache/``) that
memoizes ``fetch()`` and ``'use cache'`` results for the duration of one
render: the same data read twice in one render is computed once, with no
TTL — the memo dies with the render. PyNext has no React/RSC, so only
the idea travels: a request-scoped memo keyed by caller-chosen keys.

A memo lives in a :mod:`contextvars` variable, so each request (and each
ISR generation task, which runs in its own context) gets its own memo:

* :func:`request_memo_scope` — installed by :meth:`App.dispatch` around
  every request; page handlers, API handlers and middleware share it.
* :func:`ensure_request_memo` — a render running in a fresh context (an
  ISR generation task) gets a per-render memo instead, which is
  equivalent for de-duplication: one render is one request's page
  generation, and a background stale regeneration gets its own memo.

Rules (mirroring ``IncrementalCache.get`` bypassing the RDC after
``updateTag``):

* Values are cached for the request only; there is no TTL and no
  cross-request sharing. Use :mod:`pynext.fetch_cache` for that.
* Concurrent ``get_or_compute`` calls for the same key share one
  in-flight computation (``asyncio.shield`` so one canceller cannot kill
  the shared compute). A failed computation is *not* cached: the entry
  is dropped and the next call recomputes.
* Invalidation inside the request (``App.revalidate`` /
  ``Request.revalidate``) clears the whole current memo, so a handler
  that mutates data and then reads it back never sees its own stale
  memo (read-your-own-writes). Callers that know exactly which key went
  stale can use :meth:`RequestMemo.invalidate` instead.

Keys are caller-chosen and must be hashable; a good key names the
source and its inputs, e.g. ``("fetch", url, method)`` or
``("db", "select user where id=?", user_id)``. Two different queries
must never share a key.
"""
import asyncio
import contextvars
from contextlib import contextmanager

__all__ = [
    "RequestMemo",
    "request_memo_scope",
    "current_memo",
    "ensure_request_memo",
    "clear_request_memo",
    "memoize",
    "memoize_sync",
]

_request_memo_var = contextvars.ContextVar("pynext_request_memo", default=None)


class RequestMemo:
    """One request's memo table: values plus in-flight computations."""

    def __init__(self):
        self._values = {}
        self._inflight = {}
        # Bumped by clear(): completions from an older generation must
        # not store their value back into the memo (P2-3).
        self._generation = 0

    def __contains__(self, key):
        return key in self._values or key in self._inflight

    def __len__(self):
        return len(self._values) + len(self._inflight)

    def get(self, key, default=None):
        """Return the memoized value for ``key``, or ``default``."""
        return self._values.get(key, default)

    def set(self, key, value):
        """Memoize ``value`` under ``key`` (overwrites any prior value)."""
        self._values[key] = value
        self._inflight.pop(key, None)

    def invalidate(self, key):
        """Drop one memoized key (value and in-flight compute, if any)."""
        return self._values.pop(key, None) is not None or self._inflight.pop(key, None) is not None

    def clear(self):
        """Drop every memoized value. See the module docstring for when
        the framework does this (in-request invalidation).

        In-flight computations started before the clear are invalidated
        too: the generation is bumped, so when they finish their value
        is discarded instead of stored back into the memo (P2-3 — a
        pre-clear value must not resurrect after the clear).
        """
        self._values.clear()
        self._inflight.clear()
        self._generation += 1

    async def get_or_compute(self, key, compute):
        """Return the memoized value, computing it once if absent.

        ``compute`` is a zero-argument async callable. Concurrent calls
        for the same key await the same task; a failure is not cached.
        A computation that was in flight when :meth:`clear` ran is
        discarded instead of stored, so a pre-clear value never comes
        back after the clear.
        """
        if key in self._values:
            return self._values[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(compute())
            self._inflight[key] = task
        generation = self._generation
        try:
            value = await asyncio.shield(task)
        except BaseException:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
            raise
        if generation != self._generation:
            # A clear() happened while this computation was in flight.
            # The awaiting caller still gets the computed value, but it
            # must not be stored back into the memo.
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
            return value
        self._values[key] = value
        self._inflight.pop(key, None)
        return value

    def get_or_compute_sync(self, key, compute):
        """Synchronous variant of :meth:`get_or_compute`."""
        if key in self._values:
            return self._values[key]
        value = compute()
        self._values[key] = value
        return value


@contextmanager
def request_memo_scope():
    """Install a fresh memo for the current context; reset it on exit."""
    memo = RequestMemo()
    token = _request_memo_var.set(memo)
    try:
        yield memo
    finally:
        _request_memo_var.reset(token)


def current_memo():
    """Return the memo for the current context, or ``None``."""
    return _request_memo_var.get()


def ensure_request_memo():
    """Return the current memo, creating one in this context if absent.

    Used by renders that run in a fresh task context (ISR generation),
    where no request scope is visible: they get a per-render memo.
    """
    memo = _request_memo_var.get()
    if memo is None:
        memo = RequestMemo()
        _request_memo_var.set(memo)
    return memo


def clear_request_memo():
    """Drop every value in the current memo, if one exists.

    Called by in-request invalidation so later reads in the same
    request recompute instead of seeing pre-mutation data.
    """
    memo = _request_memo_var.get()
    if memo is not None:
        memo.clear()


async def memoize(key, compute):
    """Memoize one async computation in the current request's memo."""
    return await ensure_request_memo().get_or_compute(key, compute)


def memoize_sync(key, compute):
    """Memoize one synchronous computation in the current request's memo."""
    return ensure_request_memo().get_or_compute_sync(key, compute)
