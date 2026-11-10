import asyncio
import socket
from collections.abc import Callable
from typing import Protocol
from .app import App
from .observability import ObservabilityConfig
from .types import NativeServer as _NativeServer

REQUEST_TIMEOUT: float
STREAM_CHUNK_TIMEOUT: float

def start_native(app: App, loop: asyncio.AbstractEventLoop, host: str = ..., port: int = ...,
                 max_concurrent: int = ..., acquire_timeout_ms: int = ..., body_capacity: int = ...,
                 body_timeout_ms: int = ..., observability: ObservabilityConfig | None = ..., *,
                 reuse_port: bool = ..., listener: socket.socket | None = ..., max_connections: int = ...,
                 max_connections_per_ip: int = ..., max_concurrent_per_ip: int = ...,
                 header_read_timeout_ms: int = ...,
                 body_min_rate_bytes_per_sec: int = ..., shutdown_timeout: float = ...,
                 max_waiting: int | None = ..., acquire_timeout_max_ms: int | None = ...,
                 handler_timeout_ms: int | None = ...,
                 response_timeout_ms: int | None = ...) -> _NativeServer: ...
async def serve(app: App, host: str = ..., port: int = ..., max_concurrent: int = ...,
                acquire_timeout_ms: int = ..., body_capacity: int = ..., body_timeout_ms: int = ...,
                stop_event: asyncio.Event | None = ..., observability: ObservabilityConfig | None = ..., *,
                reuse_port: bool = ..., on_ready: Callable[[str], None] | None = ...,
                listener: socket.socket | None = ..., max_connections: int = ...,
                max_connections_per_ip: int = ..., max_concurrent_per_ip: int = ...,
                header_read_timeout_ms: int = ...,
                body_min_rate_bytes_per_sec: int = ..., shutdown_timeout: float = ...,
                max_waiting: int | None = ..., acquire_timeout_max_ms: int | None = ...,
                handler_timeout_ms: int = ..., response_timeout_ms: int = ...) -> None: ...
