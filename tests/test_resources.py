import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import serve


class Resource:
    def __init__(self, name, events, *, fail=False):
        self.name, self.events, self.fail = name, events, fail
        self.value = None

    async def startup(self):
        self.events.append("start:" + self.name)
        if self.fail:
            raise RuntimeError("startup failed")
        self.value = "connected"

    async def shutdown(self):
        self.events.append("stop:" + self.name)
        self.value = None


def project(root):
    app = root / "app"
    app.mkdir()
    (app / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"db": request.resources["db"].value})\n'
    )


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_resources_start_before_requests_and_close_in_reverse(tmp_path, mode):
    project(tmp_path)
    events = []
    app = create_app(tmp_path, resources={"db": Resource("db", events), "other": Resource("other", events)})

    async def run():
        task = None
        stop = asyncio.Event()
        if mode == "asgi":
            await app.startup()
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(serve(app, port=0, stop_event=stop, on_ready=ready.set_result))
            address = await asyncio.wait_for(ready, 5)
            client = httpx.AsyncClient(base_url=f"http://{address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/")).json() == {"db": "connected"}
        finally:
            if task:
                stop.set()
                await task
            else:
                await app.shutdown()
        assert events == ["start:db", "start:other", "stop:other", "stop:db"]
    asyncio.run(run())


def test_resource_start_failure_rolls_back_started_resources(tmp_path):
    project(tmp_path)
    events = []
    app = create_app(tmp_path, resources={"db": Resource("db", events),
                                         "broken": Resource("broken", events, fail=True)})
    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(app.startup())
    assert events == ["start:db", "start:broken", "stop:broken", "stop:db"]


def test_resource_file_is_loaded_and_verified_in_production(tmp_path):
    project(tmp_path)
    source = tmp_path / "resources.py"
    source.write_text(
        'class Database:\n'
        '    value = None\n'
        '    async def startup(self): self.value = "connected"\n'
        '    async def shutdown(self): self.value = None\n'
        'RESOURCES = {"db": Database()}\n'
    )
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def run():
        await app.startup()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/")).json() == {"db": "connected"}
        finally:
            await app.shutdown()
    asyncio.run(run())
    source.write_text(source.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="sources changed"):
        create_app(tmp_path, production=True)
