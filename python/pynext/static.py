"""Conditional and single-range responses for immutable in-memory assets.

Static path safety (S-P0-6) lives here too: :func:`validate_static_request_path`
and :func:`resolve_static_path` implement the aiohttp-style triple defense
(``web_urldispatcher.py:607-653``, CVE-2024-23334) for any code that maps a
client-supplied path onto the filesystem — the framework's own static
serving and app-level media handlers such as ``/api/media`` (POST/GET):

1. ``normpath`` + prefix check on the (already decoded) request path.
2. Absolute-path rejection, including Windows UNC paths (``\\\\server\\share``)
   and drive-letter paths (``C:\\...`` / ``C:/...``) — an UNC path can be
   abused for NTLM credential theft on Windows, which PyNext supports.
3. ``resolve()`` + ``relative_to`` check against the root, so a symlink
   inside the root that points outside is caught. ``break_symlink_sandbox``
   (aiohttp's ``_break_symlink_sandbox``) opts out of layer 3: when true,
   only the ``normpath`` check runs and symlinks are followed. The default
   (false) resolves first and blocks escapes.

Werkzeug's ``safe_join`` is the anti-pattern here: it follows symlinks, so
layer 3 must be explicit, not assumed.
"""

import os
import posixpath
import re
from pathlib import Path, PureWindowsPath

from .http import Response


class StaticPathError(ValueError):
    """A client-supplied static/media path failed the sandbox checks."""


_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def _reject_control_chars(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise StaticPathError(f"Invalid static path: {value!r}")


def _reject_absolute_forms(value):
    """Layer (2): reject absolute and UNC paths.

    ``value`` is the path with at most one leading ``/`` stripped, so a
    plain URL path such as ``/logo.txt`` is root-relative and fine, while
    ``//server/share``, ``\\\\server\\share``, ``C:\\...`` and ``C:/...``
    are rejected. Backslashes are rejected outright: they are path
    separators on Windows, so ``..\\..\\secret`` must never survive as a
    harmless filename on a POSIX host either.
    """
    if "\\" in value:
        raise StaticPathError(f"Invalid static path: {value!r}")
    if value.startswith("/") or value.startswith("\\"):
        raise StaticPathError(f"Invalid static path: {value!r}")
    if _DRIVE_LETTER.match(value):
        raise StaticPathError(f"Invalid static path: {value!r}")
    if PureWindowsPath(value).is_absolute():
        raise StaticPathError(f"Invalid static path: {value!r}")


def _check_normpath_prefix(relative):
    """Layer (1): ``normpath`` the root-relative path and check the prefix.

    ``.``/``..`` segments are rejected outright (stricter than aiohttp,
    which normalizes them away) to match PyNext's request-path policy:
    a request path never legitimately contains them. The normalized form
    must stay inside the root, never be absolute, and never escape via
    a leading ``..``.
    """
    parts = relative.split("/")
    if any(part in (".", "..") for part in parts):
        raise StaticPathError(f"Invalid static path: {relative!r}")
    normalized = posixpath.normpath("/" + relative).lstrip("/")
    if normalized == ".":
        return ""
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise StaticPathError(f"Invalid static path: {relative!r}")
    return normalized


def validate_static_request_path(path):
    """Validate an already-decoded URL path for static-asset lookup.

    Applies layers (1) and (2) and returns the path unchanged when it
    passes, so in-memory asset dictionaries (keyed by the exact URL path)
    keep working. Raises :class:`StaticPathError` otherwise; callers that
    serve from those dictionaries map that to 404. Layer (3) does not
    apply here — no disk access happens for in-memory assets; it is
    enforced when the snapshot is built (``assets._read_public``) and by
    :func:`resolve_static_path` for disk-backed serving.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise StaticPathError(f"Invalid static path: {path!r}")
    _reject_control_chars(path)
    _reject_absolute_forms(path[1:])
    _check_normpath_prefix(path[1:])
    return path


def resolve_static_path(root, request_path, *, break_symlink_sandbox=False):
    """Resolve a client-supplied path inside ``root`` (aiohttp 3-layer defense).

    ``request_path`` is an already-decoded, root-relative path (``/a/b.png``
    or ``a/b.png`` — the leading slash is optional). Returns the resolved
    absolute :class:`pathlib.Path` inside ``root``. Raises
    :class:`StaticPathError` when any layer fails: ``..`` escape, absolute
    or UNC path, or — with the default ``break_symlink_sandbox=False`` — a
    symlink (file or directory) whose resolved target leaves ``root``.
    A symlink that stays inside ``root`` resolves fine.

    With ``break_symlink_sandbox=True`` only layers (1) and (2) run and
    symlinks are followed (aiohttp's fast-path option); the caller accepts
    that a symlink inside the root may point outside it.

    The file is not required to exist: resolution uses ``strict=False``,
    so a missing id maps to a path the caller can answer 404 for instead
    of raising here.
    """
    if not isinstance(request_path, str):
        raise StaticPathError(f"Invalid static path: {request_path!r}")
    _reject_control_chars(request_path)
    relative = request_path[1:] if request_path.startswith("/") else request_path
    _reject_absolute_forms(relative)
    normalized = _check_normpath_prefix(relative)
    root = Path(root).resolve()
    candidate = root if not normalized else root / normalized
    if break_symlink_sandbox:
        return Path(os.path.normpath(candidate))
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise StaticPathError(f"Static path escapes its root: {request_path!r}") from exc
    return resolved


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
