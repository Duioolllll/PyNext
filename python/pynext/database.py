"""Optional SQLAlchemy async database resource for PyNext applications.

Each worker owns its own engine and connection pool. Sessions are deliberately
explicit and must not be shared between concurrent tasks.
"""

from contextlib import asynccontextmanager


class Database:
    """Manage an async SQLAlchemy engine through the application lifespan.

    Register an instance in ``resources.py`` or ``create_app(resources=...)``.
    ``session()`` closes without committing; ``transaction()`` commits only
    on success and rolls back on exceptions or cancellation.
    """

    def __init__(self, url="sqlite+aiosqlite:///./pynext.sqlite3", *, engine_options=None):
        if not isinstance(url, str) or not url:
            raise ValueError("database URL must be a non-empty string")
        if engine_options is not None and not isinstance(engine_options, dict):
            raise TypeError("engine_options must be a dict")
        self._url = url
        self._engine_options = dict(engine_options or {})
        self._engine = None
        self._session_factory = None

    @property
    def engine(self):
        """The started async engine; unavailable outside the app lifespan."""
        if self._engine is None:
            raise RuntimeError("database resource has not started")
        return self._engine

    async def startup(self):
        if self._engine is not None:
            raise RuntimeError("database resource is already started")
        try:
            from sqlalchemy import text
            from sqlalchemy.engine import make_url
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        except ImportError as exc:
            raise RuntimeError("install pynext-framework[database] to use Database") from exc

        try:
            url = make_url(self._url)
        except Exception:
            # Driver parsers may echo malformed URLs containing credentials.
            raise ValueError("invalid database URL") from None
        drivers = {"sqlite": "sqlite+aiosqlite", "postgres": "postgresql+asyncpg",
                   "postgresql": "postgresql+asyncpg"}
        if url.drivername in drivers:
            url = url.set(drivername=drivers[url.drivername])
        if url.drivername not in {"sqlite+aiosqlite", "postgresql+asyncpg"}:
            raise ValueError("Database supports SQLite/aiosqlite and PostgreSQL/asyncpg URLs")
        try:
            engine = create_async_engine(url, **self._engine_options)
        except ModuleNotFoundError as exc:
            extra = "postgres" if url.drivername == "postgresql+asyncpg" else "database"
            raise RuntimeError(f"install pynext-framework[{extra}] to use this database") from exc
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            factory = async_sessionmaker(engine, expire_on_commit=False)
        except BaseException:
            await engine.dispose()
            raise
        self._engine = engine
        self._session_factory = factory

    async def shutdown(self):
        engine = self._engine
        self._engine = None
        self._session_factory = None
        if engine is not None:
            await engine.dispose()

    @asynccontextmanager
    async def session(self):
        """Yield a fresh session without committing; close it on exit."""
        factory = self._session_factory
        if factory is None:
            raise RuntimeError("database resource has not started")
        async with factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self):
        """Yield a fresh session in a transaction, committing only on success."""
        async with self.session() as session:
            async with session.begin():
                yield session
