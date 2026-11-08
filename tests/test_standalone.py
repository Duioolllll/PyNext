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
