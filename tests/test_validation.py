"""S-P1-2 input validation layer (ROADMAP §1.2).

Structure / type / range validation, CSRF token binding, and the
masked-400 contract: a failure body carries field names and reason
codes only, never a submitted value.
"""

import asyncio

import httpx
import pytest

from pynext import (
    JSONResponse,
    Request,
    create_app,
    route,
)
from pynext.http import SecurityConfig, sign_csrf_token
from pynext.sessions import Session
from pynext.validation import (
    Field,
    Schema,
    ValidationError,
    bind_csrf_token,
    validate_handler,
    validate_input,
    validate_request,
    validation_error_response,
)

SECRET = "s3cr3t-value-that-must-never-echo"


# --- Schema compilation: fail fast, at construction -----------------------


def test_schema_rejects_invalid_pattern_at_construction():
    with pytest.raises(ValueError, match="invalid pattern"):
        Schema(Field("code", pattern="([unclosed"))


def test_schema_rejects_contradictory_constraints():
    with pytest.raises(ValueError):
        Schema(Field("n", "int", gt=5, ge=5))
    with pytest.raises(ValueError):
        Schema(Field("n", "int", gt=10, lt=5))
    with pytest.raises(ValueError):
        Schema(Field("s", min_length=5, max_length=2))
    with pytest.raises(ValueError):
        Schema(Field("n", "int", gt=1, lt=1))
    with pytest.raises(ValueError):
        Schema(Field("s", "str", pattern="a", gt=1))
    with pytest.raises(ValueError):
        Schema(Field("a", "int", min_length=1))


def test_schema_rejects_duplicate_fields_and_bad_csrf_field():
    with pytest.raises(ValueError, match="duplicate field"):
        Schema(Field("a"), Field("a"))
    with pytest.raises(ValueError, match="csrf_field"):
        Schema(Field("a"), csrf_field="missing")
    with pytest.raises(ValueError, match="csrf_field"):
        Schema(Field("n", "int"), csrf_field="n")


def test_field_compiles_pattern_once():
    schema = Schema(Field("code", pattern=r"[A-Z]{3}-\d{4}"))
    assert schema.by_name["code"].pattern.fullmatch("ABC-1234") is not None
    assert schema.by_name["code"].pattern.fullmatch("abc-1234") is None


# --- Structure ------------------------------------------------------------


def test_validate_rejects_non_mapping():
    schema = Schema(Field("a"))
    with pytest.raises(ValidationError) as excinfo:
        schema.validate(["not", "a", "mapping"])
    assert excinfo.value.errors == {"body": ("structure",)}


def test_validate_reports_missing_required_and_unknown():
    schema = Schema(Field("name"), Field("age", "int", required=False))
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"nickname": "x"})
    assert excinfo.value.errors == {
        "name": ("required",),
        "nickname": ("unknown",),
    }
    assert excinfo.value.fields == ["name", "nickname"]


def test_validate_allows_unknown_when_configured_and_applies_defaults():
    schema = Schema(Field("name"), Field("age", "int", required=False, default=0),
                    allow_unknown=True)
    assert schema.validate({"name": "a", "extra": 1}) == {"name": "a", "age": 0}


def test_validate_omits_optional_without_default():
    schema = Schema(Field("name"), Field("nick", required=False))
    assert schema.validate({"name": "a"}) == {"name": "a"}


# --- Types ----------------------------------------------------------------


def test_str_field_rejects_non_strings():
    schema = Schema(Field("name"))
    for bad in (42, 4.5, True, None, ["x"], {"x": 1}):
        with pytest.raises(ValidationError) as excinfo:
            schema.validate({"name": bad})
        assert excinfo.value.errors == {"name": ("type",)}


def test_int_field_coerces_decimal_strings_but_not_bool_or_float():
    schema = Schema(Field("age", "int"))
    assert schema.validate({"age": "42"}) == {"age": 42}
    assert schema.validate({"age": "-3"}) == {"age": -3}
    for bad in (True, 4.5, "4.5", "abc", "", "  ", None):
        with pytest.raises(ValidationError) as excinfo:
            schema.validate({"age": bad})
        assert excinfo.value.errors == {"age": ("type",)}, bad


def test_float_field_accepts_ints_and_rejects_nan_inf():
    schema = Schema(Field("score", "float"))
    assert schema.validate({"score": 2}) == {"score": 2.0}
    assert schema.validate({"score": "1.5"}) == {"score": 1.5}
    for bad in ("nan", "inf", "-inf", True, None):
        with pytest.raises(ValidationError):
            schema.validate({"score": bad})


def test_bool_field_accepts_words():
    schema = Schema(Field("flag", "bool"))
    assert schema.validate({"flag": "true"}) == {"flag": True}
    assert schema.validate({"flag": "0"}) == {"flag": False}
    assert schema.validate({"flag": True}) == {"flag": True}
    for bad in ("yes", "2", 1, None):
        with pytest.raises(ValidationError):
            schema.validate({"flag": bad})


def test_any_field_accepts_none():
    assert Schema(Field("v", "any")).validate({"v": None}) == {"v": None}


# --- Range / length / pattern / choices ------------------------------------


def test_numeric_bounds_are_inclusive_where_declared():
    schema = Schema(Field("age", "int", ge=0, le=150))
    assert schema.validate({"age": 0}) == {"age": 0}
    assert schema.validate({"age": 150}) == {"age": 150}
    for bad in (-1, 151):
        with pytest.raises(ValidationError) as excinfo:
            schema.validate({"age": bad})
        assert excinfo.value.errors == {"age": ("range",)}
    assert Schema(Field("n", "int", gt=0, lt=10)).validate({"n": 5}) == {"n": 5}
    for bad in (0, 10):
        with pytest.raises(ValidationError) as excinfo:
            Schema(Field("n", "int", gt=0, lt=10)).validate({"n": bad})
        assert excinfo.value.errors == {"n": ("range",)}


def test_length_and_pattern_and_choices():
    schema = Schema(Field("name", min_length=1, max_length=3),
                    Field("code", pattern=r"[A-Z]{2}\d"),
                    Field("color", choices=("red", "blue")))
    assert schema.validate({"name": "ab", "code": "AB1", "color": "red"})
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"name": "", "code": "AB1", "color": "red"})
    assert excinfo.value.errors == {"name": ("length",)}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"name": "abcd", "code": "AB1", "color": "red"})
    assert excinfo.value.errors == {"name": ("length",)}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"name": "ab", "code": "ab1", "color": "red"})
    assert excinfo.value.errors == {"code": ("pattern",)}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"name": "ab", "code": "AB1", "color": "green"})
    assert excinfo.value.errors == {"color": ("choice",)}


def test_list_length_and_item_constraints():
    schema = Schema(Field("tags", "list", min_length=1, max_length=2,
                          item=Field("tag", min_length=2)))
    assert schema.validate({"tags": ["ab", "cd"]}) == {"tags": ["ab", "cd"]}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"tags": []})
    assert excinfo.value.errors == {"tags": ("length",)}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"tags": ["a", "cd"]})
    assert excinfo.value.errors == {"tags[0]": ("length",)}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"tags": ["ab", 5]})
    assert excinfo.value.errors == {"tags[1]": ("type",)}


def test_nested_schema_reports_dotted_fields():
    schema = Schema(Field("address", "dict",
                          schema=Schema(Field("zip", pattern=r"\d{5}"))))
    assert schema.validate({"address": {"zip": "12345"}}) == {"address": {"zip": "12345"}}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"address": {"zip": "abc"}})
    assert excinfo.value.errors == {"address.zip": ("pattern",)}
    with pytest.raises(ValidationError) as excinfo:
        schema.validate({"address": {"zip": "12345", "extra": 1}})
    assert excinfo.value.errors == {"address.extra": ("unknown",)}


def test_validate_input_wrapper_and_multiple_errors():
    schema = Schema(Field("a", "int", ge=0), Field("b", min_length=2))
    assert validate_input({"a": 1, "b": "xy"}, schema) == {"a": 1, "b": "xy"}
    with pytest.raises(ValidationError) as excinfo:
        validate_input({"a": -1, "b": "x"}, schema)
    assert excinfo.value.errors == {"a": ("range",), "b": ("length",)}


# --- Masked 400 ------------------------------------------------------------


def test_validation_error_response_masks_submitted_values():
    error = ValidationError({"password": ("length",), "token": ("required",)})
    response = validation_error_response(error)
    assert response.status == 400
    body = response.body.decode()
    assert SECRET not in body  # (no value was ever in the error)
    assert body == ('{"error":"validation_failed","fields":'
                    '{"password":["length"],"token":["required"]}}')
    assert (b"content-type", b"application/json; charset=utf-8") in response.headers


def test_validation_error_rejects_empty_errors():
    with pytest.raises(ValueError):
        ValidationError({})
    with pytest.raises(TypeError):
        validation_error_response(ValueError("not a ValidationError"))


# --- CSRF token binding ----------------------------------------------------


def _request_with_cookie(cookie_value, *, state=None):
    headers = ((b"cookie", f"pynext_csrf={cookie_value}".encode("ascii")),)
    return Request("POST", "/api/x", b"", headers, b"", {}, None, state or {})


def test_bind_csrf_token_matches_signed_cookie_constant_time():
    config = SecurityConfig(csrf_signing_key="a-long-enough-test-key")
    token = "raw-token-value"
    signed = sign_csrf_token(token, config=config)
    request = _request_with_cookie(signed)
    assert bind_csrf_token(request, token, config=config) is True
    assert bind_csrf_token(request, "other-token", config=config) is False
    assert bind_csrf_token(request, token) is False  # default key differs


def test_bind_csrf_token_prefers_session_token_and_fails_closed():
    config = SecurityConfig(csrf_signing_key="a-long-enough-test-key")
    session = Session(data={}, csrf="session-token")
    request = _request_with_cookie("unsigned-or-tampered", state={"session": session})
    assert bind_csrf_token(request, "session-token", config=config) is True
    assert bind_csrf_token(request, "session-token", config=config) is True
    assert bind_csrf_token(request, "other", config=config) is False
    # No token anywhere: fails closed, never raises.
    bare = Request("POST", "/api/x", b"", (), b"", {}, None, {})
    assert bind_csrf_token(bare, "anything", config=config) is False
    assert bind_csrf_token(bare, "", config=config) is False
    assert bind_csrf_token(bare, None, config=config) is False


# --- validate_request: sources ---------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_validate_request_merges_body_query_params():
    schema = Schema(Field("name", source="body"),
                    Field("page", "int", source="query", ge=1),
                    Field("slug", source="params"))
    body = b'{"name": "hi"}'
    request = Request("GET", "/blog/x", b"page=2",
                      ((b"content-type", b"application/json"),), body, {"slug": "x"}, None, {})
    assert _run(validate_request(request, schema)) == {
        "name": "hi", "page": 2, "slug": "x"}


def test_validate_request_rejects_malformed_json_and_unknown_body_keys():
    schema = Schema(Field("name"))
    request = Request("POST", "/api/x", b"",
                      ((b"content-type", b"application/json"),), b"{bad json", {}, None, {})
    with pytest.raises(ValidationError) as excinfo:
        _run(validate_request(request, schema))
    assert excinfo.value.errors == {"body": ("structure",)}
    request = Request("POST", "/api/x", b"",
                      ((b"content-type", b"application/json"),),
                      b'{"name": "a", "admin": true}', {}, None, {})
    with pytest.raises(ValidationError) as excinfo:
        _run(validate_request(request, schema))
    assert excinfo.value.errors == {"admin": ("unknown",)}


def test_validate_request_skips_body_parse_for_query_only_schema():
    # A query-only schema never parses the body, so a garbage body
    # cannot 400 a GET handler.
    schema = Schema(Field("page", "int", source="query", ge=1))
    request = Request("GET", "/api/x", b"page=1", (), b"{bad json", {}, None, {})
    assert _run(validate_request(request, schema)) == {"page": 1}


def test_validate_request_csrf_binding():
    config = SecurityConfig(csrf_signing_key="a-long-enough-test-key")
    schema = Schema(Field("name"), Field("csrf_token"), csrf_field="csrf_token")
    signed = sign_csrf_token("tok-123", config=config)
    headers = ((b"content-type", b"application/json"),
               (b"cookie", f"pynext_csrf={signed}".encode("ascii")))
    good = Request("POST", "/api/x", b"", headers,
                   b'{"name": "a", "csrf_token": "tok-123"}', {}, None, {})
    assert _run(validate_request(good, schema, config=config)) == {
        "name": "a", "csrf_token": "tok-123"}
    bad = Request("POST", "/api/x", b"", headers,
                  b'{"name": "a", "csrf_token": "tok-999"}', {}, None, {})
    with pytest.raises(ValidationError) as excinfo:
        _run(validate_request(bad, schema, config=config))
    assert excinfo.value.errors == {"csrf_token": ("csrf",)}


# --- End to end: dispatcher entry point ------------------------------------


def _write_app(tmp_path, route_source):
    app_dir = tmp_path / "app"
    (app_dir / "api" / "users").mkdir(parents=True)
    (app_dir / "api" / "users" / "route.py").write_text(route_source, encoding="utf-8")
    return tmp_path


USER_SCHEMA_SOURCE = '''
from pynext import JSONResponse, route
from pynext.validation import Field, Schema, validate_handler

list_schema = Schema(Field("page", "int", source="query", required=False, ge=1))
schema = Schema(Field("name", min_length=1, max_length=80),
                Field("age", "int", ge=0, le=150))

@route("GET")
@validate_handler(list_schema)
async def list_users(request):
    return JSONResponse({"page": request.state["validated_input"].get("page", 1)})

@route("POST")
@validate_handler(schema)
async def create_user(request):
    data = request.state["validated_input"]
    return JSONResponse({"name": data["name"], "age": data["age"]}, status=201)
'''


def _client(tmp_path):
    app = create_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def test_dispatcher_validates_once_before_handler(tmp_path):
    _write_app(tmp_path, USER_SCHEMA_SOURCE)
    async def run():
        async with _client(tmp_path) as client:
            ok = await client.post("/api/users", json={"name": "N", "age": 30})
            assert ok.status_code == 201
            assert ok.json() == {"name": "N", "age": 30}
            # Query strings coerce: "2" -> int 2.
            page = await client.get("/api/users?page=2")
            assert page.status_code == 200
            assert page.json() == {"page": 2}
            bad_page = await client.get("/api/users?page=0")
            assert bad_page.status_code == 400
            assert bad_page.json() == {
                "error": "validation_failed", "fields": {"page": ["range"]}}
            # Range violation: 400, field + code only.
            bad_age = await client.post("/api/users", json={"name": "N", "age": 200})
            assert bad_age.status_code == 400
            assert bad_age.json() == {
                "error": "validation_failed", "fields": {"age": ["range"]}}
            # The submitted secret value is never echoed back.
            evil = await client.post(
                "/api/users", json={"name": SECRET * 9, "age": 30})
            assert evil.status_code == 400
            assert SECRET not in evil.text
            assert evil.json()["fields"] == {"name": ["length"]}
            # Unknown field: mass-assignment attempt is rejected.
            injected = await client.post(
                "/api/users", json={"name": "N", "age": 30, "admin": True})
            assert injected.status_code == 400
            assert injected.json()["fields"] == {"admin": ["unknown"]}
            # Malformed JSON is a masked structure error, not a 500.
            broken = await client.post(
                "/api/users", content=b"{bad json",
                headers={"content-type": "application/json"})
            assert broken.status_code == 400
            assert broken.json() == {
                "error": "validation_failed", "fields": {"body": ["structure"]}}
    asyncio.run(run())


CSRF_SCHEMA_SOURCE = '''
from pynext import JSONResponse, route
from pynext.validation import Field, Schema, validate_handler

schema = Schema(Field("note", min_length=1, max_length=200),
                Field("csrf_token"), csrf_field="csrf_token")

@route("POST")
@validate_handler(schema)
async def create_note(request):
    return JSONResponse({"ok": True})
'''


def test_dispatcher_csrf_binding(tmp_path):
    _write_app(tmp_path, CSRF_SCHEMA_SOURCE)
    config = SecurityConfig(csrf_signing_key="a-long-enough-test-key")
    signed = sign_csrf_token("tok-123", config=config)
    async def run():
        app = create_app(tmp_path, security=config)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     cookies={"pynext_csrf": signed}) as client:
            good = await client.post("/api/users",
                                     json={"note": "hi", "csrf_token": "tok-123"})
            assert good.status_code == 200
            assert good.json() == {"ok": True}
            bad = await client.post("/api/users",
                                    json={"note": "hi", "csrf_token": "tok-999"})
            assert bad.status_code == 400
            assert bad.json() == {
                "error": "validation_failed", "fields": {"csrf_token": ["csrf"]}}
            assert "tok-999" not in bad.text
            missing = await client.post("/api/users", json={"note": "hi"})
            assert missing.status_code == 400
            assert missing.json()["fields"] == {"csrf_token": ["required"]}
    asyncio.run(run())


def test_handler_without_schema_is_untouched(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "api" / "users").mkdir(parents=True)
    (app_dir / "api" / "users" / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\n'
        'async def create_user(request):\n'
        '    data = await request.json()\n'
        '    return JSONResponse({"name": data["name"]}, status=201)\n',
        encoding="utf-8")
    async def run():
        async with _client(tmp_path) as client:
            response = await client.post("/api/users", json={"name": "N", "age": 9999})
            assert response.status_code == 201
            assert response.json() == {"name": "N"}
    asyncio.run(run())
