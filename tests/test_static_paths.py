"""S-P0-6: aiohttp-style triple defense for static file / media serving.

Covers ``pynext.static.resolve_static_path`` / ``validate_static_request_path``
(unit level, including symlink escapes) and the HTTP serving boundaries
(public assets, ``/_pynext/image`` ``src``) in both ASGI and native (Axum)
modes. The Rust fast path serves only in-memory assets through a HashMap
lookup after ``static_path`` (``lib.rs``) rejects ``..``/``//``/backslash/
control characters and %-encoded ``/``/``\\``/NUL, so no disk change was
needed there — the native cases below pin that behavior.
"""
import asyncio
from pathlib import Path

import httpx
import pytest

from pynext import create_app, resolve_static_path, validate_static_request_path
from pynext.build import write_manifest
from pynext.server import start_native
from pynext.static import StaticPathError

# --- unit: layer (1) normpath + prefix, layer (2) absolute/UNC rejection ---


@pytest.mark.parametrize("bad", [
    "/../secret.txt",
    "/a/../../secret.txt",
    "../secret.txt",
    "a/../../secret.txt",
    "/a/../..",
    "/..",
    "..",
    "/./../secret.txt",
    "/a/./../../secret.txt",
])
def test_resolve_rejects_dotdot_escape(tmp_path, bad):
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(StaticPathError):
        resolve_static_path(root, bad)


@pytest.mark.parametrize("bad", [
    "//server/share/secret.txt",
    "\\\\server\\share\\secret.txt",
    "\\server\\share\\secret.txt",
    "C:\\Windows\\secret.txt",
    "C:/Windows/secret.txt",
    "/C:/Windows/secret.txt",
    "/C:\\Windows\\secret.txt",
    "..\\..\\secret.txt",
    "a\\..\\..\\secret.txt",
    "/a\\b.txt",
])
def test_resolve_rejects_absolute_and_unc_paths(tmp_path, bad):
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(StaticPathError):
        resolve_static_path(root, bad)


@pytest.mark.parametrize("bad", [
    "/a\x00b.txt",
    "/a\nb.txt",
    "/a\x7fb.txt",
    "",
    "C:",
])
def test_validate_request_path_rejects_bad_inputs(bad):
    # "" and "C:" are not asset paths (no leading slash / drive-relative).
    # Note "/" is valid: it passes validation and simply misses the asset
    # dictionary, so the home page still routes normally.
    with pytest.raises(StaticPathError):
        validate_static_request_path(bad)


def test_validate_request_path_accepts_root():
    assert validate_static_request_path("/") == "/"


@pytest.mark.parametrize("good", [
    "/logo.txt",
    "/styles/site.css",
    "/a/b/c.png",
    "/한 글.txt",
    "/what#.bin",
    "/what%2F.bin",
    "/a..b/c.txt",
    "/.../d.txt",
])
def test_validate_request_path_accepts_normal_paths(good):
    assert validate_static_request_path(good) == good


def test_resolve_accepts_normal_paths(tmp_path):
    root = tmp_path / "media"
    (root / "a").mkdir(parents=True)
    (root / "a" / "b.txt").write_text("hello")
    assert resolve_static_path(root, "/a/b.txt") == (root / "a" / "b.txt").resolve()
    assert resolve_static_path(root, "a/b.txt") == (root / "a" / "b.txt").resolve()
    # A missing id still resolves inside the root; the caller answers 404.
    assert resolve_static_path(root, "/missing.txt") == (root / "missing.txt").resolve()
    assert resolve_static_path(root, "/") == root.resolve()


# --- unit: layer (3) resolve() + relative_to, symlink policy ---


def test_resolve_rejects_symlink_escape_by_default(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    try:
        (root / "link.txt").symlink_to(outside / "secret.txt")
        (root / "linkdir").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable")
    with pytest.raises(StaticPathError):
        resolve_static_path(root, "/link.txt")
    with pytest.raises(StaticPathError):
        resolve_static_path(root, "/linkdir/secret.txt")
    # A symlink that stays inside the root resolves fine.
    (root / "real.txt").write_text("real")
    (root / "inner.txt").symlink_to(root / "real.txt")
    assert resolve_static_path(root, "/inner.txt") == (root / "real.txt").resolve()


def test_resolve_break_symlink_sandbox_opt_in(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    try:
        (root / "link.txt").symlink_to(outside / "secret.txt")
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable")
    # aiohttp `_break_symlink_sandbox=True` semantics: layers (1)+(2) only,
    # symlinks are followed — the caller explicitly accepts the risk. The
    # helper returns the unresolved (normpath) path; opening it follows
    # the symlink to the outside file.
    resolved = resolve_static_path(root, "/link.txt", break_symlink_sandbox=True)
    assert resolved == root.resolve() / "link.txt"
    assert resolved.read_text() == "secret"


def test_resolve_symlink_root_itself(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "linkroot"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable")
    # The root is resolved first, so a symlinked root points at its target
    # and files under it are inside the (resolved) root.
    assert resolve_static_path(link, "/secret.txt") == (outside / "secret.txt").resolve()


# --- HTTP: public assets + image src, ASGI and native modes ---


def make_project(root):
    (root / "app").mkdir()
    (root / "app" / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n'
    )
    public = root / "public"
    public.mkdir()
    (public / "logo.txt").write_bytes(b"public asset")
    (public / "styles").mkdir()
    (public / "styles" / "site.css").write_bytes(b"body { color: navy; }")
    write_manifest(root)
    return create_app(root, production=True)


async def exercise(client):
    # Normal serving still works.
    assert (await client.get("/logo.txt")).content == b"public asset"
    assert (await client.get("/styles/site.css")).content == b"body { color: navy; }"
    assert (await client.get("/")).status_code == 200
    # Path traversal via the URL: encoded dots decode to ".." segments and
    # are rejected before any asset lookup (400 from path validation).
    for url in ("/%2e%2e/%2e%2e/etc/passwd", "/%2e%2e/logo.txt",
                "/styles/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2fetc%2fpasswd"):
        assert (await client.get(url)).status_code in {400, 404}, url
    # Absolute / UNC / drive-letter image sources are rejected, never read.
    for src in ("%2F%2Fserver%2Fshare%2Fsecret.txt",  # //server/share/secret.txt
                "%5C%5Cserver%5Cshare%5Csecret.txt",  # \\server\share\secret.txt
                "C%3A%5CWindows%5Csecret.txt",          # C:\Windows\secret.txt
                "C%3A%2FWindows%2Fsecret.txt",          # C:/Windows/secret.txt
                "%2FC%3A%2FWindows%2Fsecret.txt",       # /C:/Windows/secret.txt
                "..%2F..%2Fsecret.txt",
                "%2E%2E%2Fsecret.txt"):
        response = await client.get(f"/_pynext/image?src={src}&w=20")
        assert response.status_code == 404, (src, response.status_code)
    # A traversal source for a missing asset is still just a 404.
    assert (await client.get("/_pynext/image?src=%2Fmissing.png&w=20")).status_code == 404


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_static_traversal_blocked_http(tmp_path, mode):
    app = make_project(tmp_path)

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                await exercise(client)
        finally:
            if server:
                await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def test_media_style_roundtrip_uses_resolver(tmp_path):
    """The /api/media (POST/GET) pattern: store under the media root by a
    client-supplied id, serve only through resolve_static_path."""
    media_root = tmp_path / "media"
    media_root.mkdir()
    stored = resolve_static_path(media_root, "/abc123.png")
    stored.write_bytes(b"png-bytes")
    assert resolve_static_path(media_root, "/abc123.png").read_bytes() == b"png-bytes"
    with pytest.raises(StaticPathError):
        resolve_static_path(media_root, "/../secret.txt")
    with pytest.raises(StaticPathError):
        resolve_static_path(media_root, "//server/share/x.png")
