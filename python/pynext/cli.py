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


def main():
    parser = argparse.ArgumentParser(prog="pynext")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "dev", "start"):
        command = sub.add_parser(name)
        command.add_argument("--dir", default=".", help="application project directory")
        if name != "build":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=_port, default=3000)
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    if args.command == "build":
        manifest = write_manifest(root)
        print(f"Built {len(manifest['routes'])} routes in {root / '.pynext'}")
        return
    app = create_app(root, production=args.command == "start")
    try:
        asyncio.run(serve(app, args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
