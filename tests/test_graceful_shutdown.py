"""B6: health probes, graceful drain and rolling restart.

Completion criteria (roadmap B6): SIGTERM stops new connections, in-flight
requests finish, WebSockets get a clean close inside the shutdown timeout,
and ``/healthz`` / ``/readyz`` report liveness and readiness. A rolling
restart under load must serve no 5xx.
"""
import asyncio
import base64
import hashlib
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
import pytest

from pynext import JSONResponse, Request, Response, create_app
from pynext.build import write_manifest
from pynext.ratelimit import RateLimiter
from pynext.server import serve, start_native

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="these tests use Unix process signals")

ROOT = Path(__file__).resolve().parents[1]
WS_ECHO = (
    'from pynext import WebSocket, WebSocketDisconnect, websocket\n'
    '@websocket\n'
    'async def echo(websocket: WebSocket):\n'
    '    await websocket.accept()\n'
    '    try:\n'
    '        while True:\n'
    '            kind, data = await websocket.receive()\n'
    '            if kind == "text":\n'
    '                await websocket.send_text(data)\n'
    '            else:\n'
    '                await websocket.send_bytes(data)\n'
    '    except WebSocketDisconnect:\n'
    '        pass\n'
)


def _write_api_app(tmp_path, *, slow=False, ws=False, health_route=False):
    ok = tmp_path / "app" / "api" / "ok"
    ok.mkdir(parents=True)
    (ok / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    if slow:
        route = tmp_path / "app" / "api" / "slow"
        route.mkdir(parents=True)
        (route / "route.py").write_text(
            'import asyncio\n'
            'from pathlib import Path\n'
            'from pynext import JSONResponse, route\n'
            '@route("GET")\n'
            'async def get():\n'
            '    Path(__file__).with_name("entered").write_text("yes", encoding="utf-8")\n'
            '    await asyncio.sleep(3)\n'
            '    return JSONResponse({"slow": True})\n',
            encoding="utf-8",
        )
    if ws:
        route = tmp_path / "app" / "ws" / "echo"
        route.mkdir(parents=True)
        (route / "route.py").write_text(WS_ECHO, encoding="utf-8")
    if health_route:
        route = tmp_path / "app" / "healthz"
        route.mkdir(parents=True)
        (route / "route.py").write_text(
            'from pynext import Response, route\n'
            '@route("GET")\ndef get(): return Response("user health page")\n',
            encoding="utf-8",
        )


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(app, method, path):
    return Request(method, path, b"", (), b"", _app=app)


# --- Dispatch-level health probes ---------------------------------------

def test_health_endpoints_at_dispatch(tmp_path):
    _write_api_app(tmp_path, health_route=True)

    async def run():
        async def boom(_request, call_next):
            raise RuntimeError("middleware is broken")

        app = create_app(tmp_path, middleware=[boom])
        try:
            # Before startup: liveness answers, readiness does not.
            response = await app.dispatch(_request(app, "GET", "/healthz"))
            assert response.status == 200 and response.body == b"ok"
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 503 and response.body == b"not ready"
            await app.startup()
            assert app.is_ready
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 200 and response.body == b"ready"
            response = await app.dispatch(_request(app, "HEAD", "/healthz"))
            assert response.status == 200 and response.body == b""
            response = await app.dispatch(_request(app, "POST", "/healthz"))
            assert response.status == 405
            # The framework probe wins over a user route at the same path,
            # and over broken middleware: a load balancer must be able to
            # probe a half-broken app.
            response = await app.dispatch(_request(app, "GET", "/healthz"))
            assert response.body == b"ok"
            # Draining flips readiness off while liveness stays on.
            app.begin_drain()
            assert not app.is_ready
            response = await app.dispatch(_request(app, "GET", "/readyz"))
            assert response.status == 503
            response = await app.dispatch(_request(app, "GET", "/healthz"))
            assert response.status == 200
            # ... and normal traffic still goes through middleware.
            with pytest.raises(RuntimeError):
                await app.dispatch(_request(app, "GET", "/api/ok"))
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_wait_for_inflight_timeout(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        try:
            with app._inflight_lock:
                app._inflight = 1
            assert await app.wait_for_inflight(0.2) is False
            with app._inflight_lock:
                app._inflight = 0
            assert await app.wait_for_inflight(1.0) is True
        finally:
            await app.isr.close()

    asyncio.run(run())


# --- Native server: probes, rate-limit bypass, drain --------------------

def test_native_health_endpoints_bypass_rate_limit(tmp_path):
    _write_api_app(tmp_path)

    async def run():
        app = create_app(tmp_path, rate_limit=RateLimiter(1, 60.0))
        await app.startup()
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(trust_env=False, base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.get("/healthz")).text == "ok"
                assert (await client.get("/readyz")).text == "ready"
                assert (await client.get("/api/ok")).status_code == 200
                assert (await client.get("/api/ok")).status_code == 429
                # A saturated limiter must never make the process look dead.
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/readyz")).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()

    asyncio.run(run())


class _RawWSClient:
    """Minimal WebSocket client over a plain socket (RFC 6455)."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, address, path):
        host, port = address.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=5)
        self.sock.settimeout(5)
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {address}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {self.key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("handshake closed early")
            response += chunk
        head, _, self._pending = response.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head

    def send(self, opcode, payload):
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self.send(1, text.encode("utf-8"))

    def _read_exact(self, count):
        data, self._pending = self._pending[:count], self._pending[count:]
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise AssertionError("connection closed while reading a frame")
            data += chunk
        return data

    def recv(self):
        """Return (opcode, payload) for one complete message."""
        while True:
            first, second = self._read_exact(2)
            fin = first & 0x80
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if second & 0x80:
                mask = self._read_exact(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
            else:
                payload = self._read_exact(length)
            if opcode == 9:  # ping: answer with pong
                self.send(10, payload)
                continue
            if fin:
                return opcode, payload

    def shutdown(self):
        try:
            self.sock.close()
        except OSError:
            pass


def test_serve_drains_inflight_request_and_closes_websocket(tmp_path):
    _write_api_app(tmp_path, slow=True, ws=True)
    entered_file = tmp_path / "app" / "api" / "slow" / "entered"

    async def run():
        app = create_app(tmp_path)
        stop_event = asyncio.Event()
        address = {}

        async def serve_task():
            await serve(app, "127.0.0.1", 0, stop_event=stop_event, shutdown_timeout=10.0,
                        on_ready=lambda value: address.__setitem__("value", value))

        task = asyncio.create_task(serve_task())
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while "value" not in address:
                assert asyncio.get_running_loop().time() < deadline, "server did not start"
                await asyncio.sleep(0.05)
            base_url = f"http://{address['value']}"
            async with httpx.AsyncClient(trust_env=False, base_url=base_url, timeout=10) as client:
                assert (await client.get("/readyz")).status_code == 200
                request = asyncio.create_task(client.get("/api/slow"))
                deadline = asyncio.get_running_loop().time() + 5
                while not entered_file.exists():
                    assert asyncio.get_running_loop().time() < deadline, "slow handler did not start"
                    await asyncio.sleep(0.05)
                ws_client = await asyncio.to_thread(_RawWSClient, address["value"], "/ws/echo")
                try:
                    await asyncio.to_thread(ws_client.send_text, "hello")
                    opcode, payload = await asyncio.to_thread(ws_client.recv)
                    assert (opcode, payload) == (1, b"hello")
                    # Drain starts: readiness flips off while the slow
                    # request is still running.
                    stop_event.set()
                    await asyncio.sleep(0.2)
                    assert (await client.get("/readyz")).status_code == 503
                    assert (await client.get("/healthz")).status_code == 200
                    # The WebSocket gets a clean 1001 close.
                    opcode, payload = await asyncio.to_thread(ws_client.recv)
                    assert opcode == 8
                    assert struct.unpack("!H", payload[:2])[0] == 1001
                finally:
                    ws_client.shutdown()
                assert (await asyncio.wait_for(request, 10)).json() == {"slow": True}
            await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)
        # The port stops accepting once the server is gone.
        with pytest.raises(OSError):
            socket.create_connection((address["value"].rsplit(":", 1)[0],
                                      int(address["value"].rsplit(":", 1)[1])), timeout=2)

    asyncio.run(run())


def test_serve_shutdown_timeout_abandons_slow_request(tmp_path):
    _write_api_app(tmp_path, slow=True)
    entered_file = tmp_path / "app" / "api" / "slow" / "entered"

    async def run():
        app = create_app(tmp_path)
        stop_event = asyncio.Event()
        address = {}
        task = asyncio.create_task(
            serve(app, "127.0.0.1", 0, stop_event=stop_event, shutdown_timeout=1.0,
                  on_ready=lambda value: address.__setitem__("value", value)))
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while "value" not in address:
                assert asyncio.get_running_loop().time() < deadline, "server did not start"
                await asyncio.sleep(0.05)
            started = asyncio.get_running_loop().time()
            async with httpx.AsyncClient(trust_env=False, base_url=f"http://{address['value']}", timeout=10) as client:
                request = asyncio.create_task(client.get("/api/slow"))
                deadline = asyncio.get_running_loop().time() + 5
                while not entered_file.exists():
                    assert asyncio.get_running_loop().time() < deadline, "slow handler did not start"
                    await asyncio.sleep(0.05)
                stop_event.set()
                # Bounded: serve() returns even though the handler never
                # finishes, instead of hanging on the drain.
                await asyncio.wait_for(task, 15)
                assert asyncio.get_running_loop().time() - started < 12
                with pytest.raises(Exception):
                    await asyncio.wait_for(request, 5)
        finally:
            if not task.done():
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


# --- Single process: SIGTERM --------------------------------------------

def _cli_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    return env


def test_cli_start_sigterm_drains_slow_request(tmp_path):
    _write_api_app(tmp_path, slow=True)
    write_manifest(tmp_path)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    entered = tmp_path / "app" / "api" / "slow" / "entered"
    proc = subprocess.Popen(
        [sys.executable, "-m", "pynext.cli", "start", "--dir", str(tmp_path),
         "--host", "127.0.0.1", "--port", str(port), "--shutdown-timeout", "10"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_cli_env(),
    )
    try:
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False, base_url=base_url, timeout=2) as client:
            while True:
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "server did not become ready"
                assert proc.poll() is None, f"server exited early: {proc.stdout.read()}"
                time.sleep(0.2)
        result = {}

        def fetch_slow():
            try:
                with httpx.Client(trust_env=False, base_url=base_url, timeout=10) as client:
                    result["slow"] = client.get("/api/slow")
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                result["slow_error"] = exc

        thread = threading.Thread(target=fetch_slow, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not entered.exists():
            assert time.monotonic() < deadline, "slow handler did not start"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        thread.join(timeout=10)
        assert proc.wait(timeout=20) == 0, f"server exit code: {proc.returncode}"
        assert "slow_error" not in result, f"slow request failed: {result.get('slow_error')}"
        assert result["slow"].status_code == 200
        assert result["slow"].json() == {"slow": True}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdout.read()


# --- Supervisor: rolling restart under load ------------------------------

class _Supervisor:
    def __init__(self, tmp_path, workers=2):
        _write_api_app(tmp_path)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(tmp_path),
             "--host", "127.0.0.1", "--port", str(self.port), "--workers", str(workers)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_cli_env(),
        )
        self.output = ""

    def wait_ready(self, timeout=30):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.output = self.proc.stdout.read()
                raise AssertionError(f"supervisor exited early with {self.proc.returncode}:\n{self.output}")
            try:
                with httpx.Client(trust_env=False, base_url=self.base_url, timeout=2) as client:
                    if client.get("/readyz").status_code == 200:
                        return
            except httpx.TransportError as exc:
                last_error = exc
            time.sleep(0.2)
        raise AssertionError(f"server did not become ready: {last_error}")

    def worker_pids(self):
        workers = []
        try:
            children = psutil.Process(self.proc.pid).children()
        except psutil.Error:
            return workers
        for child in children:
            try:
                if any("spawn_main" in arg for arg in child.cmdline()):
                    workers.append(child.pid)
            except psutil.Error:
                continue
        return workers

    def wait_workers(self, count, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = self.worker_pids()
            if len(pids) == count:
                return pids
            time.sleep(0.2)
        raise AssertionError(f"expected {count} workers, got {self.worker_pids()}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.output = self.proc.stdout.read()


def test_supervisor_rolling_restart_serves_no_5xx(tmp_path):
    sup = _Supervisor(tmp_path)
    try:
        sup.wait_ready()
        original = sup.wait_workers(2)
        results, errors = [], []
        stop_load = threading.Event()

        def load():
            while not stop_load.is_set():
                try:
                    with httpx.Client(trust_env=False, base_url=sup.base_url, timeout=5) as client:
                        response = client.get("/api/ok")
                    results.append(response.status_code)
                except httpx.TransportError as exc:
                    errors.append(repr(exc))
                time.sleep(0.05)

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
        time.sleep(2)
        sup.proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 60
        replaced = False
        while time.monotonic() < deadline:
            current = sup.worker_pids()
            if len(current) == 2 and not set(original) & set(current):
                replaced = True
                break
            time.sleep(0.2)
        assert replaced, f"workers were not replaced: original={original} current={sup.worker_pids()}"
        time.sleep(2)
        stop_load.set()
        thread.join(timeout=10)
        assert sup.proc.poll() is None, "supervisor died during the rolling restart"
        with httpx.Client(trust_env=False, base_url=sup.base_url, timeout=5) as client:
            assert client.get("/readyz").status_code == 200
            assert client.get("/api/ok").status_code == 200
        assert results, "load thread recorded no requests"
        assert not any(status >= 500 for status in results), f"5xx during rolling restart: {results}"
        assert not errors, f"transport errors during rolling restart: {errors[:5]}"
    finally:
        sup.stop()
