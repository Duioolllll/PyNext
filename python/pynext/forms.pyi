from dataclasses import dataclass
@dataclass(frozen=True)
class UploadFile:
    filename: str
    content_type: str
    data: bytes
def parse_form(content_type: bytes, body: bytes) -> dict[str, list[str | UploadFile]]: ...
