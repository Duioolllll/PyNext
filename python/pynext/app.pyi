from collections.abc import Awaitable, Callable, Iterable, Mapping
from os import PathLike
from typing import Any, Literal
from .http import PathHeaderRuleInput, PathHeaders, Request, Response, SecurityConfig
from .observability import MetricsCollector, ObservabilityConfig
from .proxy import ProxyTrust
from .types import ProxyEntry
from .ratelimit import RateLimiter
from .types import Middleware, Params, Resource

class App:
    production: bool
    debug: bool
    hot_reload: bool
    sync_workers: int
    sync_wait_timeout: float
    sync_queue_limit: int
    render_workers: int
    session_workers: int
    websocket_max_message: int
    build_id: str | None
    deployment_id: str | None
    security: SecurityConfig
    path_headers: PathHeaders
    proxy_trust: ProxyTrust
    resources: Mapping[str, Resource]
    metrics: MetricsCollector
    def __init__(self, root: str | PathLike[str], manifest: dict[str, Any], *, production: bool = ...,
                 security: SecurityConfig | None = ..., cache_backend: Literal["memory", "sqlite", "redis"] = ...,
                 cache_path: str | PathLike[str] | None = ..., observability: ObservabilityConfig | None = ...,
                 rate_limit: RateLimiter | None = ..., middleware: list[Middleware] | tuple[Middleware, ...] | None = ...,
                 debug: bool = ..., resources: dict[str, Resource] | None = ..., cache_url: str | None = ...,
                 cache_failure_policy: Literal["fail_open", "fail_closed"] | None = ...,
                 cache_handler: Any = ...,
                 websocket_max_message: int = ..., hot_reload: bool = ...,
                 trusted_proxies: Iterable[ProxyEntry] | None = ..., sync_workers: int = ...,
                 sync_wait_timeout: float = ..., sync_queue_limit: int | None = ...,
                 render_workers: int | None = ..., session_workers: int | None = ...,
                 strict_ssg: bool = ..., headers: Iterable[PathHeaderRuleInput] | None = ...,
                 revalidate_secret: str | None = ..., body_timeout_ms: int = ...,
                 handler_timeout_ms: int = ..., response_timeout_ms: int = ...) -> None: ...
    def configure_observability(self, config: ObservabilityConfig) -> App: ...
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
    @property
    def is_ready(self) -> bool: ...
    def begin_drain(self) -> None: ...
    async def close_websockets(self, code: int = ..., reason: str = ..., timeout: float = ...) -> bool: ...
    async def wait_for_inflight(self, timeout: float) -> bool: ...
    async def revalidate(self, path: str, type: str | None = ...) -> bool: ...
    async def revalidate_tag(self, tag: str, expire: float | None = ...) -> bool: ...
    def invalidate_layout_shell(self, pattern: str | None = ...) -> None: ...
    async def dispatch(self, request: Request) -> Response: ...
    def update_dev_client(self, manifest: dict[str, Any], assets: dict[str, bytes]) -> None: ...
    def error_response(self, exc: Exception, digest: str | None = ...) -> Response: ...
    def note_dev_bind_host(self, host: str | None) -> None: ...
    async def render_page(self, pattern: str, request: Request, params: Params, *, csp_nonce: bool = ...,
                          query: dict[str, list[str]] | None = ...) -> Response: ...
    @property
    def websocket_routes(self) -> frozenset[str]: ...
    @property
    def websocket_protocols(self) -> dict[str, list[str]]: ...
    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]],
                       send: Callable[[dict[str, Any]], Awaitable[None]]) -> None: ...

def create_app(root: str | PathLike[str] = ..., *, production: bool = ...,
               security: SecurityConfig | None = ..., cache_backend: Literal["memory", "sqlite", "redis"] = ...,
               cache_path: str | PathLike[str] | None = ..., observability: ObservabilityConfig | None = ...,
               rate_limit: RateLimiter | None = ..., middleware: list[Middleware] | tuple[Middleware, ...] | None = ...,
               debug: bool = ..., resources: dict[str, Resource] | None = ..., cache_url: str | None = ...,
               cache_failure_policy: Literal["fail_open", "fail_closed"] | None = ...,
               cache_handler: Any = ...,
               websocket_max_message: int = ..., hot_reload: bool = ...,
               trusted_proxies: Iterable[ProxyEntry] | None = ..., sync_workers: int = ...,
               sync_wait_timeout: float = ..., sync_queue_limit: int | None = ...,
               render_workers: int | None = ..., session_workers: int | None = ...,
               strict_ssg: bool = ..., headers: Iterable[PathHeaderRuleInput] | None = ...,
               revalidate_secret: str | None = ..., body_timeout_ms: int = ...,
               handler_timeout_ms: int = ..., response_timeout_ms: int = ...) -> App: ...
