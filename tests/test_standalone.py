import hashlib
import logging
import os
import select
import shutil
import subprocess
import sys
import sysconfig
import urllib.request

import pytest

from pynext.build import write_manifest
from pynext.standalone import _copy_site_packages, build_standalone

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
        assert select.select([process.stdout], [], [], 15)[0], "standalone server did not start"
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
