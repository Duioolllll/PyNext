"""Check consumers against an installed wheel, outside the source tree."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="pynext-typing-") as directory:
        folder = Path(directory)
        venv = folder / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), f"{wheel}[database]"], check=True)
        for name in ("valid.py", "invalid.py"):
            (folder / name).write_text((ROOT / "tests/typing" / name).read_text(), encoding="utf-8")
        config = {"include": ["valid.py"], "typeCheckingMode": "strict", "pythonVersion": "3.13"}
        (folder / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
        env = os.environ.copy()
        for key in ("PYTHONPATH", "MYPYPATH"):
            env.pop(key, None)
        checks = [
            [sys.executable, "-m", "mypy", "--strict", "--no-incremental", "--python-executable", str(python)],
            [sys.executable, "-m", "pyright", "--pythonpath", str(python)],
        ]
        expected_lines = {i for i, line in enumerate((folder / "invalid.py").read_text().splitlines(), 1)
                          if "# type-error" in line}
        for command in checks:
            subprocess.run([*command, "valid.py"], cwd=folder, env=env, check=True)
            result = subprocess.run([*command, "invalid.py"], cwd=folder, env=env,
                                    capture_output=True, text=True)
            output = result.stdout + result.stderr
            if result.returncode == 0 or any(f":{line}:" not in output for line in expected_lines):
                raise RuntimeError(f"Checker failed to reject the expected invalid arguments:\n{output}")
    print("Installed wheel: mypy and pyright accept valid consumers and reject all invalid arguments")


if __name__ == "__main__":
    main()
