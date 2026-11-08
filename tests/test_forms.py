import asyncio

import httpx
import pytest

from pynext import SecurityConfig, create_app
from pynext.server import start_native


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_form_uploads_and_csrf(tmp_path, mode):
    api = tmp_path / "app" / "api"
    api.mkdir(parents=True)
    (api / "route.py").write_text(
        'from pynext import JSONResponse, UploadFile, route\n'
        '@route("POST")\n'
        'async def post(request):\n'
        '    form = await request.form()\n'
        '    return JSONResponse({key: [\n'
        '        {"filename": v.filename, "type": v.content_type, "data": v.data.decode("utf-8")}\n'
        '        if isinstance(v, UploadFile) else v for v in values]\n'
        '        for key, values in form.items()})\n'
    )
    app = create_app(tmp_path, security=SecurityConfig(csrf_enabled=True))

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                client.cookies.set("pynext_csrf", "secret")
                response = await client.post("/api", files=[
                    ("csrf_token", (None, "secret")), ("tag", (None, "한글")),
                    ("tag", (None, "two")), ("upload", ("note.txt", b"hello", "text/plain")),
                ])
                assert response.status_code == 200
                assert response.json()["tag"] == ["한글", "two"]
                assert response.json()["upload"] == [{"filename": "note.txt", "type": "text/plain", "data": "hello"}]
                response = await client.post("/api", data={"csrf_token": "secret", "name": "한글"})
                assert response.status_code == 200 and response.json()["name"] == ["한글"]
                response = await client.post("/api", files={"csrf_token": (None, "wrong")})
                assert response.status_code == 403
                for content_type, body in [
                    ("multipart/form-data", b"broken"),
                    ("multipart/form-data; boundary=abc", b"--abc\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\nincomplete"),
                    ("application/x-www-form-urlencoded", b"name=%FF"),
                ]:
                    response = await client.post("/api", content=body, headers={
                        "content-type": content_type, "x-csrf-token": "secret"
                    })
                    assert response.status_code == 400
                response = await client.post("/api", content=b"x" * 1_048_577,
                                             headers={"content-type": "application/x-www-form-urlencoded",
                                                      "x-csrf-token": "secret"})
                assert response.status_code == 413
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
