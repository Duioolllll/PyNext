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


TEMPLATE_FILES = {
    "basic": {"app/page.py", "README.md", ".gitignore"},
    "islands": {"app/page.py", "islands/counter.ts", "README.md", ".gitignore"},
    "api": {"app/page.py", "app/api/hello/route.py", "README.md", ".gitignore"},
}

GITIGNORE = ".pynext/\n__pycache__/\n*.pyc\n.env*\n*.pem\n*.key\n"


def _check_app(project, production=False):
    from pynext.build import write_manifest

    if production:
        write_manifest(project)
    app = create_app(project, production=production)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            home = await client.get("/")
            assert home.status_code == 200 and "Hello PyNext!" in home.text
            if (project / "app" / "api" / "hello" / "route.py").exists():
                api = await client.get("/api/hello")
                assert api.status_code == 200
                assert api.json() == {"message": "Hello from PyNext!"}
            if (project / "islands" / "counter.ts").exists():
                assert "Count: 0" in home.text
                assert "data-pynext-island" in home.text
        await app.isr.close()

    asyncio.run(run())


@pytest.mark.parametrize("template", ["basic", "islands", "api"])
def test_init_templates_create_expected_files(tmp_path, monkeypatch, template):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--template", template])
    cli.main()
    for relative in TEMPLATE_FILES[template]:
        assert (project / relative).is_file(), relative
    assert (project / "public").is_dir()
    assert (project / ".gitignore").read_text() == GITIGNORE
    assert "Next steps" in (project / "README.md").read_text()


@pytest.mark.parametrize("template", ["basic", "api"])
def test_init_templates_are_runnable(tmp_path, monkeypatch, template):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--template", template])
    cli.main()
    _check_app(project)


def test_init_islands_template_builds_and_runs(tmp_path, monkeypatch, esbuild):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--template", "islands"])
    cli.main()
    _check_app(project, production=True)


def test_init_no_gitignore(tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--no-gitignore"])
    cli.main()
    assert not (project / ".gitignore").exists()
    assert (project / "app" / "page.py").is_file()


def test_init_with_middleware(tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--with-middleware"])
    cli.main()
    assert (project / "middleware.py").is_file()
    app = create_app(project)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert "x-response-time-ms" in response.headers
        await app.isr.close()

    asyncio.run(run())


def test_init_unknown_template(tmp_path):
    from pynext.scaffold import init_project

    project = tmp_path / "project"
    with pytest.raises(ValueError, match="unknown template"):
        init_project(project, template="spa")
    assert not project.exists()


@pytest.mark.parametrize(
    ("template", "existing"),
    [("basic", ".gitignore"), ("islands", "islands/counter.ts"), ("api", "app/api/hello/route.py")],
)
def test_init_refuses_to_overwrite_template_files(tmp_path, monkeypatch, template, existing):
    target = tmp_path / existing
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("keep")
    monkeypatch.setattr(sys, "argv",
                        ["pynext", "init", "--dir", str(tmp_path), "--template", template])
    with pytest.raises(SystemExit):
        cli.main()
    assert target.read_text() == "keep"
    assert not (tmp_path / "app" / "page.py").exists()


def test_init_cli_rejects_unknown_template(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(tmp_path), "--template", "spa"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_init_prints_next_steps(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--template", "api"])
    cli.main()
    out = capsys.readouterr().out
    assert "(template: api)" in out
    assert "pynext dev" in out and "pynext build" in out and "pynext start" in out


def test_init_islands_prints_esbuild_note(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project), "--template", "islands"])
    cli.main()
    assert "Node.js and esbuild" in capsys.readouterr().out


def _fail_named_open(monkeypatch, name):
    from pathlib import Path

    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name == name:
            raise OSError("simulated disk failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)


@pytest.mark.parametrize(("template", "failing"), [("islands", "counter.ts"), ("api", "route.py")])
def test_init_failure_removes_template_dirs(tmp_path, monkeypatch, template, failing):
    from pynext.scaffold import init_project

    _fail_named_open(monkeypatch, failing)
    project = tmp_path / "fresh"
    with pytest.raises(OSError, match="simulated disk failure"):
        init_project(project, template=template)
    assert not project.exists()


@pytest.mark.parametrize("keep_parent", [False, True])
def test_init_failure_cleans_created_ancestors_only(tmp_path, monkeypatch, keep_parent):
    from pynext.scaffold import init_project

    parent = tmp_path / "a"
    if keep_parent:
        parent.mkdir()
        (parent / "keep.txt").write_text("keep", encoding="utf-8")
    project = parent / "b" / "project"
    _fail_readme_open(monkeypatch)
    with pytest.raises(OSError, match="simulated disk failure"):
        init_project(project)
    assert not (parent / "b").exists()
    if keep_parent:
        assert (parent / "keep.txt").read_text() == "keep"
    else:
        assert not parent.exists()


def test_init_gitignore_covers_secrets(tmp_path, monkeypatch):
    # A-6: the scaffold .gitignore must cover .env files (the
    # framework loads .env, so a secret there must not be committed)
    # and private keys.
    import fnmatch
    project = tmp_path / "project"
    monkeypatch.setattr(sys, "argv", ["pynext", "init", "--dir", str(project)])
    cli.main()
    patterns = (project / ".gitignore").read_text().splitlines()
    for secret in (".env", ".env.local", ".env.production", "cert.pem", "id.key"):
        assert any(fnmatch.fnmatch(secret, pattern) for pattern in patterns), secret
