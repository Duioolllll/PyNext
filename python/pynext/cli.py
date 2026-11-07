import argparse
import asyncio
from pathlib import Path

from .app import create_app
from .build import write_manifest
from .server import serve


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
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    if args.command == "build":
        manifest = write_manifest(root)
        print(f"Built {len(manifest['routes'])} routes in {root / '.pynext'}")
        return
    app = create_app(root, production=args.command == "start")
    try:
        asyncio.run(serve(app, args.host, args.port, args.max_concurrent, args.acquire_timeout_ms,
                          args.body_capacity, args.body_timeout_ms))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
