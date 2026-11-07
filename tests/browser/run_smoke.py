"""Start the built example and run the Playwright smoke test."""
import os
import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]

# How many times to try starting the server on a fresh port before giving up.
MAX_SERVER_START_ATTEMPTS = 5


def verify_http(base):
    pages = {
        "/": "Hello PyNext!",
        "/about": "About",
        "/guides/intro": "intro",
        "/guides/routing": "routing",
        "/news/example": "example",
        "/hydrate": "Hydrated",
        "/strategies": "Island loading strategies",
        "/interaction": "Interaction example",
        "/blog/hello": "hello",
    }
    for path, expected in pages.items():
        with urlopen(base + path, timeout=10) as response:
            assert response.status == 200, path
            assert response.headers.get("content-security-policy") == "script-src 'self'; object-src 'none'", path
            assert expected in response.read().decode("utf-8"), path
    try:
        urlopen(base + "/guides/missing", timeout=10)
    except HTTPError as error:
        assert error.code == 404
    else:
        raise AssertionError("Unbuilt SSG page must return 404")
    request = Request(base + "/api/users", data=b'{"name":"Smoke"}',
                      headers={"content-type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        assert response.status == 201
        assert json.loads(response.read()) == {"name": "Smoke"}
    with urlopen(base + "/api/stream", timeout=10) as response:
        assert response.status == 200
        assert response.headers.get("content-type") == "application/x-ndjson"
        assert [json.loads(line) for line in response.read().splitlines()] == [
            {"phase": "started"}, {"phase": "finished"}
        ]
    request = Request(base + "/styles/site.css", headers={"Range": "bytes=0-3"})
    with urlopen(request, timeout=10) as response:
        assert response.status == 206 and response.headers.get("etag")
        assert response.headers.get("content-range", "").startswith("bytes 0-3/")
        assert len(response.read()) == 4


def _free_port():
    """Return a currently unbound loopback port.

    The port is only free at this instant: another process may take it before
    the server binds, so callers must retry on start failure instead of
    assuming the port is still free.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _terminate(server):
    if server.poll() is None:
        server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def _wait_ready(server, port, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        if server.poll() is not None:
            raise RuntimeError(f"PyNext server exited with status {server.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("PyNext server did not become ready")
            time.sleep(0.1)


def _start_server(argv_for_port, log, *, attempts=MAX_SERVER_START_ATTEMPTS, timeout=30,
                  popen=subprocess.Popen):
    """Start the server on a free port, retrying if it exits before ready.

    Picking a port by binding a throwaway socket and closing it leaves a
    window where another process can take the port; the server then fails to
    bind and exits before becoming ready. Handing the bound socket to the
    server is not possible here because `pynext start` only takes a port
    number, so retry on a fresh port instead. Only an early exit (the
    bind-failure symptom) is retried; a server that stays up but never
    answers still fails via TimeoutError, and other startup failures surface
    after the last attempt.
    """
    for attempt in range(1, attempts + 1):
        port = _free_port()
        server = popen(argv_for_port(port), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            _wait_ready(server, port, timeout=timeout)
            return server, port
        except RuntimeError as exc:
            _terminate(server)
            if attempt == attempts:
                raise RuntimeError(
                    f"PyNext server exited before becoming ready after {attempts} attempts; "
                    f"last error: {exc}"
                ) from exc
        except TimeoutError:
            _terminate(server)
            raise
    raise AssertionError("server start retry loop ended without a server")


def main():
    parser = argparse.ArgumentParser(description="Run browser smoke tests against a built example")
    parser.add_argument("--mode", choices=("axum", "uvicorn"), default="axum")
    mode = parser.parse_args().mode
    server_python = os.environ.get("PYNEXT_SERVER_PYTHON", sys.executable)

    def command(port):
        if mode == "uvicorn":
            return [server_python, "-m", "uvicorn", "--app-dir", str(ROOT / "tests/browser"),
                    "production_asgi:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
        return [server_python, "-m", "pynext.cli", "start", "--dir", str(ROOT / "examples/basic"),
                "--host", "127.0.0.1", "--port", str(port)]

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as log:
        server = None
        try:
            server, port = _start_server(
                command, log,
            )
            base = f"http://127.0.0.1:{port}"
            verify_http(base)
            for script in ("islands_smoke.py", "interaction_smoke.py", "navigation_smoke.py"):
                subprocess.run(
                    [sys.executable, str(ROOT / "tests/browser" / script), base],
                    cwd=ROOT, check=True, timeout=120,
                )
        finally:
            if server is not None:
                _terminate(server)
            log.seek(0)
            output = log.read()
            if output:
                print("PyNext server output:\n" + output, flush=True)


if __name__ == "__main__":
    main()
