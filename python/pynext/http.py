import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote_to_bytes


class BadRequest(Exception):
    """A request could not be parsed or failed application validation."""


def _reject_json_constant(_value):
    raise ValueError("Non-finite numbers are not valid JSON")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query_string: bytes = b""
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b""
    params: dict = field(default_factory=dict)
    _app: object = field(default=None, repr=False, compare=False)

    async def revalidate(self, path):
        """Invalidate an ISR path in the current app's process-local cache."""
        if self._app is None:
            raise RuntimeError("This request is not attached to an app")
        return await self._app.revalidate(path)

    @property
    def query(self):
        try:
            query = self.query_string.decode("utf-8", errors="strict")
            unquote_to_bytes(query).decode("utf-8", errors="strict")
            return parse_qs(query, keep_blank_values=True, max_num_fields=1000)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BadRequest("Invalid query string") from exc

    async def json(self):
        try:
            return json.loads(self.body, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise BadRequest("Invalid JSON body") from exc


class Response:
    def __init__(self, body=b"", *, status=200, headers=(), media_type="text/plain; charset=utf-8"):
        if not 100 <= status <= 599:
            raise ValueError("Invalid HTTP status")
        if isinstance(body, str):
            self.body = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray, memoryview)):
            self.body = bytes(body)
        else:
            raise TypeError(f"Response body must be str or bytes, got {type(body).__name__}")
        self.status = status
        self.headers = [(bytes(k).lower(), bytes(v)) for k, v in headers]
        for key, value in self.headers:
            if (not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9a-z-]+", key) or
                    any(c < 32 or c == 127 for c in value)):
                raise ValueError("Invalid response header")
        if media_type:
            try:
                media_type_bytes = media_type.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("Invalid media type") from exc
            if any(c < 32 or c == 127 for c in media_type_bytes):
                raise ValueError("Invalid media type")
        if media_type and not any(k == b"content-type" for k, _ in self.headers):
            self.headers.append((b"content-type", media_type_bytes))
        if not any(k == b"content-security-policy" for k, _ in self.headers):
            self.headers.append((b"content-security-policy", b"script-src 'self'; object-src 'none'"))
        if status not in (204, 304) and not any(k == b"content-length" for k, _ in self.headers):
            self.headers.append((b"content-length", str(len(self.body)).encode("ascii")))


class JSONResponse(Response):
    def __init__(self, value, *, status=200, headers=()):
        super().__init__(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                         status=status, headers=headers, media_type="application/json; charset=utf-8")


MAX_STREAM_CHUNK = 1_048_576


class StreamingResponse(Response):
    """A response produced by an asynchronous iterable, one bounded chunk at a time."""

    def __init__(self, content, *, status=200, headers=(), media_type="application/octet-stream"):
        if status < 200 or status in (204, 304):
            raise ValueError("Streaming responses require a status that permits a body")
        headers = list(headers)
        if any(bytes(key).lower() in {b"content-length", b"transfer-encoding"} for key, _ in headers):
            raise ValueError("Streaming response framing is managed by the server")
        super().__init__(b"", status=status, headers=headers, media_type=media_type)
        self.headers = [(key, value) for key, value in self.headers if key != b"content-length"]
        self._iterator = aiter(content)
        self._closed = False
        self._head_only = False

    async def _next_chunk(self):
        if self._closed:
            return None
        try:
            chunk = await anext(self._iterator)
        except StopAsyncIteration:
            return None
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            chunk = bytes(chunk)
        else:
            raise TypeError(f"Streaming response chunks must be str or bytes, got {type(chunk).__name__}")
        if len(chunk) > MAX_STREAM_CHUNK:
            raise ValueError(f"Streaming response chunk exceeds {MAX_STREAM_CHUNK} bytes")
        return chunk

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()
