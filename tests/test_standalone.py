import os
import select
import shutil
import subprocess
import sys
import urllib.request

import pytest

from pynext.build import write_manifest
from pynext.standalone import build_standalone

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="standalone builds currently support Linux")


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
