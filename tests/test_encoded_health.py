"""Reserved health probes win over assets after exactly one path decode."""
import asyncio

import httpx
import pytest

from pynext import RateLimiter, create_app
from pynext.build import write_manifest
from pynext.server import start_native


@pytest.mark.parametrize("mode", ["asgi", "axum"])
@pytest.mark.parametrize("production", [False, True])
@pytest.mark.parametrize("limited", [False, True])
def test_encoded_health_probes_cannot_be_shadowed_by_public_assets(tmp_path, mode, production, limited):
    route = tmp_path / "app/ping"
    route.mkdir(parents=True)
    (route / "route.py").write_text('from pynext import route, Response\n@route("GET")\n'
                                  'def get(): return Response("pong")\n')
    public = tmp_path / "public"
    public.mkdir()
    for name in ("healthz", "readyz"):
        (public / name).write_text("asset shadow")
    if production:
        write_manifest(tmp_path)

    async def exercise():
        limiter = RateLimiter(1, 60) if limited else None
        if limiter is not None:
            assert limiter.allow("127.0.0.1")[0]  # probes must bypass an exhausted bucket
        app = create_app(tmp_path, production=production, rate_limit=limiter)
        server = None
        if mode == "axum":
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        else:
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        try:
            async with client:
                for stage in ("before-startup", "ready", "draining"):
                    if stage == "ready":
                        await app.startup()
                    elif stage == "draining":
                        app.begin_drain()
                    for probe, paths in (
                        ("healthz", ("/healthz", "/he%61lthz", "/%68ealthz", "/health%7a")),
                        ("readyz", ("/readyz", "/read%79z", "/%72eadyz", "/ready%7A")),
                    ):
                        status = 200 if probe == "healthz" or stage == "ready" else 503
                        body = b"ok" if probe == "healthz" else b"ready" if stage == "ready" else b"not ready"
                        for path in paths:
                            for method in ("GET", "HEAD"):
                                response = await client.request(method, path)
                                assert response.status_code == status, (stage, path, method, response.text)
                                assert response.content == (body if method == "GET" else b"")
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.shutdown()

    asyncio.run(exercise())
