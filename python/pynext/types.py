"""Types shared by the public API. Dynamic resources/state stay application-defined."""
from collections.abc import Awaitable, Callable, Iterable
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from .http import Request, Response

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type Headers = Iterable[tuple[bytes, bytes]]
type Params = dict[str, str | list[str]]
type ProxyEntry = str | IPv4Address | IPv6Address | IPv4Network | IPv6Network
type Attributes = dict[str, str | int | float | bool | None]


class RenderNode(TypedDict, total=False):
    kind: str
    tag: str
    attrs: Attributes
    children: list[RenderNode]
    value: str
    name: str
    props: dict[str, JSONValue]
    hydrate: bool
    strategy: str
    model: RenderNode


type Child = RenderNode | str | int | float | None
type NextHandler = Callable[["Request"], Awaitable["Response"]]
type Middleware = Callable[["Request", NextHandler], Awaitable["Response"]]


class Resource(Protocol):
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...


class SessionStore(Protocol):
    async def load(self, token: str) -> tuple[dict[str, JSONValue], str | None, int] | None: ...
    async def save(self, token: str, data: dict[str, JSONValue], csrf: str | None, version: int | None,
                   expires: float, *, previous_token: str | None = None) -> int: ...
    # Built-in Redis stores report whether the session was revoked now
    # (True) or queued for revocation on recovery (False, fail_open);
    # other stores return None.
    async def delete(self, token: str) -> bool | None: ...
    async def purge_expired(self) -> None: ...


class NativeServer(Protocol):
    @property
    def address(self) -> str: ...
    def stop(self, timeout: float | None = ...) -> None: ...
    def rate_limit_fail_open_count(self) -> int: ...
