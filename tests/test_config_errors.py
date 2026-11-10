"""Config conflict errors carry file paths, values, and fixes (B P2-3).

Next.js reports config conflicts in the ensure-static shape: the
conflicting values with their source files, then a "Possible fixes"
list. PyNext validation errors in route discovery, static generation,
and app initialization must follow the same shape.
"""
import json

import pytest

from pynext import create_app
from pynext.build import _config_error, _materialize, _short_repr, discover, write_manifest

PAGE = 'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n'


def write_page(base, *parts, body=None):
    directory = base.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.py").write_text(body or PAGE)


def test_config_error_format():
    message = _config_error(
        "A page and an API cannot share a path: /about",
        [("page", "app/about/page.py"), ("API route", "app/about/route.py")],
        ["Move one of the files to a different route directory"],
    )
    assert message == (
        "A page and an API cannot share a path: /about\n"
        "  page: app/about/page.py\n"
        "  API route: app/about/route.py\n"
        "Possible fixes:\n"
        "  - Move one of the files to a different route directory"
    )


def test_config_error_without_details_or_fixes():
    assert _config_error("Something broke") == "Something broke"


def test_short_repr_truncates_huge_values():
    assert _short_repr("x" * 500).endswith("...")
    assert len(_short_repr("x" * 500)) == 203
    assert _short_repr({"a": 1}) == "{'a': 1}"


def test_duplicate_route_names_both_files(tmp_path):
    write_page(tmp_path / "app", "(a)")
    write_page(tmp_path / "app", "(b)")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "Duplicate route path: /" in message
    assert "app/(a)/page.py" in message
    assert "app/(b)/page.py" in message
    assert "Possible fixes:" in message


def test_page_and_api_conflict_names_both_files(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "page.py").write_text("")
    (app / "route.py").write_text("")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "share a path" in message
    assert "app/page.py" in message
    assert "app/route.py" in message
    assert "Possible fixes:" in message


def test_public_file_conflict_names_both_files(tmp_path):
    write_page(tmp_path / "app", "logo.txt")
    public = tmp_path / "public"
    public.mkdir()
    (public / "logo.txt").write_bytes(b"public asset")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "Public file conflicts with a page or API route: /logo.txt" in message
    assert "public/logo.txt" in message
    assert "app/logo.txt/page.py" in message
    assert "Possible fixes:" in message


def test_reserved_path_names_source_file(tmp_path):
    write_page(tmp_path / "app", "_pynext")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "Reserved PyNext path: /_pynext" in message
    assert "app/_pynext/page.py" in message
    assert "Possible fixes:" in message


def test_no_routes_suggests_fix(tmp_path):
    (tmp_path / "app").mkdir()
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "No page.py or route.py files found" in message
    assert "app/page.py" in message
    assert "Possible fixes:" in message


def test_invalid_group_names_file_and_segment(tmp_path):
    write_page(tmp_path / "app", "(bad")
    with pytest.raises(ValueError) as excinfo:
        discover(tmp_path)
    message = str(excinfo.value)
    assert "Invalid route group: (bad" in message
    assert "app/(bad/page.py" in message
    assert "Possible fixes:" in message


def test_materialize_invalid_segment_names_file():
    with pytest.raises(ValueError) as excinfo:
        _materialize("/[bad name]", {}, "app/blog/page.py")
    message = str(excinfo.value)
    assert "Invalid route segment: [bad name]" in message
    assert "app/blog/page.py" in message
    assert "Possible fixes:" in message


def test_generate_static_params_must_return_list_names_file(tmp_path):
    write_page(
        tmp_path / "app", "blog", "[slug]",
        body=(
            'from pynext import h, page\n'
            '@page(render="ssg")\n'
            'def Page(params): return h("p", {}, params["slug"])\n'
            'def generate_static_params(): return {"slug": "a"}\n'
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        write_manifest(tmp_path)
    message = str(excinfo.value)
    assert "generate_static_params for /blog/[slug] must return a list" in message
    assert "app/blog/[slug]/page.py" in message
    assert "dict:" in message
    assert "Possible fixes:" in message


def test_duplicate_generated_path_names_both_param_sets(tmp_path):
    write_page(
        tmp_path / "app", "blog", "[slug]",
        body=(
            'from pynext import h, page\n'
            '@page(render="ssg")\n'
            'def Page(params): return h("p", {}, params["slug"])\n'
            'def generate_static_params(): return [{"slug": "a"}, {"slug": "a"}]\n'
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        write_manifest(tmp_path)
    message = str(excinfo.value)
    assert "Duplicate generated path: /blog/a" in message
    assert "app/blog/[slug]/page.py" in message
    assert "Possible fixes:" in message


def test_invalid_static_param_value_names_file(tmp_path):
    write_page(
        tmp_path / "app", "blog", "[slug]",
        body=(
            'from pynext import h, page\n'
            '@page(render="ssg")\n'
            'def Page(params): return h("p", {}, params["slug"])\n'
            'def generate_static_params(): return [{"slug": "a/b"}]\n'
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        write_manifest(tmp_path)
    message = str(excinfo.value)
    assert "Invalid static parameter in /blog/[slug]" in message
    assert "app/blog/[slug]/page.py" in message
    assert "Possible fixes:" in message


def test_page_without_page_function_names_file_and_fix(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "page.py").write_text("x = 1\n")
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path)
    message = str(excinfo.value)
    assert "must export a @page() Page function" in message
    assert "app/page.py" in message
    assert "Possible fixes:" in message


def test_ssg_page_accepting_request_names_fix(tmp_path):
    write_page(
        tmp_path / "app",
        body=(
            'from pynext import h, page\n'
            '@page(render="ssg")\n'
            'def Page(request): return h("p", {}, "ok")\n'
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path)
    message = str(excinfo.value)
    assert "SSG page app/page.py cannot accept request" in message
    assert "Possible fixes:" in message


def test_invalid_isr_policy_names_value_and_fix(tmp_path):
    source = tmp_path / "app" / "blog" / "[slug]"
    source.mkdir(parents=True)
    (source / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'async def Page(params): return h("p", {}, params["slug"])\n'
        'Page.__pynext_cache_policy__ = {"revalidate": 0, "stale": 1}\n'
    )
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path)
    message = str(excinfo.value)
    assert "Invalid ISR policy in app/blog/[slug]/page.py" in message
    assert "'revalidate': 0" in message
    assert "Possible fixes:" in message


def test_duplicate_route_handler_names_file(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\n'
        'async def get_one(request): return JSONResponse({"a": 1})\n'
        '@route("GET")\n'
        'async def get_two(request): return JSONResponse({"a": 2})\n'
    )
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path)
    message = str(excinfo.value)
    assert "Duplicate GET handler in app/route.py" in message
    assert "Possible fixes:" in message


def test_route_without_handlers_names_fix(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "route.py").write_text("x = 1\n")
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path)
    message = str(excinfo.value)
    assert "app/route.py has no @route handlers" in message
    assert "Possible fixes:" in message


def test_unsupported_signature_param_names_fix(tmp_path):
    write_page(
        tmp_path / "app",
        body=(
            'from pynext import h, page\n'
            '@page()\n'
            'def Page(bogus): return h("p", {}, "ok")\n'
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path)
    message = str(excinfo.value)
    assert "Unsupported required parameter 'bogus' in app/page.py" in message
    assert "Possible fixes:" in message


def test_production_render_mode_mismatch_names_manifest(tmp_path):
    write_page(tmp_path / "app")
    write_manifest(tmp_path)
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["render_modes"]["/"] = "ssg"
    manifest_path.write_text(json.dumps(manifest))
    # The edited manifest is a new build output, so its BUILD_ID seal
    # must be refreshed too — otherwise load_manifest rejects it as an
    # incomplete build before the render modes are even checked.
    from pynext.build import build_id_for
    (tmp_path / ".pynext" / "BUILD_ID").write_text(
        build_id_for(manifest_path.read_bytes()) + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        create_app(tmp_path, production=True)
    message = str(excinfo.value)
    assert "Render modes changed since build" in message
    assert ".pynext/manifest.json" in message
    assert "Run `pynext build` again" in message
