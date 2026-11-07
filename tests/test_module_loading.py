import asyncio
import gc
import importlib
import importlib.util
import os
import py_compile
import sys
import weakref

import httpx
import pytest

from pynext import create_app


def make_project(root, message):
    directory = root / "app"
    dynamic = directory / "docs" / "[...path]"
    dynamic.mkdir(parents=True)
    (root / "json.py").write_text('raise RuntimeError("project shadowed stdlib json")\n', encoding="utf-8")
    (directory / "__init__.py").write_text('from .helpers import MESSAGE\n', encoding="utf-8")
    (directory / "helpers.py").write_text(f'MESSAGE = {message!r}\n', encoding="utf-8")
    (directory / "page.py").write_text(
        'from . import MESSAGE\n'
        'from pynext import h, page\n'
        '@page()\n'
        'def Page():\n'
        '    from .helpers import MESSAGE as late_message\n'
        '    return h("p", {}, MESSAGE + ":" + late_message)\n',
        encoding="utf-8",
    )
    (directory / "docs" / "helpers.py").write_text('VALUE = "docs"\n', encoding="utf-8")
    (dynamic / "helpers.py").write_text('from ..helpers import VALUE\n', encoding="utf-8")
    (dynamic / "page.py").write_text(
        'from .helpers import VALUE\n'
        'from pynext import h, page\n'
        '@page()\ndef Page(): return h("p", {}, VALUE)\n',
        encoding="utf-8",
    )


def test_relative_imports_are_isolated_between_apps_and_do_not_change_sys_path(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    make_project(first, "one")
    make_project(second, "two")
    previous_path = list(sys.path)
    app_one = create_app(first)
    app_two = create_app(second)
    assert sys.path == previous_path
    assert app_one._module("app/page.py") is not app_two._module("app/page.py")

    async def run():
        for app, message in ((app_one, "one"), (app_two, "two")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/")).text == f"<p>{message}:{message}</p>"
                assert (await client.get("/docs/one/two")).text == "<p>docs</p>"
            await app.isr.close()
    asyncio.run(run())


def test_failed_load_cleans_its_project_modules_and_sys_path(tmp_path):
    make_project(tmp_path, "value")
    (tmp_path / "app" / "page.py").write_text('from .helpers import MISSING\n', encoding="utf-8")
    previous_path = list(sys.path)
    previous_modules = {name for name in sys.modules if name.startswith("_pynext_project_")}
    with pytest.raises(ImportError):
        create_app(tmp_path)
    assert sys.path == previous_path
    assert {name for name in sys.modules if name.startswith("_pynext_project_")} == previous_modules


def test_new_app_reads_changed_helper_even_when_timestamp_and_size_match(tmp_path):
    make_project(tmp_path, "one")
    first = create_app(tmp_path)
    source = tmp_path / "app" / "helpers.py"
    before = source.stat()
    source.write_text('MESSAGE = "two"\n', encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = create_app(tmp_path)
    assert first._module("app/page.py").MESSAGE == "one"
    assert second._module("app/page.py").MESSAGE == "two"


def test_reload_of_directly_loaded_module_reads_fresh_source(tmp_path):
    directory = tmp_path / "app"
    directory.mkdir()
    source = directory / "page.py"
    source.write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "one")\n', encoding="utf-8")
    app = create_app(tmp_path)
    module = app._module("app/page.py")
    assert module.Page()["children"][0]["value"] == "one"
    before = source.stat()
    # Plant a bytecode cache for the old source, then rewrite the source with
    # the same size and mtime: a timestamp-based loader would reuse the stale
    # bytecode, while the fresh loader must recompile the new source.
    py_compile.compile(str(source), cfile=importlib.util.cache_from_source(str(source)), doraise=True)
    source.write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "two")\n', encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    reloaded = importlib.reload(module)
    assert reloaded.Page()["children"][0]["value"] == "two"


def test_module_cache_key_ignores_letter_case(tmp_path):
    from pynext.app import _module_key
    assert _module_key(tmp_path, "app/page.py") == _module_key(tmp_path, "APP/Page.py")
    assert _module_key(tmp_path, "app/page.py") == _module_key(tmp_path, "app/./page.py")


def test_case_variant_module_paths_share_one_loaded_module(tmp_path):
    probe = tmp_path / "caseprobe"
    probe.write_text("x", encoding="utf-8")
    if not (tmp_path / "CASEPROBE").is_file():
        pytest.skip("filesystem is case-sensitive")
    directory = tmp_path / "app"
    directory.mkdir()
    (directory / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n', encoding="utf-8")
    app = create_app(tmp_path)
    assert app._module("app/page.py") is app._module("APP/page.py")


def test_modules_are_removed_when_app_is_collected(tmp_path):
    make_project(tmp_path, "one")
    app = create_app(tmp_path)
    namespace = app._namespace
    app_ref = weakref.ref(app)
    assert any(name.startswith(namespace + ".") for name in sys.modules)
    del app
    gc.collect()
    assert app_ref() is None
    assert not any(name == namespace or name.startswith(namespace + ".") for name in sys.modules)
