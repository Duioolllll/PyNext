"""Signal errors, error digests and the error counter (REPORT §2.1 #5/#16/#17).

Pages signal 404/403/401/redirect by raising; those signals are answered
with their response and counted, but never logged as errors. Any other
exception gets a stable ``stringHash``-style digest: the 500 body and the
error log line carry the same digest (and request id), and the in-process
registry restores the original error record from the digest.
"""

import asyncio
import logging
import re

import httpx
import pytest

from pynext import (compute_digest, create_app, digest_for_error, error_count,
                    forbidden, get_digest_for_well_known_error, is_signal_error,
                    lookup_error, not_found, redirect, unauthorized)
from pynext.build import write_manifest
from pynext.errors import (ForbiddenError, NotFoundError, RedirectError,
                           SignalError, UnauthorizedError)
from pynext.observability import MetricsCollector
from pynext.server import start_native


def _write_page(root, route, body):
    directory = root / "app" / route if route else root / "app"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.py").write_text(
        "from pynext import h, page\n"
        "from pynext import not_found, forbidden, unauthorized, redirect\n"
        "@page()\n"
        f"def Page():\n    {body}\n"
    )


def _signal_app(tmp_path):
    _write_page(tmp_path, "", 'return h("p", {}, "home")')
    _write_page(tmp_path, "gone", "not_found()")
    _write_page(tmp_path, "denied", "forbidden()")
    _write_page(tmp_path, "login", "unauthorized()")
    _write_page(tmp_path, "old", 'redirect("/new", status=301)')
    _write_page(tmp_path, "new", 'return h("p", {}, "new home")')
    _write_page(tmp_path, "boom", 'raise ValueError("boom-unique-errdigest")')
    write_manifest(tmp_path)
    return create_app(tmp_path)


# -- signal taxonomy -------------------------------------------------------

def test_signal_helpers_raise_and_identify():
    assert is_signal_error(NotFoundError())
    assert is_signal_error(ForbiddenError())
    assert is_signal_error(UnauthorizedError())
    assert is_signal_error(RedirectError("/x"))
    assert not is_signal_error(ValueError("boom"))
    assert not is_signal_error(None)
    assert get_digest_for_well_known_error(NotFoundError()) == "PYNEXT_HTTP_ERROR_FALLBACK;404"
    assert get_digest_for_well_known_error(ForbiddenError()) == "PYNEXT_HTTP_ERROR_FALLBACK;403"
    assert get_digest_for_well_known_error(UnauthorizedError()) == "PYNEXT_HTTP_ERROR_FALLBACK;401"
    assert get_digest_for_well_known_error(RedirectError("/x")) == "PYNEXT_REDIRECT;push;/x;307;"
    assert get_digest_for_well_known_error(ValueError("boom")) is None
    with pytest.raises(NotFoundError):
        not_found()
    with pytest.raises(ForbiddenError):
        forbidden()
    with pytest.raises(UnauthorizedError):
        unauthorized()
    with pytest.raises(RedirectError):
        redirect("/x")


def test_signal_statuses_and_responses():
    assert NotFoundError().status == 404
    assert ForbiddenError().status == 403
    assert UnauthorizedError().status == 401
    assert NotFoundError().to_response().status == 404
    assert ForbiddenError().to_response().status == 403
    assert UnauthorizedError().to_response().status == 401
    response = RedirectError("/new", status=308, replace=True).to_response()
    assert response.status == 308
    assert dict(response.headers)[b"location"] == b"/new"
    # The metric label never carries the destination URL.
    assert RedirectError("/new").label_digest == "PYNEXT_REDIRECT"


def test_redirect_rejects_bad_input():
    with pytest.raises(ValueError):
        RedirectError("")
    with pytest.raises(ValueError):
        RedirectError("/x", status=200)
    with pytest.raises(ValueError):
        RedirectError("/x", status=404)
    with pytest.raises(ValueError):
        RedirectError("/bad\nlocation")
    with pytest.raises(ValueError):
        RedirectError("/caf\u00e9")
    for status in (301, 302, 303, 307, 308):
        assert RedirectError("/x", status=status).status == status


# -- digests ---------------------------------------------------------------

def _raise_value_error(message):
    raise ValueError(message)


def test_compute_digest_is_stable_and_message_sensitive():
    # Same call site (loop body), same message -> identical digest.
    digests = []
    for _ in range(2):
        try:
            _raise_value_error("same-message")
        except ValueError as exc:
            digests.append(compute_digest(exc))
    assert digests[0] == digests[1]
    assert re.fullmatch(r"[0-9a-f]{16}", digests[0])
    try:
        _raise_value_error("different-message")
    except ValueError as third:
        assert compute_digest(third) != digests[0]


def test_digest_for_error_dedups_and_restores():
    try:
        _raise_value_error("dedup-me-errdigest")
    except ValueError as exc:
        digest, count = digest_for_error(exc)
        assert lookup_error(digest)["type"] == "ValueError"
        assert lookup_error(digest)["message"] == "dedup-me-errdigest"
        assert lookup_error(digest)["count"] == count
        # Re-registering the same instance (re-raised through layers) does
        # not create a second entry or bump the count.
        digest_again, count_again = digest_for_error(exc)
        assert digest_again == digest
        assert count_again == count
        assert error_count(digest) == count
    assert lookup_error("0" * 16) is None
    assert error_count("0" * 16) == 0


def test_registry_evicts_oldest_when_full(monkeypatch):
    # A fresh registry (the process-global one already holds entries from
    # other tests, so its oldest entry is not ours).
    from pynext.errors import _ErrorRegistry
    from pynext import errors
    monkeypatch.setattr(errors, "_MAX_REGISTRY_ENTRIES", 3)
    registry = _ErrorRegistry()
    digests = []
    for i in range(4):
        try:
            _raise_value_error(f"evict-{i}-errdigest")
        except ValueError as exc:
            digest = compute_digest(exc)
            registry.record(exc, digest)
            digests.append(digest)
    assert registry.lookup(digests[0]) is None  # evicted
    assert registry.lookup(digests[-1]) is not None


# -- error counter -----------------------------------------------------------

def test_observe_error_snapshot_and_prometheus():
    collector = MetricsCollector()
    collector.observe_error("/gone", "NotFoundError", "PYNEXT_HTTP_ERROR_FALLBACK;404")
    collector.observe_error("/gone", "NotFoundError", "PYNEXT_HTTP_ERROR_FALLBACK;404")
    collector.observe_error("/boom", "ValueError", "0123456789abcdef")
    snap = collector.snapshot()
    assert snap["errors"]["by_type"]["/gone"]["NotFoundError"] == 2
    assert snap["errors"]["by_type"]["/boom"]["ValueError"] == 1
    assert snap["errors"]["by_digest"]["/gone"]["PYNEXT_HTTP_ERROR_FALLBACK;404"] == 2
    # Computed digests stay in the snapshot but never become a series label.
    assert snap["errors"]["by_digest"]["/boom"]["0123456789abcdef"] == 1
    text = collector.prometheus_text()
    assert ('pynext_errors_total{route="/gone",error_type="NotFoundError",'
            'digest="PYNEXT_HTTP_ERROR_FALLBACK;404"} 2') in text
    assert ('pynext_errors_total{route="/boom",error_type="ValueError",'
            'digest=""} 1') in text
    assert "0123456789abcdef" not in text


def test_observe_error_folds_past_256_digests():
    collector = MetricsCollector()
    for i in range(300):
        collector.observe_error("/boom", "ValueError", f"{i:016x}")
    by_digest = collector.snapshot()["errors"]["by_digest"]["/boom"]
    assert by_digest["__overflow__"] == 300 - 256
    assert len(by_digest) == 257  # 256 kept + the overflow bucket


def test_observe_error_disabled_is_noop():
    collector = MetricsCollector(enabled=False)
    collector.observe_error("/gone", "NotFoundError", "PYNEXT_HTTP_ERROR_FALLBACK;404")
    assert collector.snapshot() == {"enabled": False}


# -- dispatch: signals are answered, not logged ------------------------------

def test_signals_answer_without_error_log(tmp_path, caplog):
    app = _signal_app(tmp_path)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            gone = await client.get("/gone")
            assert gone.status_code == 404
            assert gone.headers["x-request-id"]
            denied = await client.get("/denied")
            assert denied.status_code == 403
            login = await client.get("/login")
            assert login.status_code == 401
            old = await client.get("/old", follow_redirects=False)
            assert old.status_code == 301
            assert old.headers["location"] == "/new"
            new = await client.get("/new")
            assert new.status_code == 200 and "new home" in new.text
        return app.metrics.snapshot()
    with caplog.at_level(logging.INFO, logger="pynext"):
        snap = asyncio.run(run())
    assert not [r for r in caplog.records
                if r.levelno >= logging.ERROR and "Request failed" in r.message]
    assert snap["errors"]["by_type"]["/gone"]["NotFoundError"] == 1
    assert snap["errors"]["by_type"]["/denied"]["ForbiddenError"] == 1
    assert snap["errors"]["by_type"]["/login"]["UnauthorizedError"] == 1
    assert snap["errors"]["by_type"]["/old"]["RedirectError"] == 1
    assert snap["errors"]["by_digest"]["/gone"]["PYNEXT_HTTP_ERROR_FALLBACK;404"] == 1
    assert snap["errors"]["by_digest"]["/old"]["PYNEXT_REDIRECT"] == 1


def test_500_body_carries_request_id_and_digest(tmp_path, caplog):
    app = _signal_app(tmp_path)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/boom")
    with caplog.at_level(logging.INFO, logger="pynext"):
        response = asyncio.run(run())
    assert response.status_code == 500
    assert "<private" not in response.text and "boom-unique-errdigest" not in response.text
    match = re.fullmatch(r"Internal Server Error \(request_id: ([0-9a-f-]+), "
                         r"digest: ([0-9a-f]{16})\)", response.text)
    assert match, response.text
    request_id, digest = match.groups()
    # Body id, header id and log line all agree, so a reported 500 can be
    # found in the logs.
    assert response.headers["x-request-id"] == request_id
    failed = [r for r in caplog.records
              if r.levelno >= logging.ERROR and "Request failed" in r.message]
    assert len(failed) == 1
    assert getattr(failed[0], "error_digest", None) == digest
    record = lookup_error(digest)
    assert record["type"] == "ValueError"
    assert record["message"] == "boom-unique-errdigest"
    assert record["count"] >= 1
    assert app.metrics.snapshot()["errors"]["by_type"]["/boom"]["ValueError"] == 1


def test_500_dev_page_shows_digest_and_count(tmp_path):
    _write_page(tmp_path, "", 'raise ValueError("dev-boom-errdigest")')
    write_manifest(tmp_path)
    app = create_app(tmp_path, debug=True)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/")
    response = asyncio.run(run())
    assert response.status_code == 500
    assert "request_id:" in response.text
    match = re.search(r"digest: ([0-9a-f]{16}) \(seen (\d+) time", response.text)
    assert match, response.text
    assert int(match.group(2)) >= 1
    assert lookup_error(match.group(1))["message"] == "dev-boom-errdigest"


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_signals_and_500_on_both_transports(tmp_path, mode, caplog):
    app = _signal_app(tmp_path)
    server = None

    async def run():
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            nonlocal server
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                gone = await client.get("/gone")
                assert gone.status_code == 404
                boom = await client.get("/boom")
                assert boom.status_code == 500
                assert re.search(r"digest: [0-9a-f]{16}", boom.text)
                assert boom.headers["x-request-id"] in boom.text
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    with caplog.at_level(logging.INFO, logger="pynext"):
        asyncio.run(run())
    # The 404 signal wrote no error log; the 500 wrote exactly one.
    failed = [r for r in caplog.records
              if r.levelno >= logging.ERROR and "Request failed" in r.message]
    assert len(failed) == 1
    assert getattr(failed[0], "error_digest", None)


def test_signal_error_is_base_class():
    assert issubclass(NotFoundError, SignalError)
    assert issubclass(RedirectError, SignalError)
