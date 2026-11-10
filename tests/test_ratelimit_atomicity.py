"""Rate-limit atomicity across workers (S-P1-4).

The Redis backend enforces the limit with a single Lua ``EVAL`` (see
``pynext.redis_rate._ALLOW``): the whole read-refill-decide-write step
runs atomically on the Redis server, so two workers or processes sharing
a namespace can never interleave a check and a consume. These tests
prove that property the way the Litestar lesson describes it — by
hammering one key from several threads and from several separate
processes at once and asserting the total admitted never exceeds the
limit.

Needs a real Redis server: set ``PYNEXT_TEST_REDIS_URL`` (the CI redis
job sets it), following ``tests/test_redis_cache.py``.
"""
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

pytest.importorskip("redis")

from pynext import RedisRateLimiter

URL = os.environ.get("PYNEXT_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PYNEXT_TEST_REDIS_URL is not configured")


def _namespace():
    return "atomicity-" + uuid.uuid4().hex


# Worker for the multi-process test: one OS process per worker, like a
# real `--workers N` deployment. Each builds its own limiter client
# against the same URL + namespace, waits for the go file so all
# workers start hammering together, then prints how many of its calls
# were admitted. argv: url namespace key limit calls ready_file go_file.
_WORKER = """
import os
import sys
import time

url, namespace, key, limit, calls, ready_file, go_file = sys.argv[1:8]
from pynext import RedisRateLimiter

limiter = RedisRateLimiter(int(limit), 60, url=url, namespace=namespace)
with open(ready_file, "w") as handle:
    handle.write("ready")
deadline = time.monotonic() + 30
while not os.path.exists(go_file):
    if time.monotonic() > deadline:
        raise SystemExit("go file never appeared")
    time.sleep(0.001)
admitted = 0
for _ in range(int(calls)):
    allowed, _ = limiter.allow(key)
    admitted += bool(allowed)
limiter.close()
print(admitted)
"""


def test_redis_limiter_is_atomic_under_threads():
    """Concurrent threads sharing one Redis limiter admit exactly the limit."""
    limiter = RedisRateLimiter(10, 60, url=URL, namespace=_namespace())
    try:
        barrier = threading.Barrier(8)
        admitted = []

        def hammer():
            barrier.wait(timeout=10)
            count = 0
            for _ in range(25):
                allowed, _ = limiter.allow("shared-key")
                count += bool(allowed)
            admitted.append(count)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)
        assert sum(admitted) == 10
    finally:
        limiter.close()


def test_redis_limiter_multi_worker_simulation(tmp_path):
    """Four separate processes sharing one Redis limiter admit exactly the limit.

    This is the multi-worker bypass scenario from S-P1-4: each process
    is a worker with its own client, all hammering the same key at once.
    A check-then-set implementation would let two processes both pass
    the last token; the single Lua EVAL admits exactly ``limit`` in
    total, no matter how the admissions spread across the workers.
    """
    namespace = _namespace()
    workers = 4
    limit = 10
    calls = 50
    go_file = tmp_path / "go"
    ready_files = []
    procs = []
    for index in range(workers):
        ready = tmp_path / f"ready-{index}"
        ready_files.append(ready)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _WORKER, URL, namespace, "shared-key",
             str(limit), str(calls), str(ready), str(go_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    try:
        deadline = time.monotonic() + 60
        while not all(path.exists() for path in ready_files):
            if time.monotonic() > deadline:
                raise AssertionError("workers did not become ready")
            if any(proc.poll() is not None for proc in procs):
                raise AssertionError("a worker exited before the barrier")
            time.sleep(0.01)
        go_file.touch()
        total = 0
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
            total += int(out.strip())
        assert total == limit
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


def test_redis_limiter_allow_is_a_single_eval():
    """allow() issues exactly one Redis command: the atomic Lua EVAL.

    A check-then-set implementation would issue a GET plus a SET (two
    round trips with a race window between them); this pins the S-P1-4
    mechanism at the client level.
    """
    limiter = RedisRateLimiter(5, 60, url=URL, namespace=_namespace())
    try:
        calls = []
        original_eval = limiter.client.eval

        def counting_eval(script, numkeys, *keys_and_args):
            calls.append(script)
            return original_eval(script, numkeys, *keys_and_args)

        limiter.client.eval = counting_eval
        assert limiter.allow("k") == (True, None)
        assert limiter.allow("k") == (True, None)
        assert len(calls) == 2
        assert calls[0] is calls[1]  # the same Lua script object each time
    finally:
        limiter.close()


def test_redis_limiter_refills_after_window():
    """The window still refills: denial is temporary, not a stuck bucket."""
    limiter = RedisRateLimiter(2, 1, url=URL, namespace=_namespace())
    try:
        assert limiter.allow("k") == (True, None)
        assert limiter.allow("k") == (True, None)
        assert limiter.allow("k")[0] is False
        time.sleep(1.2)
        assert limiter.allow("k") == (True, None)
    finally:
        limiter.close()
