"""Stdlib-only polling file watcher for ``pynext dev``.

The watcher snapshots the modification time and size of every file under
the watched roots (``app/``, ``islands/``, ``public/`` and
``pynext.config.py``) and reports when the snapshot changes. Polling keeps
the dev command dependency-free and portable; watchdog is not a project
dependency. Changes are debounced so one save that touches several files
restarts the server once.
"""
import asyncio
import os
import time
from pathlib import Path

# Directories never watched: build output, bytecode caches, VCS metadata,
# virtual environments and JS dependencies.
IGNORED_DIRS = {"__pycache__", ".git", ".pynext", ".venv", "venv", "node_modules"}

POLL_INTERVAL_SECONDS = 0.25
DEBOUNCE_SECONDS = 0.3


class Watcher:
    """Poll the watched roots of one project directory for changes."""

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
        config = self.root / "pynext.config.py"
        if config.is_file():
            roots.append(config)
        return roots

    def snapshot(self):
        """Map each watched file, relative to the project root, to (mtime, size)."""
        result = {}
        for watched in self.watch_roots():
            if watched.is_file():
                try:
                    stat = watched.stat()
                except OSError:
                    continue
                result[watched.relative_to(self.root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
                continue
            for current, dirs, files in os.walk(watched, followlinks=False):
                dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
                for name in files:
                    path = Path(current) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    result[path.relative_to(self.root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
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
        changed = None
        last_snapshot = self._baseline
        stable_since = None
        while True:
            current = self.snapshot()
            found = self.changed(self._baseline, current)
            now = time.monotonic()
            if found:
                changed = found
            if current != last_snapshot:
                last_snapshot = current
                stable_since = now
            elif (changed is not None and stable_since is not None
                    and now - stable_since >= self.debounce):
                self._baseline = current
                return changed
            await asyncio.sleep(self.interval)
