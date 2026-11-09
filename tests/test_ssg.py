from conftest import page_html
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
        '@page(render="ssg")\nasync def Page(params): return h("p", {}, params["slug"])\n',
        encoding="utf-8",
    )
    (app / "docs" / "[...path]" / "page.py").write_text(
        'from pynext import page, h\n'
        'async def generate_static_params(): return [{"path": ["a", "b"]}]\n'
        '@page(render="ssg")\ndef Page(params): return h("p", {}, "/".join(params["path"]))\n'
    )
    return app


async def check_static_responses(client):
    response = await client.get("/", headers={"authorization": "Bearer example", "cookie": "session=example"})
    assert response.status_code == 200 and response.text == page_html("<p>built</p>")
    assert response.headers["vary"] == "Cookie, Authorization"
    assert response.headers["content-security-policy"] == "script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    assert (await client.get("/blog/hello?query=ignored")).text == page_html("<p>hello</p>")
    assert (await client.get("/blog/%ED%95%9C%EA%B8%80")).text == page_html("<p>한글</p>")
    assert (await client.get("/docs/a/b")).text == page_html("<p>a/b</p>")
    assert (await client.get("/blog/not-generated")).status_code == 404
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/blog/a%2Fb")).status_code == 400
    head = await client.head("/")
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(page_html("<p>built</p>").encode())
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
                assert (await client.get("/blog/hello")).text == page_html("<p>hello</p>")
                head = await client.head("/docs/a/b")
                assert head.status_code == 200 and int(head.headers["content-length"]) == len(page_html("<p>a/b</p>").encode())
                assert (await client.get("/blog/missing")).status_code == 500
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_corrupt_manifest_fails_with_a_clear_build_error(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    manifest = tmp_path / ".pynext" / "manifest.json"
    manifest.write_text("{not valid json")
    from pynext.build import load_manifest

    with pytest.raises(ValueError, match="Invalid build manifest"):
        load_manifest(tmp_path)
    manifest.write_bytes(b"\xff\xfe invalid utf-8 \x80\x81")
    with pytest.raises(ValueError, match="Invalid build manifest"):
        load_manifest(tmp_path)


def test_build_outputs_are_world_readable(tmp_path):
    # _atomic_write used to leave files 0600 (NamedTemporaryFile's
    # default); a build done by one user could then not be read by the
    # server process running as another user.
    make_app(tmp_path)
    manifest = write_manifest(tmp_path)
    outputs = [tmp_path / ".pynext" / "manifest.json"]
    outputs += [tmp_path / ".pynext" / "static" / f"{entry['sha256']}.html"
                for entry in manifest["static_pages"].values()]
    assert outputs[1:]
    for output in outputs:
        assert output.stat().st_mode & 0o777 == 0o644, output


def test_ssg_artifacts_integrity_and_failed_build_is_atomic(tmp_path):
    app = make_app(tmp_path)
    manifest = write_manifest(tmp_path)
    path = tmp_path / ".pynext" / "manifest.json"
    original = path.read_bytes()
    original_blobs = {entry["sha256"]: (tmp_path / ".pynext" / "static" / (entry["sha256"] + ".html")).read_bytes()
                      for entry in manifest["static_pages"].values()}
    source = app / "blog" / "[slug]" / "page.py"
    source.write_text(source.read_text(encoding="utf-8").replace('return [{"slug": "hello"}, {"slug": "한글"}]', 'raise RuntimeError("build failed")'), encoding="utf-8")
    with pytest.raises(RuntimeError, match="build failed"):
        write_manifest(tmp_path)
    assert path.read_bytes() == original
    for digest, content in original_blobs.items():
        assert (tmp_path / ".pynext" / "static" / (digest + ".html")).read_bytes() == content
    # Restore the input and deliberately corrupt a published artifact.
    source.write_text(source.read_text(encoding="utf-8").replace('raise RuntimeError("build failed")', 'return [{"slug": "hello"}, {"slug": "한글"}]'), encoding="utf-8")
    digest = manifest["static_pages"]["/"]["sha256"]
    (tmp_path / ".pynext" / "static" / (digest + ".html")).write_text("corrupted")
    with pytest.raises(ValueError, match="integrity check"):
        create_app(tmp_path, production=True)


def test_streamed_ssg_timeout_fails_with_a_clear_build_error(tmp_path, monkeypatch):
    from pynext import build as build_module

    app_dir = make_app(tmp_path)
    (app_dir / "page.py").write_text(
        'from pynext import page, h\nimport asyncio\n@page(render="ssg")\n'
        'async def Page():\n'
        '    await asyncio.sleep(5)\n'
        '    yield h("p", {}, "slow")\n', encoding="utf-8")
    # The real limit is 30s; shrink it for the test — the timeout path
    # (asyncio.timeout -> ValueError) is the same either way.
    monkeypatch.setattr(build_module, "MAX_STREAMED_STATIC_SECONDS", 0.05)
    with pytest.raises(ValueError, match="exceeded the build time limit"):
        write_manifest(tmp_path)


def test_streamed_ssg_is_rendered_at_build_and_served_as_static(tmp_path):
    app_dir = make_app(tmp_path)
    (app_dir / "page.py").write_text(
        'from pynext import page, h\n'
        '@page(render="ssg")\n'
        'async def Page():\n'
        '    yield h("p", {}, "first")\n'
        '    yield h("p", {}, "second")\n', encoding="utf-8")
    manifest = write_manifest(tmp_path)
    digest = manifest["static_pages"]["/"]["sha256"]
    artifact = (tmp_path / ".pynext" / "static" / f"{digest}.html").read_bytes()
    assert b"<p>first</p><p>second</p>" in artifact

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/")).content == artifact
        finally:
            await app.isr.close()
    asyncio.run(run())


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
    source.write_text(source.read_text(encoding="utf-8").replace('[{"slug": "hello"}, {"slug": "한글"}]', generated), encoding="utf-8")
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
