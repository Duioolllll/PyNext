import asyncio
import json

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


def make_app(root):
    app = root / "app"
    (app / "blog" / "[slug]").mkdir(parents=True)
    (app / "docs" / "[...path]").mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    (app / "blog" / "[slug]" / "page.py").write_text(
        'from pynext import page, h\n'
        'def generate_static_params(): return [{"slug": "hello"}, {"slug": "한글"}]\n'
        '@page(render="ssg")\nasync def Page(params): return h("p", {}, params["slug"])\n'
    )
    (app / "docs" / "[...path]" / "page.py").write_text(
        'from pynext import page, h\n'
        'async def generate_static_params(): return [{"path": ["a", "b"]}]\n'
        '@page(render="ssg")\ndef Page(params): return h("p", {}, "/".join(params["path"]))\n'
    )
    return app


async def check_static_responses(client):
    response = await client.get("/", headers={"authorization": "Bearer example", "cookie": "session=example"})
    assert response.status_code == 200 and response.text == "<p>built</p>"
    assert (await client.get("/blog/hello?query=ignored")).text == "<p>hello</p>"
    assert (await client.get("/blog/%ED%95%9C%EA%B8%80")).text == "<p>한글</p>"
    assert (await client.get("/docs/a/b")).text == "<p>a/b</p>"
    assert (await client.get("/blog/not-generated")).status_code == 404
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/blog/a%2Fb")).status_code == 400
    head = await client.head("/")
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(b"<p>built</p>")
    assert (await client.post("/")).status_code == 405
    assert (await client.options("/")).status_code == 204
    assert (await client.post("/", content=b"x" * 1_048_577)).status_code == 413
    assert (await client.request("GET", "/", content=b"x" * 1_048_577)).status_code == 413


def test_ssg_in_asgi_and_axum_and_python_bypass(tmp_path):
    make_app(tmp_path)
    manifest = write_manifest(tmp_path)
    assert set(manifest["static_pages"]) == {"/", "/blog/hello", "/blog/한글", "/docs/a/b"}

    async def run():
        app = create_app(tmp_path, production=True)
        # Production SSG must not execute the page renderer again.
        async def fail_render(*args, **kwargs):
            raise AssertionError("SSG should not render on request")
        app.render_page = fail_render
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await check_static_responses(client)

        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                await check_static_responses(client)
                # Even a broken Python dispatch must not affect native static hits.
                async def fail_dispatch(request):
                    raise AssertionError("native static hit crossed the Python boundary")
                app.dispatch = fail_dispatch
                assert (await client.get("/blog/hello")).text == "<p>hello</p>"
                head = await client.head("/docs/a/b")
                assert head.status_code == 200 and int(head.headers["content-length"]) == len(b"<p>a/b</p>")
                assert (await client.get("/blog/missing")).status_code == 500
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_ssg_artifacts_integrity_and_failed_build_is_atomic(tmp_path):
    app = make_app(tmp_path)
    manifest = write_manifest(tmp_path)
    path = tmp_path / ".pynext" / "manifest.json"
    original = path.read_bytes()
    original_blobs = {entry["sha256"]: (tmp_path / ".pynext" / "static" / (entry["sha256"] + ".html")).read_bytes()
                      for entry in manifest["static_pages"].values()}
    source = app / "blog" / "[slug]" / "page.py"
    source.write_text(source.read_text().replace('return [{"slug": "hello"}, {"slug": "한글"}]', 'raise RuntimeError("build failed")'))
    with pytest.raises(RuntimeError, match="build failed"):
        write_manifest(tmp_path)
    assert path.read_bytes() == original
    for digest, content in original_blobs.items():
        assert (tmp_path / ".pynext" / "static" / (digest + ".html")).read_bytes() == content
    # Restore the input and deliberately corrupt a published artifact.
    source.write_text(source.read_text().replace('raise RuntimeError("build failed")', 'return [{"slug": "hello"}, {"slug": "한글"}]'))
    digest = manifest["static_pages"]["/"]["sha256"]
    (tmp_path / ".pynext" / "static" / (digest + ".html")).write_text("corrupted")
    with pytest.raises(ValueError, match="integrity check"):
        create_app(tmp_path, production=True)


@pytest.mark.parametrize("generated", [
    '[{"slug": "a"}, {"slug": "a"}]',
    '[{"slug": ["a"]}]',
    '[{"slug": "a/b"}]',
    '[{"slug": ".."}]',
    '[{"other": "a"}]',
    '{"slug": "a"}',
])
def test_invalid_static_params_fail_build(tmp_path, generated):
    app = make_app(tmp_path)
    source = app / "blog" / "[slug]" / "page.py"
    source.write_text(source.read_text().replace('[{"slug": "hello"}, {"slug": "한글"}]', generated))
    with pytest.raises(ValueError):
        write_manifest(tmp_path)


def test_ssg_rejects_request_access_and_shadowed_paths(tmp_path):
    app = make_app(tmp_path)
    source = app / "page.py"
    source.write_text(source.read_text().replace('def Page():', 'def Page(request):'))
    with pytest.raises(ValueError, match="cannot accept request"):
        write_manifest(tmp_path)
    source.write_text(source.read_text().replace('def Page(request):', 'def Page():'))
    shadow = app / "blog" / "hello"
    shadow.mkdir()
    (shadow / "page.py").write_text('from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "shadow")\n')
    with pytest.raises(ValueError, match="not reachable"):
        write_manifest(tmp_path)


@pytest.mark.parametrize("linked_dir", [".pynext", ".pynext/static"])
def test_build_rejects_symlinked_output_directories(tmp_path, linked_dir):
    project = tmp_path / "project"
    project.mkdir()
    make_app(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / linked_dir
    link.parent.mkdir(exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Directory symlinks unavailable")
    with pytest.raises(ValueError, match="must be a directory"):
        write_manifest(project)
    assert list(outside.iterdir()) == []
