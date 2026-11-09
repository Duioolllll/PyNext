from collections.abc import AsyncIterator, Awaitable, Callable
from typing import assert_type, cast

from pynext import (
    App, Database, DjangoResource, JSONResponse, MiddlewareResult, RateLimiter, Request, Response,
    SecurityConfig, Session, SessionMiddleware, SQLiteSessionStore, StreamingResponse, WebSocket,
    WebSocketPubSub, create_app, csrf_protect, fragment, h, island, page,
    require_auth, route, websocket,
)
from pynext.types import Middleware, NextHandler, RenderNode


@route("POST")
@csrf_protect
@require_auth
async def post(request: Request) -> Response:
    request.check_cancelled()
    assert_type(request.cancelled, bool)
    payload = await request.json()
    assert_type(request.query, dict[str, list[str]])
    database = cast(Database, request.resources["db"])
    async with database.transaction() as session:
        assert_type(session.is_active, bool)
    form = await request.form()
    session_state = cast(Session, request.state["session"])
    session_state["user_id"] = "alice"
    session_state.rotate_on_login()
    return JSONResponse({"body": payload, "fields": list(form)})


@page(render="isr", revalidate=10)
def Page(request: Request) -> RenderNode:
    return h("html", {}, h("body", {}, fragment("hello", 1), island("counter", props={"initial": 1})))


@websocket(protocols=("chat",))
async def socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text(await websocket.receive_text())


async def chunks() -> AsyncIterator[bytes]:
    yield b"hello"


async def resources(django: DjangoResource, broker: WebSocketPubSub) -> None:
    def increment(value: int) -> int:
        return value + 1
    assert_type(await django.run(increment, 1), int)
    assert_type(await broker.publish("topic", "hello"), int)
    async with broker.subscribe("topic") as subscription:
        assert_type(await subscription.receive(), str | bytes)


async def redirecting_middleware(request: Request, call_next: NextHandler) -> Response | MiddlewareResult:
    if request.path == "/old":
        return MiddlewareResult.redirect("/new")
    return await call_next(request)


# A middleware returning a MiddlewareResult is a valid Middleware.
redirecting: Middleware = redirecting_middleware

app = create_app(".", security=SecurityConfig(cors_allowed_origins=("https://client.example",)),
                 resources={"db": Database()}, middleware=[SessionMiddleware(SQLiteSessionStore("session.db"))])
assert_type(app, App)
handler: Callable[[Request], Awaitable[Response]] = post
stream: Response = StreamingResponse(chunks())
assert_type(RateLimiter(10, 60, exempt_paths=("/public",)).is_exempt_path("/public?literal"), bool)
