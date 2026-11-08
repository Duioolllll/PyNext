"""Bounded, in-memory form parsing for already-buffered HTTP requests."""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

MAX_FORM_BODY = 1_048_576
MAX_FORM_PARTS = 256
MAX_PART_HEADERS = 16_384
_BOUNDARY = re.compile(rb"[0-9A-Za-z'()+_,./:=?-]{1,70}\Z")


@dataclass(frozen=True)
class UploadFile:
    """An uploaded file. The client-provided filename is untrusted metadata."""

    filename: str
    content_type: str
    data: bytes


def parse_form(content_type, body):
    """Return a mapping of names to lists of text values or UploadFile objects."""
    if len(body) > MAX_FORM_BODY:
        raise ValueError("form body too large")
    kind, options = parse_options_header(content_type)
    if kind == b"application/x-www-form-urlencoded":
        if re.search(rb"%(?![0-9a-fA-F]{2})", body):
            raise ValueError("invalid percent escape")
        return parse_qs(body.decode("utf-8"), keep_blank_values=True,
                        encoding="utf-8", errors="strict", max_num_fields=MAX_FORM_PARTS)
    if kind != b"multipart/form-data":
        raise ValueError("unsupported form content type")
    boundary = options.get(b"boundary", b"")
    if not _BOUNDARY.fullmatch(boundary):
        raise ValueError("invalid multipart boundary")

    result = {}
    part = {}
    header_name = bytearray()
    header_value = bytearray()
    parts = 0
    complete = False

    def part_begin():
        nonlocal parts
        parts += 1
        if parts > MAX_FORM_PARTS:
            raise ValueError("too many form parts")
        part.clear()
        part.update(headers={}, data=bytearray(), header_bytes=0)

    def header_begin():
        header_name.clear()
        header_value.clear()

    def header_field(data, start, end):
        header_name.extend(data[start:end])
        part["header_bytes"] += end - start
        if part["header_bytes"] > MAX_PART_HEADERS:
            raise ValueError("multipart headers too large")

    def header_data(data, start, end):
        header_value.extend(data[start:end])
        part["header_bytes"] += end - start
        if part["header_bytes"] > MAX_PART_HEADERS:
            raise ValueError("multipart headers too large")

    def header_end():
        name = bytes(header_name).lower()
        if name in part["headers"]:
            raise ValueError("duplicate multipart header")
        part["headers"][name] = bytes(header_value)

    def part_data(data, start, end):
        part["data"].extend(data[start:end])

    def part_end():
        disposition, params = parse_options_header(part["headers"].get(b"content-disposition"))
        if disposition != b"form-data" or not params.get(b"name"):
            raise ValueError("missing multipart field name")
        name = params[b"name"].decode("utf-8")
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("invalid multipart field name")
        data = bytes(part["data"])
        if b"filename" in params:
            filename = params[b"filename"].decode("utf-8")
            if any(ord(char) < 32 or ord(char) == 127 for char in filename):
                raise ValueError("invalid filename")
            media_type = part["headers"].get(b"content-type", b"application/octet-stream").decode("latin-1")
            value = UploadFile(filename, media_type, data)
        else:
            value = data.decode("utf-8")
        result.setdefault(name, []).append(value)

    def finish():
        nonlocal complete
        complete = True

    parser = MultipartParser(boundary, callbacks={
        "on_part_begin": part_begin, "on_header_begin": header_begin,
        "on_header_field": header_field, "on_header_value": header_data,
        "on_header_end": header_end, "on_part_data": part_data,
        "on_part_end": part_end, "on_end": finish,
    }, max_size=MAX_FORM_BODY)
    parser.write(body)
    parser.finalize()
    if not complete:
        raise ValueError("incomplete multipart body")
    return result
