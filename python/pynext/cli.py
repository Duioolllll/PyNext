import argparse
import asyncio
import gc
import os
import sys
import traceback
from pathlib import Path

from .app import create_app
from .build import write_manifest
from .observability import ObservabilityConfig
from .ratelimit import RateLimiter
from .server import serve
from .supervisor import WorkerConfig, run_supervisor
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


async def run_dev(root, host, port, max_concurrent, acquire_timeout_ms,
                  body_capacity, body_timeout_ms, cache_backend="memory", cache_path=None,
                 observability=None, rate_limit=None):
    """Serve the app in development mode, restarting on file changes.

    The new app is built before the old server stops, so a broken edit
    prints its error and keeps the previous version serving until the next
    change fixes it. The server restarts on the same port.
    """
    watcher = Watcher(root)
    watched = ", ".join(str(path.relative_to(root)) for path in watcher.watch_roots())
    print(f"PyNext dev watching {watched or 'nothing'} for changes", flush=True)
    app = None
    server_task = None
    stop_server = None
    try:
        while True:
            if app is None:
                try:
                    app = create_app(root, production=False, cache_backend=cache_backend, cache_path=cache_path,
                               observability=observability, rate_limit=rate_limit)
                except Exception:
                    traceback.print_exc()
                    print("PyNext dev: fix the error above; watching for changes...", flush=True)
                    await watcher.wait_for_change()
                    continue
            stop_server = asyncio.Event()
            server_task = asyncio.create_task(serve(app, host, port, max_concurrent,
                                                    acquire_timeout_ms, body_capacity, body_timeout_ms,
                                                    stop_event=stop_server, observability=observability))
            new_app = None
            while new_app is None:
                changed = await _wait_for_change(watcher, server_task)
                if changed is None:
                    if not server_task.cancelled() and server_task.exception() is not None:
                        traceback.print_exception(server_task.exception())
                    return
                print(f"PyNext dev: change detected ({', '.join(changed)}); reloading...",
                      flush=True)
                try:
                    new_app = create_app(root, production=False, cache_backend=cache_backend, cache_path=cache_path,
                               observability=observability, rate_limit=rate_limit)
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
    if number <= 0:
        raise argparse.ArgumentTypeError("--rate-limit-window must be a number > 0")
    return number

def main():
    parser = argparse.ArgumentParser(prog="pynext")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "dev", "start"):
        command = sub.add_parser(name)
        command.add_argument("--dir", default=".", help="application project directory")
        if name != "build":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=_port, default=3000)
            command.add_argument("--max-concurrent", type=_at_least(1, "--max-concurrent"), default=64,
                                 help="in-flight requests before new ones wait for a slot")
            command.add_argument("--acquire-timeout-ms", type=_at_least(0, "--acquire-timeout-ms"), default=1000,
                                 help="how long a request waits for a slot before getting a 503")
            command.add_argument("--body-capacity", type=_at_least(1, "--body-capacity"), default=16,
                                 help="concurrent request bodies being read before new ones get a 503")
            command.add_argument("--body-timeout-ms", type=_at_least(0, "--body-timeout-ms"), default=10000,
                                 help="how long a request body may take to arrive before a 408")
            command.add_argument("--cache-backend", choices=("memory", "sqlite"), default="memory",
                                 help="ISR page cache backend: process-local memory (default) or a"
                                      " SQLite file shared by all workers")
            command.add_argument("--cache-path", default=None,
                                 help="SQLite cache file for --cache-backend sqlite;"
                                      " defaults to <project>/.pynext/isr-cache.sqlite3")
            command.add_argument("--structured-logs", dest="structured_logs",
                                 action=argparse.BooleanOptionalAction, default=None,
                                 help="emit one JSON object per log line (default: on)")
            command.add_argument("--metrics", dest="metrics",
                                 action=argparse.BooleanOptionalAction, default=None,
                                 help="collect in-memory request/cache/queue metrics (default: on)")
            command.add_argument("--tracing", dest="tracing",
                                 action=argparse.BooleanOptionalAction, default=None,
                                 help="emit OpenTelemetry spans when the opentelemetry packages are installed (default: off)")
            command.add_argument("--workers", type=_at_least(1, "--workers"), default=1,
                                 help="worker processes sharing the port (default 1: single process); "
                                      "a supervisor restarts crashed workers")
            command.add_argument("--rate-limit", type=_at_least(0, "--rate-limit"), default=0,
                                 help="requests allowed per client per --rate-limit-window (0 disables)")
            command.add_argument("--rate-limit-window", type=_positive_float, default=60.0,
                                 help="rate limit window in seconds")
            command.add_argument("--trusted-proxy", action="append", default=[], metavar="IP_OR_CIDR",
                                 help="peer IP or network whose X-Forwarded-For is honoured "
                                      "for rate limiting (repeatable; default: none trusted)")
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    if args.command == "build":
        manifest = write_manifest(root)
        print(f"Built {len(manifest['routes'])} routes in {root / '.pynext'}")
        return
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
    if args.workers > 1:
        if os.name != "posix":
            parser.error("--workers > 1 is only supported on Unix (SO_REUSEPORT)")
        if args.port == 0:
            parser.error("--workers > 1 requires a fixed --port (port 0 cannot be shared by workers)")
    try:
        limiter = (RateLimiter(args.rate_limit, args.rate_limit_window,
                               trusted_proxies=args.trusted_proxy)
                   if args.rate_limit >= 1 else None)
    except ValueError as exc:
        parser.error(str(exc))
    extra = {"observability": obs_config} if obs_config is not None else {}
    if args.workers > 1:
        config = WorkerConfig(root=str(root), host=args.host, port=args.port,
                              production=args.command == "start",
                              max_concurrent=args.max_concurrent,
                              acquire_timeout_ms=args.acquire_timeout_ms,
                              body_capacity=args.body_capacity,
                              body_timeout_ms=args.body_timeout_ms,
                              rate_limit=args.rate_limit,
                              rate_limit_window=args.rate_limit_window,
                              trusted_proxies=tuple(args.trusted_proxy),
                              cache_backend=args.cache_backend, cache_path=args.cache_path,
                              observability=obs_config)
        return run_supervisor(config, workers=args.workers)
    if args.command == "dev":
        try:
            asyncio.run(run_dev(root, args.host, args.port, args.max_concurrent, args.acquire_timeout_ms,
                                args.body_capacity, args.body_timeout_ms,
                                cache_backend=args.cache_backend, cache_path=args.cache_path,
                                rate_limit=limiter, **extra))
        except KeyboardInterrupt:
            pass
        return
    app = create_app(root, production=args.command == "start",
                     cache_backend=args.cache_backend, cache_path=args.cache_path,
                     rate_limit=limiter, **extra)
    try:
        asyncio.run(serve(app, args.host, args.port, args.max_concurrent, args.acquire_timeout_ms,
                          args.body_capacity, args.body_timeout_ms, **extra))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
