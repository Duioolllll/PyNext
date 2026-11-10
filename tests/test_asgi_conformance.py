"""ASGI conformance tests: HTTP event order, chunked bodies, disconnects,
and the lifespan protocol (finding #53, spec v0.2 section 13).

These tests drive ``App.__call__`` directly with scripted ASGI channels so
each protocol rule is checked in isolation, without a real server in the
loop. The lifespan failure paths (``lifespan.startup.failed`` /
``lifespan.shutdown.failed``) are covered here too; before this change the
app let those exceptions propagate without sending a ``.failed`` message.
"""

import asyncio
import json
from pathlib import Path

import pytest

from pynext import JSONResponse, create_app

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"
MAX_BODY = 1_048_576


class ScriptedChannel:
    """A receive queue fed from a list, plus a log of sent messages."""

    def __init__(self, messages):
        self._messages = iter(messages)
        self.sent = []

    async def receive(self):
        try:
            return next(self._messages)
        except StopIteration:
            # A conforming server blocks here until the client disconnects;
            # never raise StopIteration out of a coroutine.
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def send(self, message):
        self.sent.append(message)


def http_scope(method="GET", path="/", query_string=b"", headers=()):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "query_string": query_string,
        "headers": list(headers),
    }


def request_messages(*chunks):
    """One http.request message per chunk; all but the last carry more_body."""
    return [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]


def make_app_with_handler(tmp_path, route_name, source, handler):
    route = tmp_path / "app" / route_name
    route.mkdir(parents=True)
    (route / "route.py").write_text(source, encoding="utf-8")
    app = create_app(tmp_path)
    app.entries[f"/{route_name}"]["GET"] = handler
    return app


def test_http_response_event_order_and_framing():
    async def run():
        app = create_app(EXAMPLE)
        try:
            channel = ScriptedChannel(request_messages(b""))
            await app(http_scope(), channel.receive, channel.send)
            assert [m["type"] for m in channel.sent] == [
                "http.response.start",
                "http.response.body",
            ]
            start, body = channel.sent
            assert start["status"] == 200
            assert isinstance(start["headers"], list)
            assert all(
                isinstance(key, bytes) and isinstance(value, bytes)
                for key, value in start["headers"]
            )
            headers = dict(start["headers"])
            assert headers[b"content-type"] == b"text/html; charset=utf-8"
            assert int(headers[b"content-length"]) == len(body["body"])
            assert body["body"].startswith(b"<!DOCTYPE html><html")
            assert body.get("more_body", False) is False
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_http_head_returns_headers_without_body():
    async def run():
        app = create_app(EXAMPLE)
        try:
            channel = ScriptedChannel(request_messages(b""))
            await app(http_scope("HEAD", "/api/users"), channel.receive, channel.send)
            start, body = channel.sent
            assert start["status"] == 200
            assert body["body"] == b""
            assert int(dict(start["headers"])[b"content-length"]) > 0
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_multi_chunk_body_reassembled_across_byte_boundaries():
    async def run():
        app = create_app(EXAMPLE)
        try:
            payload = '{"name": "한글"}'.encode("utf-8")
            chunks = [payload[i:i + 3] for i in range(0, len(payload), 3)]
            assert len(chunks) > 1  # multi-byte characters are split
            channel = ScriptedChannel(request_messages(*chunks))
            await app(
                http_scope("POST", "/api/users"), channel.receive, channel.send
            )
            start, body = channel.sent
            assert start["status"] == 201
            assert json.loads(body["body"]) == {"name": "한글"}
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_empty_body_chunks_do_not_break_body_read():
    async def run():
        app = create_app(EXAMPLE)
        try:
            channel = ScriptedChannel(
                request_messages(b"", b"", b'{"name": "N"}', b"")
            )
            await app(
                http_scope("POST", "/api/users"), channel.receive, channel.send
            )
            assert channel.sent[0]["status"] == 201
            assert json.loads(channel.sent[1]["body"]) == {"name": "N"}
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_body_size_gate_is_on_total_bytes_not_per_chunk():
    async def run():
        app = create_app(EXAMPLE)
        try:
            # Exactly at the limit: passes the size gate (400 proves the
            # handler ran and rejected the JSON, instead of a 413).
            channel = ScriptedChannel(request_messages(b"x" * MAX_BODY))
            await app(
                http_scope("POST", "/api/users"), channel.receive, channel.send
            )
            assert channel.sent[0]["status"] == 400
            # One byte over, split across many small chunks: 413.
            chunks = [b"y" * 11_000] * 100
            assert sum(len(c) for c in chunks) > MAX_BODY
            channel = ScriptedChannel(request_messages(*chunks))
            await app(
                http_scope("POST", "/api/users"), channel.receive, channel.send
            )
            assert channel.sent[0]["status"] == 413
            assert channel.sent[1]["body"] == b"Payload Too Large"
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_disconnect_mid_body_sends_nothing():
    async def run():
        app = create_app(EXAMPLE)
        try:
            channel = ScriptedChannel([
                {"type": "http.request", "body": b'{"name":', "more_body": True},
                {"type": "http.disconnect"},
            ])
            await app(
                http_scope("POST", "/api/users"), channel.receive, channel.send
            )
            assert channel.sent == []
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_disconnect_during_slow_handler_cancels_it(tmp_path):
    async def run():
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def slow_handler():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        app = make_app_with_handler(
            tmp_path,
            "slow",
            'from pynext import Response, route\n'
            '@route("GET")\ndef get(): return Response("placeholder")\n',
            slow_handler,
        )
        try:
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            await incoming.put({"type": "http.request", "body": b""})
            task = asyncio.create_task(
                app(http_scope("GET", "/slow"), incoming.get, outgoing.put)
            )
            await asyncio.wait_for(started.wait(), 5)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 5)
            assert cleaned.is_set()
            assert outgoing.empty()
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_lifespan_full_cycle_with_requests_in_between():
    async def run():
        app = create_app(EXAMPLE)
        try:
            lifespan = ScriptedChannel([
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ])
            task = asyncio.create_task(
                app({"type": "lifespan"}, lifespan.receive, lifespan.send)
            )
            await asyncio.sleep(0)
            channel = ScriptedChannel(request_messages(b""))
            await app(http_scope(), channel.receive, channel.send)
            assert channel.sent[0]["status"] == 200
            channel = ScriptedChannel(request_messages(b""))
            await app(http_scope("GET", "/api/users"), channel.receive, channel.send)
            assert channel.sent[0]["status"] == 200
            await asyncio.wait_for(task, 5)
            assert lifespan.sent == [
                {"type": "lifespan.startup.complete"},
                {"type": "lifespan.shutdown.complete"},
            ]
            assert app.isr._closed
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_lifespan_startup_failure_sends_failed_instead_of_raising():
    async def run():
        app = create_app(EXAMPLE)
        original_open = app.isr.open
        try:
            def failing_open():
                raise RuntimeError("cache unavailable")

            app.isr.open = failing_open
            channel = ScriptedChannel([{"type": "lifespan.startup"}])
            await asyncio.wait_for(
                app({"type": "lifespan"}, channel.receive, channel.send), 5
            )
            assert channel.sent == [
                {
                    "type": "lifespan.startup.failed",
                    "message": "cache unavailable",
                }
            ]
        finally:
            app.isr.open = original_open
            await app.isr.close()

    asyncio.run(run())


def test_lifespan_shutdown_failure_sends_failed_instead_of_raising():
    async def run():
        app = create_app(EXAMPLE)
        original_close = app.isr.close
        try:
            channel = ScriptedChannel([
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ])

            async def failing_close(*args, **kwargs):
                raise RuntimeError("cache teardown failed")

            app.isr.close = failing_close
            await asyncio.wait_for(
                app({"type": "lifespan"}, channel.receive, channel.send), 5
            )
            assert channel.sent == [
                {"type": "lifespan.startup.complete"},
                {
                    "type": "lifespan.shutdown.failed",
                    "message": "cache teardown failed",
                },
            ]
        finally:
            app.isr.close = original_close
            await app.isr.close()

    asyncio.run(run())


def test_unexpected_message_during_body_returns_500():
    async def run():
        app = create_app(EXAMPLE)
        try:
            channel = ScriptedChannel([
                {"type": "http.request", "body": b"{}", "more_body": True},
                {"type": "websocket.connect"},
            ])
            await app(
                http_scope("POST", "/api/users"), channel.receive, channel.send
            )
            assert channel.sent[0]["status"] == 500
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_streaming_response_frame_order():
    async def run():
        app = create_app(EXAMPLE)
        try:
            channel = ScriptedChannel(request_messages(b""))
            await asyncio.wait_for(
                app(http_scope("GET", "/api/stream"), channel.receive, channel.send),
                5,
            )
            assert channel.sent[0]["type"] == "http.response.start"
            assert channel.sent[0]["status"] == 200
            assert b"content-length" not in dict(channel.sent[0]["headers"])
            bodies = channel.sent[1:]
            assert bodies
            assert all(m["type"] == "http.response.body" for m in bodies)
            assert all(m.get("more_body", False) for m in bodies[:-1])
            assert bodies[-1]["body"] == b"" and bodies[-1].get("more_body") is False
            payload = b"".join(m["body"] for m in bodies)
            assert b'"phase":"started"' in payload
            assert b'"phase":"finished"' in payload
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_query_string_and_headers_reach_handler(tmp_path):
    async def run():
        async def echo(request):
            return JSONResponse({
                "query": request.query,
                "x-test": [
                    value.decode("utf-8")
                    for key, value in request.headers
                    if key == b"x-test"
                ],
            })

        app = make_app_with_handler(
            tmp_path,
            "echo",
            'from pynext import Response, route\n'
            '@route("GET")\ndef get(): return Response("placeholder")\n',
            echo,
        )
        try:
            channel = ScriptedChannel(request_messages(b""))
            await app(
                http_scope(
                    "GET", "/echo", query_string=b"a=1&a=2&b=x",
                    headers=[(b"x-test", b"yes")],
                ),
                channel.receive,
                channel.send,
            )
            assert channel.sent[0]["status"] == 200
            assert json.loads(channel.sent[1]["body"]) == {
                "query": {"a": ["1", "2"], "b": ["x"]},
                "x-test": ["yes"],
            }
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_unsupported_scope_is_rejected_before_receiving():
    async def run():
        app = create_app(EXAMPLE)
        try:
            sent = []

            async def unused_receive():
                raise AssertionError("scope must be rejected before receiving")

            async def send(message):
                sent.append(message)

            with pytest.raises(RuntimeError, match="Unsupported ASGI scope"):
                await app({"type": "websocket"}, unused_receive, send)
            assert sent == []
        finally:
            await app.isr.close()

    asyncio.run(run())
