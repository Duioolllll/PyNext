"""Snapshot explicitly public files; never resolve request paths on disk."""
import hashlib
import os
import stat
from pathlib import Path

MAX_PUBLIC_FILES = 10_000
MAX_PUBLIC_FILE_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_TOTAL_BYTES = 128 * 1024 * 1024

_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".gif": "image/gif",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".xml": "application/xml; charset=utf-8",
}


def content_type(path):
    return _TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def _validate_component(name):
    if (not name or name.startswith(".") or "\\" in name or
            any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError(f"Invalid public file or directory name: {name!r}")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Invalid Unicode public filename: {name!r}") from exc


def _read_public(root, parts, source_dir="public"):
    """Open each component without following symlinks when the OS supports it."""
    public = root / source_dir
    opened = []
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            directory = os.open(public, flags | os.O_NOFOLLOW | os.O_DIRECTORY)
            opened.append(directory)
            for part in parts[:-1]:
                directory = os.open(part, flags | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=directory)
                opened.append(directory)
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=directory)
        else:
            current = public
            for part in parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"Public symlink is not allowed: {current}")
            if not current.resolve().is_relative_to(public.resolve()):
                raise ValueError(f"Public file escapes its directory: {current}")
            fd = os.open(current, flags)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PUBLIC_FILE_BYTES:
                raise ValueError(f"Public file is not regular or exceeds {MAX_PUBLIC_FILE_BYTES} bytes")
            data = file.read(MAX_PUBLIC_FILE_BYTES + 1)
            if len(data) > MAX_PUBLIC_FILE_BYTES:
                raise ValueError(f"Public file exceeds {MAX_PUBLIC_FILE_BYTES} bytes")
            return data
    except OSError as exc:
        raise ValueError(f"Cannot read {source_dir} file safely: {'/'.join(parts)}") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def discover_public(root):
    public = root / "public"
    if public.is_symlink() or (public.exists() and not public.is_dir()):
        raise ValueError("public must be a directory, not a symbolic link")
    if not public.exists():
        return {}
    result = {}
    total = 0

    def fail(exc):
        raise ValueError("Cannot scan public directory") from exc

    for current, dirs, files in os.walk(public, followlinks=False, onerror=fail):
        for name in dirs + files:
            _validate_component(name)
            entry = Path(current) / name
            if entry.is_symlink():
                raise ValueError(f"Public symlink is not allowed: {entry}")
        for name in sorted(files):
            parts = (Path(current) / name).relative_to(public).parts
            data = _read_public(root, parts)
            total += len(data)
            if total > MAX_PUBLIC_TOTAL_BYTES or len(result) >= MAX_PUBLIC_FILES:
                raise ValueError("Public asset count or total size limit exceeded")
            result["/" + "/".join(parts)] = hashlib.sha256(data).hexdigest()
        dirs.sort()
    return dict(sorted(result.items()))


def read_public_sources(root, sources):
    assets = {}
    for path, expected in sources.items():
        data = _read_public(root, tuple(path[1:].split("/")))
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Public files changed during build or startup; retry")
        assets[path] = (data, content_type(path))
    return assets


def read_public_artifacts(root, manifest):
    sources = manifest["public_sources"]
    entries = manifest.get("assets")
    expected = {path: {"sha256": digest, "content_type": content_type(path)}
                for path, digest in sources.items()}
    if entries != expected:
        raise ValueError("Invalid public asset manifest; rebuild the application")
    directory = root / ".pynext" / "static"
    result = {}
    for path, entry in entries.items():
        file = directory / (entry["sha256"] + ".asset")
        if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
            raise ValueError(f"Missing public asset {path}; rebuild the application")
        with file.open("rb") as source:
            body = source.read(MAX_PUBLIC_FILE_BYTES + 1)
        if len(body) > MAX_PUBLIC_FILE_BYTES or hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError(f"Public asset {path} failed integrity check; rebuild the application")
        result[path] = (body, entry["content_type"])
    return result
