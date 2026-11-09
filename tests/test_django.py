"""Exercise Django ORM/forms through the PyNext resource lifecycle."""
import asyncio
import builtins
import importlib
import os
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

pytest.importorskip("django")
from pynext import DjangoBusyError, DjangoResource, create_app
from pynext.build import write_manifest
from pynext.server import serve


@pytest.fixture(scope="module")
def django_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("django_bridge")
    previous_settings = os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    site = root / "fixture_site"
    store = site / "store"
    store.mkdir(parents=True)
    (site / "__init__.py").touch()
    (store / "__init__.py").touch()
    (site / "settings.py").write_text(
        'SECRET_KEY = "test-only"\n'
        'INSTALLED_APPS = ["fixture_site.store"]\n'
        f'DATABASES = {{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": {str(root / "test.sqlite3")!r}}}}}\n'
        'DEFAULT_AUTO_FIELD = "django.db.models.AutoField"\n', encoding="utf-8")
    (store / "models.py").write_text(
        'from django.db import models\n'
        'class Item(models.Model):\n'
        '    name = models.CharField(max_length=20)\n', encoding="utf-8")
    app_dir = root / "web" / "app" / "items"
    app_dir.mkdir(parents=True)
    (root / "web" / "resources.py").write_text(
        'from pynext import DjangoResource\nRESOURCES = {"django": DjangoResource("fixture_site.settings")}\n',
        encoding="utf-8")
    (app_dir / "route.py").write_text(
        'from django import forms\n'
        'from fixture_site.store.models import Item\n'
        'from pynext import JSONResponse, route\n'
        'class ItemForm(forms.Form):\n'
        '    name = forms.CharField(max_length=20)\n'
        '@route("POST")\n'
        'async def create(request):\n'
        '    data = await request.json()\n'
        '    def save():\n'
        '        form = ItemForm(data)\n'
        '        if not form.is_valid(): return {"ok": False, "errors": form.errors.get_json_data()}\n'
        '        item = Item.objects.create(name=form.cleaned_data["name"])\n'
        '        return {"ok": True, "name": item.name}\n'
        '    result = await request.resources["django"].run(save)\n'
        '    return JSONResponse(result, status=201 if result["ok"] else 400)\n'
        '@route("GET")\n'
        'async def list_items(request):\n'
        '    names = await request.resources["django"].run(lambda: list(Item.objects.values_list("name", flat=True)))\n'
        '    return JSONResponse({"items": names})\n', encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        yield root / "web"
    finally:
        sys.path.remove(str(root))
        if previous_settings is None:
            os.environ.pop("DJANGO_SETTINGS_MODULE", None)
        else:
            os.environ["DJANGO_SETTINGS_MODULE"] = previous_settings


def _item_model():
    return importlib.import_module("fixture_site.store.models").Item


def _reset_table():
    from django.db import connection
    item = _item_model()
    with connection.schema_editor() as editor:
        if item._meta.db_table in connection.introspection.table_names():
            editor.delete_model(item)
        editor.create_model(item)


@pytest.mark.parametrize("mode", ["asgi", "axum", "production"])
def test_models_and_forms_work_through_resource(django_project, mode):
    # Model imports in route.py occur during create_app, before startup.
    if mode == "production":
        write_manifest(django_project)
    app = create_app(django_project, production=(mode == "production"))
    resource = app.resources["django"]

    async def run():
        task = None
        stop = asyncio.Event()
        if mode != "axum":
            await app.startup()
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(serve(app, port=0, stop_event=stop, on_ready=ready.set_result))
            address = await asyncio.wait_for(ready, 10)
            client = httpx.AsyncClient(base_url=f"http://{address}", trust_env=False)
        try:
            await resource.run(_reset_table)
            async with client:
                invalid = await client.post("/items", json={"name": ""})
                assert invalid.status_code == 400 and "name" in invalid.json()["errors"]
                saved = await client.post("/items", json={"name": "hello"})
                assert saved.status_code == 201 and saved.json() == {"ok": True, "name": "hello"}
                assert (await client.get("/items")).json() == {"items": ["hello"]}
        finally:
            if task is not None:
                stop.set()
                await task
            else:
                await app.shutdown()
        with pytest.raises(RuntimeError, match="not started"):
            await resource.run(lambda: None)
    asyncio.run(run())


def test_orm_rollback_errors_and_lazy_results(django_project):
    resource = DjangoResource("fixture_site.settings")
    async def run():
        await resource.startup()
        try:
            await resource.run(_reset_table)
            # Both calls run on the same dedicated worker; the connection
            # opened by schema work must be closed before the next call.
            assert await resource.run(
                lambda: importlib.import_module("django.db").connection.connection is None)
            def fail():
                from django.db import transaction
                with transaction.atomic():
                    _item_model().objects.create(name="rolled back")
                    raise ValueError("abort")
            with pytest.raises(ValueError, match="abort"):
                await resource.run(fail)
            assert await resource.run(lambda: _item_model().objects.count()) == 0
            with pytest.raises(TypeError, match="materialize"):
                await resource.run(lambda: _item_model().objects.all())
            with pytest.raises(TypeError, match="materialize"):
                await resource.run(lambda: _item_model().objects.create(name="not returned"))
            async def invalid():
                return 1
            with pytest.raises(TypeError, match="synchronous"):
                await resource.run(invalid)
            with pytest.raises(TypeError, match="synchronous"):
                await resource.run(lambda: invalid())
            with pytest.raises(RuntimeError, match="cannot be nested"):
                await resource.run(lambda: asyncio.run(resource.run(lambda: None)))
        finally:
            await resource.shutdown()
    asyncio.run(run())


def test_queue_bound_and_cancellation_do_not_release_running_work(django_project):
    resource = DjangoResource("fixture_site.settings", max_pending=2)
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set()
        if not release.wait(10):
            raise TimeoutError("test worker did not release")
        return "finished"

    async def run():
        await resource.startup()
        first = asyncio.create_task(resource.run(slow))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            second = asyncio.create_task(resource.run(lambda: "second"))
            await asyncio.sleep(0)
            with pytest.raises(DjangoBusyError):
                await resource.run(lambda: None)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(DjangoBusyError):
                await resource.run(lambda: None)
            release.set()
            assert await asyncio.wait_for(second, 5) == "second"
            assert await resource.run(lambda: "available") == "available"
        finally:
            release.set()
            await asyncio.gather(first, *(tuple([second]) if second else ()), return_exceptions=True)
            await resource.shutdown()
    asyncio.run(run())


def test_cancelled_shutdown_waits_for_running_django_work(django_project):
    resource = DjangoResource("fixture_site.settings")
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set()
        assert release.wait(10)
        return "finished"

    async def run():
        await resource.startup()
        work = asyncio.create_task(resource.run(slow))
        shutdown = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            shutdown = asyncio.create_task(resource.shutdown())
            await asyncio.sleep(0)
            shutdown.cancel()
            await asyncio.sleep(0)
            assert not shutdown.done()
            shutdown.cancel()
            await asyncio.sleep(0)
            assert not shutdown.done()
            release.set()
            assert await asyncio.wait_for(work, 5) == "finished"
            with pytest.raises(asyncio.CancelledError):
                await shutdown
            with pytest.raises(RuntimeError, match="not started"):
                await resource.run(lambda: None)
        finally:
            release.set()
            if shutdown is not None:
                await asyncio.gather(shutdown, return_exceptions=True)
            await resource.shutdown()
    asyncio.run(run())


def test_django_settings_conflicts_are_rejected(django_project, monkeypatch):
    DjangoResource("fixture_site.settings")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "other.settings")
    with pytest.raises(RuntimeError, match="different settings"):
        DjangoResource("fixture_site.settings")


def test_missing_dependency_has_actionable_error(monkeypatch):
    original_import = builtins.__import__
    def without_django(name, *args, **kwargs):
        if name == "django":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_django)
    with pytest.raises(RuntimeError, match=r"pynext-framework\[django\]"):
        DjangoResource("fixture_site.settings")


@pytest.mark.parametrize("code", [
    'from django.conf import settings; settings.configure(SECRET_KEY="test"); '
    'from pynext import DjangoResource; '
    'DjangoResource("fixture_site.settings")',
    'from pynext import DjangoResource; DjangoResource("nonexistent_pynext_fixture.settings")',
])
def test_incompatible_or_missing_settings_fail_without_changing_environment(code):
    script = (
        'import os\n'
        'os.environ.pop("DJANGO_SETTINGS_MODULE", None)\n'
        'try:\n'
        f'    {code}\n'
        'except (RuntimeError, ModuleNotFoundError):\n'
        '    assert "DJANGO_SETTINGS_MODULE" not in os.environ\n'
        'else:\n'
        '    raise AssertionError("setup unexpectedly succeeded")\n'
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
