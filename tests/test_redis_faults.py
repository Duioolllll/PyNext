"""Exercise a real Redis connection through a controllable TCP partition."""
import asyncio
import os
import uuid
from urllib.parse import urlsplit

import pytest

pytest.importorskip("redis")

from pynext import RedisRateLimiter, Response
from pynext.cache import ISRCache
from pynext.ratelimit import RateLimitUnavailable


URL = os.environ.get("PYNEXT_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PYNEXT_TEST_REDIS_URL is not configured")


class _PartitionableProxy:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.enabled = True
        self.writers = set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._connection, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _connection(self, incoming, client):
        if not self.enabled:
            client.close()
            return
        upstream = None
        try:
            source, upstream = await asyncio.open_connection(self.host, self.port)
            self.writers.update((client, upstream))

            async def copy(reader, writer):
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()

            await asyncio.gather(copy(incoming, upstream), copy(source, client), return_exceptions=True)
        except (ConnectionError, OSError):
            pass
        finally:
            for writer in (client, upstream):
                if writer is not None:
                    self.writers.discard(writer)
                    writer.close()

    def disconnect(self):
        self.enabled = False
        for writer in tuple(self.writers):
            writer.transport.abort()

    async def close(self):
        self.disconnect()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(0)


def test_redis_partition_local_fallback_and_recovery():
    parsed = urlsplit(URL)
    if parsed.scheme != "redis" or parsed.username or parsed.password:
        pytest.skip("partition test needs an unauthenticated redis:// test instance")

    async def run():
        proxy = _PartitionableProxy(parsed.hostname, parsed.port or 6379)
        port = await proxy.start()
        proxied_url = f"redis://127.0.0.1:{port}{parsed.path or '/0'}"
        first = ISRCache(cache_backend="redis", cache_url=proxied_url)
        second = ISRCache(cache_backend="redis", cache_url=proxied_url)
        limiter = RedisRateLimiter(10, 60, url=proxied_url, namespace="partition-" + uuid.uuid4().hex)
        key = "/partition-" + uuid.uuid4().hex
        policy = {"revalidate": 30, "stale": 30}
        calls = 0

        async def render():
            nonlocal calls
            calls += 1
            return Response(str(calls))

        try:
            assert await first.get_or_render(key, policy, render) == b"1"
            assert await second.get_or_render(key, policy, render) == b"1"
            assert (await asyncio.to_thread(limiter.allow, "127.0.0.1"))[0]

            proxy.disconnect()
            await asyncio.sleep(0)
            assert await first.invalidate(key)
            assert await first.get_or_render(key, policy, render) == b"2"
            assert await second.get_or_render(key, policy, render) == b"1"
            with pytest.raises(RateLimitUnavailable):
                await asyncio.to_thread(limiter.allow, "127.0.0.1")

            proxy.enabled = True
            assert await first.get_or_render(key, policy, render) == b"1"
            assert await first.invalidate(key)
            assert await second.get_or_render(key, policy, render) == b"3"
            assert (await asyncio.to_thread(limiter.allow, "127.0.0.1"))[0]
        finally:
            await first.close()
            await second.close()
            limiter.close()
            await proxy.close()
    asyncio.run(run())
