"""Server-side sessions with opaque cookies and SQLite version fencing."""
import asyncio
import hashlib
import inspect
import json
import re
import secrets
import sqlite3
import time
from collections.abc import MutableMapping
from pathlib import Path

from .http import Response, StreamingResponse, _add_vary, _header_values

_COOKIE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MAX_SESSION_BYTES = 16_384


class SessionConflict(Exception):
    """The session was changed or revoked while this request was running."""


def _key(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SQLiteSessionStore:
    """SQLite session store; connections are opened per operation for worker safety."""

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("SQLite sessions require a persistent file")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS pynext_sessions "
                       "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, "
                       "version INTEGER NOT NULL)")

    async def load(self, token):
        def read():
            with sqlite3.connect(self.path, timeout=5) as db:
                row = db.execute("SELECT payload, version FROM pynext_sessions WHERE id=? AND expires>?",
                                 (_key(token), time.time())).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ValueError("invalid session data")
            return payload["data"], payload.get("csrf"), row[1]
        return await asyncio.to_thread(read)

    async def save(self, token, data, csrf, version, expires, *, previous_token=None):
        payload = json.dumps({"data": data, "csrf": csrf}, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > _MAX_SESSION_BYTES:
            raise ValueError("session data exceeds 16384 bytes")

        def write():
            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                if previous_token is not None:
                    cursor = db.execute("DELETE FROM pynext_sessions WHERE id=? AND version=? AND expires>?",
                                        (_key(previous_token), version, time.time()))
                    if cursor.rowcount != 1:
                        raise SessionConflict("session changed or expired")
                    version_to_save = 1
                    db.execute("INSERT INTO pynext_sessions VALUES (?, ?, ?, ?)",
                               (_key(token), payload, expires, version_to_save))
                elif version is None:
                    version_to_save = 1
                    db.execute("INSERT INTO pynext_sessions VALUES (?, ?, ?, ?)",
                               (_key(token), payload, expires, version_to_save))
                else:
                    version_to_save = version + 1
                    cursor = db.execute("UPDATE pynext_sessions SET payload=?, expires=?, version=? "
                                        "WHERE id=? AND version=? AND expires>?",
                                        (payload, expires, version_to_save, _key(token), version, time.time()))
                    if cursor.rowcount != 1:
                        raise SessionConflict("session changed or expired")
                return version_to_save
        return await asyncio.to_thread(write)

    async def delete(self, token):
        def remove():
            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute("DELETE FROM pynext_sessions WHERE id=?", (_key(token),))
        await asyncio.to_thread(remove)

    async def purge_expired(self):
        """Remove expired rows; applications may call this from maintenance jobs."""
        def purge():
            with sqlite3.connect(self.path, timeout=5) as db:
                return db.execute("DELETE FROM pynext_sessions WHERE expires<=?", (time.time(),)).rowcount
        return await asyncio.to_thread(purge)


class Session(MutableMapping):
    """Per-request session data. Call rotate() on login and invalidate() on logout."""

    def __init__(self, data=None, csrf=None, token=None, version=None):
        self._data = data if data is not None else {}
        self._csrf = csrf
        self._token = token
        self._version = version
        self._initial = json.dumps({"data": self._data, "csrf": self._csrf}, sort_keys=True)
        self._rotate = False
        self._invalidated = False

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        self._data[key] = value

    def __delitem__(self, key):
        del self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def csrf_token(self):
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        if self._csrf is None:
            self._csrf = secrets.token_urlsafe(32)
        return self._csrf

    def rotate(self):
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        self._rotate = True
        self._csrf = secrets.token_urlsafe(32)

    def invalidate(self):
        self._data.clear()
        self._csrf = None
        self._invalidated = True

    def _changed(self):
        return self._rotate or json.dumps({"data": self._data, "csrf": self._csrf}, sort_keys=True) != self._initial


class SessionMiddleware:
    """Attach ``request.state['session']`` and persist changes before sending headers."""

    def __init__(self, store, *, cookie_name="pynext_session", secure=True, same_site="Lax", max_age=86400):
        if not isinstance(cookie_name, str) or not _COOKIE_NAME.fullmatch(cookie_name):
            raise ValueError("invalid session cookie name")
        if not isinstance(secure, bool):
            raise TypeError("secure must be a bool")
        if same_site not in {"Lax", "Strict", "None"} or (same_site == "None" and not secure):
            raise ValueError("SameSite must be Lax, Strict, or None with Secure")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 0 < max_age <= 31_536_000:
            raise ValueError("max_age must be an integer from 1 to 31536000")
        if not all(inspect.iscoroutinefunction(getattr(store, name, None))
                   for name in ("load", "save", "delete")):
            raise TypeError("session store must implement async load, save and delete")
        self.store, self.cookie_name = store, cookie_name
        self.secure, self.same_site, self.max_age = secure, same_site, max_age

    def _cookie(self, request):
        values = []
        for header in _header_values(request, "cookie"):
            for item in header.split(b";"):
                name, separator, value = item.strip().partition(b"=")
                if separator and name.decode("ascii", "ignore") == self.cookie_name:
                    values.append(value.decode("ascii", "ignore"))
        if len(values) != 1 or not _COOKIE_TOKEN.fullmatch(values[0]):
            return None
        return values[0]

    def _set_cookie(self, response, token, age):
        value = f"{self.cookie_name}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite={self.same_site}"
        if self.secure:
            value += "; Secure"
        response.headers.append((b"set-cookie", value.encode("ascii")))

    async def __call__(self, request, call_next):
        token = self._cookie(request)
        loaded = await self.store.load(token) if token is not None else None
        session = Session(*(loaded[:2]), token=token, version=loaded[2]) if loaded else Session()
        request.state["session"] = session
        response = await call_next(request)
        changed = session._changed() if not session._invalidated else False
        if session._invalidated:
            if session._token:
                await self.store.delete(session._token)
            self._set_cookie(response, "", 0)
        elif changed:
            old_token = session._token
            new_token = secrets.token_urlsafe(32) if old_token is None or session._rotate else old_token
            try:
                await self.store.save(new_token, session._data, session._csrf, session._version,
                                      time.time() + self.max_age,
                                      previous_token=old_token if session._rotate and old_token else None)
            except SessionConflict:
                if isinstance(response, StreamingResponse):
                    await response.aclose()
                return Response("Session changed; retry the request", status=409,
                                headers=[(b"cache-control", b"no-store")])
            self._set_cookie(response, new_token, self.max_age)
        if loaded or changed or session._invalidated:
            response.headers = [(name, value) for name, value in response.headers if name != b"cache-control"]
            response.headers.append((b"cache-control", b"private, no-store"))
            _add_vary(response, "Cookie")
        return response
