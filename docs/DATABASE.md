# SQLAlchemy 데이터베이스 연동

`Database`는 SQLAlchemy async 엔진과 연결 풀을 PyNext 리소스 수명주기에 연결한다. SQLite와 PostgreSQL을 지원하며 ORM 모델, 스키마와 마이그레이션은 앱에서 관리한다. 이 기능은 선택 사항이고 기본 패키지에 DB 드라이버를 추가하지 않는다. **현재 개발 브랜치의 미출시 기능이며 공개된 0.1.0 패키지에는 없다.**

## 설치와 설정

```bash
# 이 기능이 포함된 저장소 체크아웃에서 실행한다 (소스 빌드에 Rust 필요).
# SQLite (aiosqlite)
pip install '.[database]'
# PostgreSQL (asyncpg; SQLite 드라이버도 포함)
pip install '.[postgres]'
```

`uv sync --extra database` 또는 `uv sync --extra postgres`도 사용할 수 있다. 기능이 포함된 릴리스가 게시되기 전에는 PyPI의 `pynext-framework[database]`나 `pynext-framework[postgres]`를 0.1.0에서 사용할 수 없다.

프로젝트 루트의 `resources.py`에 등록하면 CLI와 `create_app()`이 앱 시작 때 연결을 확인하고, 종료 때 엔진과 풀을 정리한다. 각 워커는 독립된 풀을 갖는다. ASGI 서버에서는 lifespan을 켜야 하고, 저수준 API로 앱을 직접 실행할 때는 `await app.startup()`과 `await app.shutdown()`을 호출해야 한다.

```python
# resources.py
import os
from pynext import Database

# 환경 변수가 없으면 현재 작업 디렉터리에 SQLite 파일을 만든다.
RESOURCES = {"db": Database(os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./app.sqlite3"))}
```

PostgreSQL 예: `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME`. `sqlite:///...`, `postgresql://...`, `postgres://...`도 각각 비동기 드라이버로 정규화한다. 상대 SQLite 경로는 **현재 작업 디렉터리 기준**이다. 자격 증명은 환경 변수나 비밀 저장소로 전달하고 저장소에 기록하지 않는다. 연결 수와 타임아웃 등 SQLAlchemy `create_async_engine` 옵션은 `Database(url, engine_options={...})`로 전달할 수 있다. 워커 수에 풀 크기를 곱한 총 DB 연결 수를 고려한다.

위 SQLite 폴백은 개발 예제다. 공유 DB를 사용하는 배포에서는 환경 변수 누락을 오류로 처리하도록 `Database(os.environ["DATABASE_URL"])`을 사용한다. 기동 중 연결 확인은 일회성 검사이며 이후 DB 상태를 계속 감시하거나 자동 복구를 보장하지 않는다. `echo=True` 같은 SQL 로그 옵션은 쿼리와 민감한 값을 노출할 수 있다. 세션은 컨텍스트를 빠져나와 반환해야 하며 엔진 종료가 아직 실행 중인 요청을 강제로 취소하지는 않는다.

## ORM과 트랜잭션

다음 예제는 `app/models.py`와 `app/items/route.py`를 사용한다. `app/__init__.py`를 추가하면 마이그레이션 도구에서도 `app.models`를 가져올 수 있다.

```python
# app/models.py
from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
```

```python
# app/items/route.py
from sqlalchemy import select
from pynext import JSONResponse, route
from ..models import Item

@route("GET")
async def get(request):
    async with request.resources["db"].session() as session:
        items = (await session.scalars(select(Item).order_by(Item.id))).all()
        return JSONResponse([{"id": item.id, "name": item.name} for item in items])

@route("POST")
async def post(request):
    # 실제 앱에서는 인증·CSRF와 입력 검증을 먼저 수행한다.
    data = await request.json()
    async with request.resources["db"].transaction() as session:
        item = Item(name=data["name"])
        session.add(item)
        await session.flush()
        item_id = item.id
    return JSONResponse({"id": item_id}, status=201)
```

`session()`은 새 세션을 열고 닫지만 자동 커밋하지 않는다. 쓰기를 유지하려면 명시적으로 `commit()`하거나 `transaction()`을 사용한다. `transaction()`은 정상 종료 시 커밋하고 예외·취소 시 롤백한다. `AsyncSession`은 동시 작업에 공유하지 말고 요청이나 작업마다 별도로 연다. DB 커밋 후 외부 호출 실패나 동기 작업의 강제 종료는 자동으로 되돌릴 수 없으므로 기한·멱등성·복구 정책을 앱에서 정한다.

SQLite 메모리 DB는 보통 연결 하나를 공유하므로 세션이 겹치면 다른 세션의 트랜잭션을 함께 커밋할 수 있다. 이를 방지하기 위해 `Database`는 메모리 DB 세션 전체를 직렬화하고 같은 작업에서 세션을 중첩하면 오류를 반환한다. 세션이 열린 상태에서 다른 작업의 세션 완료를 기다리면 교착될 수 있으므로 트랜잭션을 짧게 유지한다. `db.engine`을 직접 사용한 연결은 이 보호를 거치지 않으므로 세션과 동시에 사용하지 않는다. 파일 기반 SQLite 또는 PostgreSQL이 일반적인 동시 요청에 적합하다.

## Alembic 마이그레이션

앱 프로젝트에 Alembic을 별도로 설치하고 초기화한다. 배포 과정에서 마이그레이션을 **한 번** 실행하며, 각 웹 워커 시작 시 자동으로 실행하지 않는다. 자동 생성된 변경은 검토하고 백업·롤백 정책을 준비한다.

```bash
pip install 'alembic>=1.13,<2'
alembic init migrations
export DATABASE_URL='sqlite+aiosqlite:///./app.sqlite3'
# PostgreSQL이면 postgresql+asyncpg://...를 비밀 저장소에서 설정한다.
alembic revision --autogenerate -m 'create items'
alembic upgrade head
```

생성된 `migrations/env.py`를 아래처럼 설정한다. 모델 import 경로는 앱 구조에 맞게 바꾸고, `DATABASE_URL`에는 비동기 드라이버가 명시된 URL을 사용한다. 자격 증명은 `alembic.ini`에 기록하지 않는다.

```python
# migrations/env.py
import asyncio
import os
from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from app.models import Base

config = context.config
target_metadata = Base.metadata

def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_online():
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = os.environ["DATABASE_URL"]
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()

if context.is_offline_mode():
    context.configure(url=os.environ["DATABASE_URL"], target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
```

SQLite 개발 DB와 PostgreSQL 운영 DB는 SQL 방언과 마이그레이션 동작이 다를 수 있으므로 배포 대상 DB에서도 테스트한다. 현재 프레임워크는 외부 공개 운영에 적합하도록 보안 강화가 완료되지 않았다.
