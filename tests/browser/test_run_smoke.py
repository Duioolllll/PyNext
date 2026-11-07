"""Unit tests for the port-retry logic in run_smoke.py (no real server)."""
import socket
import sys
from pathlib import Path

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
