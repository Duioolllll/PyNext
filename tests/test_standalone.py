import hashlib
import json
import logging
import os
import platform
import queue
import select
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request

import pytest

from pynext.build import write_manifest
from pynext.standalone import _build_id, _copy_site_packages, build_standalone

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="standalone builds currently support Linux")


def _site_packages(bundle):
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return bundle / "runtime" / "lib" / version / "site-packages"


def _tree_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def _fake_purelib(monkeypatch, tmp_path, pynext_extra=None):
    """Point sysconfig's purelib at a fake dir; returns the fake dir.

    The fake dir holds only what the test puts in it, so a build against
    it never touches the real environment's site-packages.
    """
    fake = tmp_path / "fake-purelib"
    fake.mkdir()
    if pynext_extra:
        stale = fake / "pynext"
        stale.mkdir()
        for name, content in pynext_extra.items():
            (stale / name).write_text(content)
    real_get_path = sysconfig.get_path
    monkeypatch.setattr(sysconfig, "get_path",
                        lambda name, *args, **kwargs: str(fake) if name == "purelib"
                        else real_get_path(name, *args, **kwargs))
    return fake


def _minimal_project(tmp_path):
    project = tmp_path / "source"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "bundled")\n')
    write_manifest(project)
    return project




def test_standalone_missing_artifact_fails_cleanly_and_retry_succeeds(tmp_path):
    # Review #4 #4: with one build artifact deleted, the build must fail
    # before creating anything (no partial bundle left behind), and the
    # retry after restoring the artifact must succeed instead of dying
    # with FileExistsError on the stale output.
    project = tmp_path / "source"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "bundled")\n')
    write_manifest(project)
    static_dir = project / ".pynext" / "static"
    artifacts = sorted(static_dir.iterdir())
    assert artifacts, "expected at least one build artifact"
    artifacts[0].unlink()
    output = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Missing standalone build artifact"):
        build_standalone(project, output)
    assert not output.exists(), "failed build left a partial bundle behind"
    write_manifest(project)
    build_standalone(project, output)
    assert (output / "run").is_file() and (output / "app" / "app" / "page.py").is_file()


def test_standalone_mid_build_failure_removes_partial_bundle(tmp_path, monkeypatch):
    # Safety net for #4: a failure after copying started (here a copy
    # error while placing the interpreter) must also leave no output.
    import pynext.standalone as standalone

    project = tmp_path / "source"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "bundled")\n')
    write_manifest(project)
    real_copy2 = standalone.shutil.copy2

    def failing_copy2(*args, **kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(standalone.shutil, "copy2", failing_copy2)
    output = tmp_path / "bundle"
    with pytest.raises(OSError, match="simulated copy failure"):
        build_standalone(project, output)
    assert not output.exists(), "failed build left a partial bundle behind"
    monkeypatch.setattr(standalone.shutil, "copy2", real_copy2)
    build_standalone(project, output)
    assert (output / "run").is_file()


def test_standalone_cleanup_failure_is_reported_without_masking_build_error(tmp_path, monkeypatch, caplog):
    import pynext.standalone as standalone

    project = _minimal_project(tmp_path)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"

    def fail_copy(*args):
        (output / "partial.txt").write_text("partial", encoding="utf-8")
        raise OSError("original build failure")

    monkeypatch.setattr(standalone, "_populate_standalone", fail_copy)
    # rmtree(ignore_errors=True) may return with files still present.
    monkeypatch.setattr(standalone.shutil, "rmtree", lambda *args, **kwargs: None)
    with caplog.at_level(logging.WARNING, logger="pynext"):
        with pytest.raises(OSError, match="original build failure"):
            build_standalone(project, output)
    assert (output / "partial.txt").is_file()
    assert str(output) in caplog.text and "remove it before retrying" in caplog.text


def test_copy_site_packages_rejects_symlinked_entries(tmp_path):
    purelib = tmp_path / "purelib"
    purelib.mkdir()
    target = tmp_path / "real-pkg"
    target.mkdir()
    (target / "__init__.py").write_text("")
    (purelib / "linked").symlink_to(target, target_is_directory=True)
    (purelib / "plain.py").write_text("# plain\n")
    packages = tmp_path / "packages"
    packages.mkdir()
    with pytest.raises(ValueError, match="symlinked dependencies"):
        _copy_site_packages(purelib, packages)
    assert not (packages / "linked").exists()


def test_copy_site_packages_warns_for_nested_symlink(tmp_path, caplog):
    purelib = tmp_path / "purelib"
    pkg = purelib / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    target = tmp_path / "real-file.py"
    target.write_text("# real\n")
    (pkg / "linked.py").symlink_to(target)
    (purelib / "plain.py").write_text("# plain\n")
    packages = tmp_path / "packages"
    packages.mkdir()
    with caplog.at_level(logging.WARNING, logger="pynext"):
        _copy_site_packages(purelib, packages)
    # The nested symlink is dropped, like before, but now the build log
    # names it instead of silently shipping a bundle without that file.
    assert not (packages / "pkg" / "linked.py").exists()
    assert (packages / "pkg" / "__init__.py").exists()
    assert "linked.py" in caplog.text


def test_standalone_rejects_symlinked_purelib_entry(tmp_path, monkeypatch):
    project = _minimal_project(tmp_path)
    fake = _fake_purelib(monkeypatch, tmp_path)
    target = tmp_path / "real-pkg"
    target.mkdir()
    (target / "__init__.py").write_text("")
    (fake / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked dependencies: linked"):
        build_standalone(project, tmp_path / "bundle")


def test_standalone_bundle_is_deterministic_and_drops_stale_pynext_files(tmp_path, monkeypatch):
    project = _minimal_project(tmp_path)
    # The installed pynext copy carries a file the source tree no longer
    # has; overlaying the source used to leave it in the bundle.
    _fake_purelib(monkeypatch, tmp_path, pynext_extra={"stale_ghost.py": "# deleted from source\n"})
    # Bundles are ~126MB each: hash and drop the first before building
    # the second so only one is on disk at a time.
    build_standalone(project, tmp_path / "bundle-a")
    assert not (_site_packages(tmp_path / "bundle-a") / "pynext" / "stale_ghost.py").exists()
    hashes_a = _tree_hashes(tmp_path / "bundle-a")
    shutil.rmtree(tmp_path / "bundle-a")
    build_standalone(project, tmp_path / "bundle-b")
    assert not (_site_packages(tmp_path / "bundle-b") / "pynext" / "stale_ghost.py").exists()
    assert _tree_hashes(tmp_path / "bundle-b") == hashes_a


def test_standalone_runs_after_relocation_and_refuses_overwrite(tmp_path):
    project = tmp_path / "source"
    app = project / "app"
    app.mkdir(parents=True)
    (app / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "bundled")\n')
    write_manifest(project)
    output = tmp_path / "bundle"
    build_standalone(project, output)
    with pytest.raises(FileExistsError):
        build_standalone(project, output)
    relocated = tmp_path / "relocated"
    shutil.move(output, relocated)
    shutil.rmtree(project)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen([str(relocated / "run"), "--port", "0"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env)
    try:
        # A cold standalone start imports the bundled interpreter and app;
        # under a loaded machine that can take well over 15s.
        assert select.select([process.stdout], [], [], 60)[0], "standalone server did not start"
        line = process.stdout.readline()
        assert "PyNext listening on http://" in line, line
        address = line.split("http://", 1)[1].strip()
        with urllib.request.urlopen("http://" + address + "/", timeout=5) as response:
            assert response.status == 200 and b"bundled" in response.read()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

def _dist_info(purelib, name, version, top_levels):
    """Create a minimal *.dist-info for a fake package."""
    info = purelib / f"{name}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n", encoding="utf-8")
    (info / "top_level.txt").write_text("\n".join(top_levels) + "\n", encoding="utf-8")
    (info / "RECORD").write_text("".join(f"{top}/__init__.py,,\n" for top in top_levels),
                                 encoding="utf-8")
    return info


def _fake_purelib_with_package(monkeypatch, tmp_path):
    """Fake purelib with a versioned package, a plain module and a dropped symlink."""
    fake = _fake_purelib(monkeypatch, tmp_path)
    pkg = fake / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# fake\n")
    target = tmp_path / "real-file.py"
    target.write_text("# real\n")
    (pkg / "linked.py").symlink_to(target)
    _dist_info(fake, "fakepkg", "1.2.3", ["fakepkg"])
    (fake / "plain.py").write_text("# plain\n")
    return fake


def test_standalone_writes_bundle_manifest(tmp_path, monkeypatch):
    project = _minimal_project(tmp_path)
    (project / ".env").write_text("PYNEXT_FOO=bar\n", encoding="utf-8")
    (project / ".env.production").write_text("PYNEXT_FOO=prod\n", encoding="utf-8")
    (project / ".env.local").write_text("PYNEXT_SECRET=local\n", encoding="utf-8")
    (project / ".env.production.local").write_text("PYNEXT_SECRET=prod-local\n", encoding="utf-8")
    _fake_purelib_with_package(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    build_standalone(project, output)
    info = json.loads((output / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert info["version"] == 1
    assert info["pynext_version"] == "0.1.2"
    assert info["python_version"] == platform.python_version()
    assert info["python_implementation"] == "CPython"
    assert len(info["build_id"]) == 16 and all(c in "0123456789abcdef" for c in info["build_id"])
    assert "ad" not in info["build_id"]
    assert info["esbuild_version"] is None  # minimal project has no islands
    assert {"name": "fakepkg", "version": "1.2.3"} in info["copied_packages"]
    assert {"name": "plain", "version": None} in info["copied_packages"]
    assert info["excluded_packages"] == []
    assert info["env_files"] == [".env", ".env.production"]
    assert info["files"] > 0 and info["bytes"] > 0
    # Dropped nested symlinks are named bundle-relative, not silently lost
    # (the stdlib copy may drop further symlinks of its own).
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    assert f"runtime/lib/{version}/site-packages/fakepkg/linked.py" in info["dropped_symlinks"]
    # The bundle itself carries the env files; local overrides never ship.
    app_dir = output / "app"
    assert (app_dir / ".env").is_file() and (app_dir / ".env.production").is_file()
    assert not (app_dir / ".env.local").exists()
    assert not (app_dir / ".env.production.local").exists()


def test_build_id_is_deterministic_and_avoids_ad(monkeypatch):
    import pynext.standalone as standalone

    built = {"sources": {"app/page.py": "a" * 64, "app/layout.py": "b" * 64}}
    first = _build_id(built)
    assert first == _build_id(built)
    assert len(first) == 16 and all(c in "0123456789abcdef" for c in first)
    # A build id whose raw hash starts with "ad" must be re-hashed.
    class _FakeHash:
        def __init__(self, digest):
            self._digest = digest

        def update(self, *args):
            pass

        def hexdigest(self):
            return self._digest

    digests = iter(["ad" + "0" * 62, "f" * 64])
    monkeypatch.setattr(standalone.hashlib, "sha256",
                        lambda *args, **kwargs: _FakeHash(next(digests)))
    assert _build_id(built) == "f" * 16


def test_copy_site_packages_excludes_and_warns_for_unknown(tmp_path, caplog):
    purelib = tmp_path / "purelib"
    purelib.mkdir()
    (purelib / "plain.py").write_text("# plain\n")
    (purelib / "other.py").write_text("# other\n")
    packages = tmp_path / "packages"
    packages.mkdir()
    with caplog.at_level(logging.WARNING, logger="pynext"):
        result = _copy_site_packages(purelib, packages, excludes=frozenset({"plain", "missing-pkg"}))
    assert not (packages / "plain.py").exists()
    assert (packages / "other.py").exists()
    assert result["excluded"] == ["plain"]
    assert {"name": "other", "version": None} in result["copied"]
    assert "missing-pkg" in caplog.text


def test_standalone_exclude_is_recorded_in_manifest(tmp_path, monkeypatch):
    project = _minimal_project(tmp_path)
    fake = _fake_purelib(monkeypatch, tmp_path)
    (fake / "plain.py").write_text("# plain\n")
    (fake / "other.py").write_text("# other\n")
    output = tmp_path / "bundle"
    build_standalone(project, output, excludes=("plain",))
    info = json.loads((output / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert info["excluded_packages"] == ["plain"]
    assert not (_site_packages(output) / "plain.py").exists()
    assert (_site_packages(output) / "other.py").exists()
    with pytest.raises(ValueError, match="cannot exclude the pynext package"):
        build_standalone(project, tmp_path / "bundle2", excludes=("pynext",))


def test_read_and_log_bundle_manifest(tmp_path, caplog):
    from pynext.standalone import log_bundle_info, read_bundle_manifest

    app_dir = tmp_path / "bundle" / "app"
    app_dir.mkdir(parents=True)
    assert read_bundle_manifest(app_dir) is None
    log_bundle_info(app_dir)  # not a bundle: silent no-op
    manifest = {
        "version": 1, "pynext_version": "0.1.2", "python_version": platform.python_version(),
        "build_id": "0123456789abcdef", "copied_packages": [{"name": "x", "version": "1"}],
        "files": 3, "bytes": 42,
    }
    (tmp_path / "bundle" / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="pynext"):
        log_bundle_info(app_dir)
    assert "Standalone bundle: pynext 0.1.2" in caplog.text
    assert "0123456789abcdef" in caplog.text
    # A bundle built with another Python warns instead of failing.
    manifest["python_version"] = "3.99.0"
    (tmp_path / "bundle" / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pynext"):
        log_bundle_info(app_dir)
    assert "built with Python 3.99.0" in caplog.text
    # A corrupt manifest warns and does not raise into serve().
    (tmp_path / "bundle" / "bundle-manifest.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pynext"):
        log_bundle_info(app_dir)
    assert "unreadable" in caplog.text


def test_standalone_start_logs_bundle_info(tmp_path):
    # A bundle built from a fake purelib cannot start (real runtime
    # dependencies such as python-multipart are missing), so this test
    # builds from the real environment like the relocation test does.
    project = _minimal_project(tmp_path)
    output = tmp_path / "bundle"
    build_standalone(project, output)
    relocated = tmp_path / "relocated"
    shutil.move(output, relocated)
    shutil.rmtree(project)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen([str(relocated / "run"), "--port", "0"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env)
    # Lines are pumped into a queue by a reader thread: select() on a
    # buffered text stream would miss a line already sitting in the
    # reader's buffer (the listening and bundle lines often arrive in
    # one pipe read).
    lines = queue.Queue()

    def _pump():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=_pump, daemon=True).start()
    try:
        # A cold standalone start imports the bundled interpreter and app;
        # under a loaded machine that can take well over 15s.
        line = lines.get(timeout=60)
        assert line is not None and "PyNext listening on http://" in line, line
        info = json.loads((relocated / "bundle-manifest.json").read_text(encoding="utf-8"))
        bundle_line = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                candidate = lines.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if candidate is None:
                break
            if "Standalone bundle:" in candidate:
                bundle_line = candidate
                break
        assert bundle_line is not None, "standalone server did not log its bundle manifest"
        assert info["build_id"] in bundle_line and platform.python_version() in bundle_line
        address = line.split("http://", 1)[1].strip()
        with urllib.request.urlopen("http://" + address + "/", timeout=5) as response:
            assert response.status == 200 and b"bundled" in response.read()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
