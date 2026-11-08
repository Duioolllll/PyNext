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
