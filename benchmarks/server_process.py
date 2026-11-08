"""Private loopback server process for benchmark comparisons."""
import argparse
import asyncio

from bench import ROOT, _start_uvicorn_http
from pynext import create_app
from pynext.server import start_native


async def run(mode, production):
    app = None
    native = None
    uvicorn_server = uvicorn_task = listener = None
    if mode == "starlette":
        from starlette_app import app as reference
        uvicorn_server, uvicorn_task, listener = await _start_uvicorn_http(reference)
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    else:
        app = create_app(ROOT, production=production)
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
        if app is not None:
            await app.isr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("axum", "uvicorn", "starlette"), required=True)
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.production))
