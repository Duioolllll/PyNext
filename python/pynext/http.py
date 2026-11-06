import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs


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

    @property
    def query(self):
        try:
            value = self.query_string.decode("utf-8", errors="strict")
            return parse_qs(value, keep_blank_values=True, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            # ValueError covers parse_qs refusing more than 1,000 fields;
            # UnicodeDecodeError is a ValueError subclass, listed for clarity.
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
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.status = status
        self.headers = [(bytes(k).lower(), bytes(v)) for k, v in headers]
        for key, value in self.headers:
            if not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9a-z-]+", key) or any(c < 32 or c == 127 for c in value):
                raise ValueError("Invalid response header")
        if media_type and not any(k == b"content-type" for k, _ in self.headers):
            self.headers.append((b"content-type", media_type.encode("ascii")))
        if status not in (204, 304) and not any(k == b"content-length" for k, _ in self.headers):
            self.headers.append((b"content-length", str(len(self.body)).encode("ascii")))


class JSONResponse(Response):
    def __init__(self, value, *, status=200, headers=()):
        super().__init__(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                         status=status, headers=headers, media_type="application/json; charset=utf-8")
