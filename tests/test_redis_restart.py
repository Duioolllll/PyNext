"""Optional test of restart behavior with a disposable Docker Redis server."""
import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

pytest.importorskip("redis")
import redis

from pynext import RedisRateLimiter, Response
from pynext.cache import ISRCache


pytestmark = pytest.mark.skipif(
    os.environ.get("PYNEXT_TEST_REDIS_RESTART") != "1" or not shutil.which("docker"),
    reason="set PYNEXT_TEST_REDIS_RESTART=1 with Docker to test an isolated Redis restart")


def test_disposable_redis_restart_rebuilds_lost_state():
    name = "pynext-restart-test-" + uuid.uuid4().hex[:12]

    def docker(*args):
        return subprocess.run(["docker", "--host=unix:///var/run/docker.sock", *args],
                              check=True, capture_output=True, text=True, timeout=120).stdout.strip()

    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", "redis:7-alpine",
               "redis-server", "--save", "", "--appendonly", "no")
        url = f"redis://127.0.0.1:{port}/0"

        def wait_ready():
            client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if client.ping():
                            return
                    except redis.RedisError:
                        time.sleep(0.1)
                raise AssertionError("disposable Redis did not start")
            finally:
                client.close()

        wait_ready()

        async def run():
            cache = ISRCache(cache_backend="redis", cache_url=url)
            limiter = RedisRateLimiter(1, 60, url=url, namespace="restart-test")
            calls = 0
            policy = {"revalidate": 30, "stale": 30}

            async def render():
                nonlocal calls
                calls += 1
                return Response(str(calls))

            try:
                assert await cache.get_or_render("/restart", policy, render) == b"1"
                assert (await asyncio.to_thread(limiter.allow, "127.0.0.1"))[0]
                assert not (await asyncio.to_thread(limiter.allow, "127.0.0.1"))[0]
                await asyncio.to_thread(docker, "restart", name)
                await asyncio.to_thread(wait_ready)
                # Without persistence a Redis restart loses cache entries and
                # rate buckets; the next healthy lookup must regenerate.
                for _ in range(5):
                    body = await cache.get_or_render("/restart", policy, render)
                    if body == b"2":
                        break
                    await asyncio.sleep(0.1)
                assert body == b"2" and calls == 2
                assert (await asyncio.to_thread(limiter.allow, "127.0.0.1"))[0]
            finally:
                await cache.close()
                limiter.close()
        asyncio.run(run())
    finally:
        subprocess.run(["docker", "--host=unix:///var/run/docker.sock", "rm", "-f", name],
                       capture_output=True, text=True, timeout=30, check=False)
