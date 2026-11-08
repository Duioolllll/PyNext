import asyncio

import pytest

from pynext import PubSubOverflow, WebSocketPubSub


def test_pubsub_fanout_overflow_and_cleanup():
    async def run():
        broker = WebSocketPubSub(queue_size=1, max_topics=1, max_subscribers=3)
        async with broker.subscribe("news") as fast, broker.subscribe("news") as slow:
            assert await broker.publish("news", "one") == 2
            assert await fast.receive() == "one"
            assert await broker.publish("news", b"two") == 1
            assert await fast.receive() == b"two"
            with pytest.raises(PubSubOverflow):
                await slow.receive()
            async with broker.subscribe("news") as replacement:
                with pytest.raises(RuntimeError, match="Topic limit"):
                    async with broker.subscribe("different"):
                        pass
                assert await broker.publish("news", "three") == 2
                assert await replacement.receive() == "three"
            with pytest.raises(RuntimeError, match="closed"):
                await replacement.receive()
        async with broker.subscribe("different"):
            pass
    asyncio.run(run())


def test_pubsub_wakes_waiter_on_close_and_validates_bounds():
    async def run():
        broker = WebSocketPubSub(max_message_bytes=2)
        with pytest.raises(ValueError):
            broker.subscribe("")
        async with broker.subscribe("topic") as subscription:
            waiting = asyncio.create_task(subscription.receive())
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match="one receiver"):
                await subscription.receive()
            with pytest.raises(ValueError, match="limit"):
                await broker.publish("topic", "éé")
            with pytest.raises(TypeError):
                await broker.publish("topic", {})
            await subscription.close()
            with pytest.raises(RuntimeError, match="closed"):
                await waiting
    asyncio.run(run())
    with pytest.raises(ValueError):
        WebSocketPubSub(queue_size=0)
