"""SSG fallback modes (blocking / prerender / not_found) and ISR warmup.

Phase 2 (REPORT.md SSG 미생성 경로의 fallback 3모드 + ISR에
``generate_static_params`` 허용 + 빌드/시작 시 웜업).
"""
import asyncio
import json

import httpx
import pytest

from conftest import page_html
from pynext import create_app, page
from pynext.build import build_id_for, write_manifest
from pynext.http import Request


def make_ssg_project(root, *, fallback="not_found", dynamic_params=True,
                     layout=False, streaming=False):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    options = f'fallback="{fallback}"' if fallback != "not_found" else ""
    if not dynamic_params:
        options += ", dynamic_params=False" if options else "dynamic_params=False"
    if streaming:
        body = ('async def Page(params):\n'
                '    global calls\n'
                '    calls += 1\n'
                '    yield h("p", {}, "one")\n'
                '    yield h("p", {}, "two")\n')
    else:
        body = ('async def Page(params):\n'
                '    global calls\n'
                '    calls += 1\n'
                '    number = calls\n'
                '    if started is not None: started.set()\n'
                '    if gate is not None: await gate.wait()\n'
                '    if fail: raise RuntimeError("generation failed")\n'
                '    return h("p", {}, f"{params[\'slug\']}:{number}")\n')
    decorator = f'@page(render="ssg", {options})\n' if options else '@page(render="ssg")\n'
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\nstarted = None\ngate = None\nfail = False\n'
        'def generate_static_params():\n'
        '    return [{"slug": "hello"}]\n'
        + decorator + body
    )
    if layout:
        (root / "app" / "blog" / "layout.py").write_text(
            'from pynext import h, layout\n'
            '@layout\n'
            'def Layout(children):\n'
            '    return h("html", {}, h("body", {}, h("header", {}, "SHELL"), children))\n'
        )
    return directory / "page.py"


def make_isr_project(root, *, fail_slug=None):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    fail_check = (f'    if params["slug"] == "{fail_slug}": raise RuntimeError("warmup boom")\n'
                  if fail_slug else "")
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\nstarted = None\ngate = None\n'
        'def generate_static_params():\n'
        '    return [{"slug": "a"}, {"slug": "b"}]\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    number = calls\n'
        '    if started is not None: started.set()\n'
        '    if gate is not None: await gate.wait()\n'
        f'{fail_check}'
        '    return h("p", {}, f"{params[\'slug\']}:{number}")\n'
    )
    return directory / "page.py"


def test_ssg_fallback_not_found_is_the_default(tmp_path):
    make_ssg_project(tmp_path)
    manifest = write_manifest(tmp_path)
    assert manifest["ssg_fallback"] == {"/blog/[slug]": {"fallback": "not_found", "dynamic_params": True}}

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/blog/hello")).status_code == 200
                assert (await client.get("/blog/hello")).text == page_html("<p>hello:1</p>")
                assert (await client.get("/blog/other")).status_code == 404
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_blocking_generates_once_then_serves_statically(tmp_path):
    make_ssg_project(tmp_path, fallback="blocking")
    manifest = write_manifest(tmp_path)
    assert manifest["ssg_fallback"] == {"/blog/[slug]": {"fallback": "blocking", "dynamic_params": True}}

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                first = await client.get("/blog/new")
                assert first.status_code == 200
                assert first.text == page_html("<p>new:1</p>")
                assert module.calls == 1
                assert "/blog/new" in app.static_pages
                assert app.static_pages["/blog/new"] == page_html("<p>new:1</p>").encode()
                # Served from the runtime overlay now: no re-render.
                second = await client.get("/blog/new")
                assert second.status_code == 200 and second.text == first.text
                assert module.calls == 1
                # HEAD is served from the overlay with a length, no body.
                head = await client.head("/blog/new")
                assert head.status_code == 200 and head.text == ""
                assert int(head.headers["content-length"]) == len(page_html("<p>new:1</p>"))
                assert module.calls == 1
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_blocking_is_single_flight(tmp_path):
    make_ssg_project(tmp_path, fallback="blocking")
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                module.started, module.gate = asyncio.Event(), asyncio.Event()
                requests = [asyncio.create_task(client.get("/blog/new")) for _ in range(5)]
                await asyncio.wait_for(module.started.wait(), 3)
                assert module.calls == 1
                module.gate.set()
                results = await asyncio.gather(*requests)
                assert all(r.status_code == 200 for r in results)
                assert {r.text for r in results} == {page_html("<p>new:1</p>")}
                assert module.calls == 1
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_blocking_failure_is_500_and_retries(tmp_path):
    make_ssg_project(tmp_path, fallback="blocking")
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                module.fail = True
                assert (await client.get("/blog/new")).status_code == 500
                assert "/blog/new" not in app.static_pages
                module.fail = False
                response = await client.get("/blog/new")
                assert response.status_code == 200
                assert response.text == page_html("<p>new:2</p>")
                assert "/blog/new" in app.static_pages
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_dynamic_params_false_forces_not_found(tmp_path):
    make_ssg_project(tmp_path, fallback="blocking", dynamic_params=False)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/blog/hello")).status_code == 200
                assert (await client.get("/blog/new")).status_code == 404
                # The pre-generated page is served from the build; the
                # ungenerated path never renders in this app.
                assert module.calls == 0
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_prerender_streams_shell_first_then_caches(tmp_path):
    make_ssg_project(tmp_path, fallback="prerender", layout=True)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            module.started, module.gate = asyncio.Event(), asyncio.Event()
            response = await app.dispatch(Request("GET", "/blog/new", _app=app))
            assert response.status == 200
            # The layout shell flushes before the gated page finishes.
            first = await response._next_chunk()
            assert first is not None
            assert b"SHELL" in first
            assert b"new:1" not in first
            module.gate.set()
            rest = bytearray(first)
            while (chunk := await response._next_chunk()) is not None:
                rest.extend(chunk)
            await response.aclose()
            assert b"new:1" in bytes(rest)
            assert b"SHELL" in bytes(rest)
            # Fully delivered: the page joined the runtime static overlay.
            assert module.calls == 1
            assert "/blog/new" in app.static_pages
            assert app.static_pages["/blog/new"] == bytes(rest)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                second = await client.get("/blog/new")
                assert second.status_code == 200
                assert second.content == bytes(rest)
                assert module.calls == 1
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_blocking_generates_streaming_pages(tmp_path):
    make_ssg_project(tmp_path, fallback="blocking", streaming=True)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/blog/new")
                assert response.status_code == 200
                assert "<p>one</p>" in response.text and "<p>two</p>" in response.text
                assert module.calls == 1
                assert "/blog/new" in app.static_pages
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssg_fallback_options_are_validated():
    with pytest.raises(ValueError, match="fallback must be"):
        page(render="ssg", fallback="sometimes")
    with pytest.raises(ValueError, match="dynamic_params must be a bool"):
        page(render="ssg", dynamic_params="yes")
    with pytest.raises(ValueError, match="only valid for SSG pages"):
        page(render="ssr", fallback="blocking")
    with pytest.raises(ValueError, match="only valid for SSG pages"):
        page(render="isr", revalidate=30, stale=1, dynamic_params=False)


def test_ssg_fallback_mismatch_requires_rebuild(tmp_path):
    make_ssg_project(tmp_path, fallback="blocking")
    write_manifest(tmp_path)
    saved = tmp_path / ".pynext" / "manifest.json"
    data = json.loads(saved.read_text())
    data["ssg_fallback"]["/blog/[slug]"]["fallback"] = "not_found"
    raw = json.dumps(data).encode("utf-8")
    saved.write_bytes(raw)
    # Re-sign the edited manifest so the BUILD_ID integrity check passes
    # and the fallback check is the one that fires (same pattern as the
    # ISR-tags mismatch test).
    (tmp_path / ".pynext" / "BUILD_ID").write_text(build_id_for(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="SSG fallback modes changed"):
        create_app(tmp_path, production=True)


def test_isr_generate_static_params_enumerated_at_build_and_warmed_on_startup(tmp_path):
    make_isr_project(tmp_path)
    manifest = write_manifest(tmp_path)
    assert manifest["isr_static_paths"] == {"/blog/[slug]": ["/blog/a", "/blog/b"]}
    assert manifest["ssg_fallback"] == {}

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            assert app.isr.store.lookup("/blog/a")[0] == "miss"
            await app.startup()
            assert app._isr_warmup_task is not None
            warmed, failed = await app._isr_warmup_task
            assert (warmed, failed) == (2, 0)
            assert app.isr.store.lookup("/blog/a")[0] == "fresh"
            assert app.isr.store.lookup("/blog/b")[0] == "fresh"
            assert module.calls == 2
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/blog/a")).text == page_html("<p>a:1</p>")
                assert (await client.get("/blog/b")).text == page_html("<p>b:2</p>")
            assert module.calls == 2  # served from the warmed cache
        finally:
            await app.shutdown()
    asyncio.run(run())


def test_isr_warmup_does_not_block_startup(tmp_path):
    make_isr_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            module.started, module.gate = asyncio.Event(), asyncio.Event()
            # Startup must return while the first warmup render is gated.
            await asyncio.wait_for(app.startup(), timeout=5)
            await asyncio.wait_for(module.started.wait(), 3)
            assert not app._isr_warmup_task.done()
            module.gate.set()
            warmed, failed = await asyncio.wait_for(app._isr_warmup_task, timeout=5)
            assert (warmed, failed) == (2, 0)
            assert app.isr.store.lookup("/blog/a")[0] == "fresh"
        finally:
            await app.shutdown()
    asyncio.run(run())


def test_isr_warmup_failure_is_best_effort(tmp_path):
    make_isr_project(tmp_path, fail_slug="b")
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            await app.startup()  # must not raise when one path fails
            warmed, failed = await app._isr_warmup_task
            assert (warmed, failed) == (1, 1)
            assert app.isr.store.lookup("/blog/a")[0] == "fresh"
            assert app.isr.store.lookup("/blog/b")[0] == "miss"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/blog/a")).status_code == 200
                assert (await client.get("/blog/b")).status_code == 500
        finally:
            await app.shutdown()
    asyncio.run(run())


def test_isr_static_page_rejects_generate_static_params(tmp_path):
    directory = tmp_path / "app" / "about"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'def generate_static_params():\n'
        '    return []\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'def Page(params):\n'
        '    return h("p", {}, "about")\n'
    )
    with pytest.raises(ValueError, match="Static ISR page"):
        write_manifest(tmp_path)


def test_isr_dynamic_page_rejects_bad_generator(tmp_path):
    source = make_isr_project(tmp_path)
    source.write_text(source.read_text().replace(
        'def generate_static_params():\n    return [{"slug": "a"}, {"slug": "b"}]',
        'generate_static_params = [{"slug": "a"}]'))
    with pytest.raises(ValueError, match="must export generate_static_params"):
        write_manifest(tmp_path)
