"""Single-process ISR coordination around a bounded Rust page store."""
import asyncio
import contextvars
import logging

from . import _native

log = logging.getLogger("pynext")
MAX_INFLIGHT = 128
GENERATION_TIMEOUT = 30.0


class CacheBusy(Exception):
    pass


class ISRCache:
    def __init__(self):
        self.store = _native.PageCache()
        self._inflight = {}
        self._tasks = set()
        self._loop = None
        self._closed = False

    def open(self):
        self._closed = False

    async def close(self):
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._tasks.clear()
        self._loop = None

    async def invalidate(self, key):
        if self._closed:
            raise CacheBusy("ISR cache is shutting down")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("An ISR app instance must use a single event loop")
        retired = self._inflight.pop(key, None)
        return self.store.remove(key) or retired is not None

    async def get_or_render(self, key, policy, render):
        loop = asyncio.get_running_loop()
        if self._closed:
            raise CacheBusy("ISR cache is shutting down")
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("An ISR app instance must use a single event loop")
        status, body = self.store.lookup(key)
        if status == "fresh":
            return body
        task = self._inflight.get(key)
        if task is None and len(self._tasks) < MAX_INFLIGHT:
            async def generate():
                async with asyncio.timeout(GENERATION_TIMEOUT):
                    response = await render()
                    if response.status != 200:
                        raise ValueError("ISR can cache only successful page responses")
                    # An invalidated generation may finish for existing callers,
                    # but must never repopulate the cache after invalidation.
                    if self._inflight.get(key) is asyncio.current_task():
                        if not self.store.insert(key, response.body, policy["revalidate"], policy["stale"]):
                            log.warning("ISR cache insert failed for %s; page was served but not cached", key)
                    return response.body
            # Request-local context must not leak into a publicly shared render.
            task = asyncio.create_task(generate(), context=contextvars.Context())
            self._inflight[key] = task
            self._tasks.add(task)

            def completed(finished):
                self._tasks.discard(finished)
                if self._inflight.get(key) is finished:
                    self._inflight.pop(key, None)
                if not finished.cancelled():
                    error = finished.exception()
                    if error is not None:
                        log.error("ISR regeneration failed for %s", key,
                                  exc_info=(type(error), error, error.__traceback__))
            task.add_done_callback(completed)
        if status == "stale":
            return body
        if task is None:
            raise CacheBusy("Too many ISR generations")
        # A cancelled request must not cancel the shared render for other callers.
        return await asyncio.shield(task)
