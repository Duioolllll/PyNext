"""The native static shortcut must honor the ASGI CORS policy."""
import asyncio

import httpx
import pytest

from pynext import SecurityConfig, create_app
from pynext.build import write_manifest
from pynext.server import start_native


@pytest.mark.parametrize("origins,credentials", [
    ((), False), (("https://client.example",), False), (("*",), False), (("*",), True),
])
def test_native_static_cors_matches_asgi(tmp_path, origins, credentials):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "hello")\n')
    (tmp_path / "public").mkdir()
    (tmp_path / "public/data.txt").write_text("0123456789")
    write_manifest(tmp_path)

    async def exercise():
        app = create_app(tmp_path, production=True, security=SecurityConfig(
            cors_allowed_origins=origins, cors_allow_credentials=credentials))
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}") as native, \
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as asgi:
                for path in ("/", "/data.txt"):
                    etag = (await asgi.get(path)).headers["etag"]
                    for origin in ("https://client.example", "https://denied.example", "http://test:80"):
                        for method, extra in (
                            ("GET", {}), ("HEAD", {}), ("GET", {"range": "bytes=1-3"}),
                            ("GET", {"if-none-match": etag}),
                            ("OPTIONS", {"access-control-request-method": "GET"}),
                        ):
                            headers = {"host": "test", "origin": origin, **extra}
                            left = await native.request(method, path, headers=headers)
                            right = await asgi.request(method, path, headers=headers)
                            assert left.status_code == right.status_code, (path, method, origin, extra)
                            assert left.content == right.content
                            relevant = lambda response: {
                                key: value for key, value in response.headers.items()
                                if key.startswith("access-control-") or key == "vary"}
                            assert relevant(left) == relevant(right), (path, method, origin, extra)
                            if origin == "https://client.example" and origins:
                                assert "access-control-allow-origin" in left.headers
                            if not origins and origin != "http://test:80":
                                assert "access-control-allow-origin" not in left.headers
        finally:
            await asyncio.to_thread(server.stop)

    asyncio.run(exercise())
