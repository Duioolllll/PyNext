"""Benchmark-only SQLite resource for the ``db-query`` baseline scenario.

This is test/benchmark infrastructure, not a product feature and not a DB
integration (see roadmap E4 for that). It seeds a small deterministic table
so the baseline measures a real general query -- a point lookup by primary
key over 1,000 rows -- instead of a stub that returns a constant.

The resource is passed explicitly to ``create_app`` by
``benchmarks/serve_project.py --resources``; it is never loaded by the
example app.
"""
import os
import sqlite3
import tempfile
import threading


class SQLiteBenchmarkDB:
    """A SQLite database with one seeded table, opened at app startup."""

    ROWS = 1000

    def __init__(self):
        self.path = None
        self.connection = None
        self._query_lock = threading.Lock()

    async def startup(self):
        handle, self.path = tempfile.mkstemp(prefix="pynext-bench-db-", suffix=".sqlite3")
        os.close(handle)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        self.connection.executemany(
            "INSERT INTO users (id, name) VALUES (?, ?)",
            [(n, f"user-{n:04d}") for n in range(1, self.ROWS + 1)])
        self.connection.commit()

    def query(self, user_id):
        """Return one row by id. Runs in a thread; see the route handler."""
        # check_same_thread=False permits cross-thread access, but does not
        # protect Python's statement cache/cursor lifetime. A connection is
        # never used concurrently; this fixture measures one shared SQLite
        # connection, not a production connection pool.
        with self._query_lock:
            row = self.connection.execute(
                "SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1]}

    async def shutdown(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.path is not None:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.path = None


RESOURCES = {"db": SQLiteBenchmarkDB()}
