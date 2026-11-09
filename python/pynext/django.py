"""Optional Django ORM/form bridge; no Django HTTP or template integration."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import inspect
import logging
import os
import threading


_setup_lock = threading.RLock()
_log = logging.getLogger(__name__)


class DjangoBusyError(RuntimeError):
    """The bounded Django work queue is full."""


class DjangoResource:
    """Initialize Django models and run synchronous ORM/form work off-loop.

    Django has one process-wide settings configuration. Initialization occurs
    at construction so route modules may import models while PyNext loads them.
    The application lifespan controls a dedicated worker and bounded queue.
    """

    def __init__(self, settings_module, *, max_pending=32):
        if not isinstance(settings_module, str) or not settings_module.strip():
            raise ValueError("settings_module must be a non-empty module name")
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or not 1 <= max_pending <= 10000:
            raise ValueError("max_pending must be an integer between 1 and 10000")
        try:
            import django
            from django.apps import apps
            from django.conf import settings
        except ImportError as exc:
            raise RuntimeError("install pynext-framework[django] to use DjangoResource") from exc

        with _setup_lock:
            existing = os.environ.get("DJANGO_SETTINGS_MODULE")
            if existing and existing != settings_module:
                raise RuntimeError("Django is configured with a different settings module")
            if settings.configured and getattr(settings, "SETTINGS_MODULE", None) != settings_module:
                raise RuntimeError("Django is already configured with different settings")
            changed = not existing
            if changed:
                os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
            try:
                if not apps.ready:
                    django.setup()
            except BaseException:
                if changed and not settings.configured:
                    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
                raise
        self._slots = threading.BoundedSemaphore(max_pending)
        self._executor = None
        self._worker_ident = None
        self._lifecycle_lock = asyncio.Lock()

    async def startup(self):
        async with self._lifecycle_lock:
            if self._executor is not None:
                raise RuntimeError("Django resource is already started")
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pynext-django")

    async def shutdown(self):
        async with self._lifecycle_lock:
            executor, self._executor = self._executor, None
            if executor is None:
                return
            # Running synchronous functions cannot be cancelled safely. Wait
            # for their thread to close its own Django connections before exit.
            closing = asyncio.create_task(asyncio.to_thread(executor.shutdown, wait=True))
            cancelled = False
            try:
                while True:
                    try:
                        await asyncio.shield(closing)
                        break
                    except asyncio.CancelledError:
                        if closing.cancelled():
                            raise
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                self._worker_ident = None

    async def run(self, fn, /, *args, **kwargs):
        """Run one synchronous callable; return materialized, non-lazy data."""
        if not callable(fn) or inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError("DjangoResource.run requires a synchronous callable")
        if self._worker_ident == threading.get_ident():
            raise RuntimeError("DjangoResource.run cannot be nested on its own worker")
        executor = self._executor
        if executor is None:
            raise RuntimeError("Django resource has not started")
        if not self._slots.acquire(blocking=False):
            raise DjangoBusyError("Django resource work queue is full")
        try:
            future = executor.submit(self._execute, fn, args, kwargs)
        except BaseException:
            self._slots.release()
            raise
        # The slot belongs to the underlying work, not the awaiting request:
        # cancellation cannot free capacity while a thread is still executing.
        future.add_done_callback(lambda _: self._slots.release())
        return await asyncio.wrap_future(future)

    def _execute(self, fn, args, kwargs):
        from django.db import close_old_connections, connections
        from django.db.models import Model, QuerySet

        self._worker_ident = threading.get_ident()
        try:
            close_old_connections()
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("DjangoResource.run callable must return a synchronous value")
            if inspect.isasyncgen(result) or isinstance(result, (Model, QuerySet, Iterator)):
                if inspect.isgenerator(result):
                    result.close()
                raise TypeError("materialize lazy Django results inside DjangoResource.run")
        except BaseException:
            try:
                connections.close_all()
            except Exception:
                _log.exception("Django connection cleanup failed")
            raise
        connections.close_all()
        return result
