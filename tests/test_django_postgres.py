"""Django ORM on a disposable PostgreSQL instance, isolated from global settings."""
import os
import subprocess
import sys
import uuid

import pytest

URL = os.environ.get("PYNEXT_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PYNEXT_TEST_POSTGRES_URL is not set")


def test_django_orm_postgres_commit_and_rollback(tmp_path):
    package = tmp_path / "pg_fixture"
    store = package / "store"
    store.mkdir(parents=True)
    (package / "__init__.py").touch()
    (store / "__init__.py").touch()
    (package / "settings.py").write_text(
        'import os\nfrom urllib.parse import urlsplit, unquote\n'
        'u = urlsplit(os.environ["PYNEXT_TEST_POSTGRES_URL"])\n'
        'SECRET_KEY = "test-only"\nINSTALLED_APPS = ["pg_fixture.store"]\n'
        'DEFAULT_AUTO_FIELD = "django.db.models.AutoField"\n'
        'DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", '
        '"NAME": u.path.lstrip("/"), "USER": unquote(u.username or ""), '
        '"PASSWORD": unquote(u.password or ""), "HOST": u.hostname, "PORT": u.port}}\n',
        encoding="utf-8")
    table = "pynext_django_" + uuid.uuid4().hex
    (store / "models.py").write_text(
        'from django.db import models\nclass Item(models.Model):\n'
        '    name = models.CharField(max_length=40)\n'
        '    class Meta:\n'
        f'        db_table = {table!r}\n', encoding="utf-8")
    script = '''
import asyncio, os, sys
sys.path.insert(0, sys.argv[1])
os.environ.pop("DJANGO_SETTINGS_MODULE", None)
from pynext import DjangoResource
bridge = DjangoResource("pg_fixture.settings")
from pg_fixture.store.models import Item
from django.db import connection, transaction

def create_table():
    with connection.schema_editor() as editor: editor.create_model(Item)

def drop_table():
    with connection.schema_editor() as editor: editor.delete_model(Item)

def insert():
    with transaction.atomic(): Item.objects.create(name="saved")

def fail():
    with transaction.atomic():
        Item.objects.create(name="rolled back")
        raise ValueError("abort")

async def main():
    await bridge.startup()
    created = False
    try:
        await bridge.run(create_table)
        created = True
        await bridge.run(insert)
        try: await bridge.run(fail)
        except ValueError: pass
        else: raise AssertionError("transaction did not fail")
        assert await bridge.run(lambda: list(Item.objects.values_list("name", flat=True))) == ["saved"]
    finally:
        if created: await bridge.run(drop_table)
        await bridge.shutdown()
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
