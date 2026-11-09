"""Tests for the _ISRStreamFeed tee invariants (REPORT.md §2.3 B, P2-2).

A single ISR render is fanned out to two consumers — the cache body and
the initiating client's response stream — through one feed. These tests
pin down the feed's state machine: illegal transitions raise instead of
silently corrupting the tee, and a feed can be consumed only once.
"""

import asyncio
import uuid

import httpx
import pytest

from pynext import create_app
from pynext.app import _ISRStreamFeed
from pynext.build import write_manifest


async def _drain(feed):
    return [chunk async for chunk in feed.chunks()]


def test_feed_happy_path_delivers_chunks_in_order():
    async def run():
        feed = _ISRStreamFeed()
        assert not feed.consumed
        feed.begin()
        await feed.send(b"a")
        await feed.send(b"b")
        await feed.finish()
        assert await _drain(feed) == [b"a", b"b"]
        assert feed.consumed
    asyncio.run(run())


def test_feed_begin_twice_raises():
    async def run():
        feed = _ISRStreamFeed()
        feed.begin()
        with pytest.raises(RuntimeError, match="begin"):
            feed.begin()
    asyncio.run(run())


def test_feed_send_before_begin_raises():
    async def run():
        feed = _ISRStreamFeed()
        with pytest.raises(RuntimeError, match="before begin"):
            await feed.send(b"a")
    asyncio.run(run())


def test_feed_send_after_finish_raises():
    async def run():
        feed = _ISRStreamFeed()
        feed.begin()
        await feed.finish()
        with pytest.raises(RuntimeError, match="after finished"):
            await feed.send(b"a")
    asyncio.run(run())


def test_feed_finish_before_begin_and_twice_raise():
    async def run():
        feed = _ISRStreamFeed()
        with pytest.raises(RuntimeError, match="before begin"):
            await feed.finish()
        feed.begin()
        await feed.finish()
        with pytest.raises(RuntimeError, match="after finished"):
            await feed.finish()
    asyncio.run(run())


def test_feed_fail_delivers_error_and_second_fail_is_noop():
    async def run():
        feed = _ISRStreamFeed()
        feed.begin()
        await feed.send(b"a")
        feed.fail(ValueError("boom"))
        feed.fail(RuntimeError("second"))
        with pytest.raises(ValueError, match="boom"):
            await _drain(feed)
    asyncio.run(run())


def test_feed_fail_after_finish_raises():
    async def run():
        feed = _ISRStreamFeed()
        feed.begin()
        await feed.finish()
        with pytest.raises(RuntimeError, match="after finish"):
            feed.fail(ValueError("too late"))
    asyncio.run(run())


def test_feed_chunks_consumed_only_once():
    async def run():
        feed = _ISRStreamFeed()
        feed.begin()
        await feed.finish()
        await _drain(feed)
        with pytest.raises(RuntimeError, match="already been consumed"):
            feed.chunks()
    asyncio.run(run())


def test_feed_detach_makes_producer_calls_noop():
    async def run():
        feed = _ISRStreamFeed()
        feed.begin()
        feed.detach()
        # The consumer is gone; the producer keeps generating for the cache
        # and its calls must not raise.
        feed.begin()
        await feed.send(b"a")
        await feed.finish()
        feed.fail(ValueError("dropped"))
    asyncio.run(run())


def test_feed_fail_before_begin_is_allowed():
    # render_page() can raise before begin() is ever called; fail() must
    # still record the terminal state instead of raising a second error.
    async def run():
        feed = _ISRStreamFeed()
        feed.fail(ValueError("render failed"))
        with pytest.raises(RuntimeError, match="begin"):
            feed.begin()
    asyncio.run(run())


def test_isr_stream_and_cached_body_are_the_same_tee(tmp_path):
    """End to end: the streamed body and the cached body are identical."""
    directory = tmp_path / "app" / "stream"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page():\n'
        '    yield h("p", {}, "one")\n'
        '    yield h("p", {}, "two")\n'
        f'# test namespace {uuid.uuid4().hex}\n', encoding="utf-8")
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                first = await client.get("/stream")
                second = await client.get("/stream")
                assert first.content == second.content
                assert b"one" in first.content and b"two" in first.content
        finally:
            await app.isr.close()
    asyncio.run(run())
