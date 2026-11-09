import asyncio
import sys

import httpx
import pytest

from pynext import cli, create_app


def test_init_creates_runnable_app(tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project)])
    cli.main()
    app = create_app(project)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 200 and "Hello PyNext!" in response.text
        await app.isr.close()
    asyncio.run(run())

    with pytest.raises(SystemExit):
        cli.main()
    assert "Hello PyNext!" in (project / "app" / "page.py").read_text()


def test_init_refuses_to_overwrite_any_target(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("keep")
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        cli.main()
    assert (tmp_path / "README.md").read_text() == "keep"
    assert not (tmp_path / "app" / "page.py").exists()


def _fail_readme_open(monkeypatch):
    from pathlib import Path

    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name == "README.md":
            raise OSError("simulated disk failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)


def test_init_failure_leaves_nothing_behind(tmp_path, monkeypatch):
    from pynext.scaffold import init_project

    _fail_readme_open(monkeypatch)
    project = tmp_path / "fresh"
    with pytest.raises(OSError, match="simulated disk failure"):
        init_project(project)
    assert not project.exists()


def test_init_failure_keeps_pre_existing_root_but_removes_created_app_dir(tmp_path, monkeypatch):
    from pynext.scaffold import init_project

    _fail_readme_open(monkeypatch)
    project = tmp_path / "existing"
    project.mkdir()
    (project / "keep.txt").write_text("keep")
    with pytest.raises(OSError, match="simulated disk failure"):
        init_project(project)
    assert (project / "keep.txt").read_text() == "keep"
    assert not (project / "app").exists()
    assert not (project / "README.md").exists()
