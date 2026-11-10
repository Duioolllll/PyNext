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
    """Bounded gate for one admission stage (C-P1-7).

    ``name`` identifies the stage in :class:`DecisionUnavailable`
    messages and logs, so a 503 from this gate ("rate-limit
    decision") is distinguishable from a 503 from the Rust slot
    wait queue ("admission capacity") — the two are different
    stages, even though both answer 503 + ``Retry-After``.
    """

    def __init__(self, capacity: int = 64, timeout: float = 2.0, name: str = "decision") -> None:
        self.capacity = capacity
        self.timeout = timeout
        self.name = name
        self._active = 0
        self._lock = threading.Lock()

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._active

    async def check(self, callback: Callable[[], _T]) -> _T:
        with self._lock:
            if self._active >= self.capacity:
                raise DecisionUnavailable(f"{self.name} capacity exhausted")
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
                    raise DecisionUnavailable(f"{self.name} deadline exceeded")
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
            raise DecisionUnavailable(f"{self.name} deadline exceeded") from exc
        except asyncio.CancelledError:
            abandoned.set()
            raise
