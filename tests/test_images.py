import asyncio
import io

import httpx
import pytest

Image = pytest.importorskip("PIL.Image")

from pynext import create_app
from pynext.build import write_manifest
from pynext.images import MAX_IMAGE_QUALITY, MAX_IMAGE_WIDTH, optimize_image
from pynext.server import start_native


def _sample_png():
    buffer = io.BytesIO()
    Image.new("RGB", (80, 40), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_optimize_image_rejects_out_of_range_width_and_quality():
    # Review #4 #7: width 0 used to reach thumbnail() as a ZeroDivisionError;
    # quality outside 1..95 was silently accepted. Both are now a clear
    # ValueError from optimize_image itself (defense in depth: the HTTP
    # route already guards the same range).
    data = _sample_png()
    assert optimize_image(data, 1, 1)
    assert optimize_image(data, MAX_IMAGE_WIDTH, MAX_IMAGE_QUALITY)
    for width, quality in [(0, 80), (-1, 80), (MAX_IMAGE_WIDTH + 1, 80),
                           (20, 0), (20, MAX_IMAGE_QUALITY + 1), (20, -5),
                           (20.5, 80), (20, 80.5), (True, 80), (20, "80")]:
        with pytest.raises(ValueError, match="must be an integer between 1 and"):
            optimize_image(data, width, quality)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_local_image_optimization_and_bounds(tmp_path, mode):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    public = tmp_path / "public"
    public.mkdir()
    Image.new("RGB", (80, 40), "red").save(public / "sample.png")
    (public / "bad.png").write_bytes(b"not an image")
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                url = "/_pynext/image?src=%2Fsample.png&w=20&q=80"
                response = await client.get(url)
                assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
                with Image.open(io.BytesIO(response.content)) as result:
                    assert result.size == (20, 10) and result.format == "WEBP"
                assert (await client.get(url, headers={"if-none-match": response.headers["etag"]})).status_code == 304
                head = await client.head(url)
                assert head.status_code == 200 and head.content == b""
                assert int(head.headers["content-length"]) == len(response.content)
                assert (await client.get("/_pynext/image?src=%2Fmissing.png&w=20")).status_code == 404
                for invalid in ("src=https%3A%2F%2Fexample.com%2Fa&w=20", "src=%2Fsample.png&w=0",
                                "src=%2Fsample.png&w=20&w=30", "src=%2Fbad.png&w=20"):
                    assert (await client.get("/_pynext/image?" + invalid)).status_code in {400, 404}
                assert (await client.post(url)).status_code == 405
        finally:
            if server:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
