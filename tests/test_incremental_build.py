"""Incremental builds, build summary reports, and `pynext info`."""
import json
import sys

import pytest

from pynext import cli
from pynext.build import read_build_report, write_manifest


def make_app(root):
    app = root / "app"
    (app / "blog").mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "home")\n',
        encoding="utf-8")
    (app / "blog" / "page.py").write_text(
        'from pynext import page, h\n@page(render="ssg")\ndef Page(): return h("p", {}, "blog")\n',
        encoding="utf-8")
    # A layout scoped to /blog only: changing it must re-render /blog
    # but leave / untouched.
    (app / "blog" / "layout.py").write_text(
        'from pynext import layout, h\n@layout\ndef Layout(children): return h("div", {}, children)\n',
        encoding="utf-8")
    return app


def report(root):
    result = read_build_report(root)
    assert result is not None
    return result


def static_digests(root):
    manifest = json.loads((root / ".pynext" / "manifest.json").read_text(encoding="utf-8"))
    return {path: entry["sha256"] for path, entry in manifest["static_pages"].items()}


def test_second_build_reuses_unchanged_static_pages(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    assert report(tmp_path)["static_pages"] == {"rendered": 2, "reused": 0, "total": 2,
                                                "bytes": report(tmp_path)["static_pages"]["bytes"]}
    first = static_digests(tmp_path)
    write_manifest(tmp_path)
    second = report(tmp_path)["static_pages"]
    assert second["rendered"] == 0 and second["reused"] == 2 and second["total"] == 2
    assert static_digests(tmp_path) == first


def test_changing_one_page_rerenders_only_it(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    first = static_digests(tmp_path)
    page = tmp_path / "app" / "blog" / "page.py"
    page.write_text(page.read_text(encoding="utf-8").replace('"blog"', '"blog v2"'), encoding="utf-8")
    write_manifest(tmp_path)
    pages = report(tmp_path)["static_pages"]
    assert pages["rendered"] == 1 and pages["reused"] == 1
    second = static_digests(tmp_path)
    assert second["/"] == first["/"]
    assert second["/blog"] != first["/blog"]


def test_changing_a_layout_rerenders_only_its_pages(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    first = static_digests(tmp_path)
    layout = tmp_path / "app" / "blog" / "layout.py"
    layout.write_text(layout.read_text(encoding="utf-8").replace('h("div"', 'h("section"'), encoding="utf-8")
    write_manifest(tmp_path)
    pages = report(tmp_path)["static_pages"]
    assert pages["rendered"] == 1 and pages["reused"] == 1
    second = static_digests(tmp_path)
    assert second["/"] == first["/"]
    assert second["/blog"] != first["/blog"]


def test_changing_a_shared_helper_rerenders_everything(tmp_path):
    make_app(tmp_path)
    (tmp_path / "app" / "shared.py").write_text('LABEL = "v1"\n', encoding="utf-8")
    page = tmp_path / "app" / "page.py"
    page.write_text(
        'from pynext import page, h\nfrom .shared import LABEL\n'
        '@page(render="ssg")\ndef Page(): return h("p", {}, LABEL)\n', encoding="utf-8")
    write_manifest(tmp_path)
    first = static_digests(tmp_path)
    (tmp_path / "app" / "shared.py").write_text('LABEL = "v2"\n', encoding="utf-8")
    write_manifest(tmp_path)
    pages = report(tmp_path)["static_pages"]
    assert pages["rendered"] == 2 and pages["reused"] == 0
    assert static_digests(tmp_path)["/"] != first["/"]


def test_changing_an_island_rerenders_static_pages(tmp_path, esbuild):
    make_app(tmp_path)
    islands = tmp_path / "islands"
    islands.mkdir()
    (islands / "counter.js").write_text(
        'export function mount(el) { el.textContent = "one"; }\n', encoding="utf-8")
    page = tmp_path / "app" / "page.py"
    page.write_text(
        'from pynext import page, h, island\n@page(render="ssg")\n'
        'def Page(): return h("div", {}, island("counter"))\n', encoding="utf-8")
    write_manifest(tmp_path)
    first = static_digests(tmp_path)
    (islands / "counter.js").write_text(
        'export function mount(el) { el.textContent = "two"; }\n', encoding="utf-8")
    write_manifest(tmp_path)
    pages = report(tmp_path)["static_pages"]
    assert pages["rendered"] == 2 and pages["reused"] == 0
    assert static_digests(tmp_path)["/"] != first["/"]


def test_missing_static_file_is_rerendered(tmp_path):
    make_app(tmp_path)
    write_manifest(tmp_path)
    first = static_digests(tmp_path)
    (tmp_path / ".pynext" / "static" / f"{first['/blog']}.html").unlink()
    write_manifest(tmp_path)
    pages = report(tmp_path)["static_pages"]
    assert pages["rendered"] == 1 and pages["reused"] == 1
    assert static_digests(tmp_path) == first


def test_build_report_covers_routes_islands_and_public_files(tmp_path, esbuild):
    make_app(tmp_path)
    islands = tmp_path / "islands"
    islands.mkdir()
    (islands / "counter.js").write_text(
        'export function mount(el) { el.textContent = "one"; }\n', encoding="utf-8")
    public = tmp_path / "public"
    public.mkdir()
    (public / "note.txt").write_text("hello", encoding="utf-8")
    write_manifest(tmp_path)
    result = report(tmp_path)
    assert result["version"] == 1
    assert result["routes"] == {"total": 2, "ssg": 2, "isr": 0, "ssr": 0, "api": 0}
    assert result["static_pages"]["total"] == 2
    assert result["islands"]["esbuild_version"] is not None
    assert set(result["islands"]["bundles"]) == {"counter"}
    bundle = result["islands"]["bundles"]["counter"]
    assert bundle["bytes"] > 0
    assert bundle["inputs"] and bundle["inputs"][0]["path"] == "counter.js"
    assert result["public_assets"] == {"count": 1, "bytes": 5}
    assert result["duration_seconds"] >= 0


def test_cli_build_prints_summary_and_analyze(tmp_path, monkeypatch, capsys, esbuild):
    make_app(tmp_path)
    islands = tmp_path / "islands"
    islands.mkdir()
    (islands / "counter.js").write_text(
        'export function mount(el) { el.textContent = "one"; }\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path), "--analyze"])
    cli.main()
    out = capsys.readouterr().out
    assert "Built 2 routes" in out
    assert "Routes: 2 total (2 SSG, 0 ISR, 0 SSR, 0 API)" in out
    assert "Static pages: 2 total (2 rendered, 0 reused" in out
    assert "Islands: 1 bundles" in out and "esbuild" in out
    assert "Public assets: 0 files" in out
    assert "Build finished in" in out
    assert "counter:" in out and "counter.js" in out


def test_cli_build_failure_points_at_info(tmp_path, monkeypatch, capsys):
    make_app(tmp_path)
    (tmp_path / "app" / "page.py").write_text('raise ValueError("boom at import")\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["pynext", "build", "--dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    err = capsys.readouterr().err
    assert "boom at import" in err
    assert "pynext info" in err


def test_info_without_manifest(tmp_path, monkeypatch, capsys):
    make_app(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pynext", "info", "--dir", str(tmp_path)])
    cli.main()
    out = capsys.readouterr().out
    assert "PyNext 0.1.2" in out
    assert "Python 3" in out
    assert "Native extension: loaded" in out
    assert "esbuild: not found (no islands, not needed)" in out
    assert "Manifest: absent" in out
    assert "Build report: absent" in out


def test_info_with_manifest(tmp_path, monkeypatch, capsys, esbuild):
    make_app(tmp_path)
    islands = tmp_path / "islands"
    islands.mkdir()
    (islands / "counter.js").write_text(
        'export function mount(el) { el.textContent = "one"; }\n', encoding="utf-8")
    write_manifest(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pynext", "info", "--dir", str(tmp_path)])
    cli.main()
    out = capsys.readouterr().out
    assert "Manifest: present (schema 10, 2 routes: 2 SSG, 0 ISR, 0 SSR, 0 API)" in out
    assert "Build report: present (" in out
    assert "esbuild: 0." in out
    assert "Checks:" not in out

    # The "Checks:" section is only rendered by format_info(doctor=True);
    # `doctor` is the deploy audit (see test_deploy_check.py), not an
    # alias for `info`, so the section is covered here at the unit level.
    from pynext.info import collect_info, format_info
    assert "Checks: 3 passed, 0 failed" in format_info(collect_info(tmp_path), doctor=True)


def test_format_info_doctor_fails_without_a_build(tmp_path, monkeypatch, capsys):
    make_app(tmp_path)
    from pynext.info import collect_info, format_info
    out = format_info(collect_info(tmp_path), doctor=True)
    assert "[FAIL] build manifest: absent" in out


def test_format_bytes():
    from pynext.build import format_bytes
    assert format_bytes(0) == "0 B"
    assert format_bytes(230) == "230 B"
    assert format_bytes(1664) == "1.6 KB"
    assert format_bytes(2 * 1024 * 1024) == "2.0 MB"
