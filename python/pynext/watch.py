"""Native file notifications with snapshots for ``pynext dev``.

The watcher snapshots the modification time, size and contents of every regular file under
the watched roots (``app/``, ``islands/``, ``public/``, ``middleware.py``,
``resources.py``, ``pynext.config.py`` and the ``.env*`` files) and reports changes. Native events
are debounced and a periodic snapshot detects changes missed during startup.
"""
import asyncio
from contextlib import AsyncExitStack, aclosing
import hashlib
import logging
import os
import stat
from pathlib import Path

from watchfiles import awatch

from .env import env_file_names

# Directories never watched: build output, bytecode caches, VCS metadata,
# virtual environments and JS dependencies.
IGNORED_DIRS = {"__pycache__", ".git", ".pynext", ".venv", "venv", "node_modules"}

# Every .env file name any mode can load (development/production/test),
# so editing one restarts the dev server and reloads its values.
ENV_FILE_NAMES = frozenset(name for mode in ("development", "production", "test")
                           for name in env_file_names(mode))

POLL_INTERVAL_SECONDS = 0.25
DEBOUNCE_SECONDS = 0.3
log = logging.getLogger("pynext.watch")


class Watcher:
    """Watch the roots of one project directory for changes."""

    def __init__(self, root, *, interval=POLL_INTERVAL_SECONDS, debounce=DEBOUNCE_SECONDS):
        self.root = Path(root).resolve()
        self.interval = interval
        self.debounce = debounce
        self._baseline = self.snapshot()

    def watch_roots(self):
        """Existing watched paths, in a stable order."""
        roots = []
        for name in ("app", "islands", "public"):
            directory = self.root / name
            if directory.is_dir():
                roots.append(directory)
        for name in ("pynext.config.py", "middleware.py", "resources.py"):
            config = self.root / name
            if config.is_file():
                roots.append(config)
        for name in sorted(ENV_FILE_NAMES):
            env_file = self.root / name
            if env_file.is_file():
                roots.append(env_file)
        return roots

    def snapshot(self):
        """Map each readable regular file to its metadata and content digest."""
        result = {}

        def record(path):
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    return
                # Timestamp resolution varies by filesystem. Hash the content
                # too, so same-size saves with identical mtimes are detected.
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").digest()
                result[path.relative_to(self.root).as_posix()] = (info.st_mtime_ns, info.st_size, digest)
            except OSError:
                # An editor may replace or delete a file while it is read.
                return

        for watched in self.watch_roots():
            if watched.is_file():
                record(watched)
                continue
            for current, dirs, files in os.walk(watched, followlinks=False):
                dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
                for name in files:
                    path = Path(current) / name
                    record(path)
        return result

    @staticmethod
    def changed(baseline, current):
        """Sorted relative paths that were added, removed or modified."""
        paths = set(baseline) | set(current)
        return sorted(path for path in paths if baseline.get(path) != current.get(path))

    async def wait_for_change(self):
        """Wait until the watched files change, then return the changed paths.

        Rapid successive changes are debounced: the result is returned only
        after the snapshot has been stable for ``debounce`` seconds, so one
        save that touches several files restarts the server once.
        """
        def relevant(_change, path):
            try:
                parts = Path(path).relative_to(self.root).parts
            except ValueError:
                return False
            if not parts or any(part in IGNORED_DIRS for part in parts):
                return False
            return parts[0] in {"app", "islands", "public"} or (
                len(parts) == 1 and parts[0] in {"pynext.config.py", "middleware.py", "resources.py"}
                | ENV_FILE_NAMES)

        async def changed_snapshot():
            current = await asyncio.to_thread(self.snapshot)
            if not self.changed(self._baseline, current):
                return None
            while True:
                await asyncio.sleep(self.debounce)
                after = await asyncio.to_thread(self.snapshot)
                if after == current:
                    found = self.changed(self._baseline, after)
                    if found:
                        self._baseline = after
                        return found
                    return None
                current = after

        found = await changed_snapshot()
        if found:
            return found
        settings = dict(watch_filter=relevant, debounce=max(1, int(self.debounce * 1000)),
                        step=max(1, min(50, int(self.interval * 1000))), rust_timeout=1000,
                        yield_on_timeout=True)
        # Watch the project root non-recursively so generated output and
        # node_modules never consume native watch descriptors. Existing source
        # directories get separate recursive watches.
        directories = [self.root / name for name in ("app", "islands", "public")
                       if (self.root / name).is_dir()]
        try:
            async with AsyncExitStack() as stack:
                streams = [await stack.enter_async_context(aclosing(awatch(self.root, recursive=False, **settings)))]
                if directories:
                    streams.append(await stack.enter_async_context(aclosing(awatch(*directories, recursive=True, **settings))))
                tasks = {asyncio.create_task(anext(stream)): stream for stream in streams}
                try:
                    while tasks:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            stream = tasks.pop(task)
                            try:
                                task.result()
                            except StopAsyncIteration:
                                continue
                            tasks[asyncio.create_task(anext(stream))] = stream
                        found = await changed_snapshot()
                        if found:
                            return found
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, RuntimeError):
            log.warning("Native file watching failed; falling back to polling", exc_info=True)
        while True:
            await asyncio.sleep(self.interval)
            found = await changed_snapshot()
            if found:
                return found
