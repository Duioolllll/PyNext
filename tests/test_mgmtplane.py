"""Management plane fail-closed (S-P1-32, Sanic lesson 3).

Sanic's trap: ``INSPECTOR=True`` alone opens the management API with no
auth (``worker/inspector.py:80``), and the key check itself is a plain
``!=`` (``:85``). PyNext must do the opposite: the plane is off by
default, enabling it without a key refuses to start, the key comparison
is constant-time, and a wildcard bind warns.
"""
import asyncio
import logging

import httpx
import pytest

from pynext import create_app
from pynext.mgmtplane import ManagementPlaneConfig, ManagementPlaneError
from pynext.server import _management_response, start_native

# Obviously fake value, used only as a test fixture -- never a real key.
TEST_KEY = "test-only-mgmt-key"

MGMT_ENV_VARS = ("PYNEXT_MGMT_ENABLED", "PYNEXT_MGMT_KEY", "PYNEXT_MGMT_GATE_READY")


@pytest.fixture(autouse=True)
def _clean_mgmt_env(monkeypatch):
    for name in MGMT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _exercise(tmp_path, mgmt, calls, *, host="127.0.0.1"):
    """Start a native server with ``mgmt`` and perform ``calls``.

    Each call is ``(method, path, headers)``; returns a list of
    ``(status, body)`` in order. The app is started, so ``/readyz``
    answers 200 when it is not gated.
    """

    async def exercise():
        route = tmp_path / "app" / "ping"
        route.mkdir(parents=True, exist_ok=True)
        (route / "route.py").write_text(
            'from pynext import route, Response\n@route("GET")\n'
            'def get(): return Response("pong")\n')
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), host=host, port=0,
                              management_plane=mgmt)
        await app.startup()
        client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                results = []
                for method, path, headers in calls:
                    resp = await client.request(method, path, headers=headers)
                    results.append((resp.status_code, resp.content))
                return results
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()

    return asyncio.run(exercise())


def _key_headers(key=TEST_KEY):
    return {"Authorization": f"Bearer {key}"}


def test_metrics_disabled_by_default(tmp_path):
    statuses = _exercise(tmp_path, None, [
        ("GET", "/metrics", None),
        ("GET", "/readyz", None),
        ("GET", "/healthz", None),
    ])
    # No framework /metrics route exists when the plane is off: the app
    # router answers 404. The readiness/liveness probes stay open.
    assert statuses[0][0] == 404, statuses
    assert statuses[1] == (200, b"ready"), statuses
    assert statuses[2] == (200, b"ok"), statuses


def test_enable_without_key_refuses_start(tmp_path):
    with pytest.raises(ManagementPlaneError):
        _exercise(tmp_path, ManagementPlaneConfig(enabled=True), [])


def test_readyz_gate_without_key_refuses_start(tmp_path):
    with pytest.raises(ManagementPlaneError):
        _exercise(tmp_path, ManagementPlaneConfig(readyz_requires_key=True), [])


def test_enable_without_key_refuses_start_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNEXT_MGMT_ENABLED", "1")
    with pytest.raises(ManagementPlaneError):
        _exercise(tmp_path, None, [])


def test_blank_key_refuses_start(tmp_path):
    with pytest.raises(ManagementPlaneError):
        _exercise(tmp_path, ManagementPlaneConfig(enabled=True, key="   "), [])


def test_disabled_plane_with_key_still_validates(tmp_path):
    # A key configured while the plane stays off is harmless, not an error.
    statuses = _exercise(tmp_path, ManagementPlaneConfig(key=TEST_KEY), [
        ("GET", "/metrics", None),
    ])
    assert statuses[0][0] == 404, statuses


def test_metrics_requires_key_when_enabled(tmp_path):
    mgmt = ManagementPlaneConfig(enabled=True, key=TEST_KEY)
    results = _exercise(tmp_path, mgmt, [
        ("GET", "/metrics", None),
        ("GET", "/metrics", _key_headers("wrong-key")),
        ("GET", "/metrics", _key_headers()),
        ("GET", "/metrics", {"X-PyNext-Mgmt-Key": TEST_KEY}),
        ("POST", "/metrics", _key_headers()),
        ("HEAD", "/metrics", _key_headers()),
    ])
    assert results[0][0] == 401, results
    assert results[1][0] == 401, results
    assert results[2][0] == 200, results
    assert b"pynext" in results[2][1], results
    assert results[3][0] == 200, results
    assert results[4][0] == 405, results
    assert results[5] == (200, b""), results


def test_readyz_gate(tmp_path):
    mgmt = ManagementPlaneConfig(readyz_requires_key=True, key=TEST_KEY)
    results = _exercise(tmp_path, mgmt, [
        ("GET", "/readyz", None),
        ("GET", "/readyz", _key_headers("wrong-key")),
        ("GET", "/readyz", _key_headers()),
        ("GET", "/metrics", _key_headers()),
        ("GET", "/healthz", None),
    ])
    assert results[0][0] == 401, results
    assert results[1][0] == 401, results
    assert results[2] == (200, b"ready"), results
    # The plane itself is off, so /metrics stays unexposed even with a key.
    assert results[3][0] == 404, results
    # /healthz is never gated.
    assert results[4] == (200, b"ok"), results


def test_key_comparison_is_constant_time(monkeypatch):
    """The key check must go through hmac.compare_digest, never ==/!=."""
    import pynext.mgmtplane as mgmtplane

    calls = []
    real_compare_digest = mgmtplane.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(mgmtplane.hmac, "compare_digest", spy)
    cfg = ManagementPlaneConfig(enabled=True, key=TEST_KEY)
    assert cfg.check_key([(b"authorization", b"Bearer wrong-key")]) is False
    assert cfg.check_key([(b"authorization", b"Bearer test-only-mgmt-key")]) is True
    # A different-length candidate must not raise and must not match.
    assert cfg.check_key([(b"authorization", b"Bearer short")]) is False
    assert calls, "check_key never called hmac.compare_digest"
    # The matching candidate reached compare_digest as (candidate, expected).
    assert any(a == b == TEST_KEY.encode() for a, b in calls)


def test_wildcard_bind_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pynext"):
        _exercise(tmp_path, None, [], host="0.0.0.0")
    assert any("0.0.0.0" in rec.message and "all interfaces" in rec.message
               for rec in caplog.records), [r.message for r in caplog.records]


def test_loopback_bind_does_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pynext"):
        _exercise(tmp_path, None, [])
    assert not any("all interfaces" in rec.message for rec in caplog.records), \
        [r.message for r in caplog.records]


def test_from_env_parsing(monkeypatch):
    monkeypatch.setenv("PYNEXT_MGMT_ENABLED", "true")
    monkeypatch.setenv("PYNEXT_MGMT_KEY", TEST_KEY)
    monkeypatch.setenv("PYNEXT_MGMT_GATE_READY", "yes")
    cfg = ManagementPlaneConfig.from_env()
    assert cfg.enabled is True
    assert cfg.key == TEST_KEY
    assert cfg.readyz_requires_key is True
    cfg.validate()


def test_management_response_unit():
    """_management_response never serves metrics unauthenticated."""
    class _FakeMetrics:
        @staticmethod
        def prometheus_text():
            return "# HELP x\n"

    class _FakeApp:
        metrics = _FakeMetrics()

    app = _FakeApp()
    off = ManagementPlaneConfig()
    assert _management_response(off, app, "GET", "/metrics", []) is None
    on = ManagementPlaneConfig(enabled=True, key=TEST_KEY)
    assert _management_response(on, app, "GET", "/metrics", []).status == 401
    resp = _management_response(on, app, "GET", "/metrics",
                                [(b"authorization", b"Bearer test-only-mgmt-key")])
    assert resp.status == 200
    assert resp.body == b"# HELP x\n"
