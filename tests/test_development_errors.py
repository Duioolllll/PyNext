import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


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
