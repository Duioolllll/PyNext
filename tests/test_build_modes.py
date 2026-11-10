"""Build mode split: --only-islands (compile) / --only-static (generate),
and the static-only export (pynext build --static-only).

Track E P3-12 (REPORT.md §2.3 E): the Next.js ``experimentalBuildMode``
``compile`` / ``generate`` equivalent, plus a deliberately narrow static-only
mode — SSG pages + public assets + island bundles into a plain directory
servable without a server. It is *not* a full ``output: export`` port:
apps with dynamic SSR, ISR or API routes are refused.
"""
import functools
import http.server
import json
import sys
import threading

import httpx
import pytest

from pynext import cli, create_app
from pynext.build import export_static, load_manifest, write_manifest

ISLAND = (
    'export function mount(element, props) {\n'
    '  const button = document.createElement("button");\n'
    '  button.textContent = "Count: " + props.initial;\n'
    '  element.replaceChildren(button);\n'
    '}\n'
)

SSG_PAGE = (
    'from pynext import h, page\n'
    '@page(render="ssg")\n'
    'def Page(): return h("p", {}, "built")\n'
)

SSG_ISLAND_PAGE = (
    'from pynext import h, page, island\n'
    '@page(render="ssg")\n'
    'def Page(): return h("div", {}, island("counter", props={"initial": 1}))\n'
)


def make_app(root, *, island=False, render="ssg", api=False, public=False):
    app_dir = root / "app"
    (app_dir).mkdir(parents=True, exist_ok=True)
    if island:
        (app_dir / "page.py").write_text(SSG_ISLAND_PAGE, encoding="utf-8")
        islands = root / "islands"
        islands.mkdir(exist_ok=True)
        (islands / "counter.js").write_text(ISLAND, encoding="utf-8")
    else:
        (app_dir / "page.py").write_text(
            'from pynext import h, page\n'
            f'@page(render="{render}")\n'
            'def Page(): return h("p", {}, "built")\n',
            encoding="utf-8")
    if api:
        route_dir = app_dir / "api"
        route_dir.mkdir(exist_ok=True)
        (route_dir / "route.py").write_text(
            'from pynext import JSONResponse, route\n'
            '@route("GET")\n'
            'async def get(request): return JSONResponse({"ok": True})\n',
            encoding="utf-8")
    if public:
        public_dir = root / "public"
        public_dir.mkdir(exist_ok=True)
        (public_dir / "hello.txt").write_bytes(b"hello public\n")
    return root


def test_full_build_records_both_stages(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    manifest = write_manifest(tmp_path)
    assert manifest["stages"] == ["compile", "generate"]
    assert set(manifest["static_pages"]) == {"/"}
    assert set(manifest["client"]["bundles"]) == {"counter"}


def test_compile_only_then_generate(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    manifest = write_manifest(tmp_path, stages=("compile",))
    assert manifest["stages"] == ["compile"]
    assert manifest["static_pages"] == {}
    assert set(manifest["client"]["bundles"]) == {"counter"}
    with pytest.raises(ValueError, match="Static pages were not generated"):
        load_manifest(tmp_path)
    manifest = write_manifest(tmp_path, stages=("generate",))
    assert manifest["stages"] == ["compile", "generate"]
    assert set(manifest["static_pages"]) == {"/"}
    assert load_manifest(tmp_path)["stages"] == ["compile", "generate"]
    app = create_app(tmp_path, production=True)
    assert set(app.static_pages) == {"/"}
    bundle = manifest["client"]["bundles"]["counter"]
    assert bundle.encode() in app.static_pages["/"]


def test_generate_only_without_previous_build_fails(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    with pytest.raises(ValueError, match="No previous build found"):
        write_manifest(tmp_path, stages=("generate",))


def test_generate_only_after_island_change_fails(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    write_manifest(tmp_path)
    (tmp_path / "islands" / "counter.js").write_text(ISLAND.replace("Count: ", "Total: "),
                                                     encoding="utf-8")
    with pytest.raises(ValueError, match="Island sources changed since the last compile"):
        write_manifest(tmp_path, stages=("generate",))


def test_compile_only_after_island_change_drops_generate(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    first = write_manifest(tmp_path)
    old_digest = first["client"]["bundles"]["counter"]
    (tmp_path / "islands" / "counter.js").write_text(ISLAND.replace("Count: ", "Total: "),
                                                     encoding="utf-8")
    manifest = write_manifest(tmp_path, stages=("compile",))
    assert manifest["stages"] == ["compile"]
    assert manifest["client"]["bundles"]["counter"] != old_digest
    with pytest.raises(ValueError, match="Static pages were not generated"):
        load_manifest(tmp_path)
    manifest = write_manifest(tmp_path, stages=("generate",))
    assert manifest["stages"] == ["compile", "generate"]
    body = manifest["static_pages"]["/"]
    assert manifest["client"]["bundles"]["counter"].encode() in (
        tmp_path / ".pynext" / "static" / f"{body['sha256']}.html").read_bytes()
    assert old_digest.encode() not in (
        tmp_path / ".pynext" / "static" / f"{body['sha256']}.html").read_bytes()


def test_compile_only_without_changes_keeps_complete_build(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    write_manifest(tmp_path)
    manifest = write_manifest(tmp_path, stages=("compile",))
    assert manifest["stages"] == ["compile", "generate"]
    assert set(manifest["static_pages"]) == {"/"}
    load_manifest(tmp_path)


def test_generate_only_rerenders_changed_page_sources(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    first = write_manifest(tmp_path)
    first_body = (tmp_path / ".pynext" / "static" /
                  f"{first['static_pages']['/']['sha256']}.html").read_bytes()
    (tmp_path / "app" / "page.py").write_text(
        SSG_ISLAND_PAGE.replace('props={"initial": 1}', 'props={"initial": 2}'),
        encoding="utf-8")
    manifest = write_manifest(tmp_path, stages=("generate",))
    assert manifest["stages"] == ["compile", "generate"]
    assert manifest["client"]["bundles"] == first["client"]["bundles"]
    body = (tmp_path / ".pynext" / "static" /
            f"{manifest['static_pages']['/']['sha256']}.html").read_bytes()
    assert body != first_body


def test_write_manifest_rejects_invalid_stages(tmp_path):
    make_app(tmp_path)
    with pytest.raises(ValueError, match="Build stages must be"):
        write_manifest(tmp_path, stages=("compile", "bogus"))
    with pytest.raises(ValueError, match="Build stages must be"):
        write_manifest(tmp_path, stages=())


def test_static_only_exports_servable_files(tmp_path, esbuild):
    make_app(tmp_path, island=True, public=True)
    manifest = write_manifest(tmp_path)
    summary = export_static(tmp_path, manifest)
    output = tmp_path / ".pynext" / "export"
    assert summary["output"] == str(output)
    assert summary["pages"] == 1 and summary["assets"] == 1
    assert summary["bundles"] == len(manifest["client"]["bundles"]) + 1  # + runtime
    page = output / "index.html"
    assert page.read_bytes() == (tmp_path / ".pynext" / "static" /
                                 f"{manifest['static_pages']['/']['sha256']}.html").read_bytes()
    assert (output / "hello.txt").read_text(encoding="utf-8") == "hello public\n"
    for digest in {manifest["client"]["runtime"], *manifest["client"]["bundles"].values()}:
        assert (output / "_pynext" / "static" / f"{digest}.js").is_file()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        with httpx.Client(trust_env=False, base_url=base_url) as client:
            response = client.get("/")
            assert response.status_code == 200 and response.text == page.read_text(encoding="utf-8")
            bundle = manifest["client"]["bundles"]["counter"]
            bundle_response = client.get(f"/_pynext/static/{bundle}.js")
            assert bundle_response.status_code == 200 and "Count: " in bundle_response.text
            assert client.get("/hello.txt").text == "hello public\n"
            assert client.get("/missing").status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_static_only_exports_nested_pages(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "blog").mkdir(parents=True)
    (app_dir / "page.py").write_text(SSG_PAGE, encoding="utf-8")
    (app_dir / "blog" / "page.py").write_text(SSG_PAGE, encoding="utf-8")
    manifest = write_manifest(tmp_path)
    summary = export_static(tmp_path, manifest)
    assert summary["pages"] == 2
    output = tmp_path / ".pynext" / "export"
    assert (output / "index.html").is_file()
    assert (output / "blog" / "index.html").is_file()


def test_static_only_refuses_dynamic_isr_and_api_routes(tmp_path):
    make_app(tmp_path, render="ssr")
    write_manifest(tmp_path)
    with pytest.raises(ValueError, match=r"cannot include dynamic routes: / \(render=ssr\)"):
        export_static(tmp_path)
    isr_dir = tmp_path / "app"
    (isr_dir / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60, stale=30)\n'
        'def Page(): return h("p", {}, "isr")\n',
        encoding="utf-8")
    write_manifest(tmp_path)
    with pytest.raises(ValueError, match=r"cannot include dynamic routes: / \(render=isr\)"):
        export_static(tmp_path)
    make_app(tmp_path, api=True)
    write_manifest(tmp_path)
    with pytest.raises(ValueError, match=r"cannot include dynamic routes: /api \(API route\)"):
        export_static(tmp_path)


def test_static_only_refuses_partial_build(tmp_path, esbuild):
    make_app(tmp_path, island=True)
    write_manifest(tmp_path, stages=("compile",))
    with pytest.raises(ValueError, match="Static pages were not generated"):
        export_static(tmp_path)


def test_static_only_rejects_nonempty_custom_dir(tmp_path):
    make_app(tmp_path, public=True)
    manifest = write_manifest(tmp_path)
    custom = tmp_path / "out"
    custom.mkdir()
    (custom / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="Export directory is not empty"):
        export_static(tmp_path, manifest, custom)
    assert (custom / "keep.txt").read_text(encoding="utf-8") == "keep"
    summary = export_static(tmp_path, manifest, custom.parent / "fresh")
    assert summary["output"] == str(custom.parent / "fresh")
    # Re-exporting into the default directory clears the previous export.
    export_static(tmp_path, manifest)
    stale = tmp_path / ".pynext" / "export" / "stale.txt"
    stale.write_text("stale", encoding="utf-8")
    export_static(tmp_path, manifest)
    assert not stale.exists()
    assert (tmp_path / ".pynext" / "export" / "index.html").is_file()


def test_static_only_manifest_without_build(tmp_path):
    make_app(tmp_path)
    with pytest.raises(ValueError, match="No build manifest found"):
        export_static(tmp_path)


# --- CLI wiring -----------------------------------------------------------

def test_cli_only_islands_passes_stage(monkeypatch, tmp_path, capsys):
    seen = []

    def fake_manifest(path, stages=None):
        seen.append(stages)
        return {"routes": {"/": {}}, "client": {"bundles": {"counter": "x" * 64}},
                "static_pages": {}}

    monkeypatch.setattr(cli, "write_manifest", fake_manifest)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path), "--only-islands"])
    cli.main()
    assert seen == [("compile",)]
    assert "Compiled 1 island bundles" in capsys.readouterr().out


def test_cli_only_static_passes_stage(monkeypatch, tmp_path, capsys):
    seen = []

    def fake_manifest(path, stages=None):
        seen.append(stages)
        return {"routes": {"/": {}}, "client": {"bundles": {}},
                "static_pages": {"/": {"pattern": "/", "sha256": "x" * 64}}}

    monkeypatch.setattr(cli, "write_manifest", fake_manifest)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path), "--only-static"])
    cli.main()
    assert seen == [("generate",)]
    assert "Generated 1 static pages" in capsys.readouterr().out


def test_cli_static_only_calls_export(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_manifest(path, stages=None, **kwargs):
        # --static-only runs a full build, so the CLI passes
        # deployment_id/lock_timeout like the plain build path does.
        return {"routes": {"/": {}}}

    def fake_export(root, manifest, output=None):
        seen["output"] = output
        return {"pages": 2, "assets": 1, "bundles": 2, "output": str(tmp_path / "out")}

    monkeypatch.setattr(cli, "write_manifest", fake_manifest)
    monkeypatch.setattr(cli, "export_static", fake_export)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path),
                                      "--static-only", "--export-dir", str(tmp_path / "out")])
    cli.main()
    assert seen["output"] == tmp_path / "out"
    out = capsys.readouterr().out
    assert "Built 1 routes" in out
    assert "Exported 2 static pages" in out
    assert "python -m http.server" in out


@pytest.mark.parametrize("argv", [
    ["pynext", "build", "--only-islands", "--only-static"],
    ["pynext", "build", "--static-only", "--only-islands"],
    ["pynext", "build", "--static-only", "--only-static"],
    ["pynext", "build", "--export-dir", "out"],
    ["pynext", "build", "--standalone", "--only-islands"],
    ["pynext", "build", "--standalone", "--only-static"],
])
def test_cli_rejects_conflicting_build_modes(monkeypatch, tmp_path, capsys, argv):
    monkeypatch.setattr(cli, "write_manifest", lambda path, stages=None: pytest.fail("no build"))
    monkeypatch.setattr(sys, "argv", argv[:2] + ["--dir", str(tmp_path)] + argv[2:])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_cli_build_error_still_reports_without_traceback(monkeypatch, tmp_path, capsys):
    def failing_manifest(path, stages=None):
        raise ValueError("Island sources changed since the last compile; "
                         "run `pynext build --only-islands` first")

    monkeypatch.setattr(cli, "write_manifest", failing_manifest)
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path), "--only-static"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Island sources changed" in capsys.readouterr().err
