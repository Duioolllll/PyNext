"""Request-smuggling defence (S-P1-31, Sanic CVE-2026-85078).

Raw-socket tests against the native (hyper) transport and scope-level
tests against the ASGI path. Both transports must agree on framing:

* ``Content-Length`` + ``Transfer-Encoding`` together -> 400.
* duplicate ``Content-Length`` values that differ -> 400; identical
  duplicates pass.
* ``Transfer-Encoding`` whose final coding is not ``chunked`` -> 400.
* chunked trailers -> 400; chunk extensions are consumed per grammar
  and must not leak into the next request on a reused connection.
"""
import asyncio

from pynext import create_app
from pynext.server import start_native


def write_echo_route(tmp_path):
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import Response, route\n@route("POST")\n'
        'async def post(request):\n    return Response(request.body, media_type="text/plain")\n',
        encoding="utf-8",
    )


async def raw_head(host, port, payload):
    """Send raw bytes, return the response head (status line + headers)."""
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(payload)
        await writer.drain()
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    finally:
        writer.close()
        await writer.wait_closed()


def status_of(head):
    return head.split(b"\r\n", 1)[0]


def test_content_length_and_transfer_encoding_is_bad_request(tmp_path):
    # S-P1-31 (a): CL+TE is ambiguous framing, not "TE wins" — the old
    # behaviour ran the handler with hyper's chunked framing. Both header
    # orders must be rejected, and the connection must not be reused.
    write_echo_route(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            for payload in (
                (b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                 b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n"
                 b"0\r\n\r\n"),
                (b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                 b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n"
                 b"0\r\n\r\n"),
            ):
                head = await raw_head(host, port, payload)
                assert status_of(head).startswith(b"HTTP/1.1 400 "), head
                assert b"connection: close" in head.lower(), head
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_non_chunked_transfer_encoding_is_bad_request(tmp_path):
    # S-P1-31 (c): only chunked framing is supported; hyper rejects this
    # at parse time and the dispatch backstop agrees.
    write_echo_route(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            head = await raw_head(
                host, port,
                b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                b"Transfer-Encoding: gzip\r\nContent-Length: 5\r\n\r\nhello",
            )
            assert status_of(head).startswith(b"HTTP/1.1 400 "), head
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_identical_duplicate_content_length_is_allowed(tmp_path):
    # S-P1-31 (b): identical duplicates are one value and pass.
    write_echo_route(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 5\r\nContent-Length: 5\r\n\r\nhello"
                )
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                assert status_of(head).startswith(b"HTTP/1.1 200 "), head
                length = int(
                    next(
                        line.split(b":", 1)[1].strip()
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                )
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
                assert body == b"hello", body
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_chunked_trailers_are_bad_request(tmp_path):
    # S-P1-31 (d) / Sanic CVE-2026-85078: trailer bytes after the final
    # 0-chunk must be rejected, never silently consumed or left for the
    # next request on the connection.
    write_echo_route(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            head = await raw_head(
                host, port,
                b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"5\r\nhello\r\n0\r\nX-Trailer: smuggled\r\n\r\n",
            )
            assert status_of(head).startswith(b"HTTP/1.1 400 "), head
            assert b"connection: close" in head.lower(), head
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_chunked_extension_is_consumed_safely(tmp_path):
    # S-P1-31 (d): a chunk extension is legal chunked grammar (RFC 9112
    # section 7.1.1) and hyper consumes it within the chunk-size line;
    # the body must arrive intact and the connection must stay reusable
    # for the next request — i.e. no extension byte leaks into it.
    write_echo_route(tmp_path)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(
                    b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                    b"5;ext=1\r\nhello\r\n0\r\n\r\n"
                )
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                assert status_of(head).startswith(b"HTTP/1.1 200 "), head
                length = int(
                    next(
                        line.split(b":", 1)[1].strip()
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                )
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
                assert body == b"hello", body
                # Same connection, second request: the extension bytes must
                # not have been re-parsed as a smuggled request.
                writer.write(
                    b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 5\r\n\r\nworld"
                )
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                assert status_of(head).startswith(b"HTTP/1.1 200 "), head
                length = int(
                    next(
                        line.split(b":", 1)[1].strip()
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                )
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
                assert body == b"world", body
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


# --- ASGI path: the same framing rules, checked on scope headers before
# any body is read (the ASGI server has already de-chunked the body, so
# the app can only police the headers and the trailer signal). ---


def asgi_project(tmp_path):
    route = tmp_path / "app" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import Response, route\n@route("POST")\n'
        'async def post(request):\n    return Response(request.body, media_type="text/plain")\n',
        encoding="utf-8",
    )


async def asgi_call(app, headers, messages, *, http_version="1.1"):
    scope = {"type": "http", "path": "/input", "method": "POST",
             "scheme": "http", "http_version": http_version,
             "client": ("127.0.0.1", 1234), "headers": headers}
    queue = asyncio.Queue()
    for message in messages:
        await queue.put(message)
    sent = []

    async def receive():
        return await queue.get()

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def asgi_status(sent):
    assert sent and sent[0]["type"] == "http.response.start", sent
    return sent[0]["status"]


def test_asgi_content_length_and_transfer_encoding_is_bad_request(tmp_path):
    asgi_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asgi_call(
                app,
                [(b"host", b"localhost"), (b"content-length", b"5"),
                 (b"transfer-encoding", b"chunked")],
                [{"type": "http.request", "body": b"hello"}],
            )
            assert asgi_status(sent) == 400, sent
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_asgi_conflicting_and_identical_content_length(tmp_path):
    asgi_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asgi_call(
                app,
                [(b"host", b"localhost"), (b"content-length", b"5"),
                 (b"content-length", b"6")],
                [{"type": "http.request", "body": b"hello!"}],
            )
            assert asgi_status(sent) == 400, sent
            sent = await asgi_call(
                app,
                [(b"host", b"localhost"), (b"content-length", b"5"),
                 (b"content-length", b"5")],
                [{"type": "http.request", "body": b"hello"}],
            )
            assert asgi_status(sent) == 200, sent
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_asgi_non_chunked_transfer_encoding_is_bad_request(tmp_path):
    asgi_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asgi_call(
                app,
                [(b"host", b"localhost"), (b"transfer-encoding", b"gzip")],
                [{"type": "http.request", "body": b"hello"}],
            )
            assert asgi_status(sent) == 400, sent
            sent = await asgi_call(
                app,
                [(b"host", b"localhost"),
                 (b"transfer-encoding", b"gzip, chunked")],
                [{"type": "http.request", "body": b"hello"}],
            )
            assert asgi_status(sent) == 200, sent
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_asgi_chunk_trailers_are_bad_request(tmp_path):
    asgi_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            sent = await asgi_call(
                app,
                [(b"host", b"localhost"), (b"transfer-encoding", b"chunked")],
                [{"type": "http.request", "body": b"hello",
                  "more_body": False, "trailers": [(b"x-trailer", b"smuggled")]}],
            )
            assert asgi_status(sent) == 400, sent
        finally:
            await app.shutdown()

    asyncio.run(run())
