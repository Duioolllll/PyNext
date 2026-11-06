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


def test_cli_builds_and_starts_with_requested_settings(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    seen = []
    monkeypatch.setattr(cli, "write_manifest", lambda path: seen.append(("build", path)) or {"routes": {"/": {}}})
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(root)])
    cli.main()
    assert seen == [("build", root)]
    assert "Built 1 routes" in capsys.readouterr().out

    app = object()
    monkeypatch.setattr(cli, "create_app", lambda path, production: seen.append(("app", path, production)) or app)

    async def serve(received_app, host, port, max_concurrent, acquire_timeout_ms):
        seen.append(("serve", received_app, host, port, max_concurrent, acquire_timeout_ms))

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["pynext", "start", "--dir", str(root), "--host", "localhost", "--port", "0"])
    cli.main()
    assert seen[-2:] == [("app", root, True), ("serve", app, "localhost", 0, 64, 1000)]
