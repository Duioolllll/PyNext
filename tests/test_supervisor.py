import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"},
                                reason="these supervisor tests use Unix process signals")

ROOT = Path(__file__).resolve().parents[1]


def _write_app(tmp_path):
    ok = tmp_path / "app" / "api" / "ok"
    ok.mkdir(parents=True)
    (ok / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    crash = tmp_path / "app" / "api" / "crash"
    crash.mkdir(parents=True)
    (crash / "route.py").write_text(
        'import os\n'
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): os._exit(1)\n',
        encoding="utf-8",
    )


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _children(sup_pid):
    try:
        return [child.pid for child in psutil.Process(sup_pid).children()]
    except psutil.Error:
        return []


def _worker_pids(sup_pid):
    """Worker child PIDs (the spawn bootstrap), excluding the resource tracker."""
    workers = []
    for pid in _children(sup_pid):
        try:
            cmdline = psutil.Process(pid).cmdline()
        except psutil.Error:
            continue
        if any("spawn_main" in arg for arg in cmdline):
            workers.append(pid)
    return workers


def _is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Supervisor:
    def __init__(self, tmp_path, workers=2, port=None):
        _write_app(tmp_path)
        self.port = _free_port() if port is None else port
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["NO_PROXY"] = "localhost,127.0.0.1"
        env["no_proxy"] = "localhost,127.0.0.1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(tmp_path),
             "--host", "127.0.0.1", "--port", str(self.port), "--workers", str(workers)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        self.output = ""
        if self.port == 0:
            def read_port():
                for line in self.proc.stdout:
                    self.output += line
                    match = re.search(r"supervisor listening on http://127\.0\.0\.1:(\d+)", line)
                    if match:
                        self.port = int(match.group(1))
                        return
            reader = threading.Thread(target=read_port, daemon=True)
            reader.start()
            reader.join(timeout=30)
            if not self.port:
                self.stop()
                raise AssertionError(f"supervisor did not report its assigned port: {self.output}")
            self.base_url = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout=30):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.output = self.proc.stdout.read()
                raise AssertionError(f"supervisor exited early with {self.proc.returncode}:\n{self.output}")
            try:
                with httpx.Client(base_url=self.base_url, timeout=2) as client:
                    if client.get("/api/ok").status_code == 200:
                        return
            except httpx.TransportError as exc:
                last_error = exc
            time.sleep(0.2)
        raise AssertionError(f"server did not become ready: {last_error}")

    def wait_workers(self, count, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = _worker_pids(self.proc.pid)
            if len(pids) == count:
                return pids
            time.sleep(0.2)
        raise AssertionError(f"expected {count} workers, got {_worker_pids(self.proc.pid)}")

    def get_ok(self, timeout=5):
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            return client.get("/api/ok")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.output = self.proc.stdout.read()
        for pid in _children(self.proc.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture
def supervisor(tmp_path):
    sup = _Supervisor(tmp_path)
    try:
        sup.wait_ready()
        yield sup
    finally:
        sup.stop()


def test_killed_worker_restarts_and_server_keeps_serving(supervisor):
    workers = supervisor.wait_workers(2)
    victim = workers[0]
    os.kill(victim, signal.SIGKILL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = _worker_pids(supervisor.proc.pid)
        if len(current) == 2 and victim not in current:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"worker {victim} was not replaced: {current}")
    assert supervisor.proc.poll() is None
    assert supervisor.get_ok().status_code == 200


def test_crashing_worker_does_not_take_down_the_server(supervisor):
    supervisor.wait_workers(2)
    with httpx.Client(base_url=supervisor.base_url, timeout=5) as client:
        with pytest.raises(httpx.TransportError):
            client.get("/api/crash")
    # The other worker keeps serving while the crashed one restarts.
    deadline = time.monotonic() + 20
    while True:
        try:
            if supervisor.get_ok().status_code == 200:
                break
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, "server stopped serving after a worker crash"
        time.sleep(0.2)
    assert supervisor.proc.poll() is None
    supervisor.wait_workers(2)


def test_supervisor_graceful_shutdown_stops_all_workers(tmp_path):
    sup = _Supervisor(tmp_path)
    try:
        sup.wait_ready()
        workers = sup.wait_workers(2)
        sup.proc.send_signal(signal.SIGTERM)
        sup.proc.wait(timeout=20)
        assert sup.proc.returncode == 0
        for pid in workers:
            assert not _is_alive(pid), f"worker {pid} still alive after supervisor shutdown"
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", sup.port), timeout=1):
                    pass
            except OSError:
                break
            assert time.monotonic() < deadline, "port still accepting after shutdown"
            time.sleep(0.2)
    finally:
        sup.stop()


def test_dynamic_port_survives_all_workers_restarting(tmp_path):
    sup = _Supervisor(tmp_path, port=0)
    try:
        sup.wait_ready()
        original = sup.wait_workers(2)
        port = sup.port
        for pid in original:
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = _worker_pids(sup.proc.pid)
            if len(current) == 2 and not set(original) & set(current):
                try:
                    if sup.get_ok().status_code == 200:
                        break
                except httpx.TransportError:
                    pass
            time.sleep(0.2)
        else:
            raise AssertionError("workers did not recover on their assigned port")
        assert sup.port == port
    finally:
        sup.stop()


# --- CLI wiring for --workers ---

def test_cli_rejects_invalid_workers(monkeypatch, capsys):
    from pynext import cli

    for argv, message in [
        (["pynext", "start", "--workers", "0"], "--workers must be an integer >= 1"),
        (["pynext", "start", "--workers", "not-a-number"], "--workers must be an integer in [1, 64]"),
    ]:
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        assert message in capsys.readouterr().err


def test_cli_routes_multiple_workers_to_supervisor(tmp_path, monkeypatch):
    from pynext import cli

    root = tmp_path.resolve()
    seen = []
    monkeypatch.setattr(cli, "run_supervisor",
                        lambda config, workers: seen.append((config, workers)) or 0)
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root), "--port", "3101",
                                      "--workers", "3", "--rate-limit", "50"])
    assert cli.main() == 0
    assert len(seen) == 1
    config, workers = seen[0]
    assert workers == 3
    assert config.root == str(root)
    assert (config.host, config.port, config.production) == ("127.0.0.1", 3101, True)
    assert config.rate_limit == 50
