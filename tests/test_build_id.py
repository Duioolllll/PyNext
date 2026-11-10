"""Build ID + deployment ID (Phase 2, P0-3): manifest fields, BUILD_ID
file, and skew-protection headers on static assets, HTML and API
responses, on both the ASGI and the native transports."""
import asyncio
import re

import httpx
import pytest

from pynext import create_app
from pynext.build import (
    BUILD_ID_FILE,
    build_id_for,
    generate_build_id,
    load_manifest,
    read_build_id,
    resolve_deployment_id,
    write_manifest,
)
from pynext.server import start_native

BUILD_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def make_project(root, *, api=False, ssg=False):
    (root / "app").mkdir(parents=True)
    if ssg:
        (root / "app" / "page.py").write_text(
            'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "home")\n'
        )
    else:
        (root / "app" / "page.py").write_text(
            'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "home")\n'
        )
    if api:
        (root / "app" / "api").mkdir(exist_ok=True)
        (root / "app" / "api" / "route.py").write_text(
            'from pynext import JSONResponse, route\n@route("GET")\n'
            'async def get(request): return JSONResponse({"ok": True})\n'
        )
    public = root / "public"
    public.mkdir(exist_ok=True)
    (public / "logo.txt").write_bytes(b"public asset")
    return root


def test_build_id_is_deterministic_and_source_bound(tmp_path):
    make_project(tmp_path)
    first = write_manifest(tmp_path)
    build_id = first["build_id"]
    assert BUILD_ID_RE.match(build_id)
    assert "ad" not in build_id.lower()
    assert first["deployment_id"] is None
    # .pynext/BUILD_ID holds the manifest-integrity seal (SHA-256 of the
    # manifest bytes), not the build ID; the ID itself lives in the manifest.
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    assert (tmp_path / BUILD_ID_FILE).read_text(encoding="ascii").strip() == \
        build_id_for(manifest_path.read_bytes())
    assert read_build_id(tmp_path) == build_id
    # A rebuild of unchanged sources keeps the ID (reproducible).
    second = write_manifest(tmp_path)
    assert second["build_id"] == build_id
    # Any source change gives a new ID.
    (tmp_path / "app" / "page.py").write_text(
        'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "changed")\n'
    )
    third = write_manifest(tmp_path)
    assert third["build_id"] != build_id
    assert BUILD_ID_RE.match(third["build_id"])
    assert (tmp_path / BUILD_ID_FILE).read_text(encoding="ascii").strip() == \
        build_id_for(manifest_path.read_bytes())


def test_generate_build_id_avoids_ad_substring():
    # The generator re-hashes until the ID has no "ad": feed it
    # manifests until one would have produced "ad", and check the
    # returned ID is still clean. (Brute-forcing the input is cheap:
    # ~1 in 128 random digests contains "ad" in the first 16 chars.)
    for index in range(512):
        manifest = {"sources": {f"app/page.py": f"{index:064x}"},
                    "public_sources": {}, "client_sources": {}}
        build_id = generate_build_id(manifest)
        assert BUILD_ID_RE.match(build_id)
        assert "ad" not in build_id.lower()


def test_deployment_id_from_env_and_explicit_arg(tmp_path, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setenv("PYNEXT_DEPLOYMENT_ID", "blue-1")
    manifest = write_manifest(tmp_path)
    assert manifest["deployment_id"] == "blue-1"
    # An explicit value wins over the environment.
    manifest = write_manifest(tmp_path, deployment_id="green-2")
    assert manifest["deployment_id"] == "green-2"
    monkeypatch.delenv("PYNEXT_DEPLOYMENT_ID")
    assert write_manifest(tmp_path)["deployment_id"] is None


@pytest.mark.parametrize("bad", ["", "has space", "tab\there", "café", "x" * 129, "new\nline"])
def test_deployment_id_validation(bad):
    with pytest.raises(ValueError, match="Deployment ID"):
        resolve_deployment_id(bad)


def test_load_manifest_rejects_build_id_mismatch(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)
    create_app(tmp_path, production=True)
    (tmp_path / BUILD_ID_FILE).write_text("0" * 16 + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="Incomplete build"):
        create_app(tmp_path, production=True)


def test_load_manifest_rejects_invalid_build_id(tmp_path):
    make_project(tmp_path)
    manifest = write_manifest(tmp_path)
    saved = tmp_path / ".pynext" / "manifest.json"
    text = saved.read_text(encoding="utf-8").replace(manifest["build_id"], "not-a-build-id")
    saved.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid build ID"):
        load_manifest(tmp_path)


def _headers_for(client, path):
    return client.get(path).headers


@pytest.mark.parametrize("production", [False, True])
def test_build_headers_on_asgi_responses(tmp_path, production):
    make_project(tmp_path, api=True, ssg=True)
    manifest = write_manifest(tmp_path, deployment_id="dep-9") if production else None

    async def run():
        app = create_app(tmp_path, production=production)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            page = await client.get("/")
            assert page.status_code == 200
            api = await client.get("/api")
            assert api.status_code == 200
            asset = await client.get("/logo.txt")
            assert asset.status_code == 200
            if production:
                assert page.headers["x-pynext-build-id"] == manifest["build_id"]
                assert api.headers["x-pynext-build-id"] == manifest["build_id"]
                assert asset.headers["x-pynext-build-id"] == manifest["build_id"]
                assert page.headers["x-pynext-deployment-id"] == "dep-9"
                assert api.headers["x-pynext-deployment-id"] == "dep-9"
                assert asset.headers["x-pynext-deployment-id"] == "dep-9"
            else:
                assert "x-pynext-build-id" not in page.headers
                assert "x-pynext-deployment-id" not in api.headers
                assert "x-pynext-build-id" not in asset.headers

    asyncio.run(run())


def test_build_headers_on_native_static_responses(tmp_path):
    make_project(tmp_path, api=True, ssg=True)
    manifest = write_manifest(tmp_path, deployment_id="dep-9")

    async def run():
        app = create_app(tmp_path, production=True)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                # Static HTML and public assets are served by the Rust
                # layer itself, without crossing the Python boundary.
                page = await client.get("/")
                assert page.status_code == 200
                assert page.headers["x-pynext-build-id"] == manifest["build_id"]
                assert page.headers["x-pynext-deployment-id"] == "dep-9"
                asset = await client.get("/logo.txt")
                assert asset.status_code == 200
                assert asset.headers["x-pynext-build-id"] == manifest["build_id"]
                assert asset.headers["x-pynext-deployment-id"] == "dep-9"
                # Bridge responses (API) go through Python dispatch.
                api = await client.get("/api")
                assert api.status_code == 200
                assert api.headers["x-pynext-build-id"] == manifest["build_id"]
                assert api.headers["x-pynext-deployment-id"] == "dep-9"
        finally:
            await asyncio.to_thread(server.stop)

    asyncio.run(run())


def test_cli_build_accepts_deployment_id(tmp_path, monkeypatch, capsys):
    import sys

    from pynext import cli

    make_project(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path),
                                      "--deployment-id", "canary-3"])
    cli.main()
    out = capsys.readouterr().out
    manifest = load_manifest(tmp_path)
    assert manifest["deployment_id"] == "canary-3"
    assert f"(build {manifest['build_id']})" in out
    assert read_build_id(tmp_path) == manifest["build_id"]
