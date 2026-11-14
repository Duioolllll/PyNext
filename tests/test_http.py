import pytest

from pynext import BadRequest, Request, Response


@pytest.mark.parametrize("body", [5, 0, True, 3.14, ["a"], {"a": 1}, object()])
def test_response_rejects_non_bytes_body(body):
    with pytest.raises(TypeError, match="Response body must be str or bytes"):
        Response(body)


def test_response_accepts_str_bytes_and_bytearray():
    assert Response("ok").body == b"ok"
    assert Response(b"ok").body == b"ok"
    assert Response(bytearray(b"ok")).body == b"ok"
    assert Response(memoryview(b"ok")).body == b"ok"


def test_query_rejects_more_than_1000_fields():
    query = "&".join(f"x{i}=1" for i in range(1001))
    with pytest.raises(BadRequest, match="Invalid query string"):
        Request("GET", "/", query.encode()).query


def test_query_accepts_1000_fields():
    query = "&".join(f"x{i}=1" for i in range(1000))
    parsed = Request("GET", "/", query.encode()).query
    assert len(parsed) == 1000


def test_response_rejects_body_with_bodyless_status():
    """A1-L7: 1xx, 204 and 304 responses must not carry a body (RFC 9110
    §15), like StreamingResponse already enforces. An empty body with
    those statuses stays legal."""
    for status in (100, 101, 199, 204, 304):
        with pytest.raises(ValueError, match="not allowed with this status"):
            Response("x", status=status)
        with pytest.raises(ValueError, match="not allowed with this status"):
            Response(b"x", status=status)
        assert Response(b"", status=status).body == b""
        assert Response("", status=status).body == b""
    # Ordinary statuses still accept a body.
    assert Response("ok", status=200).body == b"ok"
