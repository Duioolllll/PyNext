"""Generic benchmark server process: serve any PyNext project over loopback HTTP.

``benchmarks/server_process.py`` only serves ``examples/basic``. This script
serves an arbitrary project directory (used for the DB-query fixture app in
``benchmarks/fixtures/db-app``). A ``resources.py`` at the project root is
discovered by ``create_app`` itself, so it is not passed here. It prints one
line and then serves until killed:

    PYNEXT_BENCH_READY 127.0.0.1:<port>

Usage:
    python benchmarks/serve_project.py --dir examples/basic --mode axum [--production]
    python benchmarks/serve_project.py --dir benchmarks/fixtures/db-app --mode axum
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import _start_uvicorn_http  # noqa: E402
from pynext import create_app  # noqa: E402
from pynext.server import start_native  # noqa: E402


async def run(directory, mode, production):
    app = create_app(directory, production=production)
    # Neither start_native nor Uvicorn (lifespan="off") starts the app, so
    # start it here: this opens the ISR cache and starts resources such as
    # the DB fixture's SQLite connection. Without this the DB route would
    # see an uninitialised resource.
    await app.startup()
    native = None
    uvicorn_server = uvicorn_task = listener = None
    if mode == "axum":
        native = start_native(app, asyncio.get_running_loop(), port=0)
        address = native.address
    else:
        uvicorn_server, uvicorn_task, listener = await _start_uvicorn_http(app)
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    print(f"PYNEXT_BENCH_READY {address}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        if uvicorn_task is not None:
            uvicorn_server.should_exit = True
            await uvicorn_task
            listener.close()
        if native is not None:
            await asyncio.to_thread(native.stop)
        await app.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="PyNext project directory")
    parser.add_argument("--mode", choices=("axum", "uvicorn"), required=True)
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.dir, args.mode, args.production))
