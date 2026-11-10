import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _restore_csrf_signing_key_env():
    """Snapshot PYNEXT_CSRF_SIGNING_KEY around every test.

    ``ensure_shared_csrf_signing_key`` publishes the shared key back
    into ``os.environ`` by design, and ``server.start_native`` calls it
    at startup — so a test that starts a native server would otherwise
    leave a ``hex:`` value behind for the rest of the pytest process.
    A stale env key makes in-process default configs disagree with the
    import-time ``DEFAULT_SECURITY`` and breaks unrelated CSRF tests
    depending on the per-process fallback, so restore the pre-test
    value here no matter what the test did to it. The shared-key
    resolution also points ``DEFAULT_SECURITY`` at the resolved key
    (so default-config signing agrees with an app's config); restore
    that too, for the same reason.
    """
    from pynext.http import DEFAULT_SECURITY

    old = os.environ.get("PYNEXT_CSRF_SIGNING_KEY")
    old_default_key = DEFAULT_SECURITY.csrf_signing_key
    yield
    if old is None:
        os.environ.pop("PYNEXT_CSRF_SIGNING_KEY", None)
    else:
        os.environ["PYNEXT_CSRF_SIGNING_KEY"] = old
    DEFAULT_SECURITY.csrf_signing_key = old_default_key


def page_html(content):
    """Expected fragment page including the shared navigation runtime."""
    from pynext.client import PREFIX, _digest, _runtime_bytes
    return f'{content}<script type="module" src="{PREFIX}{_digest(_runtime_bytes())}.js"></script>'


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
