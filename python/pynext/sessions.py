"""Server-side sessions with opaque cookies and SQLite version fencing."""
import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import MutableMapping
from pathlib import Path

from .failure_policy import FAIL_CLOSED, FAIL_OPEN, normalize_failure_policy
from .http import Response, StreamingResponse, _add_vary, _header_values

log = logging.getLogger("pynext")

_COOKIE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MAX_SESSION_BYTES = 16_384


def _check_json_serializable(key, value):
    """Raise a key-named error if ``value`` cannot be stored as JSON.

    Session data is persisted with ``json.dumps(..., allow_nan=False)``,
    so a non-serializable value used to surface only after the handler
    had run — as a bare ``TypeError`` from the middleware save, i.e. a
    500 with no pointer at the offending assignment. Validating here,
    at assignment time, fails fast with the key in the message.
    """
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise type(exc)(
            f"session value for key {key!r} is not JSON-serializable: {exc}"
        ) from exc


class SessionConflict(Exception):
    """The session was changed or revoked while this request was running."""


class SessionUnavailable(Exception):
    """The session store could not be reached (Redis outage, fail_closed).

    Raised by :class:`RedisSessionStore` under the default
    ``"fail_closed"`` failure policy instead of leaking the raw Redis
    client error, so callers can distinguish "the store is down" from a
    fencing conflict (:class:`SessionConflict`) or corrupt data
    (``ValueError``). The session middleware answers 503 for it.
    """


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
        for key, value in data.items():
            _check_json_serializable(key, value)
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
        """Remove expired rows.

        ``SessionMiddleware`` runs this automatically in the background
        (see ``purge_interval``), so applications only need to call it
        directly when they use the store without the middleware.
        """
        def purge():
            with sqlite3.connect(self.path, timeout=5) as db:
                return db.execute("DELETE FROM pynext_sessions WHERE expires<=?", (time.time(),)).rowcount
        return await asyncio.to_thread(purge)


def _session_payload(data, csrf, version):
    return json.dumps({"data": data, "csrf": csrf, "version": version}, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _decode_session(raw):
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid session data") from exc
    if (not isinstance(payload, dict) or not isinstance(payload.get("data"), dict)
            or not isinstance(payload.get("version"), int)):
        raise ValueError("invalid session data")
    return payload


def _is_watch_error(exc):
    """True for redis-py's WatchError, without importing redis.

    Injected test clients raise their own ``WatchError``; matching on the
    class name keeps the store usable with any Redis-compatible client.
    """
    return type(exc).__name__ == "WatchError"


class RedisSessionStore:
    """Redis session store with the same load/save/delete contract as SQLite.

    Sessions are stored as one key per token (``<key_prefix>:<sha256(token)>``)
    with a Redis TTL, so expiry needs no cleanup job: ``purge_expired`` is a
    no-op that returns 0. Concurrent writes are fenced with the ``version``
    embedded in the stored payload, using WATCH/MULTI optimistic locking —
    a stale writer gets :class:`SessionConflict`, exactly like the SQLite
    store. Pass a ``client`` (a ``redis.Redis`` or compatible object) to
    share a connection or to inject a fake in tests; otherwise a client is
    built from ``url`` or the ``PYNEXT_REDIS_URL`` environment variable.

    ``failure_policy`` decides what happens when Redis is unreachable:
    the default ``"fail_closed"`` raises :class:`SessionUnavailable`
    (the middleware answers 503) instead of silently losing session
    state, while ``"fail_open"`` starts a fresh ephemeral session —
    the request proceeds, but nothing is read or persisted. Reads keep
    working, but a login cannot complete during the outage: the CSRF
    token issued for it is never stored, so the login request fails
    CSRF (403). A rotation during the outage may also leave the old
    session id usable until it expires. A failed delete under ``fail_open``
    can clear the browser cookie without revoking the server session;
    its old token can remain usable until the TTL expires or a later
    successful delete. Fencing conflicts
    (:class:`SessionConflict`) and corrupt data (``ValueError``) are
    not outages and are never absorbed by either policy.
    """

    def __init__(self, url=None, *, client=None, key_prefix="pynext:session",
                 failure_policy=None):
        if not isinstance(key_prefix, str) or not key_prefix or len(key_prefix.encode("utf-8")) > 256:
            raise ValueError("session key prefix must be a nonempty string up to 256 bytes")
        self.key_prefix = key_prefix
        self.failure_policy = normalize_failure_policy(failure_policy, default=FAIL_CLOSED)
        self._owns_client = client is None
        if client is not None:
            if not all(callable(getattr(client, name, None))
                       for name in ("get", "set", "delete", "pipeline")):
                raise TypeError("session client must implement get, set, delete and pipeline")
            self.client = client
        else:
            url = url or os.environ.get("PYNEXT_REDIS_URL")
            if not isinstance(url, str) or not url.startswith(("redis://", "rediss://", "unix://")):
                raise ValueError("Redis sessions require a redis://, rediss:// or unix:// URL")
            try:
                import redis
            except ImportError as exc:
                raise ValueError("Install pynext-framework[redis] to use Redis") from exc
            self.client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1,
                                               decode_responses=False)

    def _redis_key(self, token):
        return f"{self.key_prefix}:{_key(token)}"

    def _handle_redis_error(self, operation, exc):
        """Apply the failure policy to a Redis transport error.

        Returns ``True`` when ``fail_open`` absorbs the error (the
        caller then degrades: load reports a miss, save reports the
        version that would have been stored, delete is a no-op).
        Raises :class:`SessionUnavailable` under ``fail_closed``.
        """
        if self.failure_policy == FAIL_OPEN:
            log.warning("Redis session %s failed; continuing without stored state (fail_open)",
                        operation, exc_info=True)
            return True
        raise SessionUnavailable(f"Redis session {operation} failed: {exc}") from exc

    def close(self):
        """Close the Redis client this store created (a no-op for injected clients)."""
        if self._owns_client:
            self.client.close()

    async def load(self, token):
        def read():
            raw = self.client.get(self._redis_key(token))
            if raw is None:
                return None
            payload = _decode_session(raw)
            return payload["data"], payload.get("csrf"), payload["version"]
        try:
            return await asyncio.to_thread(read)
        except ValueError:
            # Corrupt stored data is not an outage; never absorb it.
            raise
        except Exception as exc:
            self._handle_redis_error("load", exc)
            return None

    async def save(self, token, data, csrf, version, expires, *, previous_token=None):
        for key, value in data.items():
            _check_json_serializable(key, value)
        payload = json.dumps({"data": data, "csrf": csrf}, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > _MAX_SESSION_BYTES:
            raise ValueError("session data exceeds 16384 bytes")
        ttl_ms = max(0, int((expires - time.time()) * 1000))
        try:
            if previous_token is not None:
                return await asyncio.to_thread(self._rotate, previous_token, token, data, csrf, version, ttl_ms)
            if version is None:
                return await asyncio.to_thread(self._insert, token, data, csrf, ttl_ms)
            return await asyncio.to_thread(self._update, token, data, csrf, version, ttl_ms)
        except (SessionConflict, ValueError):
            # Fencing and corrupt-data errors are not outages.
            raise
        except Exception as exc:
            self._handle_redis_error("save", exc)
            # fail_open: the session is ephemeral — nothing was stored —
            # but report the version that would have been stored so the
            # request proceeds exactly as if the save had succeeded.
            return 1 if version is None or previous_token is not None else version + 1

    def _insert(self, token, data, csrf, ttl_ms):
        if ttl_ms <= 0:
            # Already expired: nothing to store, and load() reports a miss,
            # matching the SQLite store's expired row.
            return 1
        if not self.client.set(self._redis_key(token), _session_payload(data, csrf, 1),
                               nx=True, px=ttl_ms):
            raise SessionConflict("session already exists")
        return 1

    def _update(self, token, data, csrf, version, ttl_ms):
        key = self._redis_key(token)
        pipe = self.client.pipeline()
        try:
            pipe.watch(key)
            raw = pipe.get(key)
            if raw is None:
                raise SessionConflict("session changed or expired")
            if _decode_session(raw)["version"] != version:
                raise SessionConflict("session changed or expired")
            pipe.multi()
            if ttl_ms <= 0:
                pipe.delete(key)
            else:
                pipe.set(key, _session_payload(data, csrf, version + 1), px=ttl_ms)
            pipe.execute()
            return version + 1
        except SessionConflict:
            raise
        except Exception as exc:
            if _is_watch_error(exc):
                raise SessionConflict("session changed or expired") from exc
            raise
        finally:
            pipe.reset()

    def _rotate(self, previous_token, token, data, csrf, version, ttl_ms):
        old_key, new_key = self._redis_key(previous_token), self._redis_key(token)
        pipe = self.client.pipeline()
        try:
            pipe.watch(old_key, new_key)
            raw = pipe.get(old_key)
            if raw is None:
                raise SessionConflict("session changed or expired")
            if _decode_session(raw)["version"] != version:
                raise SessionConflict("session changed or expired")
            pipe.multi()
            pipe.delete(old_key)
            if ttl_ms > 0:
                pipe.set(new_key, _session_payload(data, csrf, 1), px=ttl_ms)
            pipe.execute()
            return 1
        except SessionConflict:
            raise
        except Exception as exc:
            if _is_watch_error(exc):
                raise SessionConflict("session changed or expired") from exc
            raise
        finally:
            pipe.reset()

    async def delete(self, token):
        try:
            await asyncio.to_thread(self.client.delete, self._redis_key(token))
        except Exception as exc:
            # fail_open: logout is best-effort; fail_closed raises.
            self._handle_redis_error("delete", exc)

    async def purge_expired(self):
        """No-op: Redis expires session keys with their TTL automatically."""
        return 0


class Session(MutableMapping):
    """Per-request session data.

    Security rule: rotate the session id on login. Call
    :meth:`rotate_on_login` (or :meth:`rotate`) immediately after
    verifying credentials — on every login, including re-login after a
    privilege change — so a session id issued before authentication
    (which an attacker may have fixed) cannot become an authenticated
    session. The rotation preserves the session data and issues a fresh
    CSRF token; the old id stops working as soon as the response is
    sent. Call :meth:`invalidate` on logout to delete the server-side
    session and clear the cookie.
    """

    def __init__(self, data=None, csrf=None, token=None, version=None):
        self._data = data if data is not None else {}
        for key, value in self._data.items():
            _check_json_serializable(key, value)
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
        # Fail here, at assignment time, with the key named — not after
        # the handler returns, when the middleware save would raise a
        # bare TypeError and the request would answer 500.
        _check_json_serializable(key, value)
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
        """Mark this session for id rotation when the response is sent.

        The middleware then issues a fresh opaque token, moves the
        stored data to it (version 1), and deletes the old server-side
        entry, so the pre-rotation id stops working. Rotation also
        issues a fresh CSRF token. The session data itself is preserved.
        Call this (or :meth:`rotate_on_login`) after successful
        authentication — never skip it on login, or the session is
        vulnerable to fixation: an id fixed by an attacker before login
        would stay valid after it.
        """
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        self._rotate = True
        self._csrf = secrets.token_urlsafe(32)

    def rotate_on_login(self):
        """Rotate the session id at login (see :meth:`rotate`).

        This is the login hook: call it immediately after verifying
        credentials, once the authenticated identity is stored (for
        example ``session["user_id"] = user.id``), and before returning
        the login response. It is equivalent to :meth:`rotate` but names
        the call site, so a login handler that forgets it stands out in
        review. Rotation preserves the session data and issues a fresh
        CSRF token; the old id is deleted server-side when the response
        is sent. Also call it again whenever privilege changes
        (for example after a password change or privilege escalation).
        """
        self.rotate()

    def invalidate(self):
        self._data.clear()
        self._csrf = None
        self._invalidated = True

    def _changed(self):
        try:
            return self._rotate or json.dumps({"data": self._data, "csrf": self._csrf}, sort_keys=True) != self._initial
        except (TypeError, ValueError):
            # A value mutated in place after assignment (for example
            # ``session["tags"].append(obj)``) bypasses __setitem__
            # validation; name the offending key instead of failing
            # with a bare error from the middleware save.
            for key, value in self._data.items():
                _check_json_serializable(key, value)
            raise


class SessionMiddleware:
    """Attach ``request.state['session']`` and persist changes before sending headers.

    The session cookie is ``HttpOnly`` and ``Secure`` by default with
    ``SameSite=Lax``. A request that presents the cookie name more than
    once (duplicate ``Cookie`` headers or a duplicated name in one
    header) is treated as having no session at all — fail-closed, so an
    attacker cannot smuggle a chosen session id past the single-value
    check by sending two values. Login handlers must rotate the session
    id after authentication; see :meth:`Session.rotate_on_login`.

    When ``purge_interval`` is set (seconds, default 3600), the middleware
    runs the store's ``purge_expired`` in a background task while requests
    are being served, so expired sessions are cleaned up without a separate
    maintenance job. Pass ``purge_interval=None`` to disable it, and call
    :meth:`close` to stop the task when the middleware is retired. Redis
    stores expire keys with TTL, so their purge is a no-op.
    """

    def __init__(self, store, *, cookie_name="pynext_session", secure=True, same_site="Lax", max_age=86400,
                 purge_interval=3600):
        if not isinstance(cookie_name, str) or not _COOKIE_NAME.fullmatch(cookie_name):
            raise ValueError("invalid session cookie name")
        if not isinstance(secure, bool):
            raise TypeError("secure must be a bool")
        if same_site not in {"Lax", "Strict", "None"} or (same_site == "None" and not secure):
            raise ValueError("SameSite must be Lax, Strict, or None with Secure")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 0 < max_age <= 31_536_000:
            raise ValueError("max_age must be an integer from 1 to 31536000")
        if purge_interval is not None and (isinstance(purge_interval, bool)
                                           or not isinstance(purge_interval, (int, float))
                                           or purge_interval <= 0):
            raise ValueError("purge_interval must be None or a number greater than 0")
        if not all(inspect.iscoroutinefunction(getattr(store, name, None))
                   for name in ("load", "save", "delete")):
            raise TypeError("session store must implement async load, save and delete")
        self.store, self.cookie_name = store, cookie_name
        self.secure, self.same_site, self.max_age = secure, same_site, max_age
        self.purge_interval = purge_interval
        self._purge_task = None

    def _ensure_purge_task(self):
        if self.purge_interval is None:
            return
        # purge_expired is optional for external stores; without it there is
        # nothing for the background task to do.
        if not inspect.iscoroutinefunction(getattr(self.store, "purge_expired", None)):
            return
        task = self._purge_task
        if task is None or task.done():
            self._purge_task = asyncio.get_running_loop().create_task(self._purge_loop())

    async def _purge_loop(self):
        try:
            while True:
                await asyncio.sleep(self.purge_interval)
                try:
                    await self.store.purge_expired()
                except Exception:
                    log.exception("Session purge failed")
        except asyncio.CancelledError:
            raise

    async def close(self):
        """Stop the background purge task, if one is running."""
        task = self._purge_task
        self._purge_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

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
        self._ensure_purge_task()
        token = self._cookie(request)
        try:
            loaded = await self.store.load(token) if token is not None else None
        except SessionUnavailable:
            # A fail_closed store reports the outage instead of serving
            # a request with no session state; fail_open stores never
            # raise here (they report a miss instead).
            return Response("Session unavailable", status=503,
                            headers=[(b"retry-after", b"1"), (b"cache-control", b"no-store")])
        session = Session(*(loaded[:2]), token=token, version=loaded[2]) if loaded else Session()
        request.state["session"] = session
        response = await call_next(request)
        changed = session._changed() if not session._invalidated else False
        if session._invalidated:
            if session._token:
                try:
                    await self.store.delete(session._token)
                except SessionUnavailable:
                    return Response("Session unavailable", status=503,
                                    headers=[(b"retry-after", b"1"), (b"cache-control", b"no-store")])
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
            except SessionUnavailable:
                if isinstance(response, StreamingResponse):
                    await response.aclose()
                return Response("Session unavailable", status=503,
                                headers=[(b"retry-after", b"1"), (b"cache-control", b"no-store")])
            self._set_cookie(response, new_token, self.max_age)
        if loaded or changed or session._invalidated:
            response.headers = [(name, value) for name, value in response.headers if name != b"cache-control"]
            response.headers.append((b"cache-control", b"private, no-store"))
            _add_vary(response, "Cookie")
        return response
