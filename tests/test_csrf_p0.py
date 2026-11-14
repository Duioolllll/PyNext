"""P0 CSRF/session fixes (ROADMAP §1.2): S-P0-1, S-P0-2, S-P0-5, M-10, H-6.

- S-P0-1: one CSRF signing key shared by every worker (env / shared
  config), not one per worker.
- S-P0-2: masked tokens (BREACH) + constant-time comparison + Origin
  first with a trusted-origins allowlist + rotation of the CSRF secret
  at login.
- S-P0-5: session key rotation at login / privilege escalation
  (``cycle_key``) and full destruction at logout (``flush``).
- M-10: the non-supervisor entry points (``start_native``/``serve``)
  resolve and publish the shared key too.
- H-6: the middleware rotates the session id automatically when a
  request authenticates a previously anonymous (or different-user)
  session, so a login handler that forgets ``rotate_on_login()`` cannot
  leave a fixed id authenticated.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pynext import (SecurityConfig, SessionMiddleware, SQLiteSessionStore, create_app,
                    mask_csrf_token, new_csrf_token, sign_csrf_token, unmask_csrf_token,
                    validate_csrf)
from pynext.http import Request
from pynext.server import start_native
from pynext.sessions import Session

ROOT = Path(__file__).resolve().parents[1]


# --- S-P0-2(a): token masking -------------------------------------------


def test_mask_unmask_round_trip():
    token = new_csrf_token()
    masked = mask_csrf_token(token)
    assert masked != token
    assert "." in masked
    assert unmask_csrf_token(masked) == token


def test_masked_tokens_differ_every_call():
    token = new_csrf_token()
    masked = {mask_csrf_token(token) for _ in range(10)}
    assert len(masked) == 10
    assert all(unmask_csrf_token(value) == token for value in masked)


@pytest.mark.parametrize("bad", [
    "",
    "nodot",
    "a.b",                      # pad too short to decode
    "!!!.@@@",                  # not base64url
    "a.b.c",                    # payload is not base64url
    ".abc",
    "abc.",
    "a" * 43 + "." + "b" * 10,  # pad and payload lengths differ
])
def test_unmask_rejects_malformed(bad):
    assert unmask_csrf_token(bad) is None


def test_unmask_rejects_non_string():
    assert unmask_csrf_token(None) is None
    assert unmask_csrf_token(123) is None


def test_mask_csrf_token_rejects_bad_input():
    with pytest.raises(ValueError, match="must not be empty"):
        mask_csrf_token("")
    with pytest.raises(ValueError, match="must be ASCII"):
        mask_csrf_token("héllo-token-value-long-enough")
    with pytest.raises(TypeError, match="must be str"):
        mask_csrf_token(123)


def test_session_csrf_token_is_masked_and_validates():
    session = Session()
    first, second = session.csrf_token(), session.csrf_token()
    # Every call returns a different masked value over the same secret.
    assert first != second
    assert unmask_csrf_token(first) == session._csrf
    assert unmask_csrf_token(second) == session._csrf
    # The masked value validates against the session...
    request = Request("POST", "/api/echo",
                      headers=((b"x-csrf-token", first.encode()),),
                      state={"session": session})
    assert validate_csrf(request)
    # ...and so does the raw secret (back-compat for pre-masking clients).
    request = Request("POST", "/api/echo",
                      headers=((b"x-csrf-token", session._csrf.encode()),),
                      state={"session": session})
    assert validate_csrf(request)
    # A masked token for another secret does not.
    other = Session()
    request = Request("POST", "/api/echo",
                      headers=((b"x-csrf-token", other.csrf_token().encode()),),
                      state={"session": session})
    assert not validate_csrf(request)


def test_double_submit_accepts_masked_token():
    token = new_csrf_token()
    cookie = f"pynext_csrf={sign_csrf_token(token)}".encode()
    masked = mask_csrf_token(token)
    for submitted in (token, masked):
        headers = ((b"cookie", cookie), (b"x-csrf-token", submitted.encode()))
        assert validate_csrf(Request("POST", "/api/echo", headers=headers))
    # A masked token for another secret fails.
    headers = ((b"cookie", cookie),
               (b"x-csrf-token", mask_csrf_token(new_csrf_token()).encode()))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers))
    # A malformed masked value fails.
    headers = ((b"cookie", cookie), (b"x-csrf-token", b"not-a-masked-token.!!!"))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers))


# --- S-P0-2(b): constant-time comparison ---------------------------------


def test_validate_csrf_compares_in_constant_time(monkeypatch):
    import hmac as hmac_module

    from pynext import http as http_module

    calls = []

    class _HmacSpy:
        def __getattr__(self, name):
            return getattr(hmac_module, name)

        def compare_digest(self, a, b):
            calls.append((a, b))
            return hmac_module.compare_digest(a, b)

    monkeypatch.setattr(http_module, "hmac", _HmacSpy())
    token = new_csrf_token()
    headers = ((b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"x-csrf-token", token.encode()))
    assert validate_csrf(Request("POST", "/api/echo", headers=headers))
    # The token comparison itself (not just the cookie signature check)
    # goes through compare_digest.
    assert (token, token) in calls
    # Session branch compares the stored secret, not the masked value.
    session = Session()
    submitted = session.csrf_token()
    request = Request("POST", "/api/echo",
                      headers=((b"x-csrf-token", submitted.encode()),),
                      state={"session": session})
    assert validate_csrf(request)
    assert (session._csrf, session._csrf) in calls


# --- S-P0-2(c): Origin first + trusted origins ----------------------------


def _trusted_headers(token, *, origin, host=b"test"):
    headers = [(b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"x-csrf-token", token.encode()),
               (b"host", host)]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return tuple(headers)


def test_csrf_trusted_origin_allows_cross_origin_post():
    config = SecurityConfig(csrf_trusted_origins=("https://app.example.com",))
    token = new_csrf_token()
    request = Request("POST", "/api/echo",
                      headers=_trusted_headers(token, origin="https://app.example.com"))
    assert validate_csrf(request, config=config)


def test_csrf_trusted_origin_still_rejects_unlisted_origin():
    config = SecurityConfig(csrf_trusted_origins=("https://app.example.com",))
    token = new_csrf_token()
    request = Request("POST", "/api/echo",
                      headers=_trusted_headers(token, origin="https://evil.example.com"))
    assert not validate_csrf(request, config=config)


def test_csrf_trusted_origins_empty_by_default():
    token = new_csrf_token()
    request = Request("POST", "/api/echo",
                      headers=_trusted_headers(token, origin="https://app.example.com"))
    assert not validate_csrf(request)


def test_csrf_trusted_origin_still_requires_token():
    config = SecurityConfig(csrf_trusted_origins=("https://app.example.com",))
    token = new_csrf_token()
    headers = ((b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"x-csrf-token", b"wrong"),
               (b"host", b"test"),
               (b"origin", b"https://app.example.com"))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers), config=config)
    # ...and without any submitted token at all.
    headers = ((b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"host", b"test"),
               (b"origin", b"https://app.example.com"))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers), config=config)


def test_security_config_accepts_trusted_origins():
    config = SecurityConfig(
        csrf_trusted_origins=("https://APP.example.com", "http://localhost:3000"))
    assert config.csrf_trusted_origins == ("https://APP.example.com", "http://localhost:3000")
    token = new_csrf_token()
    # Host comparison is case-insensitive; an explicit default port matches.
    request = Request("POST", "/api/echo",
                      headers=_trusted_headers(token, origin="https://app.example.com"))
    assert validate_csrf(request, config=config)
    request = Request("POST", "/api/echo",
                      headers=_trusted_headers(token, origin="http://localhost:3000"))
    assert validate_csrf(request, config=config)


@pytest.mark.parametrize("bad", [
    "https://app.example.com",   # a bare string, not a sequence of origins
    ("not-a-url",),
    ("https://app.example.com/path",),
    ("ftp://app.example.com",),
    ("https://user@app.example.com",),
])
def test_security_config_rejects_invalid_trusted_origin(bad):
    with pytest.raises((ValueError, TypeError)):
        SecurityConfig(csrf_trusted_origins=bad)


def test_security_config_rejects_samesite_none_without_secure():
    """A1-L8: SameSite=None requires Secure (RFC 6265bis) — browsers
    reject the cookie otherwise and CSRF protection silently breaks.
    The session cookie already enforces this; the CSRF cookie config
    must too, instead of relying on `pynext check`."""
    with pytest.raises(ValueError, match="requires csrf_cookie_secure=True"):
        SecurityConfig(csrf_cookie_samesite="None", csrf_cookie_secure=False)
    # SameSite=None with Secure stays legal, as do Lax/Strict either way.
    assert SecurityConfig(csrf_cookie_samesite="None",
                          csrf_cookie_secure=True).csrf_cookie_samesite == "None"
    assert SecurityConfig(csrf_cookie_samesite="Lax",
                          csrf_cookie_secure=False).csrf_cookie_samesite == "Lax"
    assert SecurityConfig(csrf_cookie_samesite="Strict",
                          csrf_cookie_secure=False).csrf_cookie_samesite == "Strict"


# --- S-P0-5: cycle_key / flush aliases ------------------------------------


def test_cycle_key_and_flush_aliases():
    session = Session({"user_id": "alice", "cart": [1]}, csrf="secret", token="t", version=1)
    session.cycle_key()
    assert session._rotate is True
    assert session["user_id"] == "alice"
    assert session["cart"] == [1]
    assert session._csrf != "secret"
    session.flush()
    assert dict(session) == {}
    assert session._invalidated is True
    # Rotating after a flush still fails; flushing again is a no-op.
    with pytest.raises(RuntimeError, match="invalidated"):
        session.cycle_key()
    session.flush()


# --- Session app flows: S-P0-2(d), S-P0-5, H-6 ----------------------------


def _write_session_app(tmp_path):
    api = tmp_path / "app" / "api"
    (api / "token").mkdir(parents=True)
    (api / "token" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"token": request.state["session"].csrf_token()})\n'
    )
    (api / "login").mkdir()
    (api / "login" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    session = request.state["session"]\n'
        '    session["user_id"] = "alice"\n'
        '    session["permissions"] = ["read"]\n'
        '    session.rotate_on_login()\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "login-no-rotate").mkdir()
    (api / "login-no-rotate" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    session = request.state["session"]\n'
        '    session["user_id"] = "alice"\n'
        '    session["permissions"] = ["read"]\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "login-bob-no-rotate").mkdir()
    (api / "login-bob-no-rotate" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    session = request.state["session"]\n'
        '    session["user_id"] = "bob"\n'
        '    session["permissions"] = ["read"]\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "escalate").mkdir()
    (api / "escalate" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n'
        '    session = request.state["session"]\n'
        '    session["permissions"] = ["read", "write"]\n'
        '    session.rotate_on_login()\n'
        '    return JSONResponse({"ok": True})\n'
    )
    (api / "private").mkdir()
    (api / "private" / "route.py").write_text(
        'from pynext import JSONResponse, require_auth, route\n@route("GET")\n@require_auth\n'
        'def get(request): return JSONResponse({"user": request.state["session"]["user_id"]})\n'
    )
    (api / "write").mkdir()
    (api / "write" / "route.py").write_text(
        'from pynext import JSONResponse, require_permission, route\n@route("POST")\n'
        '@require_permission("write")\n'
        'def post(): return JSONResponse({"ok": True})\n'
    )
    (api / "logout").mkdir()
    (api / "logout" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("POST")\n'
        'def post(request):\n    request.state["session"].invalidate()\n    return JSONResponse({"ok": True})\n'
    )


def _make_session_app(tmp_path):
    _write_session_app(tmp_path)
    return create_app(tmp_path,
                      middleware=[SessionMiddleware(
                          SQLiteSessionStore(tmp_path / "sessions.db"), secure=False)],
                      security=SecurityConfig(csrf_enabled=True))


def _asgi_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_login_rotates_csrf_secret(tmp_path):
    """S-P0-2(d): a CSRF token issued before login dies at login."""
    app = _make_session_app(tmp_path)

    async def run():
        try:
            async with _asgi_client(app) as client:
                old_token = (await client.get("/api/token")).json()["token"]
                old_cookie = client.cookies.get("pynext_session")
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": old_token})).status_code == 200
                new_cookie = client.cookies.get("pynext_session")
                assert new_cookie and new_cookie != old_cookie
                new_token = (await client.get("/api/token")).json()["token"]
                # The secret itself changed, not just the mask.
                assert unmask_csrf_token(old_token) != unmask_csrf_token(new_token)
                # The pre-login token is dead: login rotated the secret.
                assert (await client.post("/api/logout",
                                          headers={"x-csrf-token": old_token})).status_code == 403
                assert (await client.post("/api/logout",
                                          headers={"x-csrf-token": new_token})).status_code == 200
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_privilege_escalation_rotates_session(tmp_path):
    """S-P0-5: a privilege change cycles the key and keeps the data."""
    app = _make_session_app(tmp_path)

    async def run():
        try:
            async with _asgi_client(app) as client:
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": token})).status_code == 200
                first_cookie = client.cookies.get("pynext_session")
                assert (await client.get("/api/private")).json() == {"user": "alice"}
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/write",
                                          headers={"x-csrf-token": token})).status_code == 403
                assert (await client.post("/api/escalate",
                                          headers={"x-csrf-token": token})).status_code == 200
                second_cookie = client.cookies.get("pynext_session")
                assert second_cookie and second_cookie != first_cookie
                # Data survived the rotation; the new permission applies.
                assert (await client.get("/api/private")).json() == {"user": "alice"}
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/write",
                                          headers={"x-csrf-token": token})).status_code == 200
            # The pre-escalation id is dead.
            async with _asgi_client(app) as stale:
                stale.cookies.set("pynext_session", first_cookie, domain="test.local")
                assert (await stale.get("/api/private")).status_code == 401
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_session_fixation_blocked_without_explicit_rotation(tmp_path):
    """H-6: an id fixed before login must not become the authenticated
    session, even when the login handler forgets rotate_on_login()."""
    app = _make_session_app(tmp_path)

    async def run():
        try:
            # Attacker: obtain a session id.
            async with _asgi_client(app) as attacker:
                await attacker.get("/api/token")
                planted = attacker.cookies.get("pynext_session")
                assert planted
            # Victim: logs in with the planted id; the handler does NOT
            # call rotate_on_login() — the middleware must rotate anyway.
            async with _asgi_client(app) as victim:
                victim.cookies.set("pynext_session", planted, domain="test.local")
                token = (await victim.get("/api/token")).json()["token"]
                assert (await victim.post("/api/login-no-rotate",
                                          headers={"x-csrf-token": token})).status_code == 200
                new_cookie = victim.cookies.get("pynext_session")
                assert new_cookie and new_cookie != planted
                assert (await victim.get("/api/private")).json() == {"user": "alice"}
            # The planted id is dead: it never became an authenticated session.
            async with _asgi_client(app) as stale:
                stale.cookies.set("pynext_session", planted, domain="test.local")
                assert (await stale.get("/api/private")).status_code == 401
            # ...and the victim's new id works.
            async with _asgi_client(app) as authed:
                authed.cookies.set("pynext_session", new_cookie, domain="test.local")
                assert (await authed.get("/api/private")).json() == {"user": "alice"}
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_account_switch_rotates_session(tmp_path):
    """H-6: switching identity (alice -> bob) without an explicit rotation
    still cycles the id."""
    app = _make_session_app(tmp_path)

    async def run():
        try:
            async with _asgi_client(app) as client:
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": token})).status_code == 200
                first_cookie = client.cookies.get("pynext_session")
                assert (await client.get("/api/private")).json() == {"user": "alice"}
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/login-bob-no-rotate",
                                          headers={"x-csrf-token": token})).status_code == 200
                second_cookie = client.cookies.get("pynext_session")
                assert second_cookie and second_cookie != first_cookie
                assert (await client.get("/api/private")).json() == {"user": "bob"}
            async with _asgi_client(app) as stale:
                stale.cookies.set("pynext_session", first_cookie, domain="test.local")
                assert (await stale.get("/api/private")).status_code == 401
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_authenticated_requests_do_not_rotate(tmp_path):
    """H-6: a session that keeps the same identity is left alone."""
    app = _make_session_app(tmp_path)

    async def run():
        try:
            async with _asgi_client(app) as client:
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": token})).status_code == 200
                cookie = client.cookies.get("pynext_session")
                for _ in range(3):
                    response = await client.get("/api/private")
                    assert response.status_code == 200
                    assert client.cookies.get("pynext_session") == cookie
                    assert "pynext_session" not in response.headers.get("set-cookie", "")
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_logout_flush_destroys_session(tmp_path):
    """S-P0-5: logout flushes data and id; the old id never works again."""
    app = _make_session_app(tmp_path)

    async def run():
        try:
            async with _asgi_client(app) as client:
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/login",
                                          headers={"x-csrf-token": token})).status_code == 200
                cookie = client.cookies.get("pynext_session")
                assert (await client.get("/api/private")).status_code == 200
                token = (await client.get("/api/token")).json()["token"]
                assert (await client.post("/api/logout",
                                          headers={"x-csrf-token": token})).status_code == 200
            async with _asgi_client(app) as stale:
                stale.cookies.set("pynext_session", cookie, domain="test.local")
                assert (await stale.get("/api/private")).status_code == 401
        finally:
            await app.isr.close()
    asyncio.run(run())


# --- M-10 / S-P0-1: non-supervisor key sharing -----------------------------


def _write_csrf_app(tmp_path):
    token = tmp_path / "app" / "api" / "token"
    token.mkdir(parents=True)
    (token / "route.py").write_text(
        "from pynext import JSONResponse, new_csrf_token, route, set_csrf_cookie\n"
        "@route(\"GET\")\n"
        "async def get_token(request):\n"
        "    token = new_csrf_token()\n"
        "    return set_csrf_cookie(JSONResponse({\"token\": token}), token,\n"
        "                           config=request.security)\n",
        encoding="utf-8",
    )
    echo = tmp_path / "app" / "api" / "echo"
    echo.mkdir(parents=True)
    (echo / "route.py").write_text(
        "from pynext import JSONResponse, csrf_protect, route\n"
        "@route(\"POST\")\n"
        "@csrf_protect\n"
        "async def post_echo(request):\n"
        "    return JSONResponse({\"ok\": True}, status=201)\n",
        encoding="utf-8",
    )


def test_start_native_publishes_shared_csrf_key(tmp_path, monkeypatch):
    """M-10: a non-supervisor server resolves one shared key and publishes
    it, so processes spawned afterwards inherit it."""
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    _write_csrf_app(tmp_path)
    app = create_app(tmp_path)
    fallback_key = app.security.csrf_signing_key

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            return server.address
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())

    published = os.environ.get("PYNEXT_CSRF_SIGNING_KEY")
    assert published is not None and published.startswith("hex:")
    assert app.security.csrf_signing_key == bytes.fromhex(published[4:])
    assert app.security.csrf_signing_key != fallback_key
    # A fresh default config built afterwards agrees with the app.
    assert SecurityConfig().csrf_signing_key == app.security.csrf_signing_key
    # A token signed under the app's key validates under a fresh config.
    token = new_csrf_token()
    headers = ((b"cookie",
                f"pynext_csrf={sign_csrf_token(token, config=app.security)}".encode()),
               (b"x-csrf-token", token.encode()))
    assert validate_csrf(Request("POST", "/api/echo", headers=headers),
                         config=SecurityConfig())
    # A fresh process that inherits the published env agrees too.
    code = ("from pynext.http import SecurityConfig;"
            "print(SecurityConfig().csrf_signing_key.hex())")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=dict(os.environ), timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == app.security.csrf_signing_key.hex()


def test_start_native_adopts_explicit_key(tmp_path, monkeypatch):
    """M-10: an explicitly configured key wins and is published as-is."""
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    _write_csrf_app(tmp_path)
    app = create_app(tmp_path,
                     security=SecurityConfig(csrf_signing_key="explicit-key-long-enough"))

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            return server.address
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())

    assert os.environ["PYNEXT_CSRF_SIGNING_KEY"] == (
        "hex:" + b"explicit-key-long-enough".hex())
    assert app.security.csrf_signing_key == b"explicit-key-long-enough"


# --- M-10 live: two independent serve() processes --------------------------

pytestmark_live = pytest.mark.skipif(sys.platform not in {"linux", "darwin"},
                                    reason="live multi-process test uses Unix signals")

_SERVE_RUNNER = (
    "import asyncio\n"
    "import os\n"
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "root, address_path, key_path = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "\n"
    "from pynext import create_app\n"
    "from pynext.server import serve\n"
    "\n"
    "\n"
    "def on_ready(address):\n"
    "    Path(address_path).write_text(str(address), encoding='utf-8')\n"
    "    # start_native has already published the shared key by now.\n"
    "    Path(key_path).write_text(os.environ.get('PYNEXT_CSRF_SIGNING_KEY', ''),\n"
    "                             encoding='utf-8')\n"
    "\n"
    "\n"
    "async def main():\n"
    "    app = create_app(root)\n"
    "    await serve(app, '127.0.0.1', 0, on_ready=on_ready)\n"
    "\n"
    "\n"
    "asyncio.run(main())\n"
)


def _wait_for_file(path, proc, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"serve runner exited early with {proc.returncode}")
        if path.exists() and path.read_text(encoding="utf-8"):
            return path.read_text(encoding="utf-8")
        time.sleep(0.2)
    raise AssertionError(f"{path} was not written in {timeout}s")


def _stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytestmark_live
def test_two_serve_processes_share_key_generated_by_first(tmp_path):
    """M-10 end to end: the first serve() process generates the shared key
    with no key configured anywhere; a second, independently started
    serve() process that inherits the published env validates the first
    process's cookie. Before the fix the second process signed with its
    own per-process fallback key and the cross-process post failed 403.
    """
    _write_csrf_app(tmp_path)
    runner = tmp_path / "serve_runner.py"
    runner.write_text(_SERVE_RUNNER, encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYNEXT_CSRF_SIGNING_KEY", None)
    env["PYTHONPATH"] = str(ROOT / "python") + os.pathsep + env.get("PYTHONPATH", "")
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"

    address_a = tmp_path / "address_a.txt"
    key_a = tmp_path / "key_a.txt"
    proc_a = subprocess.Popen(
        [sys.executable, str(runner), str(tmp_path), str(address_a), str(key_a)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    proc_b = None
    try:
        address_a_text = _wait_for_file(address_a, proc_a)
        published = _wait_for_file(key_a, proc_a)
        assert published.startswith("hex:"), "first serve() must publish a shared key"
        # Second process, started independently, inherits the published key.
        env_b = dict(env)
        env_b["PYNEXT_CSRF_SIGNING_KEY"] = published
        address_b = tmp_path / "address_b.txt"
        key_b = tmp_path / "key_b.txt"
        proc_b = subprocess.Popen(
            [sys.executable, str(runner), str(tmp_path), str(address_b), str(key_b)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env_b)
        address_b_text = _wait_for_file(address_b, proc_b)
        assert _wait_for_file(key_b, proc_b) == published
        with httpx.Client(base_url=f"http://{address_a_text}", timeout=5) as client:
            issued = client.get("/api/token")
            assert issued.status_code == 200
            token = issued.json()["token"]
            cookie = issued.headers["set-cookie"].split(";")[0]
        # The cookie issued by A validates on B (and on A).
        with httpx.Client(base_url=f"http://{address_b_text}", timeout=5) as client:
            assert client.post("/api/echo", json={},
                               headers={"cookie": cookie,
                                        "x-csrf-token": token}).status_code == 201
        with httpx.Client(base_url=f"http://{address_a_text}", timeout=5) as client:
            assert client.post("/api/echo", json={},
                               headers={"cookie": cookie,
                                        "x-csrf-token": token}).status_code == 201
    finally:
        _stop(proc_a)
        if proc_b is not None:
            _stop(proc_b)
