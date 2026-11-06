import argparse
import asyncio
from pathlib import Path

from .app import create_app
from .build import write_manifest
from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="pynext")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "dev", "start"):
        command = sub.add_parser(name)
        command.add_argument("--dir", default=".", help="application project directory")
        if name != "build":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=3000)
            command.add_argument("--max-concurrent", type=int, default=64,
                                 help="in-flight requests before new ones wait for a slot")
            command.add_argument("--acquire-timeout-ms", type=int, default=1000,
                                 help="how long a request waits for a slot before getting a 503")
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    if args.command == "build":
        manifest = write_manifest(root)
        print(f"Built {len(manifest['routes'])} routes in {root / '.pynext'}")
        return
    app = create_app(root, production=args.command == "start")
    try:
        asyncio.run(serve(app, args.host, args.port, args.max_concurrent, args.acquire_timeout_ms))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
