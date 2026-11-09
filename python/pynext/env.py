"""Load ``.env`` files into the process environment.

This is PyNext's port of Next.js ``@next/env`` (``loadEnvConfig``) plus
the ``NEXT_PUBLIC_`` client-exposure rule, renamed to the ``PYNEXT_PUBLIC_``
prefix. Only ``PYNEXT_PUBLIC_*`` variables are ever inlined into island
bundles (see :func:`public_env_defines`); every other variable stays
server-only.

File priority, highest first (same order as Next.js):

1. ``.env.{mode}.local`` (``test`` mode still reads this one)
2. ``.env.local`` (skipped in ``test`` mode, so tests never pick up
   local secrets by accident)
3. ``.env.{mode}``
4. ``.env``

A variable already present in the process environment always wins over
a ``.env`` file, as in the standard dotenv convention. Re-loading after
a ``.env`` file changes (``pynext dev`` restart) refreshes the values
this loader previously applied, but never overwrites a variable the
process environment set itself.

Secrets (``PYNEXT_REDIS_URL``, ``PYNEXT_CSRF_SIGNING_KEY`` and friends)
belong in ``.env.local`` / ``.env.production.local`` only: those files
are never copied into a standalone bundle (see ``standalone.py``).
"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

PUBLIC_PREFIX = "PYNEXT_PUBLIC_"
"""Only variables with this prefix are inlined into island bundles."""

STANDALONE_ENV_FILES = (".env", ".env.production")
"""The only ``.env`` files copied into a standalone bundle.

``.env*.local`` files are never copied: they hold local secrets that
must not ship in a deployment artifact (same rule as Next.js).
"""

MAX_ENV_FILE_BYTES = 1 * 1024 * 1024
"""Largest accepted ``.env`` file; anything bigger is a misconfiguration."""

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MODES = {"development": "development", "dev": "development",
          "production": "production", "prod": "production",
          "build": "production", "start": "production",
          "test": "test"}
# Values this loader applied, keyed by (project root, mode). Used so a
# reload (``pynext dev`` restart after editing a ``.env`` file) refreshes
# exactly the variables that came from files, while a variable the
# process environment set itself keeps winning.
_OWNED: dict[tuple[str, str], dict[str, str]] = {}


def _mode(mode):
    try:
        return _MODES[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown env mode: {mode!r} (expected 'development', 'production' or 'test')") from exc


def env_file_names(mode):
    """``.env`` file names for ``mode``, highest priority first."""
    mode = _mode(mode)
    names = [f".env.{mode}.local"]
    if mode != "test":
        names.append(".env.local")
    names.append(f".env.{mode}")
    names.append(".env")
    return tuple(names)


def _parse_env_file(path):
    """Parse one ``.env`` file into a dict.

    Supports ``export KEY=VALUE``, ``#`` comments, empty values, and
    single- or double-quoted values (double-quoted values may span lines
    and understand ``\\n``, ``\\r``, ``\\t``, ``\\\\`` and ``\\"``).
    Anything else is a ``ValueError`` naming the file and line.
    """
    data = path.read_bytes()
    if len(data) > MAX_ENV_FILE_BYTES:
        raise ValueError(f".env file exceeds {MAX_ENV_FILE_BYTES} bytes: {path}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f".env file is not valid UTF-8: {path}") from exc
    lines = text.splitlines()
    values = {}
    index = 0
    while index < len(lines):
        line_number = index + 1
        raw = lines[index]
        index += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            raise ValueError(f"Invalid line {line_number} in {path}: expected KEY=VALUE")
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise ValueError(f"Invalid variable name {key!r} on line {line_number} in {path}")
        value = value.strip()
        quote = None
        if value[:1] in {"'", '"'}:
            quote = value[0]
            # A quoted value may span lines until its closing quote.
            while True:
                body = value[1:]
                closing = _find_closing_quote(body, quote)
                if closing is not None:
                    rest = body[closing + 1:].strip()
                    if rest and not rest.startswith("#"):
                        raise ValueError(f"Unexpected text after quoted value on line {line_number} in {path}")
                    value = body[:closing]
                    break
                if index >= len(lines):
                    raise ValueError(f"Unterminated quoted value on line {line_number} in {path}")
                value += "\n" + lines[index]
                index += 1
            if quote == '"':
                value = _unescape_double_quoted(value)
        else:
            # An unquoted value ends at a " #..." comment.
            for marker in (" #", "\t#"):
                at = value.find(marker)
                if at != -1:
                    value = value[:at]
            value = value.strip()
        values[key] = value
    return values


def _find_closing_quote(body, quote):
    """Index of the closing ``quote`` in ``body``, or None.

    A quote preceded by an odd run of backslashes is escaped (double
    quotes only; single-quoted values have no escapes).
    """
    for at, char in enumerate(body):
        if char != quote:
            continue
        if quote == "'":
            return at
        backslashes = 0
        cursor = at - 1
        while cursor >= 0 and body[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return at
    return None


_DOUBLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "$": "$"}


def _unescape_double_quoted(value):
    result = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in _DOUBLE_ESCAPES:
            result.append(_DOUBLE_ESCAPES[value[index + 1]])
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


@dataclass(frozen=True)
class LoadedEnv:
    """Result of :func:`load_env`."""

    root: Path
    mode: str
    files: tuple
    """Names of the ``.env`` files actually read, highest priority first."""
    values: dict
    """File-derived values, resolved by file priority (process-environment
    precedence is not reflected here; see :attr:`applied`)."""
    applied: dict
    """Values this call actually wrote into ``os.environ``."""

    @property
    def public_values(self):
        """Effective ``PYNEXT_PUBLIC_*`` variables (process + files)."""
        return public_env_vars()


def load_env(root=".", mode="production"):
    """Load ``.env`` files for ``root``/``mode`` into ``os.environ``.

    Files are read in :func:`env_file_names` priority order; the first
    file to define a variable wins among files, and a variable already
    in the process environment wins over every file. Returns a
    :class:`LoadedEnv` naming the files read and the values applied.
    Re-loading refreshes values this loader previously applied (so
    ``pynext dev`` picks up an edited ``.env`` file on restart) and
    removes ones whose file entry disappeared.
    """
    root = Path(root).resolve()
    mode = _mode(mode)
    values = {}
    files = []
    for name in env_file_names(mode):
        path = root / name
        if path.is_symlink():
            raise ValueError(f".env file must not be a symbolic link: {path}")
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f".env entry is not a regular file: {path}")
        for key, value in _parse_env_file(path).items():
            values.setdefault(key, value)
        files.append(name)
    owned = _OWNED.get((str(root), mode), {})
    for key, old in owned.items():
        if key not in values and os.environ.get(key) == old:
            del os.environ[key]
    applied = {}
    for key, value in values.items():
        if key in owned and os.environ.get(key) == owned[key]:
            if os.environ[key] != value:
                os.environ[key] = value
            applied[key] = value
        elif key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    _OWNED[(str(root), mode)] = {key: value for key, value in values.items()
                                 if key in applied or
                                 (key in owned and os.environ.get(key) == value)}
    return LoadedEnv(root=root, mode=mode, files=tuple(files),
                     values=dict(values), applied=applied)


def public_env_vars(environ=None):
    """``PYNEXT_PUBLIC_*`` variables from ``environ`` (default: ``os.environ``)."""
    source = os.environ if environ is None else environ
    return {key: source[key] for key in sorted(source) if key.startswith(PUBLIC_PREFIX)}


def public_env_defines(environ=None):
    """esbuild ``--define`` flags inlining the public variables.

    Each flag replaces ``process.env.PYNEXT_PUBLIC_<NAME>`` in island
    source with the JSON-quoted value. Variables without the prefix are
    never emitted, so server-only secrets cannot leak into a bundle.
    """
    return [f"--define:process.env.{key}={json.dumps(value)}"
            for key, value in public_env_vars(environ).items()]
