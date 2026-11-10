"""``pynext.config.py`` loader, schema validation and DEFAULT_CONFIG (Phase 0).

Covers the track-E P0-1 items: the config file is actually loaded, the
defaults live in one place, and validation fails startup instead of
being silently ignored.
"""
import asyncio
import inspect
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from pynext import cli
from pynext.app import create_app
from pynext.config import DEFAULT_CONFIG, ConfigError, find_config_file, load_config
from pynext.server import serve
from pynext.supervisor import WorkerConfig

PAGE = (
    "from pynext import h, page\n"
    "@page()\n"
    "async def Page():\n"
    "    return h(\"main\", {}, h(\"p\", {}, \"version-one\"))\n"
)


def write_config(root, text):
    (Path(root) / "pynext.config.py").write_text(text, encoding="utf-8")


# -- single default source (§3.4) -------------------------------------------


def test_defaults_are_single_sourced():
    # Every entry point that used to repeat a literal default now reads
    # DEFAULT_CONFIG: the CLI parser, serve(), create_app() and WorkerConfig.
    args = cli._build_parser().parse_args(["start"])
    for key in ("host", "port", "max_concurrent", "acquire_timeout_ms",
                "body_capacity", "body_timeout_ms", "cache_backend",
                "cache_failure_policy", "workers", "rate_limit",
                "rate_limit_window", "rate_limit_ipv6_prefix_len",
                "rate_limit_backend", "rate_limit_failure_policy",
                "shutdown_timeout"):
        assert getattr(args, key) == getattr(DEFAULT_CONFIG, key), key
    assert args.ws_max_message_bytes == DEFAULT_CONFIG.websocket_max_message
    assert args.max_waiting is None and args.max_connections is None

    params = inspect.signature(serve).parameters
    for key in ("host", "port", "max_concurrent", "acquire_timeout_ms",
                "body_capacity", "body_timeout_ms", "shutdown_timeout"):
        assert params[key].default == getattr(DEFAULT_CONFIG, key), key
    for key in ("max_connections", "max_connections_per_ip",
                "header_read_timeout_ms", "body_min_rate_bytes_per_sec"):
        assert params[key].default == getattr(DEFAULT_CONFIG, key), key

    params = inspect.signature(create_app).parameters
    assert params["cache_backend"].default == DEFAULT_CONFIG.cache_backend
    assert params["websocket_max_message"].default == DEFAULT_CONFIG.websocket_max_message

    worker = WorkerConfig(root=".", host="127.0.0.1", port=0, production=True,
                          max_concurrent=64, acquire_timeout_ms=1000,
                          body_capacity=16, body_timeout_ms=10000)
    for key in ("max_connections", "max_connections_per_ip",
                "header_read_timeout_ms", "body_min_rate_bytes_per_sec",
                "rate_limit", "rate_limit_window", "rate_limit_backend",
                "rate_limit_ipv6_prefix_len", "cache_backend",
                "cache_failure_policy", "rate_limit_failure_policy",
                "websocket_max_message", "shutdown_timeout"):
        assert getattr(worker, key) == getattr(DEFAULT_CONFIG, key), key


def test_concurrency_p0_defaults():
    """C-P0-1/C-P0-2: multi-worker default with a raised admission budget.

    The defaults are sized to be used together: two workers share one
    accept queue, so max_concurrent/acquire_timeout_ms cover both.
    """
    assert DEFAULT_CONFIG.workers == 2
    assert DEFAULT_CONFIG.max_concurrent == 128
    assert DEFAULT_CONFIG.acquire_timeout_ms == 3000


def test_load_config_default_workers_do_not_warn(tmp_path):
    """The multi-worker default alone must not warn on every load.

    The per-process memory-cache warning only fires when workers > 1 was
    set explicitly (file/env/cli), not when it comes from the default.
    """
    resolved = load_config(tmp_path, "dev", env={})
    assert resolved.values.workers == 2
    assert resolved.values.max_concurrent == 128
    assert resolved.values.acquire_timeout_ms == 3000
    assert resolved.warnings == ()


def test_load_config_explicit_workers_warn_about_memory_cache(tmp_path):
    """Explicit workers > 1 with the memory cache still warns."""
    resolved = load_config(tmp_path, "dev", env={}, cli_overrides={"workers": 2})
    assert any("each worker keeps its own ISR cache" in warning
               for warning in resolved.warnings)


# -- load_config --------------------------------------------------------------


def test_load_config_defaults_without_file(tmp_path):
    resolved = load_config(tmp_path, "dev", env={})
    assert resolved.values == DEFAULT_CONFIG
    assert resolved.config_file is None
    assert resolved.warnings == ()
    assert resolved.phase == "dev"
    assert resolved.source_of("port") == "default"


def test_load_config_precedence_cli_over_env_over_file_over_default(tmp_path):
    write_config(tmp_path, "config = {'port': 4000, 'max_concurrent': 10, 'metrics': True}\n")
    resolved = load_config(tmp_path, "start",
                           env={"PYNEXT_METRICS": "0"},
                           cli_overrides={"port": 5000})
    assert resolved.values.port == 5000
    assert resolved.source_of("port") == "cli"
    assert resolved.values.max_concurrent == 10
    assert resolved.source_of("max_concurrent") == "file"
    # Env beats the file for metrics, and the file beats the default.
    assert resolved.values.metrics is False
    assert resolved.source_of("metrics") == "env"
    assert resolved.config_file == tmp_path / "pynext.config.py"


def test_load_config_file_without_config_dict_warns_only(tmp_path):
    write_config(tmp_path, "x = 1\n")
    resolved = load_config(tmp_path, "dev", env={})
    assert resolved.values == DEFAULT_CONFIG
    assert len(resolved.warnings) == 1
    assert "no top-level `config` dict" in resolved.warnings[0]


def test_load_config_unknown_key_warns_with_suggestion(tmp_path):
    write_config(tmp_path, "config = {'prt': 4000, 'totally_unknown': 1}\n")
    resolved = load_config(tmp_path, "dev", env={})
    assert resolved.values == DEFAULT_CONFIG
    assert len(resolved.warnings) == 2
    assert "unknown config key 'prt'" in resolved.warnings[0]
    assert "did you mean 'port'" in resolved.warnings[0]
    assert "unknown config key 'totally_unknown'" in resolved.warnings[1]


@pytest.mark.parametrize("raw, fragment", [
    ("{'port': 'x'}", "'port' must be an integer"),
    ("{'port': 70000}", "'port' must be <= 65535"),
    ("{'port': True}", "'port' must be an integer"),
    ("{'workers': 0}", "'workers' must be >= 1"),
    ("{'workers': 65}", "'workers' must be <= 64"),
    ("{'rate_limit': -1}", "'rate_limit' must be >= 0"),
    ("{'rate_limit_window': 0}", "'rate_limit_window' must be a number > 0"),
    ("{'rate_limit_window': float('nan')}", "'rate_limit_window' must be a finite number"),
    ("{'cache_backend': 'bogus'}", "'cache_backend' must be one of memory, sqlite, redis"),
    ("{'csrf_signing_key': 'short'}", "csrf_signing_key must be at least 16 bytes"),
    ("{'csrf_signing_key': 'hex:zz'}", "csrf_signing_key is not valid hex"),
    ("{'websocket_max_message': 0}", "'websocket_max_message' must be >= 1"),
    ("{'websocket_max_message': 16777217}", "'websocket_max_message' must be <= 16777216"),
    ("{'websocket_allowed_origins': ['*']}", "invalid WebSocket origin '*'"),
    ("{'websocket_allowed_origins': 'https://example.com'}",
     "'websocket_allowed_origins' must be a list or tuple of strings"),
    ("{'cors_allowed_origins': ['not-an-origin']}", "invalid CORS origin 'not-an-origin'"),
    ("{'cors_allow_credentials': 1}", "'cors_allow_credentials' must be a bool"),
    ("{'trusted_proxies': '10.0.0.1'}", "'trusted_proxies' must be a list or tuple of strings"),
    ("{'log_level': 'VERBOSE'}", "'log_level' must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL"),
    ("{'shutdown_timeout': 3601}", "'shutdown_timeout' must be <= 3600"),
    ("{'max_waiting': -1}", "'max_waiting' must be >= 0"),
])
def test_load_config_rejects_bad_values(tmp_path, raw, fragment):
    write_config(tmp_path, f"config = {raw}\n")
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path, "dev", env={})
    assert fragment in str(error.value)


def test_load_config_collects_all_errors_at_once(tmp_path):
    write_config(tmp_path, "config = {'port': 'x', 'workers': 99, 'csrf_signing_key': 'short'}\n")
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path, "dev", env={})
    message = str(error.value)
    assert "'port' must be an integer" in message
    assert "'workers' must be <= 64" in message
    assert "csrf_signing_key must be at least 16 bytes" in message


def test_load_config_accepts_boundary_and_list_values(tmp_path):
    write_config(tmp_path,
                 "config = {'port': 0, 'shutdown_timeout': 0, 'rate_limit_window': 30,\n"
                 "          'cors_allowed_origins': ['*', 'https://example.com'],\n"
                 "          'websocket_allowed_origins': ['https://example.com'],\n"
                 "          'trusted_proxies': ['10.0.0.0/8'], 'log_level': 'debug',\n"
                 "          'csrf_signing_key': 'hex:' + 'ab' * 16}\n")
    resolved = load_config(tmp_path, "dev", env={})
    assert resolved.values.port == 0
    assert resolved.values.shutdown_timeout == 0.0
    assert resolved.values.cors_allowed_origins == ("*", "https://example.com")
    assert resolved.values.log_level == "DEBUG"
    assert resolved.warnings == ()


@pytest.mark.parametrize("text, fragment", [
    ("config = {\n", "failed to load pynext.config.py"),
    ("config = None\n", "`config` must be a dict, got NoneType"),
    ("config = []\n", "`config` must be a dict, got list"),
    ("raise RuntimeError('boom')\n", "failed to load pynext.config.py: RuntimeError: boom"),
])
def test_load_config_file_errors_are_fatal(tmp_path, text, fragment):
    write_config(tmp_path, text)
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path, "dev", env={})
    assert fragment in str(error.value)


def test_load_config_cross_check_warnings(tmp_path):
    write_config(tmp_path, "config = {'cache_path': 'cache.sqlite3', 'workers': 2}\n")
    resolved = load_config(tmp_path, "dev", env={})
    assert len(resolved.warnings) == 2
    assert "'cache_path' is only used with cache_backend 'sqlite'" in resolved.warnings[0]
    assert "each worker keeps its own ISR cache" in resolved.warnings[1]


def test_load_config_rejects_unknown_phase(tmp_path):
    with pytest.raises(ValueError, match="phase must be one of"):
        load_config(tmp_path, "test", env={})


def test_find_config_file(tmp_path):
    assert find_config_file(tmp_path) is None
    write_config(tmp_path, "config = {}\n")
    assert find_config_file(tmp_path) == tmp_path / "pynext.config.py"


# -- CLI wiring ---------------------------------------------------------------

def _run_cli(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pynext", *argv])
    cli.main()


def test_cli_config_file_values_reach_server(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    write_config(root, "config = {'host': '0.0.0.0', 'port': 4001, 'max_concurrent': 33,\n"
                       "          'shutdown_timeout': 5, 'cache_backend': 'sqlite'}\n")
    seen = {}
    app = object()
    monkeypatch.setattr(cli, "create_app",
                        lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs: seen.setdefault("app", (path, production, cache_backend)) or app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen["serve"] = (host, port, max_concurrent, acquire_timeout_ms,
                         body_capacity, body_timeout_ms, shutdown_timeout, max_waiting)

    monkeypatch.setattr(cli, "serve", serve)
    # C-P0-1: pin single-process; the default now takes the supervisor path.
    _run_cli(["start", "--dir", str(root), "--workers", "1"], monkeypatch)
    assert seen["app"] == (root, True, "sqlite")
    # C-P0-2: acquire_timeout_ms comes from the new default (3000); the
    # file only sets max_concurrent (33).
    assert seen["serve"] == ("0.0.0.0", 4001, 33, 3000, 16, 10000, 5, None)


def test_cli_flag_beats_config_file(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    write_config(root, "config = {'port': 4001, 'max_concurrent': 33}\n")
    seen = {}

    async def serve(app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen["serve"] = (host, port, max_concurrent)

    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "serve", serve)
    # C-P0-1: pin single-process; the default now takes the supervisor path.
    _run_cli(["start", "--dir", str(root), "--workers", "1", "--port", "5000"], monkeypatch)
    assert seen["serve"] == ("127.0.0.1", 5000, 33)


def test_cli_config_error_exits_2_without_traceback(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    write_config(root, "config = {'port': 'not-a-port', 'workers': 99}\n")
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    err = capsys.readouterr().err
    assert "'port' must be an integer" in err
    assert "'workers' must be <= 64" in err
    assert "Traceback" not in err


def test_cli_unknown_config_key_warns_but_starts(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    write_config(root, "config = {'bogus_key': 1}\n")
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: object())

    async def serve(*args, **kwargs):
        pass

    monkeypatch.setattr(cli, "serve", serve)
    # C-P0-1: pin single-process; the default now takes the supervisor path.
    _run_cli(["start", "--dir", str(root), "--workers", "1"], monkeypatch)
    err = capsys.readouterr().err
    assert "unknown config key 'bogus_key'" in err


def test_cli_repeatable_option_replaces_file_list(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    write_config(root, "config = {'cors_allowed_origins': ['https://file.example.com']}\n")
    seen = {}

    def create_app(path, production=False, **kwargs):
        seen.update(kwargs)
        return object()

    async def serve(*args, **kwargs):
        pass

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli, "serve", serve)
    # Without a CLI flag the file value reaches the app.
    # C-P0-1: pin single-process; the default now takes the supervisor path.
    _run_cli(["start", "--dir", str(root), "--workers", "1"], monkeypatch)
    assert seen["security"].cors_allowed_origins == ("https://file.example.com",)
    # With a CLI flag the file list is replaced, not extended.
    _run_cli(["start", "--dir", str(root), "--workers", "1",
              "--cors-origin", "https://cli.example.com"], monkeypatch)
    assert seen["security"].cors_allowed_origins == ("https://cli.example.com",)


def test_cli_config_workers_reaches_supervisor(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    write_config(root, "config = {'workers': 3, 'port': 4002}\n")
    seen = {}
    monkeypatch.setattr(cli, "run_supervisor",
                        lambda config, workers: seen.setdefault("supervisor", (config, workers)) or 0)
    _run_cli(["start", "--dir", str(root)], monkeypatch)
    config, workers = seen["supervisor"]
    assert workers == 3
    assert config.port == 4002
    assert config.host == "127.0.0.1"


def test_cli_config_observability_reaches_app(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    write_config(root, "config = {'metrics': False, 'log_level': 'warning'}\n")
    seen = {}

    def create_app(path, production=False, **kwargs):
        seen["create_app"] = kwargs
        return object()

    async def serve(app, host, port, max_concurrent, acquire_timeout_ms,
                    body_capacity, body_timeout_ms, **kwargs):
        seen["serve"] = kwargs

    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli, "serve", serve)
    # C-P0-1: pin single-process; the default now takes the supervisor path.
    _run_cli(["start", "--dir", str(root), "--workers", "1"], monkeypatch)
    config = seen["create_app"]["observability"]
    assert config.metrics is False
    assert config.log_level == "WARNING"
    assert seen["serve"]["observability"] is config


def test_cli_build_with_broken_config_fails(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    write_config(root, "config = {'cache_backend': 'bogus'}\n")
    monkeypatch.setattr(cli, "write_manifest", lambda path: pytest.fail("build must not run"))
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(root)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "'cache_backend' must be one of" in capsys.readouterr().err


def test_cli_config_csrf_key_validated_before_starting(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    write_config(root, "config = {'csrf_signing_key': 'too-short'}\n")
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "csrf_signing_key must be at least 16 bytes" in capsys.readouterr().err


# -- dev reload ---------------------------------------------------------------

def _fetch(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read()


def _wait_for_content(url, marker, timeout=15):
    deadline = time.time() + timeout
    last = b""
    while time.time() < deadline:
        try:
            last = _fetch(url)
        except Exception:
            time.sleep(0.2)
            continue
        if marker in last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"{marker!r} not served within {timeout}s; last body: {last!r}")


def _start_dev(root):
    # C-P0-1: pin single-process so the dev server keeps the run_dev path
    # (file watching/reload); the multi-worker default takes the
    # supervisor path, which prints a different listening line.
    proc = subprocess.Popen(
        [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(root), "--workers", "1", "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=reader, daemon=True).start()
    seen = []
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            assert proc.poll() is None, "dev server exited before listening"
            continue
        seen.append(line)
        match = re.search(r"PyNext listening on http://(\S+)", line)
        if match:
            return proc, lines, seen, match.group(1)
    raise AssertionError("dev server never printed its listening address")


def _drain(lines, seen, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            seen.append(lines.get(timeout=0.2))
        except queue.Empty:
            continue


def test_dev_broken_config_at_startup_exits(tmp_path):
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.py").write_text(PAGE, encoding="utf-8")
    write_config(root, "config = {'port': 'not-a-port'}\n")
    proc = subprocess.run(
        [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(root), "--port", "0"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert "'port' must be an integer" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_dev_config_edit_reloads_and_broken_config_exits(tmp_path):
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.py").write_text(PAGE, encoding="utf-8")
    write_config(root, "config = {'max_concurrent': 8}\n")
    proc, lines, seen, address = _start_dev(root)
    try:
        base = f"http://{address}"
        _wait_for_content(base + "/", b"version-one")
        # A valid config edit reloads the server (still serving).
        write_config(root, "config = {'max_concurrent': 9}\n")
        _wait_for_content(base + "/", b"version-one")
        _drain(lines, seen)
        assert any("change detected" in line for line in seen), seen
        # A broken config edit ends the dev loop instead of waiting forever.
        write_config(root, "config = {'port': 'not-a-port'}\n")
        proc.wait(timeout=15)
        _drain(lines, seen)
        assert proc.returncode == 0, seen
        assert any("PyNext config error" in line for line in seen), seen
        assert any("fix the configuration error above and restart" in line for line in seen), seen
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
