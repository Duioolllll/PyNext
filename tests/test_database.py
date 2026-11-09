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


def test_concurrent_startup_and_shutdown_are_serialized(tmp_path):
    async def run():
        db = Database(f"sqlite:///{(tmp_path / 'lifecycle.sqlite3').as_posix()}")
        results = await asyncio.gather(db.startup(), db.startup(), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        await db.shutdown()

        starting = asyncio.create_task(db.startup())
        await asyncio.sleep(0)
        await db.shutdown()
        await starting
        with pytest.raises(RuntimeError, match="has not started"):
            _ = db.engine
    asyncio.run(run())


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:",
                                 "sqlite+aiosqlite:///file:memorytest?mode=memory&cache=shared&uri=true"])
def test_memory_sqlite_transactions_cannot_commit_each_others_work(url):
    async def run():
        db = Database(url)
        await db.startup()
        try:
            async with db.engine.begin() as conn:
                await conn.execute(text("CREATE TABLE entries (name TEXT)"))
            entered, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def rollback_first():
                with pytest.raises(ValueError, match="rollback"):
                    async with db.transaction() as session:
                        await session.execute(text("INSERT INTO entries VALUES ('first')"))
                        entered.set()
                        await release.wait()
                        raise ValueError("rollback")

            async def commit_second():
                await entered.wait()
                attempted.set()
                async with db.transaction() as session:
                    await session.execute(text("INSERT INTO entries VALUES ('second')"))

            first = asyncio.create_task(rollback_first())
            second = asyncio.create_task(commit_second())
            await asyncio.wait_for(attempted.wait(), 5)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 5)
            async with db.session() as session:
                assert (await session.execute(text("SELECT name FROM entries"))).scalars().all() == ["second"]
                with pytest.raises(RuntimeError, match="nested sessions"):
                    async with db.session():
                        pass
        finally:
            await db.shutdown()
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


def test_concurrent_app_startup_keeps_database_available(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\ndef get(): return Response("ok")\n',
        encoding="utf-8")
    db = Database(f"sqlite:///{(tmp_path / 'concurrent.sqlite3').as_posix()}")
    app = create_app(tmp_path, resources={"db": db})

    async def run():
        await asyncio.gather(app.startup(), app.startup())
        try:
            assert app.is_ready
            async with db.session() as session:
                assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        finally:
            await app.shutdown()
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
