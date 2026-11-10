import asyncio
import json
from html.parser import HTMLParser

import httpx
import pytest

from pynext import create_app, h, island
from pynext.build import write_manifest
from pynext.server import start_native

def make_project(root, mode, strategy="load"):
    (root / "app").mkdir()
    (root / "islands").mkdir()
    decorator = '@page(render="isr", revalidate=30)' if mode == "isr" else f'@page(render="{mode}")'
    (root / "app" / "page.py").write_text(
        'from pynext import h, island, page\n' + decorator + '\n'
        f'def Page(): return h("main", {{}}, island("counter", props={{"initial": 3}}, hydrate=True, strategy="{strategy}", fallback=h("button", {{}}, "Count: 3")))\n'
    )
    (root / "islands" / "counter.js").write_text(
        'export function render(props) { const b=document.createElement("button"); b.textContent=`Count: ${props.initial}`; return b }\n'
        'export function hydrate(el, props) { return () => {} }\n'
        'export function mount(el, props) { el.replaceChildren(render(props)); return hydrate(el, props) }\n'
    )


class IslandParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-pynext-island" in attrs:
            self.attrs.append(attrs)


@pytest.mark.parametrize("mode", ["ssr", "ssg", "isr"])
@pytest.mark.parametrize("strategy", ["load", "visible", "idle", "interaction"])
def test_hydrated_markup_in_both_servers(tmp_path, esbuild, mode, strategy):
    make_project(tmp_path, mode, strategy)
    manifest = write_manifest(tmp_path)
    assert manifest["client"]["hydration"] == {"counter": True}

    async def check(client):
        response = await client.get("/")
        assert response.status_code == 200 and "<button>Count: 3</button>" in response.text
        parser = IslandParser()
        parser.feed(response.text)
        assert len(parser.attrs) == 1
        assert parser.attrs[0]["data-pynext-mode"] == "hydrate"
        assert parser.attrs[0]["data-pynext-strategy"] == strategy
        assert json.loads(parser.attrs[0]["data-pynext-props"]) == {"initial": 3}

    async def run():
        app = create_app(tmp_path, production=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await check(client)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                await check(client)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_hydration_requires_capable_module_and_server_content(tmp_path, esbuild):
    make_project(tmp_path, "ssg")
    source = tmp_path / "islands" / "counter.js"
    source.write_text('export function mount(el) { el.textContent = "client" }\n')
    with pytest.raises(ValueError, match="must export render and hydrate"):
        write_manifest(tmp_path)
    with pytest.raises(ValueError, match="requires server-rendered"):
        island("counter", hydrate=True)
    with pytest.raises(TypeError, match="boolean"):
        island("counter", fallback=h("p", {}, "text"), hydrate="yes")


def test_client_hydration_manifest_validation(tmp_path, esbuild):
    make_project(tmp_path, "ssr")
    write_manifest(tmp_path)
    path = tmp_path / ".pynext" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["client"]["hydration"]["counter"] = "yes"
    path.write_text(json.dumps(manifest))
    # The edited manifest is a new build output, so its BUILD_ID must
    # be refreshed too — otherwise load_manifest rejects it as an
    # incomplete build before the client manifest is even checked.
    from pynext.build import build_id_for
    (tmp_path / ".pynext" / "BUILD_ID").write_text(build_id_for(path.read_bytes()) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Incompatible client manifest"):
        create_app(tmp_path, production=True)
