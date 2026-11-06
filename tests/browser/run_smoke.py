"""Start the built example and run the Playwright smoke test."""
import os
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


def verify_http(base):
    pages = {
        "/": "Hello PyNext!",
        "/about": "About",
        "/guides/intro": "intro",
        "/guides/routing": "routing",
        "/news/example": "example",
        "/hydrate": "Hydrated",
        "/strategies": "Island loading strategies",
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


def main():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as log:
        server = subprocess.Popen(
            [os.environ.get("PYNEXT_SERVER_PYTHON", sys.executable), "-m", "pynext.cli", "start", "--dir", str(ROOT / "examples/basic"),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"PyNext server exited with status {server.returncode}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("PyNext server did not become ready")
                    time.sleep(0.1)
            base = f"http://127.0.0.1:{port}"
            verify_http(base)
            subprocess.run(
                [sys.executable, str(ROOT / "tests/browser/islands_smoke.py"), base],
                cwd=ROOT, check=True, timeout=120,
            )
        finally:
            if server.poll() is None:
                server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
            log.seek(0)
            output = log.read()
            if output:
                print("PyNext server output:\n" + output, flush=True)


if __name__ == "__main__":
    main()
