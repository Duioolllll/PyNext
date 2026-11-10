"""PyNext security P0 High fixes (H-1..H-5, M-21).

Each test pins the behaviour the security review (``pynext-security.md``
§2/§3, ROADMAP S-P0-8..12, S-P0-15) requires:

- H-1: rate limiting is on by default (300/min per client IP) so a
  default deployment answers 429 instead of serving unthrottled.
- H-2: a rate-limit decision that cannot be produced (decision gate
  full, or the 2s deadline expired while the loop was saturated) fails
  open instead of 503ing requests the limiter would have allowed.
  A decision that *was* produced but is invalid, and a limiter backend
  reporting itself unavailable, still fail closed (see
  test_asgi_admission.py and test_http_admission.py).
- H-3: /healthz and /readyz never take an execution slot.
- H-4: ``cors_allowed_origins=["*"]`` + ``cors_allow_credentials=True``
  is refused at startup, and a directly-built SecurityConfig never
  echoes an arbitrary Origin for that combination.
- H-5: one client IP may hold at most ``max_concurrent_per_ip``
  execution slots (default 16, a quarter of the 64-slot pool).
- M-21: a dynamic ISR route serves ``%3F``/``%23`` paths instead of
  500ing on the implicit path tag.
"""
import asyncio
import socket
import threading

import httpx
import pytest

from pynext import RateLimiter, SecurityConfig, create_app
from pynext.app import _implicit_path_tags
from pynext.build import write_manifest
from pynext.cache import validate_cache_tag
from pynext.cli import _limiter_from_config
from pynext.config import DEFAULT_CONFIG, ConfigError, load_config
from pynext.server import start_native


def _page_project(root):
    (root / "app").mkdir()
    # Async page: rendering never touches the sync-handler pool, so a
    # burst of fail-open requests is not 503ed by pool saturation (a
    # separate mechanism from the rate-limit decision under test).
    (root / "app" / "page.py").write_text(
        'from pynext import h, page\n@page()\nasync def Page(): return h("p", {}, "home")\n')


def _slow_api_project(root):
    route = root / "app" / "api" / "wait"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        'started = 0\nrelease = None\n'
        '@route("GET")\n'
        'async def get():\n'
        '    global started\n'
        '    started += 1\n'
        '    await release.wait()\n'
        '    return JSONResponse({"ok": True})\n')
    return root


async def _wait_until(check, timeout=10):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


# -- H-1: rate limiting on by default ---------------------------------------


def test_default_rate_limit_is_300_per_minute(tmp_path):
    resolved = load_config(tmp_path, "start", env={})
    assert resolved.values.rate_limit == 300
    assert resolved.values.rate_limit_window == 60.0
    assert resolved.values.rate_limit_exempt_paths == ("/healthz", "/readyz")
    limiter = _limiter_from_config(resolved.values)
    assert isinstance(limiter, RateLimiter)
    assert limiter.limit == 300
    # Opting out still works, and builds no limiter.
    disabled = load_config(tmp_path, "start", env={},
                           cli_overrides={"rate_limit": 0})
    assert _limiter_from_config(disabled.values) is None
    assert DEFAULT_CONFIG.rate_limit == 300
    assert DEFAULT_CONFIG.max_concurrent_per_ip == 16


def test_default_rate_limit_answers_429(tmp_path):
    _page_project(tmp_path)

    async def run():
        # The default bucket holds exactly 300 tokens: 300 decisions
        # admit, the 301st denies (no wall clock passes, so no refill).
        limiter = _limiter_from_config(load_config(tmp_path, "start", env={}).values)
        key = limiter.key_for("127.0.0.1")
        assert all(limiter.allow(key)[0] for _ in range(300))
        allowed, retry_after = limiter.allow(key)
        assert allowed is False and retry_after >= 1
        # The same config -> limiter -> app wiring, with a small limit
        # so the end-to-end check is not timing-sensitive (a 300-token
        # bucket would refill while 301 slow requests run).
        small = _limiter_from_config(load_config(
            tmp_path, "start", env={}, cli_overrides={"rate_limit": 5}).values)
        app = create_app(tmp_path, rate_limit=small)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                statuses = []
                for _ in range(6):
                    statuses.append((await client.get("/")).status_code)
                assert statuses == [200] * 5 + [429]
                assert (await client.get("/healthz")).status_code == 200
        finally:
            await app.shutdown()

    asyncio.run(run())


# -- H-2: a decision that cannot be produced fails open ----------------------


def test_asgi_decision_timeout_fails_open(tmp_path):
    _page_project(tmp_path)
    release = threading.Event()

    class Slow(RateLimiter):
        def allow(self, key):
            release.wait(10)
            return True, None

    async def run():
        app = create_app(tmp_path, rate_limit=Slow(1000, 60))
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 200
                assert app.metrics.snapshot()["rate_limit"]["fail_open"] >= 1
        finally:
            release.set()
            await app.shutdown()

    asyncio.run(run())


def test_asgi_decision_gate_full_fails_open(tmp_path):
    _page_project(tmp_path)
    release = threading.Event()
    calls = []

    class Slow(RateLimiter):
        def allow(self, key):
            calls.append(key)
            release.wait(10)
            return True, None

    async def request(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (await client.get("/")).status_code

    async def exercise():
        app = create_app(tmp_path, rate_limit=Slow(1000, 60))
        try:
            tasks = [asyncio.create_task(request(app)) for _ in range(64)]
            await _wait_until(lambda: app._asgi_admission.inflight == 64, 15)
            # The gate is full, so no decision can be produced: the
            # request is admitted instead of 503ing.
            assert await asyncio.wait_for(request(app), 15) == 200
            results = await asyncio.wait_for(asyncio.gather(*tasks), 30)
            assert results == [200] * 64
            assert app.metrics.snapshot()["rate_limit"]["fail_open"] >= 65
            # Callbacks queued in the executor past their deadline never
            # run (and never charge a token later): only the ones that
            # actually started are counted, so record the count here.
            before = len(calls)
            release.set()
            assert await app.wait_for_inflight(3)
            assert len(calls) == before
            assert await asyncio.wait_for(request(app), 10) == 200
            assert len(calls) == before + 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.wait_for_inflight(3)
            await app.shutdown()

    asyncio.run(exercise())


def test_native_decision_timeout_fails_open(tmp_path):
    route = tmp_path / "app" / "api" / "wait"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        'calls = []\nrelease = None\n'
        '@route("POST")\n'
        'async def post(request):\n'
        '    calls.append(request.body)\n'
        '    await release.wait()\n'
        '    return JSONResponse({"ok": True})\n')
    release = threading.Event()

    class SlowLimiter(RateLimiter):
        def allow(self, key):
            release.wait(10)
            return True, None

    async def run():
        app = create_app(tmp_path, rate_limit=SlowLimiter(1, 60))
        module = app._module("app/api/wait/route.py")
        module.release = asyncio.Event()
        server = start_native(app, asyncio.get_running_loop(), port=0)
        host, port = server.address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /api/wait HTTP/1.1\r\nHost: localhost\r\n"
                         b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
            await writer.drain()
            # The handler only runs after the fail-open decision; release
            # it now so it can answer as soon as the body arrives.
            module.release.set()
            # No decision within 2s: the request is admitted (100) and
            # answered (200) instead of 503ing.
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 100 "), head
            writer.write(b"x" * 100)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 200 "), head
            assert server.rate_limit_fail_open_count() >= 1
            assert module.calls == [b"x" * 100]
        finally:
            module.release.set()
            release.set()
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


# -- H-3: health probes never take an execution slot ------------------------


def test_health_bypasses_saturated_execution_slots(tmp_path):
    _slow_api_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        await app.startup()
        module = app._module("app/api/wait/route.py")
        module.release = asyncio.Event()
        server = start_native(app, asyncio.get_running_loop(), port=0, max_concurrent=2)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                tasks = [asyncio.create_task(client.get("/api/wait")) for _ in range(2)]
                await _wait_until(lambda: module.started == 2)
                # Both execution slots are held by slow requests, and a
                # third normal request would 503 after the 1s wait. The
                # probes must still answer at once.
                assert (await asyncio.wait_for(client.get("/healthz"), 3)).status_code == 200
                assert (await asyncio.wait_for(client.get("/readyz"), 3)).status_code == 200
                module.release.set()
                assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


# -- H-4: CORS "*" + credentials --------------------------------------------


def test_cors_wildcard_credentials_rejected_by_config(tmp_path):
    (tmp_path / "pynext.config.py").write_text(
        "config = {'cors_allowed_origins': ['*'], 'cors_allow_credentials': True}\n")
    with pytest.raises(ConfigError, match="cors_allow_credentials.*\\*.*cors_allowed_origins"):
        load_config(tmp_path, "start", env={})
    # The same origins without credentials, and credentials with an
    # explicit origin list, still load.
    (tmp_path / "pynext.config.py").write_text(
        "config = {'cors_allowed_origins': ['*']}\n")
    assert load_config(tmp_path, "start", env={}).values.cors_allow_credentials is False
    (tmp_path / "pynext.config.py").write_text(
        "config = {'cors_allowed_origins': ['https://client.example'], "
        "'cors_allow_credentials': True}\n")
    load_config(tmp_path, "start", env={})


def test_cors_wildcard_credentials_never_echoes_origin(tmp_path):
    _page_project(tmp_path)

    async def run():
        app = create_app(tmp_path, security=SecurityConfig(
            cors_allowed_origins=("*",), cors_allow_credentials=True))
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/", headers={"origin": "https://evil.example"})
                assert response.status_code == 200
                # Defence in depth for a SecurityConfig built directly
                # (bypassing config validation): the arbitrary Origin
                # must never be echoed back with credentials allowed.
                assert response.headers["access-control-allow-origin"] == "*"
                preflight = await client.options(
                    "/", headers={"origin": "https://evil.example",
                                "access-control-request-method": "GET"})
                assert preflight.status_code == 204
                assert preflight.headers["access-control-allow-origin"] == "*"
        finally:
            await app.shutdown()

    asyncio.run(run())


# -- H-5: one IP cannot take the whole execution pool ------------------------


def test_per_ip_quota_caps_one_ip_with_free_global_slots(tmp_path):
    """H-5 core (loopback only): one IP is capped at
    ``max_concurrent_per_ip`` even while the global pool still has free
    slots, so a single slow client cannot take the whole pool."""
    _slow_api_project(tmp_path)

    async def run():
        app = create_app(tmp_path)
        module = app._module("app/api/wait/route.py")
        module.release = asyncio.Event()
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=4, max_concurrent_per_ip=2)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         timeout=10) as client:
                tasks = [asyncio.create_task(client.get("/api/wait")) for _ in range(2)]
                await _wait_until(lambda: module.started == 2)
                # Two global slots are still free, but the same IP is
                # at its quota of 2, so the third request 503s at once.
                assert (await asyncio.wait_for(client.get("/api/wait"), 3)).status_code == 503
                module.release.set()
                assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


def _external_ip_reaches_server(host, port):
    """True when a raw TCP connection to ``host:port`` reaches the
    server (a valid HTTP response). In this sandbox non-loopback TCP
    is intercepted by the runtime egress proxy, which answers with a
    plaintext refusal instead of HTTP, so the check fails there."""
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            sock.settimeout(3)
            return sock.recv(12).startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def test_per_ip_quota_leaves_other_ips_unaffected(tmp_path):
    _slow_api_project(tmp_path)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        external_ip = probe.getsockname()[0]
        probe.close()
    except OSError:
        pytest.skip("no non-loopback address to test a second client IP")
    if external_ip == "127.0.0.1":
        pytest.skip("no non-loopback address to test a second client IP")

    async def run():
        app = create_app(tmp_path)
        module = app._module("app/api/wait/route.py")
        module.release = asyncio.Event()
        server = start_native(app, asyncio.get_running_loop(), host="0.0.0.0", port=0,
                              max_concurrent=4, max_concurrent_per_ip=2)
        try:
            port = server.address.rsplit(":", 1)[1]
            if not await asyncio.to_thread(_external_ip_reaches_server, external_ip, int(port)):
                pytest.skip("second client IP is not reachable in this sandbox "
                            "(non-loopback TCP is intercepted by the egress proxy)")
            async with httpx.AsyncClient(base_url="http://127.0.0.1:"
                                         f"{port}", timeout=10) as local, \
                    httpx.AsyncClient(base_url=f"http://{external_ip}:"
                                    f"{port}", timeout=10) as remote:
                tasks = [asyncio.create_task(local.get("/api/wait")) for _ in range(2)]
                await _wait_until(lambda: module.started == 2)
                assert (await asyncio.wait_for(local.get("/api/wait"), 3)).status_code == 503
                # A different client IP still has its own quota: its
                # request is admitted (handler starts) even while
                # 127.0.0.1 is capped. It only answers once released.
                remote_task = asyncio.create_task(remote.get("/api/wait"))
                await _wait_until(lambda: module.started == 3)
                module.release.set()
                assert (await remote_task).status_code == 200
                assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
        finally:
            module.release.set()
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())


# -- M-21: ISR "%3F"/"%23" paths --------------------------------------------


def test_implicit_path_tag_encodes_unsafe_characters():
    assert _implicit_path_tags("/blog") == ["_pynext_path_/blog"]
    assert _implicit_path_tags("/") == ["_pynext_path_/", "_pynext_path_/index"]
    for path in ("/docs/a?b", "/docs/a#b", "/docs/a?b#c", "/docs/a b", "/docs/한글"):
        tags = _implicit_path_tags(path)
        assert len(tags) == 1
        assert validate_cache_tag(tags[0]) == tags[0]
        assert tags[0].startswith("_pynext_path_h_")
    # The hashed form is deterministic, so revalidate(path) still matches.
    assert _implicit_path_tags("/docs/a?b") == _implicit_path_tags("/docs/a?b")


def test_isr_encoded_delimiter_path_renders(tmp_path):
    directory = tmp_path / "app" / "docs" / "[...path]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, str(calls))\n')
    # Production mode (via a written manifest) is what actually caches
    # ISR entries; dev mode re-renders every request, so the tag
    # invalidation below would have nothing to evict.
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            module = app._module("app/docs/[...path]/page.py")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for url in ("/docs/a%3Fb", "/docs/a%23b"):
                    first = await client.get(url)
                    assert first.status_code == 200, url
                    second = await client.get(url)
                    assert second.status_code == 200, url
                    assert first.text == second.text
                # Invalidation through the implicit path tag reaches the
                # same hashed tag the requests stored (revalidate() of a
                # decoded-delimiter path is a separate router-matching
                # gap, out of scope here).
                calls_before = module.calls
                removed = await app.isr.invalidate_tag(_implicit_path_tags("/docs/a?b")[0])
                assert removed >= 1
                response = await client.get("/docs/a%3Fb")
                assert response.status_code == 200
                assert module.calls == calls_before + 1
        finally:
            await app.shutdown()

    asyncio.run(run())
