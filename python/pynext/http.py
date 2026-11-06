import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote_to_bytes


class BadRequest(ValueError):
    """Raised when a request's user-controlled representation is invalid."""


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query_string: bytes = b""
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b""
    params: dict = field(default_factory=dict)

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
            return json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest("Invalid JSON body") from exc


class Response:
    def __init__(self, body=b"", *, status=200, headers=(), media_type="text/plain; charset=utf-8"):
        if not 100 <= status <= 599:
            raise ValueError("Invalid HTTP status")
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
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
