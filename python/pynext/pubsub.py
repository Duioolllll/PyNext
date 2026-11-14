"""Bounded, process-local message fan-out for WebSocket handlers.

One broker belongs to one asyncio event loop. For cross-process fan-out an
external broker is needed; this class deliberately makes no delivery or
durability guarantees across workers.
"""

import asyncio

from .http import MAX_WS_MESSAGE


class PubSubOverflow(Exception):
    """A slow subscriber was removed after its pending queue filled."""


class _Subscription:
    def __init__(self, broker, topic):
        self._broker = broker
        self._topic = topic
        self._queue = asyncio.Queue(maxsize=broker.queue_size)
        self._joined = False
        self._terminal = None
        self._receiving = False

    async def __aenter__(self):
        self._broker._join(self)
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def receive(self):
        self._broker._check_loop()
        if not self._joined and self._terminal is None:
            raise RuntimeError("Subscription is not active")
        if self._terminal is not None:
            raise self._terminal
        if self._receiving:
            raise RuntimeError("Only one receiver may wait on a subscription")
        self._receiving = True
        try:
            item = await self._queue.get()
            if isinstance(item, Exception):
                raise item
            return item
        finally:
            self._receiving = False

    def _finish(self, error):
        if self._terminal is not None:
            return
        self._terminal = error
        while not self._queue.empty():
            self._queue.get_nowait()
        # Wake a receiver that was already waiting at close time.
        self._queue.put_nowait(error)

    async def close(self):
        self._broker._check_loop()
        self._broker._leave(self)
        self._finish(RuntimeError("Subscription is closed"))


class WebSocketPubSub:
    """In-memory fan-out for one process and event loop.

    Messages are ``str`` or ``bytes``. Each subscription has a bounded
    queue; a slow subscriber is removed and its next receive raises
    :class:`PubSubOverflow`. ``publish`` returns the number of subscribers
    that accepted the message.
    """

    def __init__(self, *, queue_size=32, max_topics=1024, max_subscribers=4096,
                 max_message_bytes=MAX_WS_MESSAGE):
        for name, value, upper in (("queue_size", queue_size, 1024),
                                   ("max_topics", max_topics, 100000),
                                   ("max_subscribers", max_subscribers, 100000),
                                   ("max_message_bytes", max_message_bytes, 16 * 1024 * 1024)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{name} must be from 1 to {upper}")
        self.queue_size = queue_size
        self.max_topics = max_topics
        self.max_subscribers = max_subscribers
        self.max_message_bytes = max_message_bytes
        self._topics = {}
        self._subscribers = 0
        self._loop = None

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("WebSocketPubSub belongs to a different event loop")

    @staticmethod
    def _validate_topic(topic):
        if not isinstance(topic, str) or not topic or len(topic.encode("utf-8")) > 256:
            raise ValueError("Topic must be a nonempty string of at most 256 UTF-8 bytes")

    def subscribe(self, topic):
        self._validate_topic(topic)
        return _Subscription(self, topic)

    def _join(self, subscription):
        self._check_loop()
        if subscription._joined or subscription._terminal is not None:
            raise RuntimeError("Subscription cannot be entered twice")
        if self._subscribers >= self.max_subscribers:
            raise RuntimeError("Subscription limit reached")
        if subscription._topic not in self._topics and len(self._topics) >= self.max_topics:
            raise RuntimeError("Topic limit reached")
        self._topics.setdefault(subscription._topic, set()).add(subscription)
        self._subscribers += 1
        subscription._joined = True

    def _leave(self, subscription):
        if not subscription._joined:
            return
        subscribers = self._topics[subscription._topic]
        subscribers.remove(subscription)
        if not subscribers:
            del self._topics[subscription._topic]
        self._subscribers -= 1
        subscription._joined = False

    async def publish(self, topic, message):
        self._check_loop()
        self._validate_topic(topic)
        if isinstance(message, str):
            try:
                size = len(message.encode("utf-8"))
            except UnicodeEncodeError as exc:
                # A7-L2: a str with lone surrogates is not valid UTF-8
                # text. Reject it with the documented ValueError instead
                # of letting UnicodeEncodeError escape to the caller.
                raise ValueError(
                    "Message must be valid UTF-8 text (lone surrogates are not allowed)"
                ) from exc
        elif isinstance(message, bytes):
            size = len(message)
        else:
            raise TypeError("Messages must be str or bytes")
        if size > self.max_message_bytes:
            raise ValueError("Message exceeds the configured limit")
        delivered = 0
        for subscriber in tuple(self._topics.get(topic, ())):
            try:
                subscriber._queue.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:
                self._leave(subscriber)
                subscriber._finish(PubSubOverflow("Subscriber queue is full"))
        return delivered
