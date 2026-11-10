"""Finding #23: the CSRF signing key must be shared across workers.

Before the fix, ``_FALLBACK_CSRF_SIGNING_KEY`` was created at import
time, so every spawn worker had its own key and a double-submit cookie
issued by worker A failed HMAC verification on worker B (intermittent
403). The supervisor now resolves one shared key before spawning (see
``ensure_shared_csrf_signing_key``) and the CLI accepts
``--csrf-signing-key``; an invalid key fails at startup instead of
silently falling back to a random key.
"""

import base64
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

from pynext.http import (
    Request,
    SecurityConfig,
    ensure_shared_csrf_signing_key,
    parse_csrf_signing_key,
    sign_csrf_token,
    validate_csrf,
)

ROOT = Path(__file__).resolve().parents[1]


# --- Key parsing: valid forms -------------------------------------------


def test_parse_csrf_signing_key_accepts_raw_hex_base64_and_bytes():
    raw = b"a-long-enough-raw-key"
    assert parse_csrf_signing_key("a-long-enough-raw-key") == raw
    assert parse_csrf_signing_key(raw) == raw
    assert parse_csrf_signing_key(bytearray(raw)) == raw
    assert parse_csrf_signing_key("hex:" + raw.hex()) == raw
    assert parse_csrf_signing_key("base64:" + base64.b64encode(raw).decode()) == raw
    # Surrounding whitespace (e.g. a trailing newline from a secrets file)
    # is ignored, and a 16-byte key is the minimum accepted length.
    assert parse_csrf_signing_key("  a-long-enough-raw-key\n") == raw
    assert parse_csrf_signing_key("x" * 16) == b"x" * 16


@pytest.mark.parametrize("value", [
    "short",                    # raw string under 16 bytes
    "",                         # empty
    "   ",                      # blank
    "hex:zz",                   # not hex
    "hex:abcd",                 # hex decoding to 2 bytes
    "hex:",                     # hex decoding to 0 bytes
    "base64:!!!",               # not base64
    "base64:aGk=",              # base64 decoding to 2 bytes ("hi")
    "x" * 15,                   # one byte short
    b"short",
    b"",
])
def test_parse_csrf_signing_key_rejects_invalid_keys(value):
    with pytest.raises(ValueError, match="at least 16 bytes|must not be empty|not valid"):
        parse_csrf_signing_key(value)


def test_parse_csrf_signing_key_rejects_wrong_type():
    with pytest.raises(TypeError, match="must be str, bytes or None"):
        parse_csrf_signing_key(123)
    with pytest.raises(TypeError, match="must be str, bytes or None"):
        parse_csrf_signing_key(["a-long-enough-list-key"])


def test_parse_csrf_signing_key_names_the_source_in_errors():
    with pytest.raises(ValueError, match="PYNEXT_CSRF_SIGNING_KEY"):
        parse_csrf_signing_key("short", source="PYNEXT_CSRF_SIGNING_KEY")
    with pytest.raises(ValueError, match="--csrf-signing-key"):
        parse_csrf_signing_key("hex:zz", source="--csrf-signing-key")


# --- SecurityConfig: env / explicit keys ---------------------------------


@pytest.mark.parametrize("env_value", ["short", "", "hex:zz", "base64:!!!"])
def test_security_config_rejects_invalid_env_key(monkeypatch, env_value):
    # An invalid PYNEXT_CSRF_SIGNING_KEY must fail loudly, never fall
    # back to a per-process random key that other workers cannot share.
    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", env_value)
    with pytest.raises(ValueError):
        SecurityConfig()


def test_security_config_accepts_valid_env_key(monkeypatch):
    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", "a-long-enough-env-key")
    assert SecurityConfig().csrf_signing_key == b"a-long-enough-env-key"
    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", "hex:" + b"a-long-enough-env-key".hex())
    assert SecurityConfig().csrf_signing_key == b"a-long-enough-env-key"


def test_security_config_explicit_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", "a-long-enough-env-key")
    assert SecurityConfig(csrf_signing_key="explicit-key-long-enough").csrf_signing_key == (
        b"explicit-key-long-enough")


def test_security_config_rejects_short_explicit_key(monkeypatch):
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    with pytest.raises(ValueError, match="at least 16 bytes"):
        SecurityConfig(csrf_signing_key="short")


def test_single_process_fallback_key_is_shared_within_one_process(monkeypatch):
    # Single-worker/dev behaviour is unchanged: in one process every
    # default config shares the process-wide fallback key.
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    assert SecurityConfig().csrf_signing_key == SecurityConfig().csrf_signing_key


# --- ensure_shared_csrf_signing_key ---------------------------------------


def test_ensure_shared_key_generates_and_publishes_hex_env(monkeypatch):
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    key = ensure_shared_csrf_signing_key()
    assert len(key) == 32
    assert os.environ["PYNEXT_CSRF_SIGNING_KEY"] == "hex:" + key.hex()
    # Idempotent: a second call reuses the published env key.
    assert ensure_shared_csrf_signing_key() == key


def test_ensure_shared_key_reuses_env_and_explicit_keys(monkeypatch):
    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", "a-long-enough-env-key")
    assert ensure_shared_csrf_signing_key() == b"a-long-enough-env-key"
    assert ensure_shared_csrf_signing_key(b"explicit-key-long-enough") == (
        b"explicit-key-long-enough")
    # The explicit key is published too, so spawn workers inherit it.
    assert os.environ["PYNEXT_CSRF_SIGNING_KEY"] == (
        "hex:" + b"explicit-key-long-enough".hex())


def test_ensure_shared_key_rejects_invalid_env_without_fallback(monkeypatch):
    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", "short")
    with pytest.raises(ValueError, match="PYNEXT_CSRF_SIGNING_KEY"):
        ensure_shared_csrf_signing_key()
    # Nothing was silently generated in place of the bad value.
    assert os.environ["PYNEXT_CSRF_SIGNING_KEY"] == "short"


# --- Cross-process behaviour (the bug, at the signing layer) --------------


def _spawn_worker_key_and_signature(env):
    code = (
        "from pynext.http import SecurityConfig, sign_csrf_token;"
        "cfg = SecurityConfig();"
        "print(cfg.csrf_signing_key.hex());"
        "print(sign_csrf_token('test-token-123', config=cfg))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    key_hex, signed = result.stdout.strip().splitlines()
    return key_hex, signed


def test_fallback_keys_differ_across_processes():
    # The root cause, still true at import level by design: two fresh
    # processes without a shared key get different fallback keys, and a
    # cookie signed by one does not validate under the other.
    env = dict(os.environ)
    env.pop("PYNEXT_CSRF_SIGNING_KEY", None)
    key_a, signed_a = _spawn_worker_key_and_signature(env)
    key_b, _signed_b = _spawn_worker_key_and_signature(env)
    assert key_a != key_b
    cfg_b = SecurityConfig(csrf_signing_key=bytes.fromhex(key_b))
    headers = ((b"cookie", f"pynext_csrf={signed_a}".encode()),
               (b"x-csrf-token", b"test-token-123"))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers), config=cfg_b)


def test_shared_env_key_agrees_across_processes():
    env = dict(os.environ)
    env["PYNEXT_CSRF_SIGNING_KEY"] = "a-shared-signing-key"
    key_a, signed_a = _spawn_worker_key_and_signature(env)
    key_b, signed_b = _spawn_worker_key_and_signature(env)
    assert key_a == key_b
    # The signature now embeds its issue time (S-P1-21), so two workers
    # signing seconds apart produce different values; what must agree is
    # the key and cross-process validation, not the bytes.
    assert signed_a.split(".")[0] == signed_b.split(".")[0] == "test-token-123"
    assert signed_a.split(".")[1].isdigit() and signed_b.split(".")[1].isdigit()
    cfg_b = SecurityConfig(csrf_signing_key=bytes.fromhex(key_b))
    headers = ((b"cookie", f"pynext_csrf={signed_a}".encode()),
               (b"x-csrf-token", b"test-token-123"))
    assert validate_csrf(Request("POST", "/api/echo", headers=headers), config=cfg_b)


# --- Supervisor: fail fast on an invalid key ------------------------------


def test_supervisor_rejects_invalid_csrf_key_before_spawning(tmp_path, monkeypatch):
    from pynext.supervisor import WorkerConfig, run_supervisor

    monkeypatch.setenv("PYNEXT_CSRF_SIGNING_KEY", "short")
    config = WorkerConfig(root=str(tmp_path), host="127.0.0.1", port=0,
                          production=False, max_concurrent=64,
                          acquire_timeout_ms=1000, body_capacity=16,
                          body_timeout_ms=10000)
    with pytest.raises(ValueError, match="PYNEXT_CSRF_SIGNING_KEY"):
        run_supervisor(config, workers=2)


def test_supervisor_shares_configured_key_with_workers(monkeypatch):
    # Unit-level check of the sharing path: a configured SecurityConfig
    # key is adopted and published to the environment. (The live test
    # below proves workers actually agree over HTTP.)
    from pynext.supervisor import WorkerConfig

    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    config = WorkerConfig(root=".", host="127.0.0.1", port=0, production=False,
                          max_concurrent=64, acquire_timeout_ms=1000,
                          body_capacity=16, body_timeout_ms=10000,
                          security=SecurityConfig(csrf_signing_key="explicit-key-long-enough"))
    # Replicate run_supervisor's key resolution without spawning.
    key = ensure_shared_csrf_signing_key(config.security.csrf_signing_key)
    assert key == b"explicit-key-long-enough"
    assert os.environ["PYNEXT_CSRF_SIGNING_KEY"] == "hex:" + key.hex()


# --- CLI: --csrf-signing-key ------------------------------------------------


def test_cli_csrf_signing_key_reaches_supervisor_config(tmp_path, monkeypatch):
    from pynext import cli

    seen = []
    monkeypatch.setattr(cli, "run_supervisor",
                        lambda config, workers: seen.append((config, workers)) or 0)
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--workers", "2",
                                      "--csrf-signing-key", "a-long-enough-cli-key"])
    assert cli.main() == 0
    assert len(seen) == 1
    config, workers = seen[0]
    assert workers == 2
    assert config.security.csrf_signing_key == b"a-long-enough-cli-key"


def test_cli_csrf_signing_key_hex_form_reaches_app(tmp_path, monkeypatch):
    from pynext import cli

    seen = {}
    monkeypatch.setattr(cli, "create_app",
                        lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs: seen.update(kwargs) or object())

    async def serve(*args, **kwargs):
        pass

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--workers", "1",
                                      "--csrf-signing-key",
                                      "hex:" + b"a-long-enough-cli-key".hex()])
    cli.main()
    assert seen["security"].csrf_signing_key == b"a-long-enough-cli-key"


def test_cli_rejects_invalid_csrf_signing_key(tmp_path, monkeypatch, capsys):
    from pynext import cli

    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    for value in ("short", "hex:zz"):
        monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                          "--csrf-signing-key", value])
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        assert "csrf_signing_key" in capsys.readouterr().err


# --- Live multi-worker regression test --------------------------------------

pytestmark_live = pytest.mark.skipif(sys.platform not in {"linux", "darwin"},
                                    reason="supervisor live tests use Unix process signals")


def _write_csrf_app(tmp_path):
    token = tmp_path / "app" / "api" / "token"
    token.mkdir(parents=True)
    (token / "route.py").write_text(
        "from pynext import JSONResponse, new_csrf_token, route, set_csrf_cookie\n"
        "@route(\"GET\")\n"
        "async def get_token(request):\n"
        "    token = new_csrf_token()\n"
        "    return set_csrf_cookie(JSONResponse({\"token\": token}), token,\n"
        "                           config=request.security)\n",
        encoding="utf-8",
    )
    echo = tmp_path / "app" / "api" / "echo"
    echo.mkdir(parents=True)
    (echo / "route.py").write_text(
        "from pynext import JSONResponse, csrf_protect, route\n"
        "@route(\"POST\")\n"
        "@csrf_protect\n"
        "async def post_echo(request):\n"
        "    return JSONResponse({\"ok\": True}, status=201)\n",
        encoding="utf-8",
    )


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _children(pid):
    try:
        return [child.pid for child in psutil.Process(pid).children()]
    except psutil.Error:
        return []


def _worker_pids(sup_pid):
    workers = []
    for pid in _children(sup_pid):
        try:
            cmdline = psutil.Process(pid).cmdline()
        except psutil.Error:
            continue
        if any("spawn_main" in arg for arg in cmdline):
            workers.append(pid)
    return workers


class _LiveSupervisor:
    """Two-worker supervisor serving the CSRF app above, no shared key
    configured anywhere: the supervisor must generate and share one."""

    def __init__(self, tmp_path, extra_argv=(), extra_env=None):
        _write_csrf_app(tmp_path)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env.pop("PYNEXT_CSRF_SIGNING_KEY", None)
        if extra_env:
            env.update(extra_env)
        env["PYTHONPATH"] = str(ROOT / "python") + os.pathsep + env.get("PYTHONPATH", "")
        env["NO_PROXY"] = "localhost,127.0.0.1"
        env["no_proxy"] = "localhost,127.0.0.1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(tmp_path),
             "--host", "127.0.0.1", "--port", str(self.port), "--workers", "2",
             *extra_argv],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
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
                with httpx.Client(base_url=self.base_url, timeout=2) as client:
                    if client.get("/api/token").status_code == 200:
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

    def issue_token(self):
        with httpx.Client(base_url=self.base_url, timeout=5) as client:
            issued = client.get("/api/token")
            assert issued.status_code == 200
            token = issued.json()["token"]
            cookie = issued.headers["set-cookie"].split(";")[0]
            assert cookie.startswith(f"pynext_csrf={token}.")
            return cookie, token

    def post_echo(self, cookie, token):
        # Fresh client per request: no cookie jar, the cookie is sent
        # exactly as issued, and the request may land on either worker.
        with httpx.Client(base_url=self.base_url, timeout=5) as client:
            return client.post("/api/echo", json={},
                               headers={"cookie": cookie, "x-csrf-token": token})

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


@pytestmark_live
def test_csrf_cookie_issued_by_one_worker_validates_on_the_other(tmp_path):
    """Regression test for finding #23, end to end.

    No key is configured anywhere, so the supervisor must generate one
    and share it. A cookie is issued, then posted repeatedly (requests
    may land on either worker), then both workers are killed and
    replaced — the cookie issued before the restart must still validate
    on the replacements, which never held any issuing worker's key.
    Before the fix the repeated posts failed intermittently with 403
    and the post-restart post failed deterministically.
    """
    sup = _LiveSupervisor(tmp_path)
    try:
        sup.wait_ready()
        sup.wait_workers(2)
        cookie, token = sup.issue_token()
        for _ in range(30):
            assert sup.post_echo(cookie, token).status_code == 201
        # Replace every worker, then the old cookie must still validate.
        original = sup.wait_workers(2)
        for pid in original:
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = _worker_pids(sup.proc.pid)
            if len(current) == 2 and not set(original) & set(current):
                try:
                    if sup.post_echo(cookie, token).status_code == 201:
                        break
                except httpx.TransportError:
                    pass
            time.sleep(0.2)
        else:
            raise AssertionError("cookie did not validate on replacement workers")
        assert sup.post_echo(cookie, token).status_code == 201
        # Rolling restart (SIGHUP) replaces workers one at a time; the
        # replacements reuse the supervisor's shared key, so the cookie
        # issued before the roll must still validate afterwards.
        rolled_from = sup.wait_workers(2)
        sup.proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = _worker_pids(sup.proc.pid)
            if len(current) == 2 and not set(rolled_from) & set(current):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("workers were not replaced by the rolling restart")
        assert sup.post_echo(cookie, token).status_code == 201
    finally:
        sup.stop()


@pytestmark_live
def test_csrf_cookie_validates_across_workers_with_env_key(tmp_path):
    """Same cross-worker check with the key supplied via the environment."""
    sup = _LiveSupervisor(tmp_path, extra_env={
        "PYNEXT_CSRF_SIGNING_KEY": "a-long-enough-env-key"})
    try:
        sup.wait_ready()
        sup.wait_workers(2)
        cookie, token = sup.issue_token()
        for _ in range(20):
            assert sup.post_echo(cookie, token).status_code == 201
    finally:
        sup.stop()


@pytestmark_live
def test_csrf_cookie_validates_across_workers_with_cli_flag(tmp_path):
    """Same cross-worker check with an explicit --csrf-signing-key."""
    sup = _LiveSupervisor(tmp_path, extra_argv=(
        "--csrf-signing-key", "a-long-enough-cli-key", "--csrf"))
    try:
        sup.wait_ready()
        sup.wait_workers(2)
        cookie, token = sup.issue_token()
        for _ in range(20):
            assert sup.post_echo(cookie, token).status_code == 201
    finally:
        sup.stop()
