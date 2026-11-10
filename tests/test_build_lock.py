"""Build lock, prechecks, staged failures and BUILD_ID (P1-6).

Next.js reference: ``build/index.ts`` — ``isWriteable``,
``Lockfile.acquireWithRetriesOrExit``, per-stage separation, and the
build id recorded last so a failed build never looks complete.
"""
import errno
import hashlib
import io
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

import pynext.build as build_module
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
    "import os, sys, time\n"
    "from pynext.build import BuildLock\n"
    "with BuildLock(sys.argv[1]):\n"
    "    print(f'locked {os.getpid()}', flush=True)\n"
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
        # The holder reports its own pid: on Windows the venv python.exe
        # is a redirector that spawns the real interpreter as a child, so
        # holder.pid (the redirector) is not the pid holding the lock.
        # The lock file records the real holder pid (os.getpid() inside
        # the holder), so compare against the printed pid, not holder.pid.
        locked_line = holder.stdout.readline().strip()
        assert locked_line.startswith("locked ")
        holder_pid = int(locked_line.split()[1])
        lock_pid = int((tmp_path / ".pynext" / ".build.lock").read_text(encoding="utf-8").strip())
        assert lock_pid == holder_pid
        with pytest.raises(BuildLockError) as excinfo:
            write_manifest(tmp_path, lock_timeout=0)
        assert str(holder_pid) in str(excinfo.value)
        manifest = write_manifest(tmp_path, lock_timeout=10)
        assert manifest["static_pages"]
    finally:
        holder.wait(timeout=10)


def test_cross_process_build_lock_pid_matches_real_holder_not_launcher(tmp_path):
    """The lock file must record the real holder pid, not a launcher pid.

    Regression: on Windows the venv python.exe is a redirector that spawns
    the real interpreter as a child, so Popen(...).pid (the redirector)
    differs from os.getpid() inside the holder (the real interpreter).
    The holder reports its own pid in the "locked" line; the lock file
    must match that pid. This test simulates the redirector on any
    platform by spawning the holder through a wrapper process, so the
    failure reproduces on Linux too if the test ever goes back to
    comparing against the Popen pid.
    """
    make_app(tmp_path)
    launcher_script = tmp_path / "launcher.py"
    launcher_script.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', " + repr(_HOLDER_SCRIPT) + ", sys.argv[1], sys.argv[2]])\n"
        "sys.exit(child.wait())\n",
        encoding="utf-8")
    launcher = subprocess.Popen(
        [sys.executable, str(launcher_script), str(tmp_path), "2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        locked_line = launcher.stdout.readline().strip()
        assert locked_line.startswith("locked ")
        holder_pid = int(locked_line.split()[1])
        # The launcher pid is a different process than the real holder.
        assert launcher.pid != holder_pid
        lock_pid = int((tmp_path / ".pynext" / ".build.lock").read_text(encoding="utf-8").strip())
        assert lock_pid == holder_pid
    finally:
        launcher.wait(timeout=10)


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
    def locked(path, deployment_id=None, lock_timeout=None):
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
        assert holder.stdout.readline().strip().startswith("locked")
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


# --- Windows byte-range lock regression --------------------------------
# ``msvcrt.locking`` locks a byte range starting at the CURRENT file
# position, unlike POSIX ``flock`` which locks the whole file.
# BuildLock used to take the lock at the "a+" open position (EOF) but
# release it at the end of the pid it wrote, so on Windows the release
# raised Permission denied and a second build could take a different,
# free byte instead of being blocked. The lock then moved to a fixed
# byte past the pid text: Windows locks are mandatory, so a lock byte
# inside the pid made ``read_text()`` of the lock file raise Permission
# denied while the lock was held. The fake below reproduces real
# Windows semantics on any platform, so these tests fail on the old
# code and pass on the fixed code everywhere.


class _FakeWindowsLocking:
    """Fake ``msvcrt`` with real Windows byte-range lock semantics.

    ``locking(fd, mode, nbytes)`` locks or unlocks ``nbytes`` starting
    at the file's current position. Locking an already locked byte and
    unlocking a byte that was never locked both fail with EACCES
    (Permission denied), like the CRT's ``_locking``.

    Windows locks are mandatory: reading a byte locked by another
    handle fails with EACCES. ``read_blocked(path)`` models that for a
    fresh-handle read (``Path.read_text()`` opens one): it is True when
    any byte in the first 8 KiB read chunk is locked, so a lock byte
    inside the pid text makes the pid unreadable while the lock is held.
    """

    LK_NBLCK = 0
    LK_UNLCK = 1

    def __init__(self):
        self._locked = {}
        self.lock_positions = []
        self.unlock_positions = []

    @staticmethod
    def _key(fd):
        stat = os.fstat(fd)
        return (stat.st_dev, stat.st_ino)

    def locking(self, fd, mode, nbytes):
        position = os.lseek(fd, 0, os.SEEK_CUR)
        locked = self._locked.setdefault(self._key(fd), set())
        wanted = set(range(position, position + nbytes))
        if mode == self.LK_NBLCK:
            self.lock_positions.append(position)
            if wanted & locked:
                raise OSError(errno.EACCES, "Permission denied")
            locked |= wanted
        elif mode == self.LK_UNLCK:
            self.unlock_positions.append(position)
            if not wanted <= locked:
                raise OSError(errno.EACCES, "Permission denied")
            locked -= wanted
        else:  # pragma: no cover - invalid mode
            raise ValueError(mode)

    def read_blocked(self, path):
        """True when a fresh-handle read of ``path`` would fail on Windows."""
        try:
            stat = os.stat(path)
        except OSError:
            return False
        locked = self._locked.get((stat.st_dev, stat.st_ino))
        if not locked:
            return False
        return any(offset < io.DEFAULT_BUFFER_SIZE for offset in locked)


@pytest.fixture
def windows_locking(monkeypatch):
    fake = _FakeWindowsLocking()
    monkeypatch.setattr(build_module, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    real_read_text = Path.read_text

    def _checked_read_text(self, *args, **kwargs):
        if fake.read_blocked(self):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _checked_read_text)
    return fake


def test_windows_build_lock_releases_without_permission_denied(tmp_path, windows_locking):
    """Acquiring and releasing the lock on a fresh file must not raise.

    Regression: the lock used to be taken at the "a+" open position
    (EOF, byte 0 for a fresh file) but released at the end of the
    written pid, so Windows raised Permission denied in ``__exit__``.
    Both are now pinned to the fixed lock byte past the pid text.
    """
    with BuildLock(tmp_path):
        pass
    # Re-acquiring right after release works: the lock is really gone.
    with BuildLock(tmp_path, timeout=0):
        pass
    assert windows_locking.lock_positions == [build_module._LOCK_BYTE_OFFSET] * 2
    assert windows_locking.unlock_positions == [build_module._LOCK_BYTE_OFFSET] * 2


def test_windows_second_build_is_blocked(tmp_path, windows_locking):
    """A second build must see the lock, not take a different byte.

    Regression: the second build used to lock at its own "a+" open
    position (EOF, past the first build's pid), a byte nobody held,
    so mutual exclusion silently broke on Windows.
    """
    with BuildLock(tmp_path):
        with pytest.raises(BuildLockError):
            with BuildLock(tmp_path, timeout=0):
                pass


def test_windows_build_lock_with_stale_pid_of_other_length(tmp_path, windows_locking):
    """Release must work when the lock file held a pid of another length.

    The stale pid is deliberately longer than this process's pid, so
    the acquire and release positions only coincide when both are
    pinned to the fixed lock byte. The pid must also stay readable
    while the lock is held: on Windows a lock byte inside the pid text
    makes ``read_text()`` raise Permission denied.
    """
    lock_path = tmp_path / ".pynext" / ".build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("9" * (len(str(os.getpid())) + 3), encoding="utf-8")
    with BuildLock(tmp_path):
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    with BuildLock(tmp_path, timeout=0):
        pass
    assert windows_locking.lock_positions == [build_module._LOCK_BYTE_OFFSET] * 2
    assert windows_locking.unlock_positions == [build_module._LOCK_BYTE_OFFSET] * 2
