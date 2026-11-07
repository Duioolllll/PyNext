"""Conditional and single-range responses for immutable in-memory assets."""

from .http import Response


def _values(request, name):
    return [value for key, value in request.headers if key.lower() == name]


def _tags_match(values, etag, *, weak):
    for value in values:
        # Commas inside a quoted opaque tag are not list separators.
        parts, start, quoted = [], 0, False
        for index, char in enumerate(value):
            if char == 34:
                quoted = not quoted
            elif char == 44 and not quoted:
                parts.append(value[start:index].strip(b" \t"))
                start = index + 1
        if quoted:
            continue
        parts.append(value[start:].strip(b" \t"))
        for part in parts:
            if part == b"*":
                return True
            if weak and part.startswith(b"W/"):
                part = part[2:]
            if part == etag:
                return True
    return False


def _bounded_number(value, ceiling):
    if not value or not value.isdigit():
        return None
    value = value.lstrip(b"0") or b"0"
    if len(value) > 20:
        return ceiling + 1
    return min(int(value), ceiling + 1)


def _single_range(value, length):
    """Return (start, end), False for unsatisfiable, or None to ignore."""
    unit, separator, fields = value.partition(b"=")
    if not separator or unit.strip().lower() != b"bytes" or b"," in fields:
        return None
    fields = fields.strip(b" \t")
    first, dash, last = fields.partition(b"-")
    if not dash or b"-" in last:
        return None
    start = _bounded_number(first, length) if first else None
    end = _bounded_number(last, length) if last else None
    if (first and start is None) or (last and end is None) or (not first and not last):
        return None
    if start is not None:
        if end is not None and end < start:
            return None
        if start >= length:
            return False
        return start, min(end if end is not None else length - 1, length - 1)
    if end == 0 or length == 0:
        return False
    return max(0, length - end), length - 1


def asset_response(request, body, media_type, etag, *, cache_control, extra_headers=()):
    etag = etag.encode("ascii")
    headers = [(b"etag", etag), (b"cache-control", cache_control),
               (b"accept-ranges", b"bytes"), *extra_headers]
    matches = _values(request, b"if-match")
    if matches and not _tags_match(matches, etag, weak=False):
        return Response(b"", status=412, headers=headers, media_type=media_type)
    nonmatches = _values(request, b"if-none-match")
    if nonmatches and _tags_match(nonmatches, etag, weak=True):
        return Response(b"", status=304, headers=headers, media_type=media_type)
    ranges = _values(request, b"range")
    if request.method == "GET" and len(ranges) == 1:
        validators = _values(request, b"if-range")
        if not validators or (len(validators) == 1 and validators[0].strip(b" \t") == etag):
            interval = _single_range(ranges[0], len(body))
            if interval is False:
                headers.append((b"content-range", f"bytes */{len(body)}".encode("ascii")))
                return Response(b"", status=416, headers=headers, media_type=media_type)
            if interval is not None:
                start, end = interval
                headers.append((b"content-range", f"bytes {start}-{end}/{len(body)}".encode("ascii")))
                return Response(body[start:end + 1], status=206, headers=headers, media_type=media_type)
    if request.method == "HEAD":
        headers.append((b"content-length", str(len(body)).encode("ascii")))
        body = b""
    return Response(body, headers=headers, media_type=media_type)
