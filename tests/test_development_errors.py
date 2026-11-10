import asyncio
import re
from urllib.parse import quote

import httpx
import pytest

from pynext import create_app
from pynext.app import _dev_error_digest, _render_dev_traceback
from pynext.build import write_manifest
from pynext.server import start_native


def _run_client(app, mode):
    server = None
    if mode == "asgi":
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    else:
        server = start_native(app, asyncio.get_running_loop(), port=0)
        client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
    return client, server


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_dev_error_page_header_editor_links_and_occurrences(tmp_path, mode):
    api = tmp_path / "app"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ValueError("<private error>")\n'
    )
    write_manifest(tmp_path)
    route_path = quote(str(api / "route.py"), safe="/:")

    async def run():
        app = create_app(tmp_path, debug=True)
        client, server = _run_client(app, mode)
        try:
            async with client:
                first = await client.get("/", headers={"x-request-id": "dev-req-1"})
                assert first.status_code == 500
                assert "<dt>Request ID</dt><dd>dev-req-1</dd>" in first.text
                assert "<dt>Route</dt><dd>/</dd>" in first.text
                assert re.search(r"<dt>Digest</dt><dd>[0-9a-f]{12}</dd>", first.text)
                assert re.search(r"<dt>Time</dt><dd>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*Z</dd>",
                                 first.text)
                assert "Seen 1 time in this process" in first.text
                assert "&lt;private error&gt;" in first.text
                assert "<private error>" not in first.text
                # Traceback frames link to the editor at file:line.
                assert f'<a href="vscode://file/{route_path}:' in first.text
                assert "Traceback truncated" not in first.text
                second = await client.get("/", headers={"x-request-id": "dev-req-2"})
                assert "Seen 2 times in this process" in second.text
                assert "<dt>Request ID</dt><dd>dev-req-2</dd>" in second.text
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_dev_error_page_truncation_is_announced(tmp_path, mode):
    api = tmp_path / "app"
    api.mkdir()
    # Distinct frames: traceback.format_exception collapses identical
    # repeated frames ("[Previous line repeated N more times]"), so a
    # plain recursive boom() formats to under 500 chars and never
    # triggers the 16 KiB cut. 300 distinct functions keep every frame.
    lines = [
        "from pynext import route",
        "@route(\"GET\")",
        "def get():",
        "    return boom_0()",
    ]
    for i in range(300):
        lines.append(f"def boom_{i}():")
        if i == 299:
            lines.append('    raise ValueError("deep boom")')
        else:
            lines.append(f"    return boom_{i + 1}()")
    (api / "route.py").write_text("\n".join(lines) + "\n")
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, debug=True)
        client, server = _run_client(app, mode)
        try:
            async with client:
                response = await client.get("/")
                assert response.status_code == 500
                assert "Traceback truncated: showing the last 16,384 of" in response.text
                assert "characters omitted from the start" in response.text
                # The tail (nearest the failure) survives the cut.
                assert "deep boom" in response.text
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_dev_error_page_editor_url_is_configurable(tmp_path, monkeypatch):
    api = tmp_path / "app"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ValueError("boom")\n'
    )
    write_manifest(tmp_path)
    monkeypatch.setenv("PYNEXT_EDITOR_URL", "myeditor://open/{path}:{line}")

    async def run():
        app = create_app(tmp_path, debug=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 500
            assert "myeditor://open/" in response.text
            assert "vscode://file/" not in response.text
        await app.isr.close()
    asyncio.run(run())


def test_render_dev_traceback_links_frames_and_escapes_rest():
    text = ('Traceback (most recent call last):\n'
            '  File "/srv/app/page.py", line 12, in get\n'
            '    raise ValueError("<x>")\n'
            'During handling of the above exception, another exception occurred:\n'
            'ValueError: <x>\n')
    rendered = _render_dev_traceback(text)
    assert '<a href="vscode://file//srv/app/page.py:12">/srv/app/page.py</a>' in rendered
    assert "&lt;x&gt;" in rendered
    assert "<x>" not in rendered
    assert "During handling of the above exception" in rendered


def test_dev_error_digest_is_stable_and_message_specific():
    def make(message):
        try:
            raise ValueError(message)
        except ValueError as exc:
            return exc

    first = make("same")
    second = make("same")
    assert _dev_error_digest(first) == _dev_error_digest(second)
    assert re.fullmatch(r"[0-9a-f]{12}", _dev_error_digest(first))
    assert _dev_error_digest(make("different")) != _dev_error_digest(first)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_detailed_errors_are_opt_in_and_html_escaped(tmp_path, mode):
    api = tmp_path / "app"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import route\n@route("GET")\ndef get(): raise ValueError("<private error>")\n'
    )
    write_manifest(tmp_path)
    with pytest.raises(ValueError, match="debug cannot"):
        create_app(tmp_path, production=True, debug=True)

    async def run(debug):
        app = create_app(tmp_path, debug=debug)
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                response = await client.get("/")
                assert response.status_code == 500
                if debug:
                    assert "&lt;private error&gt;" in response.text
                    assert "<private error>" not in response.text
                    assert response.headers["cache-control"] == "no-store"
                else:
                    # Production body carries the correlation id + digest,
                    # never the exception itself.
                    assert response.text.startswith("Internal Server Error (request_id: ")
                    assert response.text.endswith(")")
                    assert f"request_id: {response.headers['x-request-id']}" in response.text
                    assert ", digest: " in response.text
                    assert "<private error>" not in response.text
                assert (await client.head("/")).content == b""
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run(False))
    asyncio.run(run(True))
