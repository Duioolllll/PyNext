"""A release must identify the same version in Python, Cargo and the binary."""
from importlib.metadata import version
from pathlib import Path
import tomllib

import pynext
from pynext import _native


def test_installed_python_and_native_versions_match_distribution():
    assert pynext.__version__ == version("pynext-framework")
    assert _native.__version__ == pynext.__version__


def test_workspace_and_locked_crates_share_project_version():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    workspace = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]
    assert workspace["package"]["version"] == project
    names = set()
    for member in workspace["members"]:
        package = tomllib.loads((root / member / "Cargo.toml").read_text())["package"]
        assert package["version"] == {"workspace": True}
        names.add(package["name"])
    locked = tomllib.loads((root / "Cargo.lock").read_text())["package"]
    assert {package["name"]: package["version"] for package in locked if package["name"] in names} == {
        name: project for name in names
    }
