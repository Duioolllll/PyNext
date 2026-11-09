"""Bounded ASGI rate-limit decisions, independent of handler execution."""
import asyncio
from collections.abc import Callable
import contextvars
import threading
import time
from typing import TypeVar

_T = TypeVar("_T")


class DecisionUnavailable(RuntimeError):
    """No decision capacity, deadline exceeded, or executor unavailable."""


class DecisionGate:
    def __init__(self, capacity: int = 64, timeout: float = 2.0) -> None:
        self.capacity = capacity
        self.timeout = timeout
        self._active = 0
        self._lock = threading.Lock()

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._active

    async def check(self, callback: Callable[[], _T]) -> _T:
        with self._lock:
            if self._active >= self.capacity:
                raise DecisionUnavailable("Rate-limit decision capacity exhausted")
            self._active += 1
        deadline = time.monotonic() + self.timeout
        abandoned = threading.Event()

        def release() -> None:
            with self._lock:
                self._active -= 1

        def work() -> _T:
            try:
                # A request can expire while queued in the host executor.
                # Never start a stale callback and charge a new token then.
                if abandoned.is_set() or time.monotonic() >= deadline:
                    raise DecisionUnavailable("Rate-limit decision deadline exceeded")
                return callback()
            finally:
                # A cancelled awaiter cannot release a running thread's slot.
                release()

        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, contextvars.copy_context().run, work)
        except BaseException:
            release()
            raise

        def consume(done: asyncio.Future[_T]) -> None:
            if not done.cancelled():
                done.exception()  # a timed-out caller cannot retrieve late failures

        future.add_done_callback(consume)
        try:
            return await asyncio.wait_for(asyncio.shield(future), max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            abandoned.set()
            raise DecisionUnavailable("Rate-limit decision deadline exceeded") from exc
        except asyncio.CancelledError:
            abandoned.set()
            raise
