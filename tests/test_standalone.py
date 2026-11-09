import hashlib
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
    build_standalone(project, tmp_path / "bundle-a")
    build_standalone(project, tmp_path / "bundle-b")
    for bundle in (tmp_path / "bundle-a", tmp_path / "bundle-b"):
        assert not (_site_packages(bundle) / "pynext" / "stale_ghost.py").exists()
    assert _tree_hashes(tmp_path / "bundle-a") == _tree_hashes(tmp_path / "bundle-b")


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
