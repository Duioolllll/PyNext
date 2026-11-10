from collections.abc import Callable, Iterable
from typing import Any, Literal, overload, TypeVar
from .types import Attributes, Child, JSONValue, RenderNode

_F = TypeVar("_F", bound=Callable[..., Any])
def h(tag: str, attrs: Attributes | None = ..., *children: Child) -> RenderNode: ...
def fragment(*children: Child) -> RenderNode: ...
def model_prop(name: str) -> RenderNode: ...
def island(name: str, *, props: dict[str, JSONValue] | None = ..., fallback: Child = ...,
           hydrate: bool = ..., strategy: Literal["load", "visible", "idle", "interaction"] = ...,
           model: RenderNode | None = ...) -> RenderNode: ...
def page(*, render: Literal["ssr", "ssg", "isr"] = ..., revalidate: int | float | bool | None = ...,
         stale: int | None = ..., expire: int | float | None = ..., cache_life: str | None = ...,
         query_keys: Iterable[str] = ...,
         dynamic: Literal["auto", "error", "force-dynamic"] = ...,
         tags: Iterable[str] = ...,
         fallback: Literal["not_found", "blocking", "prerender"] = ...,
         dynamic_params: bool = ...) -> Callable[[_F], _F]: ...
@overload
def layout(fn: _F) -> _F: ...
@overload
def layout(fn: None = ..., *, revalidate: int | None = ...,
           stale: int | None = ..., expire: int | float | None = ...,
           dynamic: bool = ...) -> Callable[[_F], _F]: ...
