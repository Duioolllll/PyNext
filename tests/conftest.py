import sys
from pathlib import Path

import pytest


@pytest.fixture
def esbuild(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        executables = list((root / "node_modules" / "@esbuild").glob("win32-*/esbuild.exe"))
        if len(executables) != 1:
            pytest.fail("Run npm ci to install the native Windows esbuild dependency")
        executable = executables[0]
    else:
        executable = root / "node_modules" / ".bin" / "esbuild"
    if not executable.is_file():
        pytest.fail("Run npm ci to install the islands build dependency")
    monkeypatch.setenv("PYNEXT_ESBUILD", str(executable))
