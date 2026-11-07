import asyncio
import logging

from pynext import Response
from pynext.cache import ISRCache


class _FakeStore:
    def __init__(self, insert_result):
        self.insert_result = insert_result
        self.insert_calls = []

    def lookup(self, key):
        return ("miss", None)

    def insert(self, key, body, revalidate, stale):
        self.insert_calls.append((key, body, revalidate, stale))
        return self.insert_result


async def _render_ok():
    return Response(b"<p>ok</p>")


def test_get_or_render_warns_and_still_serves_when_insert_fails(caplog):
    async def run():
        cache = ISRCache()
        cache.store = _FakeStore(insert_result=False)
        try:
            with caplog.at_level(logging.WARNING, logger="pynext"):
                body = await cache.get_or_render(
                    "/page", {"revalidate": 60.0, "stale": 60.0}, _render_ok
                )
            assert body == b"<p>ok</p>"
            assert cache.store.insert_calls == [("/page", b"<p>ok</p>", 60.0, 60.0)]
        finally:
            await cache.close()

    asyncio.run(run())
    assert "ISR cache insert failed for /page" in caplog.text


def test_get_or_render_stays_quiet_when_insert_succeeds(caplog):
    async def run():
        cache = ISRCache()
        cache.store = _FakeStore(insert_result=True)
        try:
            with caplog.at_level(logging.WARNING, logger="pynext"):
                body = await cache.get_or_render(
                    "/page", {"revalidate": 60.0, "stale": 60.0}, _render_ok
                )
            assert body == b"<p>ok</p>"
            assert cache.store.insert_calls == [("/page", b"<p>ok</p>", 60.0, 60.0)]
        finally:
            await cache.close()

    asyncio.run(run())
    assert "ISR cache insert failed" not in caplog.text
