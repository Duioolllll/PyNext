"""Response splitting defense (S-P1-30, Sanic CVE-2026-85077 lesson).

The ``Response`` constructor already rejects CR/LF in headers, but headers
are a mutable list: a handler, middleware, or a set-cookie/CORS/path-header
helper can append a value afterwards. :func:`pynext.http.sanitize_response_headers`
is therefore applied exactly once in ``app._send_response`` — the single
ASGI serialization point — and these tests drive that point (plus the
helper directly) to prove no CR/LF/NUL can reach the wire.
"""

import asyncio

import pytest

from pynext import Response, StreamingResponse, create_app
from pynext.app import _send_response
from pynext.http import sanitize_response_headers


def _wire_headers(messages):
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["headers"]


def _assert_no_crlf(headers):
    for name, value in headers:
        assert b"\r" not in name and b"\n" not in name and b"\x00" not in name
        assert b"\r" not in value and b"\n" not in value and b"\x00" not in value


# --- Helper unit tests ---------------------------------------------------


def test_sanitize_strips_crlf_and_nul_from_values():
    headers = sanitize_response_headers([
        (b"x-evil", b"a\r\nInjected: 1"),
        (b"x-cr", b"a\rb"),
        (b"x-lf", b"a\nb"),
        (b"x-nul", b"a\x00b"),
    ])
    assert headers == [
        (b"x-evil", b"aInjected: 1"),
        (b"x-cr", b"ab"),
        (b"x-lf", b"ab"),
        (b"x-nul", b"ab"),
    ]


def test_sanitize_strips_crlf_from_names():
    headers = sanitize_response_headers([(b"x-bad\r\nname", b"v")])
    assert headers == [(b"x-badname", b"v")]


def test_sanitize_drops_invalid_names_and_control_values():
    headers = sanitize_response_headers([
        (b"bad name", b"v"),          # space is not a valid token char
        (b"x-ctl", b"a\x01b"),        # other control bytes are not stripped, pair dropped
        (b"x-del", b"a\x7fb"),
        (b"\r\n", b"v"),              # name empties out after stripping
        (b"x-ok", b"fine"),
    ])
    assert headers == [(b"x-ok", b"fine")]


def test_sanitize_leaves_valid_headers_unchanged():
    original = [(b"content-type", b"text/plain; charset=utf-8"),
                (b"x-custom", b"a b~c"),
                (b"set-cookie", b"a=b; Path=/; HttpOnly")]
    assert sanitize_response_headers(original) == original
    assert sanitize_response_headers([]) == []


def test_sanitize_does_not_mutate_input():
    original = [(b"x-evil", b"a\r\nb")]
    sanitize_response_headers(original)
    assert original == [(b"x-evil", b"a\r\nb")]


def test_response_constructor_still_rejects_crlf():
    # Construction-time rejection is the first layer; the serializer is
    # the second. Neither may silently regress.
    with pytest.raises(ValueError):
        Response("ok", headers=[(b"x-evil", b"a\r\nb")])
    with pytest.raises(ValueError):
        Response("ok", headers=[(b"x-evil", b"a\nb")])
    with pytest.raises(ValueError):
        Response("ok", headers=[(b"x\r\nbad", b"v")])


# --- Serialization-point tests (drive _send_response directly) -----------


def _capture_send():
    sent = []

    async def send(message):
        sent.append(message)

    return sent, send


def test_send_response_strips_crlf_appended_after_construction():
    response = Response("ok")
    response.headers.append((b"x-evil", b"a\r\nInjected: 1"))
    response.headers.append((b"location", b"/x\r\nSet-Cookie: forged=1"))
    sent, send = _capture_send()
    asyncio.run(_send_response(response, send, config=None))
    headers = _wire_headers(sent)
    _assert_no_crlf(headers)
    assert (b"x-evil", b"aInjected: 1") in headers
    assert (b"location", b"/xSet-Cookie: forged=1") in headers
    assert sent[0]["status"] == 200
    assert sent[1] == {"type": "http.response.body", "body": b"ok"}


def test_send_response_keeps_valid_headers_untouched():
    response = Response("ok", headers=[(b"x-custom", b"hello world")])
    sent, send = _capture_send()
    asyncio.run(_send_response(response, send, config=None))
    headers = _wire_headers(sent)
    assert (b"x-custom", b"hello world") in headers
    _assert_no_crlf(headers)


def test_send_response_streaming_strips_crlf_too():
    async def chunks():
        yield b"a"
        yield b"b"

    response = StreamingResponse(chunks(), media_type="text/plain")
    response.headers.append((b"x-evil", b"a\nb"))
    sent, send = _capture_send()
    asyncio.run(_send_response(response, send, config=None))
    headers = _wire_headers(sent)
    _assert_no_crlf(headers)
    assert (b"x-evil", b"ab") in headers
    assert [m["type"] for m in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
        "http.response.body",
    ]


# --- End-to-end ASGI test (handler mutates headers, app serves them) ----


class _Channel:
    def __init__(self, messages):
        self._messages = iter(messages)
        self.sent = []

    async def receive(self):
        try:
            return next(self._messages)
        except StopIteration:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def send(self, message):
        self.sent.append(message)


def _http_scope(method="GET", path="/"):
    return {"type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "query_string": b"", "headers": []}


def test_app_serves_mutated_headers_without_crlf(tmp_path):
    async def evil_handler():
        response = Response("ok")
        # Attacker-controlled data reaching a header after construction:
        # a Location redirect and a Content-Disposition filename.
        response.headers.append((b"location", b"/done\r\nInjected: 1"))
        response.headers.append(
            (b"content-disposition", b'attachment; filename="x\r\nY.txt"'))
        return response

    route = tmp_path / "app" / "split"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        "from pynext import Response, route\n"
        '@route("GET")\ndef get(): return Response("placeholder")\n',
        encoding="utf-8")
    app = create_app(tmp_path)
    app.entries["/split"]["GET"] = evil_handler
    try:
        async def run():
            channel = _Channel([{"type": "http.request", "body": b""}])
            await app(_http_scope("GET", "/split"), channel.receive, channel.send)
            return channel.sent

        sent = asyncio.run(run())
        headers = _wire_headers(sent)
        _assert_no_crlf(headers)
        assert (b"location", b"/doneInjected: 1") in headers
        assert (b"content-disposition", b'attachment; filename="xY.txt"') in headers
    finally:
        asyncio.run(app.isr.close())
