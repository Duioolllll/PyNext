"""Two-stage parameter decoding (Next.js `decodePathParams`).

Route matching runs on the still-encoded path, so an encoded delimiter
inside a parameter value stays one segment; only the matched values are
decoded (and delimiters re-encoded) before they reach the handler.
"""

import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.app import (
    _decode_param,
    _escape_path_delimiters,
    _normalize_path_for_matching,
    _split_request_path,
)
from pynext.http import BadRequest


def test_escape_path_delimiters_re_encodes_decoded_delimiters():
    assert _escape_path_delimiters("a/b") == "a%2Fb"
    assert _escape_path_delimiters("a?b#c\\d") == "a%3Fb%23c%5Cd"
    # An encoded delimiter that was already present is re-encoded, so
    # it cannot be decoded a second time.
    assert _escape_path_delimiters("a%2Fb") == "a%252Fb"
    assert _escape_path_delimiters("a%2fb") == "a%252fb"
    assert _escape_path_delimiters("plain") == "plain"
    assert _escape_path_delimiters("한글") == "한글"


def test_decode_param_decodes_value_and_re_escapes_delimiters():
    assert _decode_param("slug", "a%2Fb") == "a%2Fb"
    assert _decode_param("slug", "%ED%95%9C%EA%B8%80") == "한글"
    assert _decode_param("slug", "a%3Fb") == "a%3Fb"
    assert _decode_param("slug", "a%23b") == "a%23b"
    # Double-encoded: the value is literally "a%2Fb".
    assert _decode_param("slug", "a%252Fb") == "a%252Fb"
    assert _decode_param("slug", "hello") == "hello"


def test_decode_param_failure_names_the_parameter():
    with pytest.raises(BadRequest, match="Failed to decode path param 'slug'"):
        _decode_param("slug", "%FF")


def test_normalize_path_for_matching_decodes_segments_and_re_escapes_delimiters():
    # Static segments compare decoded; encoded delimiters stay one segment.
    assert _normalize_path_for_matching("/in%70ut") == "/input"
    assert _normalize_path_for_matching("/files/a%2Fb") == "/files/a%2Fb"
    assert _normalize_path_for_matching("/files/%ED%95%9C%EA%B8%80") == "/files/한글"
    assert _normalize_path_for_matching("/files/a%3Fb%23c") == "/files/a%3Fb%23c"
    assert _normalize_path_for_matching("/files/a%252Fb") == "/files/a%252Fb"
    assert _normalize_path_for_matching("/files/hello") == "/files/hello"
    assert _normalize_path_for_matching("/") == "/"


def test_normalize_path_for_matching_failure_names_the_segment():
    with pytest.raises(BadRequest, match="Failed to decode path param 'a%FF'"):
        _normalize_path_for_matching("/files/a%FF")


@pytest.mark.parametrize(
    "path,raw_path,expected",
    [
        ("/blog/hello", None, ("/blog/hello", "/blog/hello")),
        ("/blog/a%2Fb", b"/blog/a%2Fb", ("/blog/a/b", "/blog/a%2Fb")),
        (
            "/blog/%ED%95%9C%EA%B8%80",
            b"/blog/%ED%95%9C%EA%B8%80",
            ("/blog/한글", "/blog/%ED%95%9C%EA%B8%80"),
        ),
        ("/blog/a%3Fb", b"/blog/a%3Fb", ("/blog/a?b", "/blog/a%3Fb")),
    ],
)
def test_split_request_path_returns_decoded_and_encoded(path, raw_path, expected):
    assert _split_request_path(path, raw_path) == expected


@pytest.mark.parametrize(
    "raw_path",
    [
        b"/%ff",  # invalid UTF-8
        b"/input%",  # malformed percent sequence
        b"/%00",  # NUL byte
        b"/../input",  # traversal segment
        b"/%2finput",  # decodes to "//input" (empty segment)
        b"/files/%2E%2E",  # decodes to a ".." segment
        b"/files/a%5Cb",  # decodes to a backslash
        b"/healthz%zz",  # malformed percent sequence
    ],
)
def test_split_request_path_rejects_malformed_paths(raw_path):
    with pytest.raises((ValueError, UnicodeError)):
        _split_request_path("/replacement", raw_path)


def make_app(root):
    app = root / "app"
    (app / "files" / "[name]").mkdir(parents=True)
    (app / "docs" / "[...path]").mkdir(parents=True)
    (app / "api" / "echo" / "[name]").mkdir(parents=True)
    (app / "input").mkdir(parents=True)
    (app / "page.py").write_text(
        'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "home")\n'
    )
    (app / "input" / "page.py").write_text(
        'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "input")\n'
    )
    (app / "files" / "[name]" / "page.py").write_text(
        'from pynext import page, h\n@page()\nasync def Page(params): return h("p", {}, params["name"])\n'
    )
    (app / "docs" / "[...path]" / "page.py").write_text(
        'from pynext import page, h\n@page()\nasync def Page(params): return h("p", {}, "/".join(params["path"]))\n'
    )
    (app / "api" / "echo" / "[name]" / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\nasync def echo(params):\n'
        '    return JSONResponse({"name": params["name"]})\n'
    )
    return root


@pytest.mark.parametrize(
    "url,expected_text",
    [
        ("/files/a%2Fb", "<p>a%2Fb</p>"),
        ("/files/%ED%95%9C%EA%B8%80", "<p>한글</p>"),
        ("/files/a%3Fb", "<p>a%3Fb</p>"),
        ("/files/a%23b", "<p>a%23b</p>"),
        ("/files/a%252Fb", "<p>a%252Fb</p>"),
        ("/files/hello", "<p>hello</p>"),
        ("/docs/a/b", "<p>a/b</p>"),
        ("/docs/a%2Fb/c", "<p>a%2Fb/c</p>"),
    ],
)
def test_encoded_delimiters_stay_inside_one_parameter(tmp_path, url, expected_text):
    make_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(url)
            assert response.status_code == 200
            assert expected_text in response.text

    asyncio.run(run())


def test_static_segment_matches_when_percent_encoded(tmp_path):
    make_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # "%70" is an encoded "p": the static segment still matches.
            response = await client.get("/in%70ut")
            assert response.status_code == 200
            assert "<p>input</p>" in response.text

    asyncio.run(run())


def test_api_handler_receives_decoded_parameter(tmp_path):
    make_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/echo/a%2Fb")
            assert response.status_code == 200
            assert response.json() == {"name": "a%2Fb"}

    asyncio.run(run())


@pytest.mark.parametrize("url", ["/files/%FF", "/files/%00", "/files/%zz", "/%2finput"])
def test_malformed_encoded_parameters_are_rejected(tmp_path, url):
    make_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get(url)).status_code == 400

    asyncio.run(run())


def test_trailing_slash_redirect_preserves_encoding(tmp_path):
    make_app(tmp_path)

    async def run():
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/files/a%2Fb/", follow_redirects=False)
            assert response.status_code == 308
            assert response.headers["location"] == "/files/a%2Fb"

    asyncio.run(run())
