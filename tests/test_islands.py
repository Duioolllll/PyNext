import asyncio
import json
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from pynext import _native, create_app, h, island
from pynext.build import write_manifest
from pynext.components import bind_islands
from pynext.server import start_native

ROOT = Path(__file__).resolve().parents[1]
ATTACK = '</script><img src=x onerror=alert(1)>"&'


@pytest.fixture
def esbuild(monkeypatch):
    executable = ROOT / "node_modules" / ".bin" / "esbuild"
    if not executable.is_file():
        pytest.fail("Run npm ci to install the islands build dependency")
    monkeypatch.setenv("PYNEXT_ESBUILD", str(executable))


def make_project(root, mode="ssr"):
    (root / "app" / "plain").mkdir(parents=True)
    (root / "app" / "[...path]").mkdir(parents=True)
    (root / "islands" / "lib").mkdir(parents=True)
    decorator = '@page(render="isr", revalidate=30)' if mode == "isr" else f'@page(render="{mode}")'
    (root / "app" / "page.py").write_text(
        'from pynext import h, island, page\n' + decorator + '\n'
        'def Page():\n'
        f'    return h("html", {{}}, h("body", {{}}, h("h1", {{}}, "Hello"), island("counter", props={{"initial": 1, "text": {ATTACK!r}}}, fallback=h("p", {{}}, "Loading"))))\n'
    )
    (root / "app" / "plain" / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "plain")\n'
    )
    (root / "app" / "[...path]" / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "catch-all")\n'
    )
    (root / "islands" / "counter.ts").write_text(
        'import { label } from "./lib/label";\n'
        'export function mount(el: HTMLElement, props: {initial: number}) { el.textContent = label(props.initial); }\n'
    )
    (root / "islands" / "lib" / "label.ts").write_text('export function label(n: number) { return `Count: ${n}`; }\n')
    (root / "islands" / "unused.js").write_text('export function mount(el) { el.textContent = "unused" }\n')


class HTMLNodes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.islands, self.scripts, self.tags = [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if "data-pynext-island" in attrs:
            self.islands.append(attrs)
        if tag == "script":
            self.scripts.append(attrs)


@pytest.mark.parametrize("mode", ["ssr", "ssg", "isr"])
def test_islands_in_both_servers_and_render_modes(tmp_path, esbuild, mode, monkeypatch):
    make_project(tmp_path, mode)
    manifest = write_manifest(tmp_path)
    assert set(manifest["client"]["bundles"]) == {"counter", "unused"}
    # Production startup consumes verified artifacts and does not require esbuild.
    monkeypatch.setenv("PYNEXT_ESBUILD", "/does/not/exist")

    async def check(client, app):
        response = await client.get("/")
        assert response.status_code == 200
        doc = HTMLNodes()
        doc.feed(response.text)
        assert len(doc.islands) == len(doc.scripts) == 1
        assert "img" not in doc.tags and "Loading" in response.text
        attrs = doc.islands[0]
        assert json.loads(attrs["data-pynext-props"]) == {"initial": 1, "text": ATTACK}
        assert attrs["data-pynext-module"].endswith(app.client_manifest["bundles"]["counter"] + ".js")
        assert app.client_manifest["bundles"]["unused"] not in response.text
        runtime = doc.scripts[0]["src"]
        assert doc.scripts[0]["type"] == "module"
        assert response.text.index(runtime) < response.text.index("</body>")
        for url in (runtime, attrs["data-pynext-module"]):
            result = await client.get(url)
            assert result.status_code == 200
            assert result.headers["content-type"] == "text/javascript; charset=utf-8"
            assert result.headers["cache-control"] == "public, max-age=31536000, immutable"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert (await client.post(url)).status_code == 405
        assert (await client.get("/plain")).text == "<p>plain</p>"
        assert (await client.get("/_pynext/static/not-found.js")).status_code == 404
        return runtime, attrs["data-pynext-module"]

    async def run():
        app = create_app(tmp_path, production=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            urls = await check(client, app)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as native:
                await check(native, app)
                async def fail_dispatch(request):
                    raise AssertionError("client asset crossed the Python boundary")
                app.dispatch = fail_dispatch
                for url in urls:
                    assert (await native.get(url)).status_code == 200
                assert (await native.get("/plain")).status_code == 500
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_client_sources_and_artifacts_are_verified(tmp_path, esbuild):
    make_project(tmp_path)
    manifest = write_manifest(tmp_path)
    source = tmp_path / "islands" / "lib" / "label.ts"
    original = source.read_text()
    source.write_text(original + "// changed\n")
    with pytest.raises(ValueError, match="changed since build"):
        create_app(tmp_path, production=True)
    source.write_text(original)
    digest = manifest["client"]["bundles"]["counter"]
    (tmp_path / ".pynext" / "static" / f"{digest}.js").write_text("corrupt")
    with pytest.raises(ValueError, match="integrity check"):
        create_app(tmp_path, production=True)


@pytest.mark.parametrize("source, message", [
    ('import { secret } from "../secret.js"; export function mount() { return secret }', "outside"),
    ('import x from "https://example.com/x.js"; export function mount() { return x }', "external imports"),
    ('export function mount(el, props) { return import(props.url) }', "Could not bundle"),
    ('export const notMount = 1', "named mount export"),
])
def test_client_bundle_rejects_untracked_or_invalid_imports(tmp_path, esbuild, source, message):
    make_project(tmp_path)
    (tmp_path / "secret.js").write_text('export const secret = "do not ship"')
    (tmp_path / "islands" / "counter.ts").write_text(source)
    with pytest.raises(ValueError, match=message):
        write_manifest(tmp_path)


def test_missing_unknown_or_nested_islands_fail_safely(tmp_path, esbuild, monkeypatch):
    make_project(tmp_path, "ssg")
    source = tmp_path / "app" / "page.py"
    original = source.read_text()
    source.write_text(original.replace('island("counter",', 'island("missing",'))
    with pytest.raises(ValueError, match="Unknown island"):
        write_manifest(tmp_path)
    source.write_text(original.replace('fallback=h("p", {}, "Loading")', 'fallback=island("unused")'))
    with pytest.raises(ValueError, match="Nested islands"):
        write_manifest(tmp_path)
    source.write_text(original)
    monkeypatch.setenv("PYNEXT_ESBUILD", "/does/not/exist")
    with pytest.raises(ValueError, match="PYNEXT_ESBUILD"):
        write_manifest(tmp_path)


def test_reserved_namespace_and_hidden_sources_are_rejected(tmp_path, esbuild):
    make_project(tmp_path)
    reserved = tmp_path / "public" / "_pynext"
    reserved.mkdir(parents=True)
    (reserved / "x.js").write_text("public")
    with pytest.raises(ValueError, match="Reserved PyNext path"):
        write_manifest(tmp_path)
    (reserved / "x.js").unlink()
    reserved.rmdir()
    (tmp_path / "islands" / ".secret.js").write_text("secret")
    with pytest.raises(ValueError, match="Invalid public"):
        write_manifest(tmp_path)


def test_island_source_count_is_bounded(tmp_path, esbuild, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setattr("pynext.client.MAX_SOURCE_FILES", 2)
    with pytest.raises(ValueError, match="count or size limit"):
        write_manifest(tmp_path)


@pytest.mark.parametrize("props", [
    {"a": float("nan")}, {"a": 2**53}, {"a": {1: "value"}},
    {"a": object()}, {"a": "x" * 20_000},
])
def test_invalid_props_are_rejected(props):
    with pytest.raises((TypeError, ValueError)):
        island("counter", props=props)


def test_island_prop_cycles_and_invalid_names_are_rejected():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycles"):
        island("counter", props={"cycle": cycle})
    with pytest.raises(ValueError, match="name"):
        island("../secret", props={})
    assert island("counter", fallback=h("p", {}, "Loading"))["children"][0]["kind"] == "element"


def test_multiple_islands_share_one_runtime_and_internal_nodes_are_rejected():
    manifest = {"runtime": "a" * 64, "bundles": {"counter": "b" * 64}}
    tree = h("main", {}, island("counter", props={"initial": 1}), island("counter", props={"initial": 2}))
    rendered = _native.render(json.dumps(bind_islands(tree, manifest)))
    assert rendered.count('data-pynext-island="counter"') == 2
    assert rendered.count('<script type="module"') == 1
    with pytest.raises(ValueError, match="framework-internal"):
        bind_islands({"kind": "client_runtime", "src": "https://example.com"}, manifest)
