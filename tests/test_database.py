"""Exercise the optional database resource and actual transaction boundaries."""
import asyncio
import builtins
import os
import uuid

import httpx
import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import Integer, String, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pynext import Database, create_app
from pynext.server import serve


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "pynext_test_" + uuid.uuid4().hex
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("PYNEXT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("PYNEXT_TEST_POSTGRES_URL is not set")
        return url
    return f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"


def test_orm_transactions_rollback_and_lifespan(database_url):
    async def run():
        db = Database(database_url)
        with pytest.raises(RuntimeError, match="has not started"):
            async with db.session():
                pass
        await db.startup()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                await db.startup()
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            async with db.transaction() as session:
                session.add(Item(name="saved"))
            with pytest.raises(ValueError, match="abort"):
                async with db.transaction() as session:
                    session.add(Item(name="rolled back"))
                    await session.flush()
                    raise ValueError("abort")
            async with db.session() as session:
                session.add(Item(name="not committed"))
                await session.flush()
            async with db.session() as session:
                assert list((await session.scalars(select(Item.name))).all()) == ["saved"]
            with pytest.raises(asyncio.CancelledError):
                async with db.transaction() as session:
                    session.add(Item(name="cancelled"))
                    await session.flush()
                    raise asyncio.CancelledError()
            async with db.session() as session:
                assert await session.scalar(select(func.count()).select_from(Item)) == 1
            async def read_name():
                async with db.session() as session:
                    return await session.scalar(select(Item.name))
            assert await asyncio.gather(*(read_name() for _ in range(3))) == ["saved"] * 3
        finally:
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await db.shutdown()
        with pytest.raises(RuntimeError, match="has not started"):
            _ = db.engine
        await db.shutdown()
    asyncio.run(run())


def test_failed_startup_and_unsupported_url_leave_resource_stopped(tmp_path):
    async def run():
        db = Database("mysql://user:secret@example.invalid/db")
        with pytest.raises(ValueError, match="supports SQLite"):
            await db.startup()
        await db.shutdown()
        broken = Database(f"sqlite+aiosqlite:///{(tmp_path / 'missing' / 'db.sqlite').as_posix()}")
        with pytest.raises(Exception):
            await broken.startup()
        with pytest.raises(RuntimeError, match="has not started"):
            _ = broken.engine
        await broken.shutdown()
    asyncio.run(run())


def test_invalid_url_does_not_expose_credentials():
    async def run():
        db = Database("not-a-url-secret-value")
        with pytest.raises(ValueError) as caught:
            await db.startup()
        assert "secret-value" not in str(caught.value)
        assert caught.value.__suppress_context__
    asyncio.run(run())


def test_later_resource_failure_disposes_database(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\ndef get(): return Response("ok")\n',
        encoding="utf-8")
    db = Database(f"sqlite:///{(tmp_path / 'rollback.sqlite3').as_posix()}")

    class BrokenResource:
        async def startup(self):
            raise RuntimeError("resource startup failed")

        async def shutdown(self):
            pass

    app = create_app(tmp_path, resources={"db": db, "broken": BrokenResource()})

    async def run():
        with pytest.raises(RuntimeError, match="resource startup failed"):
            await app.startup()
        with pytest.raises(RuntimeError, match="has not started"):
            _ = db.engine
        # A failed app startup must not leave the database resource unusable.
        await db.startup()
        await db.shutdown()
    asyncio.run(run())


def test_missing_optional_dependency_has_actionable_error(monkeypatch):
    original_import = builtins.__import__
    def without_sqlalchemy(name, *args, **kwargs):
        if name == "sqlalchemy":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_sqlalchemy)
    with pytest.raises(RuntimeError, match=r"pynext-framework\[database\]"):
        asyncio.run(Database().startup())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_project_resource_serves_requests_with_isolated_transactions(tmp_path, mode):
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        'from sqlalchemy import text\n'
        '@route("POST")\n'
        'async def create(request):\n'
        '    async with request.resources["db"].transaction() as session:\n'
        '        await session.execute(text("INSERT INTO entries (name) VALUES (:name)"), {"name": "example"})\n'
        '    return JSONResponse({"ok": True})\n'
        '@route("GET")\n'
        'async def count(request):\n'
        '    async with request.resources["db"].session() as session:\n'
        '        result = await session.execute(text("SELECT count(*) FROM entries"))\n'
        '        return JSONResponse({"count": result.scalar_one()})\n', encoding="utf-8")
    (tmp_path / "resources.py").write_text(
        'from pynext import Database\n'
        f'RESOURCES = {{"db": Database({("sqlite:///" + (tmp_path / "app.sqlite3").as_posix())!r})}}\n',
        encoding="utf-8")
    app = create_app(tmp_path)

    async def run():
        task = None
        stop = asyncio.Event()
        if mode == "asgi":
            await app.startup()
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(serve(app, port=0, stop_event=stop, on_ready=ready.set_result))
            address = await asyncio.wait_for(ready, 10)
            client = httpx.AsyncClient(base_url=f"http://{address}", trust_env=False)
        try:
            async with app.resources["db"].engine.begin() as conn:
                await conn.execute(text("CREATE TABLE entries (name VARCHAR(80) NOT NULL)"))
            async with client:
                assert (await client.post("/")).json() == {"ok": True}
                assert (await client.get("/")).json() == {"count": 1}
        finally:
            if task is not None:
                stop.set()
                await task
            else:
                await app.shutdown()
        with pytest.raises(RuntimeError, match="has not started"):
            _ = app.resources["db"].engine
    asyncio.run(run())
