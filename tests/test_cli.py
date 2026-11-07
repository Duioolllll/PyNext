import sys
from pathlib import Path

import pytest

from pynext import cli


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
    ("--max-concurrent", "not-a-number", "--max-concurrent must be an integer >= 1"),
    ("--body-capacity", "-1", "--body-capacity must be an integer >= 1"),
    ("--body-capacity", "0", "--body-capacity must be an integer >= 1"),
    ("--acquire-timeout-ms", "-1", "--acquire-timeout-ms must be an integer >= 0"),
    ("--acquire-timeout-ms", "not-a-number", "--acquire-timeout-ms must be an integer >= 0"),
    ("--body-timeout-ms", "-1", "--body-timeout-ms must be an integer >= 0"),
    ("--body-timeout-ms", "not-a-number", "--body-timeout-ms must be an integer >= 0"),
])
def test_cli_rejects_out_of_range_limits_before_starting(flag, value, message, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pynext", "dev", flag, value])
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: pytest.fail("unexpected startup"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err


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
                   body_capacity, body_timeout_ms):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root),
                                      "--max-concurrent", "1", "--acquire-timeout-ms", "0",
                                      "--body-capacity", "1", "--body-timeout-ms", "0"])
    cli.main()
    assert seen == [("app", root, True, "memory", None),
                    ("serve", app, "127.0.0.1", 3000, 1, 0, 1, 0)]


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
                   body_capacity, body_timeout_ms):
        seen.append(("serve", received_app, host, port, max_concurrent,
                     acquire_timeout_ms, body_capacity, body_timeout_ms))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root), "--host", "localhost", "--port", "0",
                                      "--cache-backend", "sqlite", "--cache-path", str(root / "cache.sqlite3")])
    cli.main()
    assert seen[-2:] == [("app", root, True, "sqlite", str(root / "cache.sqlite3")),
                         ("serve", app, "localhost", 0, 64, 1000, 16, 10000)]
