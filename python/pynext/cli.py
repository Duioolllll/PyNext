import argparse
import asyncio
import gc
import math
import os
import sys
import traceback
from pathlib import Path

from .app import create_app
from .build import write_manifest
from .client import compile_client, discover_client
from .http import MAX_WS_MESSAGE, SecurityConfig
from .observability import ObservabilityConfig
from .ratelimit import RateLimiter
from .redis_rate import RedisRateLimiter
from .scaffold import init_project
from .server import (MAX_BODY_CAPACITY, MAX_BODY_MIN_RATE_BYTES_PER_SEC,
                     MAX_CONCURRENT, MAX_CONNECTIONS, MAX_CONNECTIONS_PER_IP,
                     MAX_SHUTDOWN_TIMEOUT, MAX_TIMEOUT_MS, MAX_WAITING, serve)
from .supervisor import MAX_WORKERS, WorkerConfig, run_supervisor
from .standalone import build_standalone
from .watch import Watcher


def _port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 0 to 65535") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 0 to 65535")
    return port


def _at_least(minimum, label):
    """argparse type for an integer that must be >= ``minimum``.

    Counts (--max-concurrent, --body-capacity) need at least 1: Rust rejects
    0 with ValueError and a negative value dies in the PyO3 u64/usize
    conversion, both as tracebacks. Timeouts (--acquire-timeout-ms,
    --body-timeout-ms) allow 0: a 0ms wait is a valid "take a slot only if
    instantly free, else 503/408" mode (Duration::from_millis(0) in Rust).
    --max-waiting also allows 0: no request waits for a slot at all.
    """
    def convert(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} must be an integer >= {minimum}") from exc
        if number < minimum:
            raise argparse.ArgumentTypeError(f"{label} must be an integer >= {minimum}")
        return number
    return convert


def _bounded(minimum, maximum, label):
    """argparse type for an integer that must be within [``minimum``, ``maximum``].

    Same lower-bound story as :func:`_at_least`, plus an upper bound: a
    value past the native usize/u64/f64 conversion range dies with an
    OverflowError traceback instead of a CLI error, and an absurd but
    fitting value (--workers 100000, --max-concurrent in the billions)
    exhausts resources before serving anything. Both ends are rejected
    here, at parse time, with a clear ``parser.error`` message.
    """
    def convert(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} must be an integer in [{minimum}, {maximum}]") from exc
        if number < minimum:
            raise argparse.ArgumentTypeError(f"{label} must be an integer >= {minimum}")
        if number > maximum:
            raise argparse.ArgumentTypeError(f"{label} must be an integer <= {maximum}")
        return number
    return convert


async def _wait_for_change(watcher, server_task):
    """Wait for a watched file to change.

    Returns the changed paths, or None if the server stops first.
    """
    watcher_task = asyncio.create_task(watcher.wait_for_change())
    try:
        done, _ = await asyncio.wait({server_task, watcher_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        raise
    if server_task in done:
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return None
    return watcher_task.result()


async def _reload_dev_client(app, root):
    sources = await asyncio.to_thread(discover_client, root)
    manifest, assets = await asyncio.to_thread(compile_client, root, sources)
    if await asyncio.to_thread(discover_client, root) != sources:
        raise ValueError("Island sources changed during compilation; retry the edit")
    app.update_dev_client(manifest, assets)


async def run_dev(root, host, port, max_concurrent, acquire_timeout_ms,
                  body_capacity, body_timeout_ms, cache_backend="memory", cache_path=None,
                 observability=None, rate_limit=None, security=None, websocket_max_message=MAX_WS_MESSAGE,
                 max_connections=None, max_connections_per_ip=None,
                 header_read_timeout_ms=None, body_min_rate_bytes_per_sec=None,
                 trusted_proxies=(), cache_failure_policy=None, shutdown_timeout=30.0,
                 max_waiting=None, strict_ssg=False):
    """Serve the app in development mode, replacing changed island bundles.

    Other file changes restart the server on the same port and reload open
    pages. Broken edits retain the previous app or bundles until fixed.
    """
    conn_kwargs = {}
    if max_connections is not None:
        conn_kwargs["max_connections"] = max_connections
    if max_connections_per_ip is not None:
        conn_kwargs["max_connections_per_ip"] = max_connections_per_ip
    if header_read_timeout_ms is not None:
        conn_kwargs["header_read_timeout_ms"] = header_read_timeout_ms
    if body_min_rate_bytes_per_sec is not None:
        conn_kwargs["body_min_rate_bytes_per_sec"] = body_min_rate_bytes_per_sec
    watcher = Watcher(root)
    watched = ", ".join(str(path.relative_to(root)) for path in watcher.watch_roots())
    print(f"PyNext dev watching {watched or 'nothing'} for changes", flush=True)
    app = None
    server_task = None
    stop_server = None
    current_port = port

    def record_address(address):
        nonlocal current_port
        if current_port == 0:
            current_port = int(address.rsplit(":", 1)[1])
    try:
        while True:
            if app is None:
                try:
                    app = create_app(root, production=False, cache_backend=cache_backend, cache_path=cache_path,
                               observability=observability, rate_limit=rate_limit, debug=True, security=security,
                               websocket_max_message=websocket_max_message, hot_reload=True,
                               trusted_proxies=trusted_proxies, strict_ssg=strict_ssg,
                               **({"cache_failure_policy": cache_failure_policy}
                                  if cache_failure_policy is not None else {}))
                except Exception:
                    traceback.print_exc()
                    print("PyNext dev: fix the error above; watching for changes...", flush=True)
                    await watcher.wait_for_change()
                    continue
            stop_server = asyncio.Event()
            server_task = asyncio.create_task(serve(app, host, current_port, max_concurrent,
                                                    acquire_timeout_ms, body_capacity, body_timeout_ms,
                                                    stop_event=stop_server, observability=observability,
                                                    shutdown_timeout=shutdown_timeout,
                                                    on_ready=record_address, max_waiting=max_waiting,
                                                    **conn_kwargs))
            new_app = None
            while new_app is None:
                changed = await _wait_for_change(watcher, server_task)
                if changed is None:
                    if not server_task.cancelled() and server_task.exception() is not None:
                        traceback.print_exception(server_task.exception())
                    return
                if changed and all(path.startswith("islands/") for path in changed):
                    try:
                        await _reload_dev_client(app, root)
                        print(f"PyNext dev: island modules updated ({', '.join(changed)})", flush=True)
                    except Exception:
                        traceback.print_exc()
                        print("PyNext dev: keeping previous island modules; fix the error above.", flush=True)
                    continue
                print(f"PyNext dev: change detected ({', '.join(changed)}); reloading...", flush=True)
                try:
                    new_app = create_app(root, production=False, cache_backend=cache_backend, cache_path=cache_path,
                               observability=observability, rate_limit=rate_limit, debug=True, security=security,
                               websocket_max_message=websocket_max_message, hot_reload=True,
                               trusted_proxies=trusted_proxies,
                               **({"cache_failure_policy": cache_failure_policy}
                                  if cache_failure_policy is not None else {}))
                except Exception:
                    traceback.print_exc()
                    print("PyNext dev: keeping the previous version running; "
                          "fix the error above.", flush=True)
            stop_server.set()
            await server_task
            server_task = None
            # Drop the old app so its per-app modules are released before the
            # new app loads the edited sources.
            del app
            gc.collect()
            app = new_app
    finally:
        if server_task is not None and not server_task.done():
            if stop_server is not None:
                stop_server.set()
            await asyncio.gather(server_task, return_exceptions=True)


def _positive_float(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--rate-limit-window must be a number > 0") from exc
    # float("nan") and float("inf") parse fine but are not usable windows:
    # nan passes the <= 0 check (comparisons are False) and only survived
    # because RateLimiter.__init__ rejects non-finite windows downstream.
    # Reject them here so the CLI reports the bad flag instead.
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("--rate-limit-window must be a number > 0")
    return number

def main():
    parser = argparse.ArgumentParser(prog="pynext")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a minimal application")
    init.add_argument("--dir", default=".", help="application project directory")
    for name in ("build", "dev", "start"):
        command = sub.add_parser(name)
        command.add_argument("--dir", default=".", help="application project directory")
        if name == "build":
            command.add_argument("--standalone", action="store_true", help="create a Linux standalone directory")
            command.add_argument("--standalone-dir", default=None, help="new output directory for --standalone")
        if name != "build":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=_port, default=3000)
            command.add_argument("--max-concurrent", type=_bounded(1, MAX_CONCURRENT, "--max-concurrent"),
                                 default=64,
                                 help="in-flight requests before new ones wait for a slot (1-1000000)")
            command.add_argument("--acquire-timeout-ms", type=_bounded(0, MAX_TIMEOUT_MS, "--acquire-timeout-ms"),
                                 default=1000,
                                 help="how long a request waits for a slot before getting a 503 (0-86400000)")
            command.add_argument("--max-waiting", type=_bounded(0, MAX_WAITING, "--max-waiting"), default=None,
                                 help="requests waiting for an execution slot before new ones get a 503"
                                      " (default: min(--max-concurrent, 64); 0 waits for nobody; max 1000000)")
            command.add_argument("--body-capacity", type=_bounded(1, MAX_BODY_CAPACITY, "--body-capacity"),
                                 default=16,
                                 help="concurrent request bodies being read before new ones get a 503 (1-1000000)")
            command.add_argument("--body-timeout-ms", type=_bounded(0, MAX_TIMEOUT_MS, "--body-timeout-ms"),
                                 default=10000,
                                 help="how long a request body may take to arrive before a 408 (0-86400000)")
            command.add_argument("--max-connections", type=_bounded(1, MAX_CONNECTIONS, "--max-connections"),
                                 default=None,
                                 help="concurrent TCP connections before new ones are dropped at accept (1-1000000)")
            command.add_argument("--max-connections-per-ip",
                                 type=_bounded(1, MAX_CONNECTIONS_PER_IP, "--max-connections-per-ip"),
                                 default=None,
                                 help="concurrent TCP connections allowed from one client IP (1-1000000)")
            command.add_argument("--header-read-timeout-ms",
                                 type=_bounded(0, MAX_TIMEOUT_MS, "--header-read-timeout-ms"),
                                 default=None,
                                 help="how long request headers (and idle keep-alive) may take before the "
                                      "connection is closed (0-86400000)")
            command.add_argument("--body-min-rate-bytes-per-sec",
                                 type=_bounded(0, MAX_BODY_MIN_RATE_BYTES_PER_SEC,
                                               "--body-min-rate-bytes-per-sec"), default=None,
                                 help="minimum average body receive rate in bytes/second after a 2s grace "
                                      "window; slower bodies get a 408 (0 disables; max 1000000000)")
            command.add_argument("--cache-backend", choices=("memory", "sqlite", "redis"), default="memory",
                                 help="ISR page cache backend: process-local memory (default) or a"
                                      " shared SQLite file or Redis via PYNEXT_REDIS_URL")
            command.add_argument("--cache-path", default=None,
                                 help="SQLite cache file for --cache-backend sqlite;"
                                      " defaults to <project>/.pynext/isr-cache.sqlite3")
            command.add_argument("--cache-failure-policy", choices=("fail_open", "fail_closed"),
                                 default="fail_open",
                                 help="shared ISR cache behaviour when its backend is unavailable:"
                                      " fail_open serves from the local cache (default);"
                                      " fail_closed answers 503 instead")
            command.add_argument("--structured-logs", dest="structured_logs",
                                 action=argparse.BooleanOptionalAction, default=None,
                                 help="emit one JSON object per log line (default: on)")
            command.add_argument("--metrics", dest="metrics",
                                 action=argparse.BooleanOptionalAction, default=None,
                                 help="collect in-memory request/cache/queue metrics (default: on)")
            command.add_argument("--tracing", dest="tracing",
                                 action=argparse.BooleanOptionalAction, default=None,
                                 help="emit OpenTelemetry spans when the opentelemetry packages are installed (default: off)")
            command.add_argument("--workers", type=_bounded(1, MAX_WORKERS, "--workers"), default=1,
                                 help="worker processes sharing the port (default 1: single process; max 64); "
                                      "a supervisor restarts crashed workers")
            command.add_argument("--rate-limit", type=_at_least(0, "--rate-limit"), default=0,
                                 help="requests allowed per client per --rate-limit-window (0 disables)")
            command.add_argument("--rate-limit-window", type=_positive_float, default=60.0,
                                 help="rate limit window in seconds")
            command.add_argument("--rate-limit-ipv6-prefix-len",
                                 type=_bounded(0, 128, "--rate-limit-ipv6-prefix-len"), default=64,
                                 help="IPv6 prefix length sharing one rate-limit bucket (default 64;"
                                      " 48 groups a whole /48 into one bucket, which stops a /48"
                                      " holder minting fresh buckets, but also throttles every host"
                                      " sharing that prefix together)")
            command.add_argument("--rate-limit-backend", choices=("memory", "redis"), default="memory",
                                 help="per-process memory (default) or Redis via PYNEXT_REDIS_URL")
            command.add_argument("--rate-limit-failure-policy", choices=("fail_open", "fail_closed"),
                                 default="fail_closed",
                                 help="Redis rate limiter behaviour when Redis is unavailable:"
                                      " fail_closed answers 503 (default);"
                                      " fail_open admits the request")
            command.add_argument("--trusted-proxy", action="append", default=[], metavar="IP_OR_CIDR",
                                 help="peer IP or network whose forwarded headers "
                                      "(X-Forwarded-For, Forwarded, X-Forwarded-Proto, "
                                      "X-Forwarded-Host) are honoured for rate limiting and "
                                      "client identity (repeatable; default: none trusted)")
            command.add_argument("--rate-limit-exempt-path", action="append", default=[], metavar="PATH",
                                 help="path prefix that skips rate limiting, e.g. /healthz "
                                      "(repeatable; default: nothing is exempt)")
            command.add_argument("--csrf", action=argparse.BooleanOptionalAction, default=None,
                                 help="protect state-changing API requests (default: auto — on when a "
                                      "session middleware is installed; --no-csrf opts out)")
            command.add_argument("--csrf-signing-key", default=None, metavar="KEY",
                                 help="HMAC key signing double-submit CSRF cookies: a string of at "
                                      "least 16 bytes, 'hex:<hex>' or 'base64:<base64>' (also via "
                                      "PYNEXT_CSRF_SIGNING_KEY); with --workers N the supervisor "
                                      "shares this key with every worker instead of generating one; "
                                      "an invalid key fails at startup")
            command.add_argument("--csrf-cookie-secure", action=argparse.BooleanOptionalAction, default=None,
                                 help="send the double-submit CSRF cookie only over HTTPS "
                                      "(default: on for start, off for dev over plain http)")
            command.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN",
                                 help="allow this CORS origin (repeatable)")
            command.add_argument("--cors-credentials", action="store_true",
                                 help="allow credentials for configured CORS origins")
            command.add_argument("--csp-nonce", action="store_true", help="enable CSP nonces on dynamic pages")
            command.add_argument("--ws-max-message-bytes", type=_at_least(1, "--ws-max-message-bytes"),
                                 default=MAX_WS_MESSAGE, help="WebSocket message limit in bytes (max 16777216)")
            command.add_argument("--ws-allowed-origin", action="append", default=[], metavar="ORIGIN",
                                 help="allow an additional WebSocket origin (repeatable)")
            command.add_argument("--ws-allow-missing-origin", action="store_true",
                                 help="allow WebSocket clients that omit Origin")
            command.add_argument("--shutdown-timeout", type=_bounded(0, MAX_SHUTDOWN_TIMEOUT, "--shutdown-timeout"),
                                 default=30,
                                 help="seconds to let in-flight requests finish and WebSockets close "
                                      "after SIGTERM before the server stops anyway (default 30; max 3600)")
            command.add_argument("--strict-ssg", action="store_true",
                                 help="development only: answer 404 for SSG paths that "
                                      "generate_static_params would not produce, like production does, "
                                      "instead of rendering them with a warning")
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    if args.command == "init":
        try:
            init_project(root)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Created a PyNext app in {root}")
        return
    if args.command == "build":
        if args.standalone_dir and not args.standalone:
            parser.error("--standalone-dir requires --standalone")
        try:
            manifest = write_manifest(root)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Built {len(manifest['routes'])} routes in {root / '.pynext'}")
        if args.standalone:
            output = Path(args.standalone_dir) if args.standalone_dir else root / ".pynext" / "standalone"
            try:
                build_standalone(root, output)
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
            print(f"Created standalone directory in {output}")
        return
    if args.ws_max_message_bytes > 16 * 1024 * 1024:
        parser.error("--ws-max-message-bytes must be at most 16777216")
    obs_config = None
    if (args.structured_logs is not None or args.metrics is not None
            or args.tracing is not None):
        # Only pass a config explicitly when a CLI flag overrides the
        # environment: create_app() already reads the same PYNEXT_* variables,
        # so the default path stays exactly the old call shape.
        obs_config = ObservabilityConfig.from_env()
        if args.structured_logs is not None:
            obs_config.structured_logs = args.structured_logs
        if args.metrics is not None:
            obs_config.metrics = args.metrics
        if args.tracing is not None:
            obs_config.tracing = args.tracing
    try:
        limiter_type = RedisRateLimiter if args.rate_limit_backend == "redis" else RateLimiter
        options = ({"namespace": os.environ.get("PYNEXT_RATE_LIMIT_NAMESPACE", "default"),
                    "failure_policy": args.rate_limit_failure_policy}
                   if args.rate_limit_backend == "redis" else {})
        limiter = (limiter_type(args.rate_limit, args.rate_limit_window,
                                trusted_proxies=args.trusted_proxy,
                                exempt_paths=tuple(args.rate_limit_exempt_path),
                                ipv6_prefix_len=args.rate_limit_ipv6_prefix_len, **options)
                   if args.rate_limit >= 1 else None)
        security = (SecurityConfig(csrf_enabled=args.csrf,
                                   csrf_signing_key=args.csrf_signing_key,
                                   csrf_cookie_secure=(args.csrf_cookie_secure
                                                      if args.csrf_cookie_secure is not None
                                                      else args.command == "start"),
                                   cors_allowed_origins=tuple(args.cors_origin),
                                   cors_allow_credentials=args.cors_credentials,
                                   websocket_allowed_origins=tuple(args.ws_allowed_origin),
                                   websocket_allow_missing_origin=args.ws_allow_missing_origin,
                                   csp_nonce_enabled=args.csp_nonce)
                    if (args.csrf is not None or args.csrf_signing_key is not None or
                        args.csrf_cookie_secure is not None or args.cors_origin or
                        args.cors_credentials or args.csp_nonce or args.ws_allowed_origin or
                        args.ws_allow_missing_origin) else None)
    except ValueError as exc:
        parser.error(str(exc))
    extra = {"observability": obs_config} if obs_config is not None else {}
    # Connection limits keep the historical serve() call shape when no flag
    # is given (see test_cli_without_observability_flags_keeps_old_call_shape):
    # only explicitly set limits are forwarded, the rest stay at serve()'s
    # defaults.
    conn_kwargs = {}
    if args.max_connections is not None:
        conn_kwargs["max_connections"] = args.max_connections
    if args.max_connections_per_ip is not None:
        conn_kwargs["max_connections_per_ip"] = args.max_connections_per_ip
    if args.header_read_timeout_ms is not None:
        conn_kwargs["header_read_timeout_ms"] = args.header_read_timeout_ms
    if args.body_min_rate_bytes_per_sec is not None:
        conn_kwargs["body_min_rate_bytes_per_sec"] = args.body_min_rate_bytes_per_sec
    if args.workers > 1:
        config = WorkerConfig(root=str(root), host=args.host, port=args.port,
                              production=args.command == "start",
                              max_concurrent=args.max_concurrent,
                              acquire_timeout_ms=args.acquire_timeout_ms,
                              max_waiting=args.max_waiting,
                              body_capacity=args.body_capacity,
                              body_timeout_ms=args.body_timeout_ms,
                              **conn_kwargs,
                              rate_limit=args.rate_limit,
                              rate_limit_window=args.rate_limit_window,
                              rate_limit_backend=args.rate_limit_backend,
                              rate_limit_exempt_paths=tuple(args.rate_limit_exempt_path),
                              rate_limit_ipv6_prefix_len=args.rate_limit_ipv6_prefix_len,
                              rate_limit_failure_policy=args.rate_limit_failure_policy,
                              trusted_proxies=tuple(args.trusted_proxy),
                              cache_backend=args.cache_backend, cache_path=args.cache_path,
                              cache_failure_policy=args.cache_failure_policy,
                              observability=obs_config, security=security,
                              websocket_max_message=args.ws_max_message_bytes,
                              shutdown_timeout=args.shutdown_timeout, strict_ssg=args.strict_ssg)
        return run_supervisor(config, workers=args.workers)
    if args.command == "dev":
        try:
            asyncio.run(run_dev(root, args.host, args.port, args.max_concurrent, args.acquire_timeout_ms,
                                args.body_capacity, args.body_timeout_ms,
                                cache_backend=args.cache_backend, cache_path=args.cache_path,
                                cache_failure_policy=args.cache_failure_policy,
                                rate_limit=limiter, security=security,
                                websocket_max_message=args.ws_max_message_bytes,
                                trusted_proxies=args.trusted_proxy,
                                shutdown_timeout=args.shutdown_timeout, max_waiting=args.max_waiting,
                                strict_ssg=args.strict_ssg,
                                **extra, **conn_kwargs))
        except KeyboardInterrupt:
            pass
        return
    try:
        app = create_app(root, production=args.command == "start",
                         cache_backend=args.cache_backend, cache_path=args.cache_path,
                         rate_limit=limiter,
                         **({"trusted_proxies": args.trusted_proxy} if args.trusted_proxy else {}),
                         **({"security": security} if security else {}),
                         **({"cache_failure_policy": args.cache_failure_policy}
                            if args.cache_failure_policy != "fail_open" else {}),
                         **({"websocket_max_message": args.ws_max_message_bytes}
                            if args.ws_max_message_bytes != MAX_WS_MESSAGE else {}),
                         **({"strict_ssg": True} if args.strict_ssg else {}), **extra)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        asyncio.run(serve(app, args.host, args.port, args.max_concurrent, args.acquire_timeout_ms,
                          args.body_capacity, args.body_timeout_ms,
                          shutdown_timeout=args.shutdown_timeout,
                          **({"max_waiting": args.max_waiting} if args.max_waiting is not None else {}),
                          **extra, **conn_kwargs))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
