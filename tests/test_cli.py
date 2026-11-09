import asyncio
import sys
from pathlib import Path

import pytest

from pynext import cli, create_app
from pynext.server import start_native


@pytest.mark.parametrize("value", ["-1", "65536", "not-a-port"])
def test_cli_rejects_invalid_port_before_starting(value, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pynext", "dev", "--port", value])
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "port must be an integer from 0 to 65535" in capsys.readouterr().err


@pytest.mark.parametrize("flag,value,message", [
    ("--max-concurrent", "-1", "--max-concurrent must be an integer >= 1"),
    ("--max-concurrent", "0", "--max-concurrent must be an integer >= 1"),
    ("--max-concurrent", "not-a-number", "--max-concurrent must be an integer in [1, 1000000]"),
    ("--body-capacity", "-1", "--body-capacity must be an integer >= 1"),
    ("--body-capacity", "0", "--body-capacity must be an integer >= 1"),
    ("--acquire-timeout-ms", "-1", "--acquire-timeout-ms must be an integer >= 0"),
    ("--acquire-timeout-ms", "not-a-number", "--acquire-timeout-ms must be an integer in [0, 86400000]"),
    ("--body-timeout-ms", "-1", "--body-timeout-ms must be an integer >= 0"),
    ("--body-timeout-ms", "not-a-number", "--body-timeout-ms must be an integer in [0, 86400000]"),
    ("--max-waiting", "-1", "--max-waiting must be an integer >= 0"),
    ("--max-waiting", "not-a-number", "--max-waiting must be an integer in [0, 1000000]"),
])
def test_cli_rejects_out_of_range_limits_before_starting(flag, value, message, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pynext", "dev", flag, value])
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("flag,value,message", [
    ("--max-concurrent", "1000001", "--max-concurrent must be an integer <= 1000000"),
    ("--max-concurrent", "99999999999999999999999999", "--max-concurrent must be an integer <= 1000000"),
    ("--max-waiting", "1000001", "--max-waiting must be an integer <= 1000000"),
    ("--body-capacity", "1000001", "--body-capacity must be an integer <= 1000000"),
    ("--max-connections", "1000001", "--max-connections must be an integer <= 1000000"),
    ("--max-connections-per-ip", "1000001", "--max-connections-per-ip must be an integer <= 1000000"),
    ("--shutdown-timeout", "3601", "--shutdown-timeout must be an integer <= 3600"),
    ("--shutdown-timeout", "99999999999999999999999999", "--shutdown-timeout must be an integer <= 3600"),
    ("--workers", "65", "--workers must be an integer <= 64"),
    ("--workers", "100000", "--workers must be an integer <= 64"),
    ("--acquire-timeout-ms", "86400001", "--acquire-timeout-ms must be an integer <= 86400000"),
    ("--body-timeout-ms", "86400001", "--body-timeout-ms must be an integer <= 86400000"),
    ("--header-read-timeout-ms", "86400001", "--header-read-timeout-ms must be an integer <= 86400000"),
    ("--body-min-rate-bytes-per-sec", "1000000001",
     "--body-min-rate-bytes-per-sec must be an integer <= 1000000000"),
])
def test_cli_rejects_above_maximum_limits_before_starting(flag, value, message, monkeypatch, capsys):
    # Review #4 #5: huge counts used to die with an OverflowError
    # traceback (or, for --workers, attempt a resource-exhausting spawn);
    # now argparse rejects them with exit code 2 and a clear message.
    monkeypatch.setattr(sys, "argv", ["pynext", "dev", flag, value])
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_accepts_maximum_limits(tmp_path, monkeypatch):
    # Boundary values (not just the small ones) must still reach serve()
    # unchanged.
    root = tmp_path.resolve()
    seen = []
    app = object()
    monkeypatch.setattr(cli, "create_app", lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs:
                        seen.append(("app", path, production, cache_backend, cache_path)) or app)

    async def serve(*args, **kwargs):
        seen.append(("serve", args, kwargs))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--max-concurrent", "1000000", "--acquire-timeout-ms", "86400000",
                                      "--max-waiting", "1000000", "--body-capacity", "1000000",
                                      "--body-timeout-ms", "86400000", "--max-connections", "1000000",
                                      "--max-connections-per-ip", "1000000",
                                      "--header-read-timeout-ms", "86400000",
                                      "--body-min-rate-bytes-per-sec", "1000000000",
                                      "--shutdown-timeout", "3600"])
    cli.main()
    assert seen[0][0] == "app"
    args, kwargs = seen[1][1], seen[1][2]
    assert args[1:] == ("127.0.0.1", 3000, 1000000, 86400000, 1000000, 86400000)
    assert kwargs["shutdown_timeout"] == 3600
    assert kwargs["max_waiting"] == 1000000
    assert kwargs["max_connections"] == 1000000
    assert kwargs["max_connections_per_ip"] == 1000000
    assert kwargs["header_read_timeout_ms"] == 86400000
    assert kwargs["body_min_rate_bytes_per_sec"] == 1000000000


def test_cli_workers_boundary_reaches_supervisor(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_supervisor", lambda config, workers: seen.append(workers) or 0)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path), "--workers", "64"])
    assert cli.main() == 0
    assert seen == [64]


def test_server_limits_rejected_directly_without_overflow(tmp_path):
    # Review #4 #5: a direct serve()/start_native() caller bypasses the
    # CLI, so the same bounds are enforced there with a clear ValueError
    # instead of the PyO3 OverflowError traceback.
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    app = create_app(tmp_path)

    async def run():
        loop = asyncio.get_running_loop()
        with pytest.raises(ValueError, match="max_concurrent must be an integer between 1 and 1000000"):
            start_native(app, loop, port=0, max_concurrent=99999999999999999999999999)
        with pytest.raises(ValueError, match="shutdown_timeout must be a number between 0 and 3600"):
            start_native(app, loop, port=0, shutdown_timeout=99999999999999999999999999)
        with pytest.raises(ValueError, match="max_waiting must be an integer between 0 and 1000000"):
            start_native(app, loop, port=0, max_waiting=1000001)
        from pynext.supervisor import WorkerConfig, run_supervisor
        config = WorkerConfig(root=str(tmp_path), host="127.0.0.1", port=0, production=True,
                              max_concurrent=64, acquire_timeout_ms=1000, body_capacity=16,
                              body_timeout_ms=10000)
        with pytest.raises(ValueError, match="workers must be an integer between 1 and 64"):
            run_supervisor(config, workers=100000)

    asyncio.run(run())


def test_cli_accepts_boundary_limits(tmp_path, monkeypatch):
    # 1 is the smallest count, 0ms is a valid "only if instantly free"
    # timeout; both must reach serve() unchanged instead of dying in the
    # PyO3 conversion / Rust ValueError as a traceback.
    root = tmp_path.resolve()
    seen = []
    app = object()
    monkeypatch.setattr(cli, "create_app", lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs:
                        seen.append(("app", path, production, cache_backend, cache_path)) or app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                   body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms, shutdown_timeout,
                     max_waiting))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--max-concurrent", "1", "--acquire-timeout-ms", "0",
                                      "--body-capacity", "1", "--body-timeout-ms", "0"])
    cli.main()
    assert seen == [("app", root, True, "memory", None),
                    ("serve", app, "127.0.0.1", 3000, 1, 0, 1, 0, 30, None)]


def test_cli_max_waiting_reaches_serve(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    seen = []
    app = object()
    monkeypatch.setattr(cli, "create_app", lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs:
                        seen.append(("app", path, production, cache_backend, cache_path)) or app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                   body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms, max_waiting))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root), "--max-waiting", "0"])
    cli.main()
    assert seen[1] == ("serve", app, "127.0.0.1", 3000, 64, 1000, 16, 10000, 0)


def test_cli_builds_and_starts_with_requested_settings(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    seen = []
    monkeypatch.setattr(cli, "write_manifest", lambda path: seen.append(("build", path)) or {"routes": {"/": {}}})
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(root)])
    cli.main()
    assert seen == [("build", root)]
    assert "Built 1 routes" in capsys.readouterr().out

    app = object()
    monkeypatch.setattr(cli, "create_app", lambda path, production, cache_backend="memory", cache_path=None,
                        rate_limit=None, **kwargs:
                        seen.append(("app", path, production, cache_backend, cache_path)) or app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms,
                   body_capacity, body_timeout_ms, shutdown_timeout=30, max_waiting=None):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms, shutdown_timeout,
                     max_waiting))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root), "--host", "localhost", "--port", "0",
                                      "--cache-backend", "sqlite", "--cache-path", str(root / "cache.sqlite3")])
    cli.main()
    assert seen[-2:] == [("app", root, True, "sqlite", str(root / "cache.sqlite3")),
                         ("serve", app, "localhost", 0, 64, 1000, 16, 10000, 30, None)]


def test_cli_shutdown_timeout_reaches_serve(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    seen = []
    app = object()
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: app)

    async def serve(*args, shutdown_timeout, **kwargs):
        seen.append(shutdown_timeout)

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--shutdown-timeout", "5"])
    cli.main()
    assert seen == [5]
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--shutdown-timeout", "-1"])
    with pytest.raises(SystemExit):
        cli.main()


def test_cli_build_reports_manifest_errors_instead_of_traceback(tmp_path, monkeypatch, capsys):
    def failing_manifest(path):
        raise ValueError("Application sources changed during build; retry the build")

    monkeypatch.setattr(cli, "write_manifest", failing_manifest)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Application sources changed during build" in capsys.readouterr().err


@pytest.mark.parametrize(("option", "bounds"), [("--workers", "[1, 64]"),
                                                ("--max-concurrent", "[1, 1000000]")])
def test_cli_non_integer_count_reports_full_range(monkeypatch, capsys, option, bounds):
    monkeypatch.setattr(sys, "argv", ["pynext", "start", option, "many"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert option in message and bounds in message


def test_cli_start_reports_non_object_manifest(tmp_path, monkeypatch, capsys):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n', encoding="utf-8")
    cli.write_manifest(tmp_path)
    (tmp_path / ".pynext" / "manifest.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "expected a JSON object" in message and "rebuild" in message
    assert "Traceback" not in message


def test_cli_security_flags_reach_app(tmp_path, monkeypatch):
    seen = {}
    def create_app(path, production=False, **kwargs):
        seen.update(kwargs)
        return object()
    async def serve(*args, **kwargs):
        pass
    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path), "--csrf",
                                      "--csrf-cookie-secure", "--cors-origin", "https://example.com",
                                      "--cors-credentials", "--csp-nonce",
                                      "--ws-allowed-origin", "https://example.com",
                                      "--ws-allowed-origin", "https://client.example.com",
                                      "--ws-allow-missing-origin"])
    cli.main()
    security = seen["security"]
    assert security.csrf_enabled and security.csrf_cookie_secure and security.csp_nonce_enabled
    assert security.cors_allowed_origins == ("https://example.com",) and security.cors_allow_credentials
    assert security.websocket_allowed_origins == ("https://example.com", "https://client.example.com")
    assert security.websocket_allow_missing_origin
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(tmp_path),
                                      "--ws-allowed-origin", "*"])
    with pytest.raises(SystemExit):
        cli.main()
