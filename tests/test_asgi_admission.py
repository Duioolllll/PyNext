"""ASGI admission must bound slow decisions before receiving request bodies."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import uuid

import httpx
import pytest

from pynext import RateLimiter, RedisRateLimiter, create_app
from pynext.admission import DecisionGate


def project(root):
    for name in ("input", "socket"):
        (root / "app" / name).mkdir(parents=True)
    (root / "app/input/route.py").write_text(
        'from pynext import Response, route\ncalls = []\n@route("POST")\n'
        'async def post(request):\n    calls.append(request.body)\n    return Response("ok")\n')
    (root / "app/socket/route.py").write_text(
        'from pynext import websocket\n@websocket\nasync def socket(websocket):\n'
        '    await websocket.accept()\n    await websocket.close()\n')


async def wait_until(check):
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


async def request(app, *, websocket=False, path=None, raw_path=None, no_receive=True, method="POST"):
    scope = {"type": "websocket" if websocket else "http", "path": path or ("/socket" if websocket else "/input"),
             "method": method, "scheme": "http", "client": ("127.0.0.1", 1234),
             "headers": [(b"host", b"localhost"), (b"origin", b"http://localhost")]}
    if raw_path is not None:
        scope["raw_path"] = raw_path
    messages = asyncio.Queue()
    await messages.put({"type": "websocket.connect"} if websocket else {"type": "http.request", "body": b""})
    sent = []
    async def receive():
        if no_receive:
            raise AssertionError("Admission denial must not receive a body/connect message")
        return await messages.get()
    async def send(message):
        sent.append(message)
    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize("websocket", [False, True])
def test_unmarked_blocking_callback_times_out_and_fails_open_without_blocking_health(tmp_path, monkeypatch, websocket):
    project(tmp_path)
    release = threading.Event()
    class Slow(RateLimiter):
        # No blocking=True hint: an arbitrary application callback can block.
        def allow(self, key):
            release.wait(5)
            return True, None
    monkeypatch.setattr("pynext.app.ASGI_DECISION_TIMEOUT", 0.1)

    async def exercise():
        app = create_app(tmp_path, rate_limit=Slow(1, 60))
        try:
            # H-2: no decision within the deadline, so the request is
            # admitted instead of 503ing, and the miss is counted.
            sent = await asyncio.wait_for(request(app, websocket=websocket, no_receive=False), 1)
            if websocket:
                assert sent[0]["type"] == "websocket.accept"
            else:
                assert sent[0]["status"] == 200
                assert app._module("app/input/route.py").calls == [b""]
            assert app.metrics.snapshot()["rate_limit"]["fail_open"] == 1
            assert not await app.wait_for_inflight(0)
            health = await asyncio.wait_for(request(app, path="/healthz", method="GET", no_receive=False), 1)
            assert health[0]["status"] == 200
        finally:
            release.set()
            assert await app.wait_for_inflight(3)
            await app.shutdown()
    asyncio.run(exercise())


def test_http_and_websocket_share_64_slots_and_timeouts_fail_open(tmp_path):
    project(tmp_path)
    release = threading.Event()
    calls = []
    class Slow(RateLimiter):
        def allow(self, key):
            calls.append(key)
            release.wait(10)
            return True, None

    async def exercise():
        app = create_app(tmp_path, rate_limit=Slow(1000, 60))
        tasks = []
        try:
            tasks = [asyncio.create_task(request(app, websocket=i % 2 == 0, no_receive=False)) for i in range(64)]
            await wait_until(lambda: app._asgi_admission.inflight == 64)
            # H-2: the gate is full, so no decision can be produced for
            # the next requests: they are admitted instead of 503ing.
            for websocket in (False, True):
                sent = await asyncio.wait_for(request(app, websocket=websocket, no_receive=False), 1)
                if websocket:
                    assert sent[0]["type"] == "websocket.accept"
                else:
                    assert sent[0]["status"] == 200
            results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert all(
                (result[0]["type"] == "websocket.accept" if result[0].get("type", "").startswith("websocket")
                 else result[0]["status"] == 200)
                for result in results)
            assert not await app.wait_for_inflight(0)
            # Timed-out running work still owns its slots.
            assert (await asyncio.wait_for(request(app, no_receive=False), 1))[0]["status"] == 200
            before = len(calls)
            release.set()
            assert await app.wait_for_inflight(3)
            # Expired executor-queued jobs must never charge a token later.
            assert len(calls) == before
            recovered = await request(app, no_receive=False)
            assert recovered[0]["status"] == 200
            assert len(calls) == before + 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.wait_for_inflight(3)
            await app.shutdown()
    asyncio.run(exercise())


def test_cancelled_executor_queued_decision_never_runs():
    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        gate = DecisionGate(capacity=2, timeout=5)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow():
            entered.set()
            release.wait(5)
            return True, None
        first = asyncio.create_task(gate.check(slow))
        second = None
        try:
            await wait_until(entered.is_set)
            second = asyncio.create_task(gate.check(lambda: calls.append("charged")))
            await wait_until(lambda: gate.inflight == 2)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            release.set()
            assert await first == (True, None)
            await wait_until(lambda: gate.inflight == 0)
            assert calls == []
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("raw_path,path", [
    (b"/%ff", "/replacement"), (b"/%2finput", "//input"), (b"/input%", "/input%"),
    (b"/../input", "/../input"), (b"/healthz%zz", "/healthz"),
])
def test_malformed_http_paths_are_rejected_before_charging(tmp_path, raw_path, path):
    project(tmp_path)
    class Counting(RateLimiter):
        calls = 0
        def allow(self, key):
            self.calls += 1
            return super().allow(key)
    async def exercise():
        limiter = Counting(1, 60)
        app = create_app(tmp_path, rate_limit=limiter)
        try:
            limiter.allow("127.0.0.1")  # exhausted bucket must not hide invalid paths with 429
            sent = await request(app, path=path, raw_path=raw_path)
            assert sent[0]["status"] == 400
            assert limiter.calls == 1
        finally:
            await app.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize("decision,status", [(None, 503), ((None, None), 503),
    ((True, "invalid"), 503), ((False, -1), 503), ((False, None), 429), ((False, 0), 429)])
@pytest.mark.parametrize("websocket", [False, True])
def test_invalid_decision_fails_closed_and_missing_retry_defaults_to_one(tmp_path, decision, status, websocket):
    project(tmp_path)
    class Invalid(RateLimiter):
        def allow(self, key):
            return decision
    async def exercise():
        app = create_app(tmp_path, rate_limit=Invalid(1, 60))
        try:
            sent = await request(app, websocket=websocket)
            assert sent[0]["status"] == status
            assert (b"retry-after", b"1") in sent[0]["headers"]
        finally:
            await app.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_asgi_http_and_websocket_charge_one_shared_token(tmp_path, backend):
    project(tmp_path)
    if backend == "redis":
        pytest.importorskip("redis")
        url = os.environ.get("PYNEXT_TEST_REDIS_URL")
        if not url:
            pytest.skip("PYNEXT_TEST_REDIS_URL is not set")
        limiter = RedisRateLimiter(1, 60, url=url, namespace=uuid.uuid4().hex)
    else:
        limiter = RateLimiter(1, 60)
    async def exercise():
        app = create_app(tmp_path, rate_limit=limiter)
        try:
            sent = await request(app, no_receive=False)
            assert sent[0]["status"] == 200
            sent = await request(app, websocket=True)
            assert sent[0]["status"] == 429
            assert 1 <= int(dict(sent[0]["headers"])[b"retry-after"]) <= 60
            assert app._module("app/input/route.py").calls == [b""]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                assert (await client.get("/healthz")).status_code == 200
        finally:
            await app.shutdown()
            if backend == "redis":
                limiter.close()
    asyncio.run(exercise())
