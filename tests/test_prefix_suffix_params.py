import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.build import _materialize, _segment_param, write_manifest


def test_segment_param_parses_prefix_and_suffix():
    assert _segment_param("[name]") == ("name", "dynamic", "", "")
    assert _segment_param("[name].json") == ("name", "dynamic", "", ".json")
    assert _segment_param("v[version]") == ("version", "dynamic", "v", "")
    assert _segment_param("prefix-[id].json") == ("id", "dynamic", "prefix-", ".json")
    assert _segment_param("[...rest]") == ("rest", "catch_all", "", "")
    assert _segment_param("[[...rest]]") == ("rest", "optional", "", "")
    assert _segment_param("about") is None


def test_segment_param_rejects_affixed_catch_all_and_adjacent_params():
    for segment in [
        "[...rest].json",
        "v[...version]",
        "[[...rest]].json",
        "[a][b]",
        "[a",
        "a]",
        "v[",
        "[..id]",
    ]:
        with pytest.raises(ValueError):
            _segment_param(segment)


def test_materialize_rebuilds_affixed_segments():
    assert _materialize("/files/[name].json", {"name": "report"}) == "/files/report.json"
    assert _materialize("/v[version]/api", {"version": "2"}) == "/v2/api"
    assert _materialize("/[lang]-docs", {"lang": "ko"}) == "/ko-docs"
    with pytest.raises(ValueError, match="Repeated or ambiguous parameter name"):
        _materialize("/[a]/v[a]", {"a": "x"})


def _write_affixed_app(root, with_generator=True):
    app_dir = root / "app"
    page_dir = app_dir / "files" / "[name].json"
    page_dir.mkdir(parents=True)
    generator = (
        'def generate_static_params():\n    return [{"name": "report"}]\n\n'
        if with_generator else ""
    )
    (page_dir / "page.py").write_text(
        "from pynext import h, page\n\n"
        + generator
        + '@page(render="ssg")\n'
        + "def Page(params):\n"
        + '    return h("p", {}, params["name"])\n'
    )
    api_dir = app_dir / "v[version]" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "route.py").write_text(
        "from pynext import JSONResponse, route\n\n"
        '@route("GET")\n'
        "async def get(request):\n"
        '    return JSONResponse({"version": request.params["version"]})\n'
    )


def test_affixed_routes_serve_pages_and_api(tmp_path):
    _write_affixed_app(tmp_path, with_generator=True)

    async def run():
        application = create_app(tmp_path)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v2/api")
            assert response.status_code == 200
            assert response.json() == {"version": "2"}
            assert (await client.get("/v/api")).status_code == 404
        await application.isr.close()

    asyncio.run(run())


def test_ssg_affixed_page_requires_static_params(tmp_path):
    # A prefix/suffix segment is dynamic too: an SSG page under it must
    # export generate_static_params, like a bare [name] page.
    _write_affixed_app(tmp_path, with_generator=False)
    with pytest.raises(ValueError, match="must export generate_static_params"):
        create_app(tmp_path)


def test_ssg_affixed_page_builds_static_page(tmp_path):
    _write_affixed_app(tmp_path, with_generator=True)
    write_manifest(tmp_path)
    application = create_app(tmp_path, production=True)

    async def run():
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/files/report.json")
            assert response.status_code == 200
            assert "report" in response.text
            assert (await client.get("/files/other.json")).status_code == 404
        await application.isr.close()

    asyncio.run(run())
