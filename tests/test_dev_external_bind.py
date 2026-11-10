"""S-P1-28 (M-19): dev server bound to an external address must warn at
startup and must not serve the detailed development error page.

The detailed dev page carries the traceback, file paths and the exception
message; with ``debug=True`` + ``--host 0.0.0.0`` that reaches anyone on
the network. The app therefore records its bind address
(:meth:`App.note_dev_bind_host`, called by ``server.serve`` once the
socket is bound), warns once, and serves the generic production-style
error body while externally bound.
"""

import asyncio
import logging

import httpx
import pytest

from pynext import create_app
from pynext.app import _is_external_bind_host
from pynext.build import write_manifest
from pynext.server import serve, start_native


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", False),
    ("::1", False),
    ("localhost", False),
    ("LOCALHOST", False),
    ("foo.localhost", False),
    ("0.0.0.0", True),
    ("::", True),
    ("192.168.1.10", True),
    ("10.0.0.5", True),
    ("example.com", True),
    (None, False),
    ("", False),
])
def test_is_external_bind_host(host, expected):
    assert _is_external_bind_host(host) is expected


def test_note_dev_bind_host_warns_once_for_external_debug_bind(tmp_path, caplog):
    _write_crashing_app(tmp_path)
    app = create_app(tmp_path, debug=True)
    try:
        with caplog.at_level(logging.WARNING, logger="pynext"):
            app.note_dev_bind_host("0.0.0.0")
            app.note_dev_bind_host("0.0.0.0")
        warnings = [r for r in caplog.records if "listening on 0.0.0.0" in r.getMessage()]
        assert len(warnings) == 1
        assert "debug=True" in warnings[0].message
        assert "127.0.0.1" in warnings[0].message
    finally:
        asyncio.run(app.isr.close())


def test_note_dev_bind_host_silent_for_loopback_and_production(tmp_path, caplog):
    _write_crashing_app(tmp_path)
    debug_app = create_app(tmp_path, debug=True)
    prod_app = create_app(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="pynext"):
            debug_app.note_dev_bind_host("127.0.0.1")
            prod_app.note_dev_bind_host("0.0.0.0")
        assert [r for r in caplog.records if "listening on" in r.getMessage()] == []
    finally:
        asyncio.run(debug_app.isr.close())
        asyncio.run(prod_app.isr.close())


def _write_crashing_app(tmp_path):
    api = tmp_path / "app"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ValueError("<private error>")\n'
    )
    write_manifest(tmp_path)


def _run_client(app, mode):
    server = None
    if mode == "asgi":
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    else:
        server = start_native(app, asyncio.get_running_loop(), port=0)
        client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
    return client, server


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_dev_error_page_suppressed_when_externally_bound(tmp_path, mode, caplog):
    _write_crashing_app(tmp_path)

    async def run():
        app = create_app(tmp_path, debug=True)
        app.note_dev_bind_host("0.0.0.0")
        client, server = _run_client(app, mode)
        try:
            async with client:
                response = await client.get("/", headers={"x-request-id": "dev-ext-1"})
                assert response.status_code == 500
                # Generic production-style body: correlation ids only.
                assert response.text.startswith("Internal Server Error (request_id: dev-ext-1")
                assert ", digest: " in response.text
                # No internals leak: no traceback, exception message or paths.
                assert "Traceback" not in response.text
                assert "<private error>" not in response.text
                assert "route.py" not in response.text
                assert "Development error" not in response.text
                assert "Seen 1 time" not in response.text
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    with caplog.at_level(logging.WARNING, logger="pynext"):
        asyncio.run(run())
    assert any("suppressed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_dev_error_page_still_detailed_when_loopback_bound(tmp_path, mode):
    _write_crashing_app(tmp_path)

    async def run():
        app = create_app(tmp_path, debug=True)
        app.note_dev_bind_host("127.0.0.1")
        client, server = _run_client(app, mode)
        try:
            async with client:
                response = await client.get("/")
                assert response.status_code == 500
                assert "Development error" in response.text
                assert "&lt;private error&gt;" in response.text
                assert "Seen 1 time in this process" in response.text
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_serve_warns_on_external_debug_bind(tmp_path, caplog):
    _write_crashing_app(tmp_path)

    async def run():
        app = create_app(tmp_path, debug=True)
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            serve(app, "0.0.0.0", 0, stop_event=stop, on_ready=ready.set_result))
        try:
            address = await asyncio.wait_for(ready, timeout=30)
            # The server is up and externally bound; a local request still
            # gets the generic page, never the traceback.
            port = str(address).rsplit(":", 1)[1]
            async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                response = await client.get("/")
            assert response.status_code == 500
            assert response.text.startswith("Internal Server Error (request_id: ")
            assert "Traceback" not in response.text
            assert "<private error>" not in response.text
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=30)
            await app.isr.close()
    with caplog.at_level(logging.WARNING, logger="pynext"):
        asyncio.run(run())
    assert any("listening on 0.0.0.0" in r.getMessage() for r in caplog.records)
