import asyncio

import httpx
import pytest

from pynext import (JSONResponse, Request, Response, SecurityConfig, create_app, csrf_protect,
                    new_csrf_token, set_csrf_cookie, sign_csrf_token, validate_csrf)
from pynext.server import start_native


def make_project(root):
    api = root / "app" / "api"
    (api / "echo").mkdir(parents=True)
    (api / "open").mkdir(parents=True)
    (api / "token").mkdir(parents=True)
    (api / "echo" / "route.py").write_text(
        "from pynext import JSONResponse, csrf_protect, route\n"
        "@route(\"GET\")\n"
        "async def get_echo():\n"
        "    return JSONResponse({\"ok\": True})\n"
        "@route(\"POST\")\n"
        "@csrf_protect\n"
        "async def post_echo(request):\n"
        "    return JSONResponse({\"ok\": True}, status=201)\n"
    )
    (api / "open" / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"POST\")\n"
        "async def post_open(request):\n"
        "    return JSONResponse({\"ok\": True}, status=201)\n"
    )
    (api / "token" / "route.py").write_text(
        "from pynext import JSONResponse, new_csrf_token, route, set_csrf_cookie\n"
        "@route(\"GET\")\n"
        "async def get_token():\n"
        "    token = new_csrf_token()\n"
        "    return set_csrf_cookie(JSONResponse({\"token\": token}), token)\n"
    )


def run(coro):
    return asyncio.run(coro)


# --- Finding #39: CSRF protection ---


def test_csrf_tokens_are_unique_and_urlsafe():
    first, second = new_csrf_token(), new_csrf_token()
    assert first != second
    assert len(first) >= 32
    assert all(c.isalnum() or c in "-_" for c in first)


def test_set_csrf_cookie_defaults():
    response = set_csrf_cookie(Response("ok"), "token-value")
    cookie = [v for k, v in response.headers if k == b"set-cookie"]
    assert len(cookie) == 1
    # The cookie is Secure by default and carries the HMAC-signed value.
    assert cookie[0] == (f"pynext_csrf={sign_csrf_token('token-value')}; "
                         "Path=/; SameSite=Lax; Secure").encode("ascii")


def test_set_csrf_cookie_insecure_for_local_dev():
    config = SecurityConfig(csrf_cookie_secure=False)
    response = set_csrf_cookie(Response("ok"), "token-value", config=config)
    cookie = [v for k, v in response.headers if k == b"set-cookie"][0]
    assert cookie == (f"pynext_csrf={sign_csrf_token('token-value', config=config)}; "
                      "Path=/; SameSite=Lax").encode("ascii")


def test_set_csrf_cookie_secure_flag():
    config = SecurityConfig(csrf_cookie_secure=True, csrf_cookie_samesite="Strict")
    response = set_csrf_cookie(Response("ok"), "token-value", config=config)
    cookie = [v for k, v in response.headers if k == b"set-cookie"][0]
    assert cookie == (f"pynext_csrf={sign_csrf_token('token-value', config=config)}; "
                      "Path=/; SameSite=Strict; Secure").encode("ascii")


def test_security_config_rejects_bad_values():
    with pytest.raises(ValueError, match="csrf_cookie_name"):
        SecurityConfig(csrf_cookie_name="bad name")
    with pytest.raises(ValueError, match="cors_max_age"):
        SecurityConfig(cors_max_age=-1)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_validate_csrf_exempts_safe_methods(method):
    assert validate_csrf(Request(method, "/api/echo"))


def test_validate_csrf_requires_matching_cookie_and_header():
    token = new_csrf_token()
    headers = ((b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"x-csrf-token", token.encode()))
    assert validate_csrf(Request("POST", "/api/echo", headers=headers))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers[:1]))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers[1:]))
    wrong = ((b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
             (b"x-csrf-token", b"wrong"))
    assert not validate_csrf(Request("POST", "/api/echo", headers=wrong))


def test_validate_csrf_rejects_unsigned_cookie_even_with_matching_token():
    # Subdomain cookie injection: the attacker plants a plain cookie
    # carrying the victim's (or its own) token and submits the same token
    # in the header. Without an HMAC signature from the app key it fails.
    token = new_csrf_token()
    headers = ((b"cookie", f"pynext_csrf={token}".encode()),
               (b"x-csrf-token", token.encode()))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers))
    # A signature made with a different key fails the same way.
    foreign = SecurityConfig(csrf_signing_key="a-different-signing-key")
    headers = ((b"cookie", f"pynext_csrf={sign_csrf_token(token, config=foreign)}".encode()),
               (b"x-csrf-token", token.encode()))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers))
    # Tampering with one character of a valid signature fails too.
    signed = sign_csrf_token(token)
    tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")
    headers = ((b"cookie", f"pynext_csrf={tampered}".encode()),
               (b"x-csrf-token", token.encode()))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers))


def test_validate_csrf_accepts_form_field_fallback():
    token = new_csrf_token()
    headers = ((b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"content-type", b"application/x-www-form-urlencoded"))
    body = f"csrf_token={token}&name=x".encode()
    assert validate_csrf(Request("POST", "/api/echo", headers=headers, body=body))
    assert not validate_csrf(Request("POST", "/api/echo", headers=headers,
                                     body=b"csrf_token=wrong"))


def test_csrf_protect_marks_handler():
    @csrf_protect
    async def handler(request):
        return Response("ok")

    assert handler.__pynext_csrf__ is True


def test_csrf_protected_route_rejects_missing_token(tmp_path):
    async def exercise():
        make_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            assert (await client.post("/api/echo", json={})).status_code == 403
            assert (await client.get("/api/echo")).status_code == 200
            assert (await client.post("/api/open", json={})).status_code == 201
    run(exercise())


def test_csrf_protected_route_accepts_matching_token(tmp_path):
    async def exercise():
        make_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            issued = await client.get("/api/token")
            assert issued.status_code == 200
            token = issued.json()["token"]
            # The cookie must be sent back exactly as issued: it carries
            # the HMAC signature, which cannot be rebuilt from the token.
            cookie = issued.headers["set-cookie"].split(";")[0]
            assert cookie.startswith(f"pynext_csrf={token}.")
            headers = {"cookie": cookie, "x-csrf-token": token}
            assert (await client.post("/api/echo", json={}, headers=headers)).status_code == 201
            headers["x-csrf-token"] = "wrong"
            assert (await client.post("/api/echo", json={}, headers=headers)).status_code == 403
    run(exercise())


def test_csrf_protected_route_accepts_form_field(tmp_path):
    async def exercise():
        make_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            issued = await client.get("/api/token")
            token = issued.json()["token"]
            headers = {"cookie": issued.headers["set-cookie"].split(";")[0]}
            data = {"csrf_token": token, "name": "x"}
            assert (await client.post("/api/echo", data=data, headers=headers)).status_code == 201
    run(exercise())


def test_csrf_app_wide_mode_protects_all_state_changing_routes(tmp_path):
    async def exercise():
        make_project(tmp_path)
        security = SecurityConfig(csrf_enabled=True)
        app = create_app(tmp_path, security=security)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/open", json={})).status_code == 403
            issued = await client.get("/api/token")
            token = issued.json()["token"]
            headers = {"cookie": issued.headers["set-cookie"].split(";")[0], "x-csrf-token": token}
            assert (await client.post("/api/open", json={}, headers=headers)).status_code == 201
    run(exercise())


def test_csrf_protection_on_native_server(tmp_path):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.post("/api/echo", json={})).status_code == 403
                issued = await client.get("/api/token")
                token = issued.json()["token"]
                headers = {"cookie": issued.headers["set-cookie"].split(";")[0], "x-csrf-token": token}
                assert (await client.post("/api/echo", json={}, headers=headers)).status_code == 201
        finally:
            await asyncio.to_thread(server.stop)
    run(exercise())


# --- P0/A2: CSRF bypass scenarios (Origin, Sec-Fetch-Site, signed cookie,
#     token reuse, session-middleware auto-protection) ---


def _signed_headers(token, *, origin=None, fetch_site=None, host=b"test"):
    headers = [(b"cookie", f"pynext_csrf={sign_csrf_token(token)}".encode()),
               (b"x-csrf-token", token.encode())]
    if host is not None:
        headers.append((b"host", host))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    return tuple(headers)


@pytest.mark.parametrize("origin,allowed", [
    ("http://test", True),            # same origin
    ("http://evil.example", False),   # forged cross-site origin
    ("http://sub.test", False),       # subdomain is a different origin
    ("null", False),                  # sandboxed / opaque origin
    ("not-a-url", False),             # malformed origin fails closed
])
# Note: only the host is compared, not the scheme — Request carries no
# scheme, and an attacker that can serve the app's own host already
# controls it. A different scheme on the same host is not a new vector.
def test_validate_csrf_origin_check(origin, allowed):
    token = new_csrf_token()
    request = Request("POST", "/api/echo", headers=_signed_headers(token, origin=origin))
    assert validate_csrf(request) is allowed


def test_validate_csrf_origin_check_requires_host():
    token = new_csrf_token()
    request = Request("POST", "/api/echo",
                      headers=_signed_headers(token, origin="http://test", host=None))
    assert not validate_csrf(request)


@pytest.mark.parametrize("fetch_site,allowed", [
    ("same-origin", True),
    ("none", True),          # non-browser client (curl, etc.)
    ("same-site", False),    # subdomain page must not drive state changes
    ("cross-site", False),
    (None, True),            # no signal at all: token check still applies
])
def test_validate_csrf_sec_fetch_site_check(fetch_site, allowed):
    token = new_csrf_token()
    request = Request("POST", "/api/echo",
                      headers=_signed_headers(token, fetch_site=fetch_site))
    assert validate_csrf(request) is allowed


def test_validate_csrf_origin_check_can_be_disabled():
    token = new_csrf_token()
    config = SecurityConfig(csrf_origin_check=False)
    request = Request("POST", "/api/echo", headers=_signed_headers(
        token, origin="http://evil.example"))
    assert validate_csrf(request, config=config)
    # The token check itself still applies when the origin check is off.
    assert not validate_csrf(Request("POST", "/api/echo", headers=_signed_headers(
        token, origin="http://evil.example")[:1]), config=config)


def test_csrf_route_rejects_cross_site_origin_with_valid_token(tmp_path):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path, security=SecurityConfig(csrf_enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            issued = await client.get("/api/token")
            token = issued.json()["token"]
            cookie = issued.headers["set-cookie"].split(";")[0]
            base = {"cookie": cookie, "x-csrf-token": token}
            assert (await client.post("/api/echo", json={},
                                      headers={**base, "origin": "https://evil.example"})).status_code == 403
            assert (await client.post("/api/echo", json={},
                                      headers={**base, "sec-fetch-site": "cross-site"})).status_code == 403
            assert (await client.post("/api/echo", json={},
                                      headers={**base, "origin": "http://test"})).status_code == 201
            assert (await client.post("/api/echo", json={},
                                      headers={**base, "sec-fetch-site": "same-origin"})).status_code == 201
    run(exercise())


def test_csrf_double_submit_token_is_reusable_but_not_transferable(tmp_path):
    """A double-submit token is not one-time: reusing it is fine. But a
    token bound to one session must not work with another session."""
    from pynext.sessions import Session

    async def exercise():
        make_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            issued = await client.get("/api/token")
            token = issued.json()["token"]
            headers = {"cookie": issued.headers["set-cookie"].split(";")[0],
                       "x-csrf-token": token}
            assert (await client.post("/api/echo", json={}, headers=headers)).status_code == 201
            assert (await client.post("/api/echo", json={}, headers=headers)).status_code == 201
    run(exercise())
    # Cross-session reuse fails at the unit level: the submitted token
    # belongs to a different session than the one attached to the request.
    first, second = Session(), Session()
    first_token, second_token = first.csrf_token(), second.csrf_token()
    assert first_token != second_token
    request = Request("POST", "/api/echo", headers=_signed_headers(second_token),
                      state={"session": first})
    assert not validate_csrf(request)
    request = Request("POST", "/api/echo", headers=_signed_headers(first_token),
                      state={"session": first})
    assert validate_csrf(request)


def make_session_project(root):
    api = root / "app" / "api"
    (api / "open").mkdir(parents=True)
    (api / "token").mkdir(parents=True)
    (api / "open" / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"POST\")\n"
        "async def post_open(request):\n"
        "    return JSONResponse({\"ok\": True}, status=201)\n"
    )
    (api / "token" / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"GET\")\n"
        "async def get_token(request):\n"
        "    return JSONResponse({\"token\": request.state[\"session\"].csrf_token()})\n"
    )


def test_session_middleware_auto_enables_csrf(tmp_path):
    from pynext import SessionMiddleware, SQLiteSessionStore

    async def exercise():
        make_session_project(tmp_path)
        store = SQLiteSessionStore(tmp_path / "sessions.db")
        # No explicit csrf_enabled: the session middleware turns CSRF on.
        app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)])
        assert app.security.csrf_enabled is True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/open", json={})).status_code == 403
            issued = await client.get("/api/token")
            token = issued.json()["token"]
            session_cookie = issued.headers["set-cookie"].split(";")[0]
            headers = {"cookie": session_cookie, "x-csrf-token": token}
            assert (await client.post("/api/open", json={}, headers=headers)).status_code == 201
    run(exercise())


def test_session_middleware_csrf_can_be_opted_out(tmp_path):
    from pynext import SessionMiddleware, SQLiteSessionStore

    async def exercise():
        make_session_project(tmp_path)
        store = SQLiteSessionStore(tmp_path / "sessions.db")
        app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)],
                         security=SecurityConfig(csrf_enabled=False))
        assert app.security.csrf_enabled is False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/open", json={})).status_code == 201
    run(exercise())


def test_no_session_middleware_leaves_csrf_off_by_default(tmp_path):
    async def exercise():
        make_session_project(tmp_path)
        app = create_app(tmp_path)
        assert app.security.csrf_enabled is False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post("/api/open", json={})).status_code == 201
    run(exercise())


# --- Finding #40: CORS configuration ---

CORS = SecurityConfig(cors_allowed_origins=("https://example.com",))


def test_cors_default_config_denies_preflight(tmp_path):
    async def exercise():
        make_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            response = await client.options("/api/echo", headers={
                "origin": "https://example.com", "access-control-request-method": "POST"})
            assert response.status_code == 403
            assert response.headers["vary"] == "Origin"
            # A plain OPTIONS request without CORS headers keeps the old Allow behaviour.
            response = await client.options("/api/echo")
            assert response.status_code == 204
            assert "POST" in response.headers["allow"]
    run(exercise())


def test_cors_preflight_allowed_origin(tmp_path):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path, security=CORS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.options("/api/echo", headers={
                "origin": "https://example.com", "access-control-request-method": "POST",
                "access-control-request-headers": "X-CSRF-Token, Content-Type"})
            assert response.status_code == 204
            assert response.headers["access-control-allow-origin"] == "https://example.com"
            assert "POST" in response.headers["access-control-allow-methods"]
            assert "X-CSRF-Token" in response.headers["access-control-allow-headers"]
            assert response.headers["access-control-max-age"] == "600"
            assert response.headers["vary"] == "Origin"
            assert "POST" in response.headers["allow"]
    run(exercise())


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example", "access-control-request-method": "POST"},
    {"origin": "https://example.com", "access-control-request-method": "DELETE"},
    {"origin": "https://example.com", "access-control-request-method": "POST",
     "access-control-request-headers": "X-Evil-Header"},
])
def test_cors_preflight_rejects_disallowed_origin_method_or_header(tmp_path, headers):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path, security=SecurityConfig(
            cors_allowed_origins=("https://example.com",),
            cors_allowed_methods=("GET", "POST"),
            cors_allowed_headers=("Content-Type",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.options("/api/echo", headers=headers)
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
    run(exercise())


def test_cors_actual_request_headers(tmp_path):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path, security=CORS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/api/echo", headers={"origin": "https://example.com"})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "https://example.com"
            assert response.headers["vary"] == "Origin"
            response = await client.get("/api/echo", headers={"origin": "https://evil.example"})
            assert response.status_code == 200
            assert "access-control-allow-origin" not in response.headers
            assert response.headers["vary"] == "Origin"
            response = await client.get("/api/echo")
            assert "access-control-allow-origin" not in response.headers
            assert "vary" not in response.headers
    run(exercise())


def test_cors_same_origin_preflight_is_not_blocked(tmp_path):
    async def exercise():
        make_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            # Origin matches the Host header, so this is not cross-origin.
            response = await client.options("/api/echo", headers={
                "origin": "http://test", "access-control-request-method": "POST"})
            assert response.status_code == 204
            assert "access-control-allow-origin" not in response.headers
    run(exercise())


def test_cors_wildcard_and_credentials(tmp_path):
    async def exercise():
        make_project(tmp_path)
        wildcard = create_app(tmp_path, security=SecurityConfig(cors_allowed_origins=("*",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wildcard),
                                     base_url="http://test") as client:
            response = await client.get("/api/echo", headers={"origin": "https://any.example"})
            assert response.headers["access-control-allow-origin"] == "*"
            assert "access-control-allow-credentials" not in response.headers
        credentialed = create_app(tmp_path, security=SecurityConfig(
            cors_allowed_origins=("*",), cors_allow_credentials=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=credentialed),
                                     base_url="http://test") as client:
            response = await client.get("/api/echo", headers={"origin": "https://any.example"})
            assert response.headers["access-control-allow-origin"] == "https://any.example"
            assert response.headers["access-control-allow-credentials"] == "true"
    run(exercise())


def test_cors_preflight_on_native_server(tmp_path):
    async def exercise():
        make_project(tmp_path)
        app = create_app(tmp_path, security=CORS)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                response = await client.options("/api/echo", headers={
                    "origin": "https://example.com", "access-control-request-method": "POST"})
                assert response.status_code == 204
                assert response.headers["access-control-allow-origin"] == "https://example.com"
                response = await client.options("/api/echo", headers={
                    "origin": "https://evil.example", "access-control-request-method": "POST"})
                assert response.status_code == 403
        finally:
            await asyncio.to_thread(server.stop)
    run(exercise())


# --- Finding #41: CSP nonce policy ---


def make_island_project(root):
    (root / "app").mkdir(parents=True)
    (root / "islands" / "lib").mkdir(parents=True)
    (root / "app" / "page.py").write_text(
        "from pynext import h, island, page\n@page()\n"
        "def Page():\n"
        f"    return h(\"html\", {{}}, h(\"body\", {{}}, h(\"h1\", {{}}, \"Hello\"), island(\"counter\", props={{\"initial\": 1}}, fallback=h(\"p\", {{}}, \"Loading\"))))\n"
    )
    (root / "islands" / "counter.ts").write_text(
        'import { label } from "./lib/label";\n'
        'export function mount(el: HTMLElement, props: {initial: number}) { el.textContent = label(props.initial); }\n'
    )
    (root / "islands" / "lib" / "label.ts").write_text('export function label(n: number) { return `Count: ${n}`; }\n')


def test_render_adds_nonce_to_client_runtime_script():
    from pynext import _native, h, island
    from pynext.components import bind_islands

    tree = bind_islands(
        h("html", {}, h("body", {}, island("counter", props={"initial": 1},
                                        fallback=h("p", {}, "Loading")))),
        {"runtime": "a" * 64, "bundles": {"counter": "b" * 64}, "hydration": {}})
    import json

    rendered = _native.render(json.dumps(tree), "test-nonce_1")
    assert 'nonce="test-nonce_1"' in rendered
    assert "<script type=\"module\" src=\"/_pynext/static/" in rendered
    assert _native.render(json.dumps(tree)) == rendered.replace(' nonce="test-nonce_1"', "")
    with pytest.raises(ValueError, match="Invalid CSP nonce"):
        _native.render(json.dumps(tree), 'bad" nonce')


def test_csp_nonce_in_header_and_matching_script_tag(tmp_path, esbuild):
    async def exercise():
        make_island_project(tmp_path)
        app = create_app(tmp_path, security=SecurityConfig(csp_nonce_enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            first = await client.get("/")
            assert first.status_code == 200
            csp = first.headers["content-security-policy"]
            assert csp.startswith("script-src 'self' 'nonce-")
            assert "style-src 'self'" in csp
            assert csp.endswith("'; frame-ancestors 'none'")
            nonce = csp.split("'nonce-")[1].split("'")[0]
            assert f'nonce="{nonce}"' in first.text
            second = await client.get("/")
            assert second.headers["content-security-policy"] != csp
    run(exercise())


def test_csp_nonce_disabled_keeps_static_policy(tmp_path, esbuild):
    async def exercise():
        make_island_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == "script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
            assert "nonce=" not in response.text
    run(exercise())

# --- A1: default security headers ---

from pynext import apply_security_headers, content_security_policy, security_headers
from pynext.build import write_manifest

DEFAULT_CSP = ("script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; "
               "form-action 'self'; frame-ancestors 'none'")

EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "content-security-policy": DEFAULT_CSP,
}


def _header_map(response):
    # httpx responses expose a case-insensitive mapping; raw Response objects
    # carry a list of (bytes, bytes) pairs. Normalize both to str keys/values.
    pairs = response.headers.items() if hasattr(response.headers, "items") else response.headers
    out = {}
    for key, value in pairs:
        if isinstance(key, bytes):
            key = key.decode("ascii")
        if isinstance(value, bytes):
            value = value.decode("ascii")
        out[key.lower()] = value
    return out


def assert_security_headers(response, *, production=False):
    headers = _header_map(response)
    for name, value in EXPECTED_HEADERS.items():
        assert headers[name] == value, name
    if production:
        assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    else:
        assert "strict-transport-security" not in headers


def test_content_security_policy_defaults():
    assert content_security_policy() == DEFAULT_CSP
    assert content_security_policy("abc123") == (
        "script-src 'self' 'nonce-abc123'; style-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    with pytest.raises(ValueError, match="Invalid CSP nonce"):
        content_security_policy('bad" nonce')


def test_security_config_header_defaults():
    config = SecurityConfig()
    assert config.hsts_enabled is True
    assert config.hsts_max_age == 31_536_000
    assert config.hsts_include_subdomains is True
    assert config.hsts_preload is False
    assert config.frame_options == "DENY"
    assert config.referrer_policy == "strict-origin-when-cross-origin"
    assert config.permissions_policy == "camera=(), microphone=(), geolocation=()"
    assert config.csp_style_src == "'self'"
    assert config.csp_frame_ancestors == "'none'"
    assert config.csp_base_uri == "'self'"
    assert config.csp_form_action == "'self'"


def test_security_config_rejects_bad_header_values():
    with pytest.raises(ValueError, match="frame_options"):
        SecurityConfig(frame_options="ALLOW")
    with pytest.raises(ValueError, match="referrer_policy"):
        SecurityConfig(referrer_policy="unsafe")
    with pytest.raises(ValueError, match="permissions_policy"):
        SecurityConfig(permissions_policy="camera=(); injected: yes")
    with pytest.raises(ValueError, match="hsts_max_age"):
        SecurityConfig(hsts_max_age=-1)
    with pytest.raises(ValueError, match="csp_style_src"):
        SecurityConfig(csp_style_src="'self'; script-src 'unsafe-inline'")


def test_security_headers_dev_omits_hsts_but_keeps_the_rest():
    headers = dict(security_headers(production=False))
    assert "strict-transport-security" not in headers
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"strict-origin-when-cross-origin"
    assert headers[b"permissions-policy"] == b"camera=(), microphone=(), geolocation=()"
    assert headers[b"content-security-policy"] == DEFAULT_CSP.encode("ascii")


def test_security_headers_production_includes_hsts():
    headers = dict(security_headers(production=True))
    assert headers[b"strict-transport-security"] == b"max-age=31536000; includeSubDomains"
    config = SecurityConfig(hsts_preload=True, hsts_include_subdomains=False)
    headers = dict(security_headers(config=config, production=True))
    assert headers[b"strict-transport-security"] == b"max-age=31536000; preload"


def test_security_headers_custom_config_can_disable_headers():
    config = SecurityConfig(hsts_enabled=False, frame_options=None,
                            referrer_policy=None, permissions_policy=None)
    headers = dict(security_headers(config=config, production=True))
    assert "strict-transport-security" not in headers
    assert b"x-frame-options" not in headers
    assert b"referrer-policy" not in headers
    assert b"permissions-policy" not in headers
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"content-security-policy"] == DEFAULT_CSP.encode("ascii")


def test_apply_security_headers_is_idempotent_and_keeps_explicit_choices():
    response = apply_security_headers(Response("ok"), production=False)
    assert_security_headers(response)
    apply_security_headers(response, production=False)
    assert_security_headers(response)
    assert len([k for k, _ in response.headers if k == b"x-frame-options"]) == 1

    explicit = Response("ok", headers=[(b"x-frame-options", b"SAMEORIGIN"),
                                        (b"content-security-policy", b"script-src 'none'")])
    apply_security_headers(explicit, production=False)
    assert [v for k, v in explicit.headers if k == b"x-frame-options"] == [b"SAMEORIGIN"]
    assert [v for k, v in explicit.headers if k == b"content-security-policy"] == [b"script-src 'none'"]
    assert "strict-transport-security" not in [k for k, _ in explicit.headers]

    production = apply_security_headers(Response("ok"), production=True)
    assert_security_headers(production, production=True)


def test_apply_security_headers_preserves_nonce_and_updates_directives():
    nonce = "test-nonce_1"
    response = Response("ok", csp_nonce=nonce)
    custom = SecurityConfig(csp_style_src="'self' https://cdn.example")
    apply_security_headers(response, config=custom)
    csp = [v for k, v in response.headers if k == b"content-security-policy"][0].decode()
    assert f"'nonce-{nonce}'" in csp
    assert "style-src 'self' https://cdn.example" in csp


def make_header_project(root):
    app = root / "app"
    (app / "api" / "echo").mkdir(parents=True)
    (app / "api" / "boom").mkdir(parents=True)
    (app / "api" / "echo" / "route.py").write_text(
        "from pynext import JSONResponse, route\n"
        "@route(\"GET\")\n"
        "async def get_echo():\n"
        "    return JSONResponse({\"ok\": True})\n"
    )
    (app / "api" / "boom" / "route.py").write_text(
        "from pynext import route\n"
        "@route(\"GET\")\n"
        "async def get_boom():\n"
        "    raise RuntimeError(\"boom\")\n"
    )
    (app / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    public = root / "public"
    public.mkdir()
    (public / "data.bin").write_bytes(b"0123456789")


async def check_header_scan(client, *, production=False):
    assert_security_headers(await client.get("/api/echo"), production=production)
    assert_security_headers(await client.get("/"), production=production)
    assert_security_headers(await client.get("/data.bin"), production=production)
    assert_security_headers(await client.get("/missing"), production=production)
    response = await client.post("/")
    assert response.status_code == 405
    assert_security_headers(response, production=production)
    response = await client.get("/api/boom")
    assert response.status_code == 500
    assert_security_headers(response, production=production)
    head = await client.head("/api/echo")
    assert head.status_code == 200
    assert_security_headers(head, production=production)
    response = await client.options("/api/echo", headers={
        "origin": "https://example.com", "access-control-request-method": "POST"})
    assert response.status_code == 403
    assert_security_headers(response, production=production)


def test_a1_header_scan_asgi_dev(tmp_path):
    async def exercise():
        make_header_project(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                     base_url="http://test") as client:
            await check_header_scan(client, production=False)
    run(exercise())


def test_a1_header_scan_asgi_production(tmp_path):
    make_header_project(tmp_path)
    # write_manifest renders SSG pages via asyncio.run, so it must run
    # outside the exercise coroutine (a loop is already running there).
    write_manifest(tmp_path)

    async def exercise():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(tmp_path, production=True)),
                base_url="http://test") as client:
            await check_header_scan(client, production=True)
    run(exercise())


def test_a1_header_scan_native_and_rust_fast_path(tmp_path):
    make_header_project(tmp_path)
    write_manifest(tmp_path)

    async def exercise():
        app = create_app(tmp_path, production=True)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                await check_header_scan(client, production=True)
                # Static pages and public assets are served by the Rust
                # layer without Python dispatch: prove the headers still
                # come from Rust by breaking dispatch and re-requesting.
                async def fail_dispatch(request):
                    raise AssertionError("native static hit crossed the Python boundary")
                app.dispatch = fail_dispatch
                assert_security_headers(await client.get("/"), production=True)
                assert_security_headers(await client.get("/data.bin"), production=True)
        finally:
            await asyncio.to_thread(server.stop)
    run(exercise())


def test_a1_nonce_page_keeps_nonce_and_other_headers(tmp_path, esbuild):
    async def exercise():
        make_island_project(tmp_path)
        app = create_app(tmp_path, security=SecurityConfig(csp_nonce_enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 200
            csp = response.headers["content-security-policy"]
            assert csp.startswith("script-src 'self' 'nonce-")
            assert csp.endswith("'; frame-ancestors 'none'")
            nonce = csp.split("'nonce-")[1].split("'")[0]
            assert f'nonce="{nonce}"' in response.text
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
            assert "strict-transport-security" not in response.headers
    run(exercise())
