import json
import os
import sys

import pytest

from pynext import cli
from pynext.build import write_manifest
from pynext.env import (
    LoadedEnv,
    env_file_names,
    load_env,
    public_env_defines,
    public_env_vars,
)
from pynext.standalone import build_standalone
from pynext.watch import Watcher


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """load_env writes straight into os.environ, so restore it by hand."""
    before = dict(os.environ)
    yield
    for key in list(os.environ):
        if key not in before:
            del os.environ[key]
    for key, value in before.items():
        os.environ[key] = value
    import pynext.env as env_module
    env_module._OWNED.pop((str(tmp_path.resolve()), "production"), None)
    env_module._OWNED.pop((str(tmp_path.resolve()), "development"), None)
    env_module._OWNED.pop((str(tmp_path.resolve()), "test"), None)


def _project(root, *, page="from pynext import h, page\n@page()\ndef Page(): return h('p', {}, 'hi')\n"):
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "page.py").write_text(page)


# --- file priority -------------------------------------------------------

def test_env_file_priority(tmp_path):
    (tmp_path / ".env").write_text("A=from-env\nB=from-env\nC=from-env\nD=from-env\n")
    (tmp_path / ".env.production").write_text("B=from-prod\nC=from-prod\nD=from-prod\n")
    (tmp_path / ".env.local").write_text("C=from-local\nD=from-local\n")
    (tmp_path / ".env.production.local").write_text("D=from-prod-local\n")
    loaded = load_env(tmp_path, mode="production")
    assert loaded.files == (".env.production.local", ".env.local", ".env.production", ".env")
    assert loaded.values == {"A": "from-env", "B": "from-prod", "C": "from-local", "D": "from-prod-local"}
    assert os.environ["D"] == "from-prod-local"
    assert isinstance(loaded, LoadedEnv)


def test_env_file_priority_development_and_test_modes(tmp_path):
    assert env_file_names("development") == (
        ".env.development.local", ".env.local", ".env.development", ".env")
    assert env_file_names("production") == (
        ".env.production.local", ".env.local", ".env.production", ".env")
    # Test mode still reads .env.test.local but never .env.local.
    assert env_file_names("test") == (".env.test.local", ".env.test", ".env")
    (tmp_path / ".env.local").write_text("A=from-local\n")
    (tmp_path / ".env.test").write_text("B=from-test\n")
    (tmp_path / ".env.test.local").write_text("C=from-test-local\n")
    loaded = load_env(tmp_path, mode="test")
    assert loaded.files == (".env.test.local", ".env.test")
    assert loaded.values == {"B": "from-test", "C": "from-test-local"}
    assert "A" not in os.environ


def test_process_environment_wins_over_env_files(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("A=from-file\nB=from-file\n")
    monkeypatch.setenv("A", "from-process")
    loaded = load_env(tmp_path, mode="production")
    assert os.environ["A"] == "from-process"
    assert os.environ["B"] == "from-file"
    assert loaded.applied == {"B": "from-file"}
    assert loaded.values == {"A": "from-file", "B": "from-file"}


def test_reload_refreshes_loader_owned_values_but_not_process_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("A=one\n")
    monkeypatch.setenv("B", "from-process")
    (tmp_path / ".env.local").write_text("B=from-file\n")
    load_env(tmp_path, mode="production")
    assert os.environ["A"] == "one" and os.environ["B"] == "from-process"
    env_file.write_text("A=two\n")
    load_env(tmp_path, mode="production")
    assert os.environ["A"] == "two"
    assert os.environ["B"] == "from-process"
    env_file.write_text("")
    load_env(tmp_path, mode="production")
    assert "A" not in os.environ


def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown env mode"):
        load_env(tmp_path, mode="staging")


# --- parsing ---------------------------------------------------------------

def test_env_parsing(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\n"
        "export EXPORTED=yes\n"
        "EMPTY=\n"
        "PLAIN=hello world # trailing comment\n"
        "HASHED=a#b\n"
        "SINGLE='literal $HOME \\n'\n"
        'DOUBLE="line1\\nline2 \\"quoted\\" $HOME"\n'
        "SPACED =  spaced value  \n"
    )
    loaded = load_env(tmp_path, mode="production")
    assert loaded.values == {
        "EXPORTED": "yes",
        "EMPTY": "",
        "PLAIN": "hello world",
        "HASHED": "a#b",
        "SINGLE": "literal $HOME \\n",
        "DOUBLE": 'line1\nline2 "quoted" $HOME',
        "SPACED": "spaced value",
    }


def test_env_multiline_double_quoted_value(tmp_path):
    (tmp_path / ".env").write_text('MULTI="one\ntwo\nthree"\nAFTER=ok\n')
    loaded = load_env(tmp_path, mode="production")
    assert loaded.values == {"MULTI": "one\ntwo\nthree", "AFTER": "ok"}


@pytest.mark.parametrize("content", [
    "NO_EQUALS_HERE\n",
    "1BAD=value\n",
    "KEY='unterminated\n",
    'KEY="unterminated\n',
    "KEY='closed' junk\n",
])
def test_env_invalid_lines_rejected(tmp_path, content):
    (tmp_path / ".env").write_text(content)
    with pytest.raises(ValueError, match=r"\.env|line"):
        load_env(tmp_path, mode="production")


def test_env_symlink_rejected(tmp_path):
    target = tmp_path / "real.env"
    target.write_text("A=1\n")
    (tmp_path / ".env").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        load_env(tmp_path, mode="production")


def test_env_missing_files_are_a_noop(tmp_path):
    loaded = load_env(tmp_path, mode="production")
    assert loaded.files == () and loaded.values == {} and loaded.applied == {}


# --- PYNEXT_PUBLIC_ exposure rule ------------------------------------------

def test_public_env_vars_and_defines_only_expose_prefix(monkeypatch):
    monkeypatch.setenv("PYNEXT_PUBLIC_API_URL", "https://api.example.test")
    monkeypatch.setenv("PYNEXT_PUBLIC_FLAG", "on")
    monkeypatch.setenv("PYNEXT_SECRET", "do-not-expose")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    assert public_env_vars() == {
        "PYNEXT_PUBLIC_API_URL": "https://api.example.test",
        "PYNEXT_PUBLIC_FLAG": "on",
    }
    assert public_env_defines() == [
        '--define:process.env.PYNEXT_PUBLIC_API_URL="https://api.example.test"',
        '--define:process.env.PYNEXT_PUBLIC_FLAG="on"',
    ]


def test_public_value_with_quotes_is_json_escaped(monkeypatch):
    monkeypatch.setenv("PYNEXT_PUBLIC_MSG", 'say "hi"\nbye')
    assert public_env_defines() == [
        r'--define:process.env.PYNEXT_PUBLIC_MSG="say \"hi\"\nbye"',
    ]


def test_build_records_env_files_and_public_keys_not_values(tmp_path):
    _project(tmp_path)
    (tmp_path / ".env").write_text("PYNEXT_PUBLIC_API_URL=https://api.example.test\nSERVER_ONLY=topsecret\n")
    (tmp_path / ".env.production").write_text("PYNEXT_PUBLIC_FLAG=on\n")
    manifest = write_manifest(tmp_path)
    assert manifest["env_files"] == [".env.production", ".env"]
    assert manifest["public_env_keys"] == ["PYNEXT_PUBLIC_API_URL", "PYNEXT_PUBLIC_FLAG"]
    assert "topsecret" not in str(manifest) and "https://api.example.test" not in str(manifest)


def test_build_loads_env_for_static_rendering(tmp_path):
    _project(tmp_path, page=(
        "import os\nfrom pynext import h, page\n@page(render=\"ssg\")\n"
        "def Page(): return h('p', {}, os.environ.get('GREETING', 'missing'))\n"))
    (tmp_path / ".env").write_text("GREETING=hello-from-env\n")
    manifest = write_manifest(tmp_path)
    body = (tmp_path / ".pynext" / "static" / (manifest["static_pages"]["/"]["sha256"] + ".html")).read_bytes()
    assert b"hello-from-env" in body


def test_cli_build_uses_production_env_not_development(tmp_path, monkeypatch):
    # PR #22 P1: `pynext build` used to load the development env files
    # first (cli.py), poisoning os.environ so write_manifest's own
    # production load could not override the values — a variable already
    # in the process environment wins over every file. The built HTML
    # baked in the DEV values instead of the production ones.
    _project(tmp_path, page=(
        "import os\nfrom pynext import h, page\n@page(render=\"ssg\")\n"
        "def Page(): return h('p', {}, os.environ.get('GREETING', 'missing'))\n"))
    (tmp_path / ".env.development").write_text("GREETING=hello-dev\n")
    (tmp_path / ".env.production").write_text("GREETING=hello-prod\n")
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path)])
    cli.main()
    manifest = json.loads((tmp_path / ".pynext" / "manifest.json").read_text())
    body = (tmp_path / ".pynext" / "static" / (manifest["static_pages"]["/"]["sha256"] + ".html")).read_bytes()
    assert b"hello-prod" in body
    assert b"hello-dev" not in body


def test_island_bundle_inlines_public_only(tmp_path, esbuild, monkeypatch):
    _project(tmp_path)
    (tmp_path / "islands").mkdir()
    (tmp_path / "islands" / "counter.ts").write_text(
        "const url = process.env.PYNEXT_PUBLIC_API_URL;\n"
        "export function mount(el: HTMLElement) { el.textContent = url; }\n")
    (tmp_path / ".env").write_text(
        "PYNEXT_PUBLIC_API_URL=https://api.example.test\n"
        "SERVER_ONLY=topsecret-value\n")
    manifest = write_manifest(tmp_path)
    digest = manifest["client"]["bundles"]["counter"]
    bundle = (tmp_path / ".pynext" / "static" / f"{digest}.js").read_bytes()
    assert b"https://api.example.test" in bundle
    assert b"topsecret-value" not in bundle
    assert b"process.env.PYNEXT_PUBLIC_API_URL" not in bundle


def test_dev_create_app_loads_development_env(tmp_path):
    from pynext import create_app
    _project(tmp_path)
    (tmp_path / ".env.development").write_text("GREETING=hello-dev\n")
    (tmp_path / ".env.production").write_text("GREETING=hello-prod\n")
    create_app(tmp_path, production=False)
    assert os.environ["GREETING"] == "hello-dev"


# --- dev watcher -------------------------------------------------------------

def test_watcher_sees_env_file_changes(tmp_path):
    _project(tmp_path)
    (tmp_path / ".env").write_text("A=1\n")
    watcher = Watcher(tmp_path)
    assert any(str(path).endswith(".env") for path in watcher.watch_roots())
    (tmp_path / ".env").write_text("A=2\n")
    assert ".env" in Watcher.changed(watcher._baseline, watcher.snapshot())


# --- standalone --------------------------------------------------------------

def _fake_purelib(monkeypatch, tmp_path):
    """Point sysconfig's purelib at an empty dir, like test_standalone.py."""
    import sysconfig
    fake = tmp_path / "fake-purelib"
    fake.mkdir()
    real_get_path = sysconfig.get_path
    monkeypatch.setattr(sysconfig, "get_path",
                        lambda name, *args, **kwargs: str(fake) if name == "purelib"
                        else real_get_path(name, *args, **kwargs))
    return fake


def test_standalone_copies_env_but_never_local(tmp_path, monkeypatch):
    project = tmp_path / "source"
    _project(project)
    (project / ".env").write_text("PYNEXT_PUBLIC_API_URL=https://api.example.test\n")
    (project / ".env.production").write_text("MODE=prod\n")
    (project / ".env.local").write_text("SECRET=local-secret\n")
    (project / ".env.production.local").write_text("SECRET=prod-local-secret\n")
    write_manifest(project)
    _fake_purelib(monkeypatch, tmp_path)
    output = tmp_path / "bundle"
    build_standalone(project, output)
    app_dir = output / "app"
    assert (app_dir / ".env").read_text() == "PYNEXT_PUBLIC_API_URL=https://api.example.test\n"
    assert (app_dir / ".env.production").read_text() == "MODE=prod\n"
    assert not (app_dir / ".env.local").exists()
    assert not (app_dir / ".env.production.local").exists()
