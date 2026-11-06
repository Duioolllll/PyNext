from types import SimpleNamespace

import pytest

import pynext.client as client


def test_windows_esbuild_uses_node_wrapper_without_a_shell(tmp_path, monkeypatch):
    wrapper = tmp_path / "node_modules" / "esbuild" / "bin" / "esbuild"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("// wrapper", encoding="utf-8")
    monkeypatch.delenv("PYNEXT_ESBUILD", raising=False)
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(client, "shutil", SimpleNamespace(which=lambda name: "node.exe" if name == "node" else None))
    assert client._esbuild(tmp_path) == ["node.exe", str(wrapper)]


def test_windows_explicit_batch_wrapper_is_rejected(tmp_path, monkeypatch):
    wrapper = tmp_path / "esbuild.cmd"
    wrapper.write_text("@echo off", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PYNEXT_ESBUILD", str(wrapper))
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(client, "shutil", SimpleNamespace(which=lambda name: None))
    with pytest.raises(ValueError, match="native esbuild executable"):
        client._esbuild(tmp_path)


def test_windows_global_npm_wrapper_uses_node_without_a_shell(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    wrapper = npm / "node_modules" / "esbuild" / "bin" / "esbuild"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("// wrapper", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.delenv("PYNEXT_ESBUILD", raising=False)
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    paths = {"node": "node.exe", "esbuild": str(npm / "esbuild.cmd")}
    monkeypatch.setattr(client, "shutil", SimpleNamespace(which=paths.get))
    assert client._esbuild(project) == ["node.exe", str(wrapper)]
