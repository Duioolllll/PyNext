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
# PYNEXT_WHEEL selects one wheel file (used by the Wheels workflow, where
# each job builds exactly one platform wheel). Otherwise dist/ must hold
# exactly one wheel. PYNEXT_SKIP_SDIST=1 skips the source-distribution
# checks (wheel jobs build no sdist).
selected_wheel = os.environ.get("PYNEXT_WHEEL")
if selected_wheel:
    wheels = [Path(selected_wheel)]
    if not wheels[0].is_file():
        raise RuntimeError(f"PYNEXT_WHEEL does not exist: {selected_wheel}")
else:
    wheels = list(DIST.glob("pynext_framework-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected one PyNext wheel")
skip_sdist = os.environ.get("PYNEXT_SKIP_SDIST") == "1"
if skip_sdist:
    sources = []
else:
    sources = list(DIST.glob("pynext_framework-*.tar.gz"))
    if len(sources) != 1:
        raise RuntimeError("Expected one PyNext source distribution")

# The wheel filename is pynext_framework-<version>-<python>-<abi>-<platform>.whl.
# Linux wheels must be manylinux-tagged: a plain linux_x86_64 wheel only
# installs on the distro it was built on, which defeats D1. Enforced only
# when PYNEXT_REQUIRE_MANYLINUX=1 so the main CI's local `uv build` check
# keeps its existing behaviour.
parts = wheels[0].name.split("-")
if len(parts) != 5 or parts[0] != "pynext_framework":
    raise RuntimeError(f"Unexpected wheel filename: {wheels[0].name}")
platform_tag = parts[4].removesuffix(".whl")
if os.environ.get("PYNEXT_REQUIRE_MANYLINUX") == "1" and not platform_tag.startswith("manylinux"):
    raise RuntimeError(f"Expected a manylinux wheel, got platform tag {platform_tag}")

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
    for suffix in ("pynext/py.typed", "pynext/__init__.pyi", "pynext/http.pyi", "pynext/types.py"):
        if not any(name.endswith(suffix) for name in names):
            raise RuntimeError(f"Wheel is missing its public typing contract: {suffix}")

if not skip_sdist:
    with tarfile.open(sources[0]) as archive:
        names = archive.getnames()
        if not any(name.endswith("/LICENSE") for name in names):
            raise RuntimeError("Source distribution is missing its license")
        if not any(name.endswith("/third_party/NEXTJS.md") for name in names):
            raise RuntimeError("Source distribution is missing third-party notices")
        if not any(name.endswith("/python/pynext/py.typed") for name in names):
            raise RuntimeError("Source distribution is missing py.typed")

with tempfile.TemporaryDirectory(prefix="pynext-wheel-check-") as directory:
    venv = Path(directory) / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=ROOT, check=True)
    wheel_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(["uv", "pip", "install", "--python", str(wheel_python), str(wheels[0])],
                   cwd=ROOT, check=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    check = ("from pathlib import Path; import pynext, sys; "
             "assert Path(pynext.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())")
    subprocess.run([str(wheel_python), "-c", check, str(venv)], cwd=ROOT, env=env, check=True)
    subprocess.run([str(wheel_python), "-m", "pynext.cli", "build", "--dir", str(ROOT / "examples/basic")],
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
                lambda port: [str(wheel_python), "-m", "pynext.cli", "start", "--dir", str(ROOT / "examples/basic"),
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
if skip_sdist:
    print("Isolated wheel build and HTTP serving verified")
else:
    print("Source distribution and isolated wheel build and HTTP serving verified")
