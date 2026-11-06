import asyncio
import os
from pathlib import Path

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


def make_project(root):
    (root / "app" / "[slug]").mkdir(parents=True)
    (root / "app" / "page.py").write_text(
        'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "home")\n'
    )
    (root / "app" / "[slug]" / "page.py").write_text(
        'from pynext import page, h\n@page()\ndef Page(params): return h("p", {}, params["slug"])\n'
    )
    public = root / "public"
    (public / "styles").mkdir(parents=True)
    (public / "styles" / "site.css").write_bytes(b"body { color: navy; }")
    (public / "logo.txt").write_bytes(b"public asset")
    (public / "한 글.txt").write_text("hello", encoding="utf-8")
    (public / "what#.bin").write_bytes(b"\x00\xff")
    if os.name != "nt":
        (public / "what?.bin").write_bytes(b"\x00\xff")
    return public


async def check_responses(client):
    response = await client.get("/styles/site.css?version=1")
    assert response.status_code == 200 and response.content == b"body { color: navy; }"
    assert response.headers["content-type"] == "text/css; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
    head = await client.head("/styles/site.css")
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(response.content)
    assert (await client.get("/%ED%95%9C%20%EA%B8%80.txt")).text == "hello"
    binary = await client.get("/what%23.bin")
    assert binary.content == b"\x00\xff" and binary.headers["content-type"] == "application/octet-stream"
    if os.name != "nt":
        assert (await client.get("/what%3F.bin")).content == b"\x00\xff"
    assert (await client.get("/logo.txt")).content == b"public asset"
    assert (await client.get("/something-else")).text == "<p>something-else</p>"
    assert (await client.get("/styles%2Fsite.css")).status_code == 400
    assert (await client.post("/logo.txt")).status_code == 405
    assert (await client.options("/logo.txt")).status_code == 204
    assert (await client.request("GET", "/logo.txt", content=b"x" * 1_048_577)).status_code == 413


@pytest.mark.parametrize("production", [False, True])
def test_public_assets_in_asgi_and_axum_with_native_bypass(tmp_path, production):
    make_project(tmp_path)
    if production:
        manifest = write_manifest(tmp_path)
        assert "/styles/site.css" in manifest["assets"]

    async def run():
        app = create_app(tmp_path, production=production)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await check_responses(client)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                await check_responses(client)
                async def fail_dispatch(request):
                    raise AssertionError("native public file crossed the Python boundary")
                app.dispatch = fail_dispatch
                assert (await client.get("/styles/site.css")).content == b"body { color: navy; }"
                head = await client.head("/what%23.bin")
                assert head.status_code == 200 and head.content == b""
                assert int(head.headers["content-length"]) == 2
                assert (await client.get("/something-else")).status_code == 500
                error_head = await client.head("/something-else")
                assert error_head.status_code == 500 and error_head.content == b""
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_public_assets_detect_changes_and_corruption(tmp_path):
    public = make_project(tmp_path)
    manifest = write_manifest(tmp_path)
    source = public / "logo.txt"
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed since build"):
        create_app(tmp_path, production=True)
    source.write_bytes(b"public asset")
    digest = manifest["assets"]["/logo.txt"]["sha256"]
    (tmp_path / ".pynext" / "static" / (digest + ".asset")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity check"):
        create_app(tmp_path, production=True)


def test_public_path_conflicts_are_rejected(tmp_path):
    make_project(tmp_path)
    route = tmp_path / "app" / "logo.txt"
    route.mkdir()
    (route / "page.py").write_text('from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "page")\n')
    with pytest.raises(ValueError, match="conflicts with a page or API route"):
        write_manifest(tmp_path)
    (route / "page.py").unlink()
    dynamic = tmp_path / "app" / "[slug]" / "page.py"
    dynamic.write_text(
        'from pynext import page, h\ndef generate_static_params(): return [{"slug": "logo.txt"}]\n'
        '@page(render="ssg")\ndef Page(params): return h("p", {}, params["slug"])\n'
    )
    with pytest.raises(ValueError, match="shadows a generated page"):
        write_manifest(tmp_path)


@pytest.mark.parametrize("kind", ["file", "directory", "root"])
def test_public_symlinks_are_rejected(tmp_path, kind):
    public = make_project(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("secret")
    if kind == "file":
        link, destination = public / "link.txt", target / "secret.txt"
    elif kind == "directory":
        link, destination = public / "link", target
    else:
        (public / "styles" / "site.css").unlink()
        (public / "styles").rmdir()
        for path in public.iterdir():
            path.unlink()
        public.rmdir()
        link, destination = public, target
    try:
        link.symlink_to(destination, target_is_directory=kind != "file")
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError, match="symlink|symbolic link"):
        write_manifest(tmp_path)


def test_public_hidden_and_oversized_files_are_rejected(tmp_path, monkeypatch):
    public = make_project(tmp_path)
    hidden = public / ".env"
    hidden.write_text("not public")
    with pytest.raises(ValueError, match="Invalid public"):
        write_manifest(tmp_path)
    hidden.unlink()
    monkeypatch.setattr("pynext.assets.MAX_PUBLIC_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="exceeds"):
        write_manifest(tmp_path)
