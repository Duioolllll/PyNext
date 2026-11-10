"""Standalone bundle ``.env`` handling (M-18 / S-P1-27).

A bundle that includes ``.env`` / ``.env.production`` carries their values
verbatim, so the build must warn (with values masked) and offer an explicit
opt-out. These tests cover:

- including env files warns, names the variables as ``KEY=***``, and never
  logs a secret value;
- ``include_env=False`` leaves the files out of the bundle and records the
  omission in the bundle manifest;
- startup logging (``log_bundle_info``) warns again for a bundle that
  carries env files, since the bundle may outlive its build machine.
"""
import json
import logging
import sys
import sysconfig

import pytest

from pynext.build import write_manifest
from pynext.standalone import build_standalone, log_bundle_info

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Standalone directory builds are Linux-only")

SECRET_VALUE = "topsecret-value-that-must-never-appear-in-logs"


def _fake_purelib(monkeypatch, tmp_path):
    """Point sysconfig's purelib at an empty dir, like test_standalone.py."""
    fake = tmp_path / "fake-purelib"
    fake.mkdir()
    real_get_path = sysconfig.get_path
    monkeypatch.setattr(sysconfig, "get_path",
                        lambda name, *args, **kwargs: str(fake) if name == "purelib"
                        else real_get_path(name, *args, **kwargs))
    return fake


def _project_with_env(tmp_path):
    project = tmp_path / "source"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "bundled")\n')
    (project / ".env").write_text(
        f"PYNEXT_PUBLIC_API_URL=https://api.example.test\nSECRET_KEY={SECRET_VALUE}\n")
    (project / ".env.production").write_text("MODE=prod\n")
    (project / ".env.local").write_text("SECRET=local-secret\n")
    write_manifest(project)
    return project


def _manifest(output):
    return json.loads((output / "bundle-manifest.json").read_text(encoding="utf-8"))


def test_include_env_warns_with_masked_keys(tmp_path, monkeypatch, caplog):
    project = _project_with_env(tmp_path)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    with caplog.at_level(logging.WARNING, logger="pynext"):
        build_standalone(project, output)
    app_dir = output / "app"
    assert (app_dir / ".env").read_text().startswith("PYNEXT_PUBLIC_API_URL=")
    assert (app_dir / ".env.production").read_text() == "MODE=prod\n"
    assert not (app_dir / ".env.local").exists()
    warnings = [record for record in caplog.records
                if record.levelno >= logging.WARNING and ".env" in record.getMessage()]
    assert warnings, "expected a warning when .env files are bundled"
    joined = "\n".join(record.getMessage() for record in warnings)
    assert SECRET_VALUE not in joined, "secret value leaked into the build log"
    assert SECRET_VALUE not in caplog.text, "secret value leaked into captured logs"
    assert "SECRET_KEY=***" in joined
    assert "PYNEXT_PUBLIC_API_URL=***" in joined
    assert _manifest(output)["env_files"] == [".env", ".env.production"]
    assert _manifest(output)["env_files_excluded"] == []


def test_include_env_false_leaves_env_files_out(tmp_path, monkeypatch, caplog):
    project = _project_with_env(tmp_path)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    with caplog.at_level(logging.WARNING, logger="pynext"):
        build_standalone(project, output, include_env=False)
    app_dir = output / "app"
    assert not (app_dir / ".env").exists()
    assert not (app_dir / ".env.production").exists()
    assert not (app_dir / ".env.local").exists()
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno >= logging.WARNING and ".env" in record.getMessage()]
    assert any("leaves out" in message for message in warnings), \
        "expected a warning naming the excluded .env files"
    joined = "\n".join(warnings)
    assert SECRET_VALUE not in joined, "secret value leaked into the build log"
    assert "SECRET_KEY=***" in joined
    manifest = _manifest(output)
    assert manifest["env_files"] == []
    assert manifest["env_files_excluded"] == [".env", ".env.production"]


def test_no_env_files_no_env_warning(tmp_path, monkeypatch, caplog):
    project = tmp_path / "source"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "bundled")\n')
    write_manifest(project)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    with caplog.at_level(logging.WARNING, logger="pynext"):
        build_standalone(project, output)
    assert not any(".env" in record.getMessage() for record in caplog.records
                   if record.levelno >= logging.WARNING)
    manifest = _manifest(output)
    assert manifest["env_files"] == []
    assert manifest["env_files_excluded"] == []


def test_include_env_must_be_bool(tmp_path, monkeypatch):
    project = _project_with_env(tmp_path)
    _fake_purelib(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="include_env must be a bool"):
        build_standalone(project, tmp_path / "bundle", include_env="yes")


def test_log_bundle_info_warns_for_env_bundle(tmp_path, monkeypatch, caplog):
    project = _project_with_env(tmp_path)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    build_standalone(project, output)
    with caplog.at_level(logging.WARNING, logger="pynext"):
        log_bundle_info(output / "app")
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno >= logging.WARNING]
    assert any(".env" in message and "secret-bearing" in message for message in warnings), \
        "expected a startup warning for a bundle carrying .env files"
    assert SECRET_VALUE not in caplog.text


def test_log_bundle_info_notes_excluded_env_files(tmp_path, monkeypatch, caplog):
    project = _project_with_env(tmp_path)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    build_standalone(project, output, include_env=False)
    with caplog.at_level(logging.INFO, logger="pynext"):
        log_bundle_info(output / "app")
    assert any(".env" in record.getMessage() and "leaves out" in record.getMessage()
               for record in caplog.records)
