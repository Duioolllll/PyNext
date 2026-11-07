"""Stdlib-only polling file watcher for ``pynext dev``.

The watcher snapshots the modification time, size and contents of every regular file under
the watched roots (``app/``, ``islands/``, ``public/`` and
``pynext.config.py``) and reports when the snapshot changes. Polling keeps
the dev command dependency-free and portable; watchdog is not a project
dependency. Changes are debounced so one save that touches several files
restarts the server once.
"""
import asyncio
import hashlib
import os
import stat
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
        changed = None
        last_snapshot = self._baseline
        stable_since = None
        while True:
            # Digesting public assets must not block the serving event loop.
            current = await asyncio.to_thread(self.snapshot)
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
