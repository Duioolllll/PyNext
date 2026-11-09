"""Build lock, prechecks, staged failures and BUILD_ID (P1-6).

Next.js reference: ``build/index.ts`` — ``isWriteable``,
``Lockfile.acquireWithRetriesOrExit``, per-stage separation, and the
build id recorded last so a failed build never looks complete.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

import pytest

from pynext import cli
from pynext.build import (
    BuildError,
    BuildLock,
    BuildLockError,
    build_id_for,
    load_manifest,
    write_manifest,
)

_USAGE = namedtuple("usage", "total used free")

# A helper process that holds the build lock for N seconds, printing
# "locked" once it owns it. Runs with the test venv's python, where
# pynext is installed.
_HOLDER_SCRIPT = (
    "import sys, time\n"
    "from pynext.build import BuildLock\n"
    "with BuildLock(sys.argv[1]):\n"
    "    print('locked', flush=True)\n"
    "    time.sleep(float(sys.argv[2]))\n"
)


def make_app(root):
    app = Path(root) / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n',
        encoding="utf-8")
    return app


def test_build_writes_build_id_matching_manifest(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    build_id_path = tmp_path / ".pynext" / "BUILD_ID"
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    assert build_id_path.is_file()
    assert build_id_path.read_text(encoding="utf-8").strip() == build_id_for(manifest_path.read_bytes())
    assert build_id_path.read_text(encoding="utf-8").strip() == hashlib.sha256(
        manifest_path.read_bytes()).hexdigest()
    load_manifest(tmp_path)


def test_load_manifest_rejects_missing_or_stale_build_id(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    build_id_path = tmp_path / ".pynext" / "BUILD_ID"
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    build_id_path.unlink()
    with pytest.raises(ValueError, match="Incomplete build"):
        load_manifest(tmp_path)
    write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest, indent=4), encoding="utf-8")
    with pytest.raises(ValueError, match="Incomplete build"):
        load_manifest(tmp_path)


def test_build_lock_blocks_second_build(tmp_path):
    make_app(tmp_path)
    with BuildLock(tmp_path):
        lock_path = tmp_path / ".pynext" / ".build.lock"
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
        with pytest.raises(BuildLockError) as excinfo:
            write_manifest(tmp_path, lock_timeout=0)
        assert "another build is already running" in str(excinfo.value)
        assert excinfo.value.stage == "lock"
        assert str(lock_path) in str(excinfo.value)
        with pytest.raises(BuildLockError):
            with BuildLock(tmp_path, timeout=0):
                pass
    # The lock is released: the build now succeeds, and the lock file
    # is kept (never unlinked) but unlocked.
    write_manifest(tmp_path)
    with BuildLock(tmp_path, timeout=0):
        pass
    assert (tmp_path / ".pynext" / ".build.lock").is_file()


def test_cross_process_build_lock_waits_then_succeeds(tmp_path):
    make_app(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(tmp_path), "2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        holder_pid = int((tmp_path / ".pynext" / ".build.lock").read_text(encoding="utf-8").strip())
        assert holder_pid == holder.pid
        with pytest.raises(BuildLockError) as excinfo:
            write_manifest(tmp_path, lock_timeout=0)
        assert str(holder.pid) in str(excinfo.value)
        manifest = write_manifest(tmp_path, lock_timeout=10)
        assert manifest["static_pages"]
    finally:
        holder.wait(timeout=10)


def test_precheck_rejects_low_disk_space(tmp_path, monkeypatch):
    make_app(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: _USAGE(1 << 30, 1 << 30, 0))
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "precheck"
    assert "bytes free" in str(excinfo.value)


def test_precheck_rejects_unwritable_build_dir(tmp_path, monkeypatch):
    make_app(tmp_path)

    def _raise(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _raise)
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "precheck"
    assert "not writable" in str(excinfo.value)


def test_discover_stage_is_tagged(tmp_path):
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "discover"
    assert "App directory not found" in str(excinfo.value)


def test_render_stage_is_tagged(tmp_path):
    app = Path(tmp_path) / "app"
    (app / "blog" / "[slug]").mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")
    (app / "blog" / "[slug]" / "page.py").write_text(
        'from pynext import page, h\n'
        'def generate_static_params(): return [{"slug": "x"}]\n'
        '@page(render="ssg")\ndef Page(params): raise RuntimeError("boom")\n',
        encoding="utf-8")
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "render"
    assert "/blog/x" in str(excinfo.value) and "boom" in str(excinfo.value)


def test_failed_pages_are_isolated_and_all_reported(tmp_path):
    app = Path(tmp_path) / "app"
    (app / "blog" / "[slug]").mkdir(parents=True)
    (app / "docs" / "[...path]").mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")
    (app / "blog" / "[slug]" / "page.py").write_text(
        'from pynext import page, h\n'
        'def generate_static_params(): return [{"slug": "good"}, {"slug": "bad"}]\n'
        '@page(render="ssg")\n'
        'def Page(params):\n'
        '    if params["slug"] == "bad": raise RuntimeError("boom bad")\n'
        '    return h("p", {}, params["slug"])\n',
        encoding="utf-8")
    (app / "docs" / "[...path]" / "page.py").write_text(
        'from pynext import page, h\n'
        'async def generate_static_params(): return [{"path": ["x"]}]\n'
        '@page(render="ssg")\n'
        'def Page(params): raise RuntimeError("boom docs")\n',
        encoding="utf-8")
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "render"
    # Both failures are listed: the build did not stop at the first one.
    message = str(excinfo.value)
    assert "/blog/bad" in message and "boom bad" in message
    assert "/docs/x" in message and "boom docs" in message
    assert "2 static page(s) failed" in message
    # Nothing was published: no manifest and no BUILD_ID.
    assert not (tmp_path / ".pynext" / "manifest.json").exists()
    assert not (tmp_path / ".pynext" / "BUILD_ID").exists()


def test_failed_generator_is_isolated_per_pattern(tmp_path):
    app = Path(tmp_path) / "app"
    (app / "blog" / "[slug]").mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")
    (app / "blog" / "[slug]" / "page.py").write_text(
        'from pynext import page, h\n'
        'def generate_static_params(): raise RuntimeError("generator boom")\n'
        '@page(render="ssg")\ndef Page(params): return h("p", {}, "blog")\n',
        encoding="utf-8")
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "render"
    assert "/blog/[slug] (generate_static_params)" in str(excinfo.value)
    assert "generator boom" in str(excinfo.value)


def test_failed_build_leaves_previous_manifest_and_build_id(tmp_path):
    app = make_app(tmp_path)
    write_manifest(tmp_path)
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    build_id_path = tmp_path / ".pynext" / "BUILD_ID"
    original_manifest = manifest_path.read_bytes()
    original_build_id = build_id_path.read_bytes()
    page = app / "page.py"
    original_page = page.read_text(encoding="utf-8")
    page.write_text(
        'from pynext import page, h\n'
        '@page(render="ssg")\ndef Page(): raise RuntimeError("boom")\n',
        encoding="utf-8")
    with pytest.raises(BuildError) as excinfo:
        write_manifest(tmp_path)
    assert excinfo.value.stage == "render"
    assert manifest_path.read_bytes() == original_manifest
    assert build_id_path.read_bytes() == original_build_id
    # The previous build is still complete and startable once the
    # broken source is restored.
    page.write_text(original_page, encoding="utf-8")
    load_manifest(tmp_path)


def test_prune_removes_orphan_digest_files(tmp_path):
    make_app(tmp_path)
    manifest = write_manifest(tmp_path)
    static_dir = tmp_path / ".pynext" / "static"
    referenced = {f"{entry['sha256']}.html" for entry in manifest["static_pages"].values()}
    assert referenced
    orphans = [static_dir / ("0" * 64 + ".html"), static_dir / ("f" * 64 + ".asset"),
               static_dir / ("a" * 64 + ".js")]
    keep = static_dir / "keep.txt"
    not_hex = static_dir / "not-a-digest.html"
    for path in (*orphans, keep, not_hex):
        path.write_text("x", encoding="utf-8")
    write_manifest(tmp_path)
    for orphan in orphans:
        assert not orphan.exists()
    assert keep.exists() and not_hex.exists()
    for name in referenced:
        assert (static_dir / name).exists()


def test_cli_build_reports_lock_contention(tmp_path, monkeypatch, capsys):
    def locked(path, lock_timeout=None):
        raise BuildLockError("another build is already running for /x")

    monkeypatch.setattr(cli, "write_manifest", locked)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "another build is already running" in capsys.readouterr().err


def test_cli_build_lock_timeout_reaches_subprocess(tmp_path):
    make_app(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(tmp_path), "5"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        result = subprocess.run(
            [sys.executable, "-m", "pynext.cli", "build", "--dir", str(tmp_path),
             "--lock-timeout", "0"],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 2
        assert "another build is already running" in result.stderr
    finally:
        holder.wait(timeout=10)


def test_cli_rejects_negative_lock_timeout(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--lock-timeout", "-1"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "--lock-timeout" in capsys.readouterr().err
