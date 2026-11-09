"""Internal-header stripping at the entry points (Next.js P1-15 / D#3).

Next.js removes its internal headers (`x-middleware-*`, `x-matched-path`,
...) from inbound requests in `router-server.ts:387` via
`filterInternalHeaders` (`server-ipc/utils.ts:42-65`), so a client cannot
spoof the signals middleware and routing use to steer a request. PyNext
middleware communicates through ``Request``/``Response`` objects, so there
is no such protocol to preserve — but the same boundary habit applies:
``pynext.http.filter_internal_headers`` runs at both entry points (ASGI
``App.__call__`` and the native ``create_server`` callbacks, HTTP and
WebSocket), before the request context, rate limiting, proxy trust, or any
middleware sees the request.
"""
import asyncio
import json

import httpx
import pytest

from pynext import JSONResponse, create_app, route  # noqa: F401  (route re-exported for clarity)
from pynext.build import write_manifest
from pynext.http import INTERNAL_HEADERS, filter_internal_headers
from pynext.server import start_native

INTERNAL_NAMES = {name.decode("ascii") for name in INTERNAL_HEADERS}
SPOOFED = {name: "spoofed" for name in INTERNAL_NAMES}
KEEPER = "x-keep-me"


def test_filter_removes_all_internal_headers():
    headers = [(b"host", b"example.com")] + [
        (name.encode("ascii"), b"spoofed") for name in INTERNAL_NAMES
    ] + [(b"x-keep-me", b"yes")]
    assert [name for name, _ in filter_internal_headers(headers)] == [
        b"host", b"x-keep-me",
    ]


def test_filter_is_case_insensitive_and_preserves_order_and_duplicates():
    headers = [
        (b"X-Middleware-Rewrite", b"/evil"),
        (b"X-MATCHED-PATH", b"/evil"),
        (b"x-custom", b"1"),
        (b"X-Custom", b"2"),
        (b"x-nextjs-data", b"1"),
        (b"x-custom", b"3"),
    ]
    assert filter_internal_headers(headers) == (
        (b"x-custom", b"1"),
        (b"X-Custom", b"2"),
        (b"x-custom", b"3"),
    )


def test_filter_empty_and_list_input():
    assert filter_internal_headers([]) == ()
    assert filter_internal_headers(()) == ()
    assert filter_internal_headers([(b"host", b"a")]) == ((b"host", b"a"),)


def test_filter_keeps_deliberately_honored_headers():
    # x-request-id is echoed by design (observability.begin_request_context),
    # traceparent is W3C tracing, x-forwarded-* is only honored from trusted
    # proxies (proxy.ProxyTrust) — none of these is an internal signal.
    headers = [
        (b"x-request-id", b"client-chosen-id"),
        (b"traceparent", b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
        (b"x-forwarded-for", b"203.0.113.7"),
    ]
    assert filter_internal_headers(headers) == tuple(headers)


def _project(tmp_path):
    api = tmp_path / "app" / "api"
    api.mkdir(parents=True)
    (api / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"GET\")\n"
        "def get(request):\n"
        "    return JSONResponse({\"headers\": [k.decode(\"latin-1\") for k, _ in request.headers]})\n"
    )
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "file.txt").write_text("asset")
    write_manifest(tmp_path)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_entry_strips_internal_headers(tmp_path, mode):
    """Neither middleware nor the handler ever sees a spoofed internal header."""
    _project(tmp_path)
    seen_by_middleware = []

    async def recorder(request, call_next):
        seen_by_middleware.append([k.decode("latin-1") for k, _ in request.headers])
        return await call_next(request)

    app = create_app(tmp_path, production=True, middleware=[recorder])

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                sent = dict(SPOOFED)
                sent[KEEPER] = "yes"
                response = await client.get("/api", headers=sent)
                assert response.status_code == 200
                seen_by_handler = response.json()["headers"]
                assert not (set(seen_by_handler) & INTERNAL_NAMES)
                assert KEEPER in seen_by_handler
                assert seen_by_middleware
                assert not (set(seen_by_middleware[0]) & INTERNAL_NAMES)
                assert KEEPER in seen_by_middleware[0]
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


class _ScriptedConnection:
    def __init__(self, incoming):
        self._incoming = iter(incoming)
        self.sent = []

    async def receive(self):
        return next(self._incoming)

    async def send(self, message):
        self.sent.append(message)


def test_asgi_ws_preflight_strips_internal_headers(tmp_path):
    """The WS pre-flight synthetic GET and the socket itself see no spoofed headers."""
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    seen_file = tmp_path / "ws-seen.json"
    (ws_dir / "route.py").write_text(
        "from pynext import websocket\n"
        "import json\n"
        f"SEEN_FILE = {str(seen_file)!r}\n"
        "@websocket\n"
        "async def handler(websocket):\n"
        "    with open(SEEN_FILE, \"w\") as f:\n"
        "        json.dump([k.decode(\"latin-1\") for k, _ in websocket.headers], f)\n"
        "    await websocket.accept()\n"
        "    await websocket.send_text(\"ok\")\n"
        "    await websocket.receive()\n"
    )
    seen_by_middleware = []

    async def recorder(request, call_next):
        seen_by_middleware.append([k.decode("latin-1") for k, _ in request.headers])
        return await call_next(request)

    app = create_app(tmp_path, middleware=[recorder])

    async def run():
        headers = [(b"host", b"localhost"), (b"origin", b"http://localhost"),
                   (b"x-keep-me", b"yes")] + [
            (name.encode("ascii"), b"spoofed") for name in INTERNAL_NAMES
        ]
        scope = {"type": "websocket", "path": "/ws", "headers": headers, "query_string": b""}
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app(scope, conn.receive, conn.send)
        assert conn.sent and conn.sent[0] == {"type": "websocket.accept"}
        assert seen_by_middleware
        assert not (set(seen_by_middleware[0]) & INTERNAL_NAMES)
        assert KEEPER in seen_by_middleware[0]
        seen_by_socket = json.loads(seen_file.read_text())
        assert not (set(seen_by_socket) & INTERNAL_NAMES)
        assert KEEPER in seen_by_socket
        await app.isr.close()

    asyncio.run(run())
