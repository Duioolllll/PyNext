"""Port retry and subprocess cleanup checks for run_smoke.py."""
import socket
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_smoke


class _FakeServer:
    """Stand-in for subprocess.Popen with a scripted exit status."""

    def __init__(self, exit_code):
        self.returncode = exit_code
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_free_port_returns_a_bindable_port():
    port = run_smoke._free_port()
    with socket.socket() as listener:
        # Best effort: the port was free when picked; binding it again here
        # only proves the value came from a real ephemeral bind.
        listener.bind(("127.0.0.1", port))


def test_wait_ready_raises_when_server_exits_early():
    with pytest.raises(RuntimeError, match="exited with status 1"):
        run_smoke._wait_ready(_FakeServer(1), 12345)


def test_wait_ready_times_out_when_server_stays_up_but_never_answers(monkeypatch):
    monkeypatch.setattr(run_smoke, "_free_port", lambda: 12345)
    with pytest.raises(TimeoutError, match="did not become ready"):
        run_smoke._wait_ready(_FakeServer(None), 12345, timeout=0)


def test_start_server_retries_after_early_exit_then_succeeds(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    live_port = listener.getsockname()[1]
    try:
        exit_codes = iter([1, 1, None])
        created = []

        def fake_popen(argv, **kwargs):
            created.append(argv)
            return _FakeServer(next(exit_codes))

        ports = iter([11111, 22222, live_port])
        monkeypatch.setattr(run_smoke, "_free_port", lambda: next(ports))
        server, port = run_smoke._start_server(
            lambda chosen: ["pynext", "start", "--port", str(chosen)],
            log=None, popen=fake_popen,
        )
        assert port == live_port
        assert server.poll() is None
        assert len(created) == 3
        assert created[2][-1] == str(live_port)
    finally:
        listener.close()


def test_start_server_gives_up_after_repeated_early_exits(monkeypatch):
    created = []

    def fake_popen(argv, **kwargs):
        created.append(argv)
        return _FakeServer(1)

    monkeypatch.setattr(run_smoke, "_free_port", lambda: 12345)
    with pytest.raises(RuntimeError, match="after 5 attempts"):
        run_smoke._start_server(
            lambda chosen: ["pynext", "start", "--port", str(chosen)],
            log=None, popen=fake_popen,
        )
    assert len(created) == 5


def test_start_server_does_not_retry_a_server_that_never_becomes_ready(monkeypatch):
    created = []

    def fake_popen(argv, **kwargs):
        created.append(argv)
        return _FakeServer(None)

    monkeypatch.setattr(run_smoke, "_free_port", lambda: 12345)
    with pytest.raises(TimeoutError, match="did not become ready"):
        run_smoke._start_server(
            lambda chosen: ["pynext", "start", "--port", str(chosen)],
            log=None, timeout=0, popen=fake_popen,
        )
    assert len(created) == 1


def test_axum_smoke_cleanup_leaves_no_server_processes(tmp_path, monkeypatch):
    from pynext.build import write_manifest

    project = tmp_path / "examples" / "basic"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "route.py").write_text(
        'from pynext import Response, route\n@route("GET")\n'
        'async def get(): return Response("ready")\n', encoding="utf-8")
    write_manifest(project)
    monkeypatch.setattr(run_smoke, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_smoke.py", "--mode", "axum"])
    monkeypatch.delenv("PYNEXT_SERVER_PYTHON", raising=False)
    # Exercise main's real server lifecycle without running Playwright.
    monkeypatch.setattr(run_smoke.subprocess, "run", lambda *args, **kwargs: None)
    processes = []
    servers = []
    start = run_smoke._start_server

    def start_server(*args, **kwargs):
        server, port = start(*args, **kwargs)
        servers.append(server)
        processes.append(psutil.Process(server.pid))
        return server, port

    def verify_http(base):
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=base, trust_env=False, timeout=1) as client:
            while True:
                try:
                    response = client.get("/")
                    assert response.status_code == 200 and response.text == "ready"
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        processes.extend(processes[0].children(recursive=True))

    monkeypatch.setattr(run_smoke, "_start_server", start_server)
    monkeypatch.setattr(run_smoke, "verify_http", verify_http)
    try:
        run_smoke.main()
        assert servers[0].poll() is not None
        _, alive = psutil.wait_procs(processes, timeout=5)
        assert not alive, f"Smoke runner leaked server processes: {[proc.pid for proc in alive]}"
    finally:
        # Clean up even if the regression returns on Windows, where killing
        # a supervisor does not run its child shutdown path.
        for process in processes:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(processes, timeout=5)
