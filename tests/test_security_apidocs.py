"""S-P1-34 — API docs off by default + production 500 masking (FastAPI lesson 4).

PyNext ships no API schema/documentation endpoints at all: a repo-wide
search finds no ``/openapi.json``, ``/docs`` or ``/redoc`` route, so there
is nothing to disable — these tests pin that absence in both production
and development mode, so a future docs endpoint cannot silently appear
exposed. The production 500 body is already masked to correlation id +
digest (see ``App.error_response``); these tests pin the masking against
an exception whose message carries a file path and a secret-looking
token, on both the ASGI and the native (Axum) transports.
"""

import asyncio
import re

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native

# Paths FastAPI/Starlette expose by default; PyNext must answer 404 for
# all of them unless an application route defines them (none do here).
DOC_PATHS = ("/openapi.json", "/docs", "/redoc")

# A message that must never reach a client: an absolute internal path and
# a secret-looking token, plus a traceback marker.
SECRET_MESSAGE = (
    "boom at /home/hoyeong/secret/config.py line 42 "
    "with token sk-live-7f3a9c1e: Traceback (most recent call last)"
)

MASKED_500 = re.compile(
    r"Internal Server Error \(request_id: ([0-9a-f-]+), digest: ([0-9a-f]{16})\)"
)


def _write_home_route(root, body):
    api = root / "app"
    api.mkdir(parents=True, exist_ok=True)
    (api / "route.py").write_text(
        "from pynext import JSONResponse, route\n@route(\"GET\")\n"
        f"def get(): {body}\n"
    )
    write_manifest(root)


def _run_client(app, mode):
    server = None
    if mode == "asgi":
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
    else:
        server = start_native(app, asyncio.get_running_loop(), port=0)
        client = httpx.AsyncClient(
            base_url=f"http://{server.address}", trust_env=False
        )
    return client, server


@pytest.mark.parametrize("path", DOC_PATHS)
def test_no_api_docs_endpoints_in_production(tmp_path, path):
    """Production serves no API docs/schema endpoint (S-P1-34 (a))."""
    _write_home_route(tmp_path, 'return JSONResponse({"ok": True})')
    app = create_app(tmp_path, production=True)

    async def run():
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.get(path)
        finally:
            await app.isr.close()

    response = asyncio.run(run())
    assert response.status_code == 404
    assert "openapi" not in response.text.lower()
    assert "swagger" not in response.text.lower()


@pytest.mark.parametrize("path", DOC_PATHS)
def test_no_api_docs_endpoints_in_development(tmp_path, path):
    """Development (non-debug) also exposes no docs endpoint by default."""
    _write_home_route(tmp_path, 'return JSONResponse({"ok": True})')
    app = create_app(tmp_path)

    async def run():
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.get(path)
        finally:
            await app.isr.close()

    response = asyncio.run(run())
    assert response.status_code == 404
    assert "openapi" not in response.text.lower()
    assert "swagger" not in response.text.lower()


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_production_500_masks_paths_secrets_and_tracebacks(tmp_path, mode):
    """Production 500 carries only request_id + digest (S-P1-34 (b)).

    The raised message contains an absolute file path, a secret-looking
    token and a traceback marker; none of them may appear in the body.
    """
    _write_home_route(
        tmp_path, f"raise ValueError({SECRET_MESSAGE!r})"
    )
    app = create_app(tmp_path, production=True)

    async def run():
        client, server = _run_client(app, mode)
        try:
            async with client:
                return await client.get("/")
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()

    response = asyncio.run(run())
    assert response.status_code == 500
    match = MASKED_500.fullmatch(response.text)
    assert match, response.text
    request_id, _digest = match.groups()
    assert response.headers["x-request-id"] == request_id
    for leaked in (
        "/home/hoyeong/secret/config.py",
        "sk-live-7f3a9c1e",
        "Traceback",
        'File "',
        "config.py",
    ):
        assert leaked not in response.text
