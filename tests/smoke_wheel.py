"""Verify built distributions and run the example using an isolated wheel."""
import importlib.util
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("PYNEXT_DIST_DIR", ROOT / "dist"))
wheels = list(DIST.glob("pynext_framework-*.whl"))
sources = list(DIST.glob("pynext_framework-*.tar.gz"))
if len(wheels) != 1 or len(sources) != 1:
    raise RuntimeError("Expected one PyNext wheel and one source distribution")

with ZipFile(wheels[0]) as archive:
    names = archive.namelist()
    if archive.testzip() is not None:
        raise RuntimeError("Corrupt wheel")
    if not any(name.endswith("/LICENSE") for name in names):
        raise RuntimeError("Wheel is missing its license")
    if not any(name.endswith("third_party/NEXTJS.md") for name in names):
        raise RuntimeError("Wheel is missing third-party notices")
    if not any(name.endswith("pynext/browser/runtime.js") for name in names):
        raise RuntimeError("Wheel is missing its browser runtime")

with tarfile.open(sources[0]) as archive:
    names = archive.getnames()
    if not any(name.endswith("/LICENSE") for name in names):
        raise RuntimeError("Source distribution is missing its license")
    if not any(name.endswith("/third_party/NEXTJS.md") for name in names):
        raise RuntimeError("Source distribution is missing third-party notices")

with tempfile.TemporaryDirectory(prefix="pynext-wheel-check-") as directory:
    subprocess.run(["uv", "pip", "install", "--python", sys.executable, "--no-deps",
                    "--target", directory, str(wheels[0])], cwd=ROOT, check=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = directory
    check = ("from pathlib import Path; import pynext, sys; "
             "assert Path(pynext.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())")
    subprocess.run([sys.executable, "-c", check, directory], cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "-m", "pynext.cli", "build", "--dir", str(ROOT / "examples/basic")],
                   cwd=ROOT, env=env, check=True)
    # Serve the example from the installed wheel as well as building it. Reuse
    # the browser smoke runner's bounded port retry and HTTP checks without
    # installing or launching a browser in this packaging test.
    spec = importlib.util.spec_from_file_location("pynext_browser_smoke", ROOT / "tests/browser/run_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as log:
        server = None
        try:
            server, port = smoke._start_server(
                lambda port: [sys.executable, "-m", "pynext.cli", "start", "--dir", str(ROOT / "examples/basic"),
                              "--host", "127.0.0.1", "--port", str(port)],
                log, popen=lambda *args, **kwargs: subprocess.Popen(*args, env=env, **kwargs),
            )
            smoke.verify_http(f"http://127.0.0.1:{port}")
        except Exception:
            log.seek(0)
            print(log.read(), file=sys.stderr, flush=True)
            raise
        finally:
            if server is not None:
                smoke._terminate(server)
print("Source distribution and isolated wheel build and HTTP serving verified")
