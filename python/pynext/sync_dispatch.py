"""Sync/async dispatch: one classification, one sync entry, one exit lane.

C-P1-3 / C-P1-4 (ROADMAP.md §1.1). Two rules live here:

1. **Classify once, at registration.** :func:`is_async_callable` is the
   Starlette verdict (``starlette/_utils.py`` — unwrap ``functools.partial``,
   then coroutine function or coroutine ``__call__``); :func:`cache_call_kind`
   stamps the verdict onto the callable itself as ``__pynext_call_kind__``,
   next to the ``__pynext_page__`` / ``__pynext_layout__`` / ``__pynext_method__``
   markers the decorators already set. Request handling reads the stamp via
   :func:`cached_call_kind` instead of running ``inspect`` on every request.
   Callables that cannot carry an attribute (bound methods, ``__slots__``
   instances) simply fall back to classifying on each call.

2. **Run sync work on a worker thread with the caller's context.**
   :func:`run_sync_with_context` is Litestar's ``sync_to_thread`` shape
   (``litestar/concurrency.py:30-40``): ``contextvars.copy_context()`` plus
   ``ctx.run`` around the callable, executed with ``run_in_executor``. The
   copy keeps request scope (request id, tracing, logging contextvars)
   visible in the worker, and mutations the worker makes to a contextvar
   never leak back into the caller. ``asyncio.to_thread`` did this
   implicitly; spelling it out keeps the guarantee when the executor
   changes — C-P1-1 moves entry work onto the app-owned sync pool with the
   same call shape, and the exit lane (``App._run_exit_sync``) uses it on
   its own single-thread executor.
"""
import asyncio
import contextvars
import functools
import inspect

CALL_SYNC = "sync"
CALL_ASYNC = "async"
CALL_ASYNC_GEN = "asyncgen"

_CALL_KIND_ATTR = "__pynext_call_kind__"
_KINDS = frozenset({CALL_SYNC, CALL_ASYNC, CALL_ASYNC_GEN})


def is_async_callable(fn):
    """Return whether ``fn`` is an async callable (Starlette's verdict).

    Unwraps ``functools.partial`` first, then accepts a coroutine function
    or any callable whose ``__call__`` is one. A plain class is sync: its
    ``__call__`` is ``type.__call__``, not a coroutine function.
    """
    while isinstance(fn, functools.partial):
        fn = fn.func
    return inspect.iscoroutinefunction(fn) or (
        callable(fn) and inspect.iscoroutinefunction(getattr(fn, "__call__", None)))


def _is_asyncgen_callable(fn):
    while isinstance(fn, functools.partial):
        fn = fn.func
    return inspect.isasyncgenfunction(fn)


def classify_callable(fn):
    """Classify ``fn`` as ``"sync"``, ``"async"`` or ``"asyncgen"``.

    An async generator function is its own kind: calling it only builds
    the iterator, so it must not go through the sync worker path the way
    a plain sync function does (see ``App._render_page_inner``).
    """
    if _is_asyncgen_callable(fn):
        return CALL_ASYNC_GEN
    return CALL_ASYNC if is_async_callable(fn) else CALL_SYNC


def cache_call_kind(fn):
    """Classify ``fn`` once and stamp the verdict on it; return the kind.

    Best-effort: callables that cannot carry an attribute (bound methods,
    ``__slots__`` instances) keep working — :func:`cached_call_kind` just
    classifies them again on each call.
    """
    kind = classify_callable(fn)
    try:
        setattr(fn, _CALL_KIND_ATTR, kind)
    except (AttributeError, TypeError):
        pass
    return kind


def cached_call_kind(fn):
    """Return ``fn``'s registered kind, classifying and stamping if new."""
    kind = getattr(fn, _CALL_KIND_ATTR, None)
    if kind in _KINDS:
        return kind
    return cache_call_kind(fn)


async def run_sync_with_context(fn, *args, executor=None, **kwargs):
    """Run sync ``fn`` on a worker thread inside a copy of this context.

    ``executor=None`` means the loop's default executor. The context is
    copied before the hop, so the worker sees the caller's contextvars
    (request id, tracing) and its own writes stay in the copy.
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        executor, functools.partial(ctx.run, fn, *args, **kwargs))
