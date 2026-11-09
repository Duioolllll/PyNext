from collections.abc import Iterable
from typing import Literal
from .types import ProxyEntry
from .ratelimit import RateLimiter
class RedisRateLimiter(RateLimiter):
    last_unavailable_at: float | None
    def __init__(self, limit: int, window_seconds: float, *, url: str | None = ..., namespace: str = ...,
                 trusted_proxies: Iterable[ProxyEntry] = ..., exempt_paths: Iterable[str] = ...,
                 max_buckets: int | None = ..., max_new_keys_per_window: int | None = ...,
                 ipv6_prefix_len: int = ..., failure_policy: Literal["fail_open", "fail_closed"] | None = ...) -> None: ...
    def close(self) -> None: ...
