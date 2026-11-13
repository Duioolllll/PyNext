"""Server-side sessions with opaque cookies and SQLite version fencing.

Session payload protection (S-P1-25 / M-16)
--------------------------------------------
Session payloads (``user_id``, ``permissions``, CSRF token) used to be
stored in plaintext in the SQLite file and in Redis, so anyone who could
read the store (a backup, a shared Redis, a world-readable SQLite file)
learned every session's contents. Two layers now protect them:

1. **AEAD encryption (opt-in, via a key).** Pass ``encryption_key`` (or
   ``encryption_keys`` for rotation) to :class:`SQLiteSessionStore` /
   :class:`RedisSessionStore`, or set the ``PYNEXT_SESSION_KEY``
   environment variable. Payloads are then sealed with AES-256-GCM
   (``cryptography`` package, ``pynext-framework[encryption]``) before
   they reach the store and unsealed on load. The nonce is fresh and
   random per save, and the ciphertext is bound to the session id
   (AAD = SHA-256 of the token), so a sealed payload cannot be
   transplanted onto another session id.
2. **Store hardening (always on).** The SQLite file (and its WAL files)
   are created/tightened to mode ``0600``, and a plaintext ``redis://``
   URL logs a warning recommending ``rediss://`` (TLS) or a Unix socket.

Key handling
------------
* A key is 32 raw bytes, or a base64 / hex string decoding to 32 bytes.
  Anything else is a ``ValueError`` at store construction (fail-closed:
  a misconfigured key is refused instead of silently storing plaintext).
* If a key is configured but ``cryptography`` is not installed, store
  construction raises ``ValueError`` telling the operator to install
  ``pynext-framework[encryption]`` — again fail-closed, never silent
  plaintext.
* If no key is configured, payloads are still stored in plaintext (the
  historical format) and a warning is logged once per store: encryption
  is opt-in so existing deployments keep working, but the operator is
  told how to turn it on.
* Key rotation: pass ``encryption_keys=[new_key, old_key]``. Saves always
  use the first (current) key; loads try every key, so sessions sealed
  under the old key keep working and are re-sealed under the new key on
  their next save. Once every live session has been re-saved (after at
  most one ``max_age`` window), drop the old key.
* Removing the key entirely invalidates sessions sealed under it: their
  rows fail to decode and load raises ``ValueError`` ("invalid session
  data"), exactly like corrupt stored data today. Rotate keys; do not
  silently drop them.

Backwards compatibility
------------------------
Plaintext rows written before encryption was enabled have no envelope
prefix and keep loading unchanged; their next save re-seals them under
the current key. Encrypted rows are prefixed ``v1:`` so the two formats
are unambiguous. The token identifier itself is unchanged (unsalted
SHA-256 of the opaque token); only the payload is sealed.
"""
import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import stat
import time
from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .failure_policy import FAIL_CLOSED, FAIL_OPEN, normalize_failure_policy
from .http import Response, StreamingResponse, _add_vary, _header_values, mask_csrf_token

log = logging.getLogger("pynext")

_COOKIE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MAX_SESSION_BYTES = 16_384

# Envelope prefix for AEAD-sealed payloads. Plaintext rows (the historical
# format) carry no prefix, so the two formats are distinguishable on load.
_ENCRYPTED_PREFIX = "v1:"
_NONCE_BYTES = 12
#: ``request.state`` key marking a WebSocket authorization pre-flight
#: (set by ``Application._authorize_websocket`` on its synthetic GET).
#: :meth:`SessionMiddleware.__call__` treats a marked request as
#: read-only for the session: it still loads the session so the
#: authorization check sees the identity, but it never persists a
#: change — no save, no rotation, no invalidation. The pre-flight
#: response is discarded, so a rotated token's ``Set-Cookie`` would
#: never reach the client while the old token is already gone from
#: the store (M-15). The key is framework-internal; app middleware
#: must not set it on ordinary requests.
WS_PREFLIGHT_STATE_KEY = "_pynext_ws_preflight"
# Session lifetime bounds (ROADMAP S-P1-9 / S-P1-21).
_DEFAULT_ABSOLUTE_MAX_AGE = 604_800   # 7 days: a stolen id dies even if kept active
_MAX_SESSION_LIFETIME = 2_592_000     # 30 days: no session credential may live longer
_DEFAULT_TOUCH_INTERVAL = 300         # refresh the idle deadline every 5 min of activity
_DEFAULT_SINGLE_USE_MAX_AGE = 600     # 10 minutes for a single-use sensitive-action token
_MAX_SINGLE_USE_MAX_AGE = 86_400      # a single-use token never lives longer than a day
_META_KEY = "__pynext_meta__"         # reserved session-data key (see Session)
_SINGLE_USE_PURPOSE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


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


# Fallback pool for SQLite session I/O when a store is used without an
# App (C-P1-1). A dedicated pool — never the shared asyncio default
# executor — so session reads/writes cannot be starved by sync handlers
# or renders, and vice versa. Apps pass their own session pool via the
# store's executor argument (App wires it in at construction).
_DEFAULT_SESSION_EXECUTOR = None
_DEFAULT_SESSION_EXECUTOR_LOCK = threading.Lock()
def _lifetime_timestamp(value, *, now):
    """A stored ``created_at``/``last_seen`` value, or ``now`` when absent.

    Rows written before S-P1-9 carry no timestamps; they get a fresh
    absolute clock instead of being killed on upgrade (a one-time grace).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return now
    return value


class SessionRecord(tuple):
    """A loaded session: ``(data, csrf, version)`` plus lifetime metadata.

    Subclasses :class:`tuple` so existing code keeps unpacking and
    comparing it as a plain 3-tuple — ``data, csrf, version = record``
    and ``record == (data, csrf, version)`` both still hold — while the
    middleware can read ``record.created_at`` / ``record.last_seen``
    (epoch seconds) to enforce the absolute lifetime and idle timeout
    (S-P1-9). Third-party stores may return a plain 3-tuple (then no
    lifetime is enforced) or a 4-tuple whose last item is
    ``{"created_at": ..., "last_seen": ...}``.
    """

    created_at: float
    last_seen: float

    def __new__(cls, data, csrf, version, *, created_at, last_seen):
        record = super().__new__(cls, (data, csrf, version))
        record.created_at = created_at
        record.last_seen = last_seen
        return record


def _loaded_lifetime(loaded):
    """``(created_at, last_seen)`` for a loaded session, or ``(None, None)``.

    Accepts a :class:`SessionRecord`, a plain 3-tuple (no timestamps) or
    a 4-tuple with a ``{"created_at", "last_seen"}`` metadata dict.
    Non-numeric timestamps are treated as absent.
    """
    if loaded is None:
        return None, None
    created_at = getattr(loaded, "created_at", None)
    last_seen = getattr(loaded, "last_seen", None)
    if created_at is None and len(loaded) == 4 and isinstance(loaded[3], dict):
        created_at = loaded[3].get("created_at")
        last_seen = loaded[3].get("last_seen")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        created_at = None
    if isinstance(last_seen, bool) or not isinstance(last_seen, (int, float)):
        last_seen = None
    return created_at, last_seen




def _default_session_executor():
    global _DEFAULT_SESSION_EXECUTOR
    with _DEFAULT_SESSION_EXECUTOR_LOCK:
        if _DEFAULT_SESSION_EXECUTOR is None:
            _DEFAULT_SESSION_EXECUTOR = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="pynext-session")
        return _DEFAULT_SESSION_EXECUTOR


def _parse_session_key(material, *, where="encryption key"):
    """Normalize key material to 32 raw bytes, or raise ``ValueError``.

    Accepts raw 32-byte keys, a base64 string decoding to 32 bytes, or a
    64-hex-character string. Anything else is refused fail-closed: a
    short or mistyped key must never silently weaken (or disable) the
    seal.
    """
    if isinstance(material, (bytes, bytearray)):
        raw = bytes(material)
        if len(raw) != 32:
            raise ValueError(f"{where} must be 32 bytes")
        return raw
    if isinstance(material, str):
        text = material.strip()
        try:
            raw = base64.b64decode(text, validate=True)
        except Exception:
            raw = None
        if raw is None or len(raw) != 32:
            try:
                raw = bytes.fromhex(text)
            except ValueError:
                raw = None
            if raw is None or len(raw) != 32:
                raise ValueError(
                    f"{where} must be 32 raw bytes, a base64 string, or "
                    "64 hex characters decoding to 32 bytes"
                ) from None
        return raw
    raise TypeError(f"{where} must be bytes or str")


def _resolve_session_keys(encryption_key=None, encryption_keys=None):
    """Resolve the configured session encryption keys.

    Returns a list of 32-byte keys (first = current, rest = retired keys
    still accepted for decryption), or ``None`` when encryption is not
    configured. Explicit arguments win over the ``PYNEXT_SESSION_KEY``
    environment variable; ``encryption_keys`` wins over ``encryption_key``.
    """
    materials = None
    if encryption_keys is not None:
        if isinstance(encryption_keys, (bytes, str)) or not hasattr(encryption_keys, "__iter__"):
            raise TypeError("encryption_keys must be a sequence of keys")
        materials = list(encryption_keys)
        if len(materials) == 0:
            raise ValueError("encryption_keys must not be empty")
    elif encryption_key is not None:
        materials = [encryption_key]
    else:
        env = os.environ.get("PYNEXT_SESSION_KEY")
        if env:
            materials = [env]
    if materials is None:
        return None
    return [_parse_session_key(m, where=f"encryption key #{i + 1}")
            for i, m in enumerate(materials)]


class SessionPayloadCipher:
    """AES-256-GCM seal for session payloads (S-P1-25 / M-16).

    One instance holds the current key plus any retired keys accepted
    for decryption (key rotation). ``seal`` always uses the current
    (first) key; ``unseal`` tries each key in order. The ciphertext is
    bound to the session id via AAD (SHA-256 of the token), so a sealed
    payload cannot be replayed under a different session id.
    """

    def __init__(self, keys):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ValueError(
                "Session payload encryption requires the cryptography "
                "package: install pynext-framework[encryption]"
            ) from exc
        self._aeads = [AESGCM(key) for key in keys]

    def seal(self, plaintext, token):
        """Seal ``plaintext`` (bytes) for ``token``; returns an ASCII string."""
        nonce = secrets.token_bytes(_NONCE_BYTES)
        aad = hashlib.sha256(token.encode("ascii")).digest()
        sealed = self._aeads[0].encrypt(nonce, plaintext, aad)
        return _ENCRYPTED_PREFIX + base64.b64encode(nonce + sealed).decode("ascii")

    def unseal(self, sealed_text, token):
        """Unseal a string produced by :meth:`seal`; raises ``ValueError``.

        Every failure — bad prefix handling aside, wrong key, truncated
        or tampered data, wrong session id — surfaces as ``ValueError``
        ("invalid session data"), the same as corrupt stored data, so
        callers need no new error path.
        """
        from cryptography.exceptions import InvalidTag
        try:
            raw = base64.b64decode(sealed_text[len(_ENCRYPTED_PREFIX):], validate=True)
        except Exception as exc:
            raise ValueError("invalid session data") from exc
        if len(raw) <= _NONCE_BYTES:
            raise ValueError("invalid session data")
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        aad = hashlib.sha256(token.encode("ascii")).digest()
        for aead in self._aeads:
            try:
                return aead.decrypt(nonce, ciphertext, aad)
            except InvalidTag:
                continue
            except Exception as exc:
                raise ValueError("invalid session data") from exc
        raise ValueError("invalid session data")


def _make_session_cipher(encryption_key=None, encryption_keys=None):
    """Build a :class:`SessionPayloadCipher` from the configured keys.

    Returns ``None`` (and logs a plaintext warning) when no key is
    configured. Raises ``ValueError`` fail-closed when a key is
    configured but unusable (bad format, or ``cryptography`` missing).
    """
    keys = _resolve_session_keys(encryption_key, encryption_keys)
    if keys is None:
        log.warning(
            "Session payloads are stored in plaintext. Set the "
            "PYNEXT_SESSION_KEY environment variable (or pass "
            "encryption_key to the session store) to seal them with "
            "AES-256-GCM, and prefer rediss:// for Redis."
        )
        return None
    return SessionPayloadCipher(keys)


def _seal_payload(cipher, plaintext, token):
    """Seal ``plaintext`` (bytes) when a cipher is configured, else pass through."""
    if cipher is None:
        return plaintext
    return cipher.seal(plaintext, token).encode("ascii")


def _unseal_payload(cipher, raw, token):
    """Unseal stored ``raw`` (bytes/str) for ``token``; returns plaintext bytes.

    Encrypted rows (``v1:`` prefix) require a configured cipher holding a
    key that seals them; plaintext rows pass through unchanged. A sealed
    row with no usable key, or a tampered sealed row, raises
    ``ValueError`` ("invalid session data") — it is never silently
    treated as an empty session.
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    if not text.startswith(_ENCRYPTED_PREFIX):
        # Historical plaintext format: loads unchanged (lazy migration —
        # the next save re-seals it when a cipher is configured).
        return text.encode("utf-8")
    if cipher is None:
        raise ValueError("invalid session data")
    return cipher.unseal(text, token)


def _tighten_sqlite_permissions(path):
    """Best-effort: restrict the SQLite file (and WAL sidecars) to 0600.

    The session store holds authentication-adjacent data, so the file
    must not be world/group readable. Failures (e.g. a file owned by
    another user) are logged, not raised: tightening is hardening, and
    refusing to serve sessions over an un-tightenable file would turn a
    permissions quirk into an outage.
    """
    for candidate in (path, path + "-wal", path + "-shm"):
        try:
            mode = stat.S_IMODE(os.stat(candidate).st_mode)
        except OSError:
            continue
        if mode & 0o077:
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                log.warning("Could not restrict session store file %s to 0600", candidate)



class SQLiteSessionStore:
    """SQLite session store; connections are opened per operation for worker safety.

    ``encryption_key`` / ``encryption_keys`` (or the ``PYNEXT_SESSION_KEY``
    environment variable) seal payloads with AES-256-GCM before they are
    written — see the module docstring for the key format, rotation, and
    backwards-compatibility rules. Without a key, payloads are stored in
    the historical plaintext format and a warning is logged. The SQLite
    file (and its WAL sidecars) is restricted to mode ``0600``.
    """

    def __init__(self, path, executor=None, *, encryption_key=None, encryption_keys=None):
        if str(path) == ":memory:":
            raise ValueError("SQLite sessions require a persistent file")
        # Optional executor for the blocking sqlite calls. When None the
        # module-level session pool is used; App passes its own pool.
        self.executor = executor
        self.path = str(Path(path).resolve())
        self._cipher = _make_session_cipher(encryption_key, encryption_keys)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS pynext_sessions "
                       "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, "
                       "version INTEGER NOT NULL)")
        _tighten_sqlite_permissions(self.path)

    async def load(self, token):
        def read():
            with sqlite3.connect(self.path, timeout=5) as db:
                row = db.execute("SELECT payload, version FROM pynext_sessions WHERE id=? AND expires>?",
                                 (_key(token), time.time())).fetchone()
            if row is None:
                return None
            payload = json.loads(_unseal_payload(self._cipher, row[0], token))
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ValueError("invalid session data")
            now = time.time()
            return SessionRecord(payload["data"], payload.get("csrf"), row[1],
                                 created_at=_lifetime_timestamp(payload.get("created_at"), now=now),
                                 last_seen=_lifetime_timestamp(payload.get("last_seen"), now=now))
        return await asyncio.get_running_loop().run_in_executor(
            self.executor or _default_session_executor(), read)

    async def save(self, token, data, csrf, version, expires, *, previous_token=None):
        for key, value in data.items():
            _check_json_serializable(key, value)
        # Size-check with the longest plausible timestamps, so the payload
        # actually stored (with real timestamps) never exceeds the cap.
        checked = _stored_payload(data, csrf, 9_999_999_999.999, 9_999_999_999.999)
        if len(checked.encode("utf-8")) > _MAX_SESSION_BYTES:
            raise ValueError("session data exceeds 16384 bytes")

        def write():
            now = time.time()

            def created_at_of(row, token_for_aad):
                # A corrupt old row heals as before (fresh absolute clock)
                # instead of failing the save. The old row may be sealed
                # (S-P1-25), so decode it cipher-aware with the token that
                # sealed it — otherwise every re-save under encryption
                # would reset the absolute clock (S-P1-9).
                try:
                    old_payload = json.loads(_unseal_payload(self._cipher, row[0], token_for_aad))
                except ValueError:
                    old_payload = {}
                if not isinstance(old_payload, dict):
                    old_payload = {}
                return _lifetime_timestamp(old_payload.get("created_at"), now=now)

            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                if previous_token is not None:
                    old = db.execute("SELECT payload FROM pynext_sessions WHERE id=? AND version=? AND expires>?",
                                     (_key(previous_token), version, now)).fetchone()
                    if old is None:
                        raise SessionConflict("session changed or expired")
                    created_at = created_at_of(old, previous_token)
                    cursor = db.execute("DELETE FROM pynext_sessions WHERE id=? AND version=? AND expires>?",
                                        (_key(previous_token), version, now))
                    if cursor.rowcount != 1:
                        raise SessionConflict("session changed or expired")
                    version_to_save = 1
                    db.execute("INSERT INTO pynext_sessions VALUES (?, ?, ?, ?)",
                               (_key(token), _stored_payload(data, csrf, created_at, now, cipher=self._cipher, token=token), expires, version_to_save))
                elif version is None:
                    version_to_save = 1
                    db.execute("INSERT INTO pynext_sessions VALUES (?, ?, ?, ?)",
                               (_key(token), _stored_payload(data, csrf, now, now, cipher=self._cipher, token=token), expires, version_to_save))
                else:
                    old = db.execute("SELECT payload FROM pynext_sessions WHERE id=? AND version=? AND expires>?",
                                     (_key(token), version, now)).fetchone()
                    if old is None:
                        raise SessionConflict("session changed or expired")
                    created_at = created_at_of(old, token)
                    version_to_save = version + 1
                    cursor = db.execute("UPDATE pynext_sessions SET payload=?, expires=?, version=? "
                                        "WHERE id=? AND version=? AND expires>?",
                                        (_stored_payload(data, csrf, created_at, now, cipher=self._cipher, token=token), expires, version_to_save, _key(token), version, now))
                    if cursor.rowcount != 1:
                        raise SessionConflict("session changed or expired")
                return version_to_save
        result = await asyncio.get_running_loop().run_in_executor(
            self.executor or _default_session_executor(), write)
        # The first WAL write creates the -wal/-shm sidecars; tighten them too.
        _tighten_sqlite_permissions(self.path)
        return result

    async def delete(self, token):
        def remove():
            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute("DELETE FROM pynext_sessions WHERE id=?", (_key(token),))
        await asyncio.get_running_loop().run_in_executor(
            self.executor or _default_session_executor(), remove)

    async def purge_expired(self):
        """Remove expired rows.

        ``SessionMiddleware`` runs this automatically in the background
        (see ``purge_interval``), so applications only need to call it
        directly when they use the store without the middleware.
        """
        def purge():
            with sqlite3.connect(self.path, timeout=5) as db:
                return db.execute("DELETE FROM pynext_sessions WHERE expires<=?", (time.time(),)).rowcount
        return await asyncio.get_running_loop().run_in_executor(
            self.executor or _default_session_executor(), purge)


def _stored_payload(data, csrf, created_at, last_seen, cipher=None, token=None):
    """Session payload JSON for the SQLite store (version lives in a column).

    ``created_at`` is the session's birth (absolute lifetime anchor) and
    ``last_seen`` the last request that touched it (idle timeout anchor);
    both are refreshed/preserved by :meth:`SQLiteSessionStore.save`.
    Sealed with AES-256-GCM when a cipher is configured (S-P1-25).
    """
    raw = json.dumps({"data": data, "csrf": csrf, "created_at": created_at, "last_seen": last_seen},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if cipher is None:
        return raw
    return _seal_payload(cipher, raw.encode("utf-8"), token).decode("ascii")


def _session_payload(data, csrf, version, cipher=None, token=None, *, created_at=None, last_seen=None):
    """Session payload JSON for the Redis store (version lives in the payload).

    ``created_at`` / ``last_seen`` are the S-P1-9 lifetime anchors
    (omitted when unknown); sealed with AES-256-GCM when a cipher
    is configured (S-P1-25).
    """
    payload = {"data": data, "csrf": csrf, "version": version}
    if created_at is not None:
        payload["created_at"] = created_at
    if last_seen is not None:
        payload["last_seen"] = last_seen
    raw = json.dumps(payload, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    if cipher is None:
        return raw
    return _seal_payload(cipher, raw.encode("utf-8"), token).decode("ascii")


def _decode_session(raw, cipher=None, token=None):
    try:
        payload = json.loads(_unseal_payload(cipher, raw, token))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid session data") from exc
    if (not isinstance(payload, dict) or not isinstance(payload.get("data"), dict)
            or not isinstance(payload.get("version"), int)):
        raise ValueError("invalid session data")
    for field in ("created_at", "last_seen"):
        value = payload.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
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
    CSRF (403). A rotation during the outage leaves the old session id
    usable until it expires — unless it is queued for revocation (see
    below). A failed delete under ``fail_open`` clears the browser
    cookie without revoking the server session, so the old token stays
    usable — but it is queued in :attr:`pending_revocations` and
    revoked as soon as Redis is reachable again: every successful
    load/save/delete first flushes the queue, and
    :meth:`flush_pending_revocations` drains it explicitly. ``delete``
    reports the outcome (``True`` revoked now, ``False`` queued for
    later) so callers can tell a confirmed logout from a deferred one.
    Fencing conflicts (:class:`SessionConflict`) and corrupt data
    (``ValueError``) are not outages and are never absorbed by either
    policy.
    ``encryption_key`` / ``encryption_keys`` (or the ``PYNEXT_SESSION_KEY``
    environment variable) seal payloads with AES-256-GCM before they are
    written — see the module docstring for the key format, rotation, and
    backwards-compatibility rules. Without a key, payloads are stored in
    the historical plaintext format and a warning is logged. A plaintext
    ``redis://`` URL also logs a warning recommending ``rediss://`` (TLS)
    or a Unix socket, so session contents do not cross the network
    unencrypted.
    """

    def __init__(self, url=None, *, client=None, key_prefix="pynext:session",
                 failure_policy=None, encryption_key=None, encryption_keys=None,
                 max_pending_revocations=10_000):
        if not isinstance(key_prefix, str) or not key_prefix or len(key_prefix.encode("utf-8")) > 256:
            raise ValueError("session key prefix must be a nonempty string up to 256 bytes")
        if isinstance(max_pending_revocations, bool) or not isinstance(max_pending_revocations, int) \
                or not 0 <= max_pending_revocations <= 1_000_000:
            raise ValueError("max_pending_revocations must be an integer from 0 to 1000000")
        self.key_prefix = key_prefix
        self.failure_policy = normalize_failure_policy(failure_policy, default=FAIL_CLOSED)
        self._cipher = _make_session_cipher(encryption_key, encryption_keys)
        self.max_pending_revocations = max_pending_revocations
        # token hashes whose delete/rotation failed under fail_open, in
        # queue order; flushed on the next successful store operation
        # (S-P1-20 / M-11). 0 disables queueing (revocation is lost).
        self._pending_revocations = {}
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
            if url.startswith("redis://"):
                log.warning(
                    "Redis sessions use a plaintext redis:// URL: session "
                    "traffic is unencrypted on the network. Prefer "
                    "rediss:// (TLS) or a unix:// socket."
                )
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

    def _queue_revocation(self, token):
        """Queue a token hash for revocation once Redis is reachable (S-P1-20).

        Only called on the ``fail_open`` path, where a delete/rotation
        could not reach Redis: without the queue the old token would
        stay usable until its TTL. The queue is bounded — when it is
        full the oldest entry is dropped (and logged), so an outage
        cannot grow it without limit.
        """
        if self.max_pending_revocations == 0:
            log.warning("Redis session revocation for a token could not be queued "
                        "(max_pending_revocations=0); the old token stays valid until its TTL")
            return
        self._pending_revocations[_key(token)] = time.time()
        while len(self._pending_revocations) > self.max_pending_revocations:
            oldest = next(iter(self._pending_revocations))
            del self._pending_revocations[oldest]
            log.warning("Pending session revocation queue full; dropped oldest entry %s", oldest)
        log.warning("Redis session revocation queued for recovery "
                    "(%d pending); the old token stays valid until Redis recovers",
                    len(self._pending_revocations))

    @property
    def pending_revocations(self):
        """SHA-256 hashes of tokens queued for revocation (S-P1-20), in queue order."""
        return tuple(self._pending_revocations)

    def _flush_pending_sync(self):
        """Delete every queued token hash from Redis; never raises.

        Returns the number of keys Redis reported deleting. Keys that
        already expired (or were never stored) count as revoked too —
        they are dropped from the queue either way.
        """
        keys = tuple(self._pending_revocations)
        if not keys:
            return 0
        try:
            removed = self.client.delete(*[f"{self.key_prefix}:{token_hash}" for token_hash in keys])
            count = removed if isinstance(removed, int) else len(keys)
        except Exception:
            log.debug("Flushing pending session revocations failed; keeping %d queued",
                      len(keys), exc_info=True)
            return 0
        for token_hash in keys:
            self._pending_revocations.pop(token_hash, None)
        return count

    async def flush_pending_revocations(self):
        """Drain the pending-revocation queue now; never raises.

        Returns ``(revoked, still_pending)``. A transport error leaves
        the queue untouched for the next attempt (including the automatic
        flush after the next successful store operation).
        """
        if not self._pending_revocations:
            return 0, 0
        revoked = await asyncio.to_thread(self._flush_pending_sync)
        return revoked, len(self._pending_revocations)

    async def _maybe_flush_pending(self):
        """Best-effort flush after a successful operation (recovery path)."""
        if self._pending_revocations:
            await asyncio.to_thread(self._flush_pending_sync)

    def close(self):
        """Close the Redis client this store created (a no-op for injected clients)."""
        if self._owns_client:
            self.client.close()

    async def load(self, token):
        def read():
            raw = self.client.get(self._redis_key(token))
            if raw is None:
                return None
            payload = _decode_session(raw, self._cipher, token)
            now = time.time()
            return SessionRecord(payload["data"], payload.get("csrf"), payload["version"],
                                 created_at=_lifetime_timestamp(payload.get("created_at"), now=now),
                                 last_seen=_lifetime_timestamp(payload.get("last_seen"), now=now))
        try:
            # Revoke anything queued during the outage *before* reading:
            # the first request after recovery must already see the
            # logout/rotation, not get one last read of the dead token.
            await self._maybe_flush_pending()
            result = await asyncio.to_thread(read)
        except ValueError:
            # Corrupt stored data is not an outage; never absorb it.
            raise
        except Exception as exc:
            self._handle_redis_error("load", exc)
            return None
        return result

    async def save(self, token, data, csrf, version, expires, *, previous_token=None):
        for key, value in data.items():
            _check_json_serializable(key, value)
        # Size-check with the longest plausible timestamps, so the payload
        # actually stored never exceeds the cap.
        checked = _session_payload(data, csrf, 1_000_000_000,
                                   created_at=9_999_999_999.999, last_seen=9_999_999_999.999)
        if len(checked.encode("utf-8")) > _MAX_SESSION_BYTES:
            raise ValueError("session data exceeds 16384 bytes")
        ttl_ms = max(0, int((expires - time.time()) * 1000))
        try:
            if previous_token is not None:
                result = await asyncio.to_thread(self._rotate, previous_token, token, data, csrf, version, ttl_ms)
            elif version is None:
                result = await asyncio.to_thread(self._insert, token, data, csrf, ttl_ms)
            else:
                result = await asyncio.to_thread(self._update, token, data, csrf, version, ttl_ms)
        except (SessionConflict, ValueError):
            # Fencing and corrupt-data errors are not outages.
            raise
        except Exception as exc:
            self._handle_redis_error("save", exc)
            # fail_open only: the rotation never happened, so the old id
            # is still valid — queue it for revocation on recovery
            # instead of leaving it usable until its TTL (S-P1-20).
            if previous_token is not None:
                self._queue_revocation(previous_token)
            # fail_open: the session is ephemeral — nothing was stored —
            # but report the version that would have been stored so the
            # request proceeds exactly as if the save had succeeded.
            return 1 if version is None or previous_token is not None else version + 1
        await self._maybe_flush_pending()
        return result

    def _insert(self, token, data, csrf, ttl_ms):
        if ttl_ms <= 0:
            # Already expired: nothing to store, and load() reports a miss,
            # matching the SQLite store's expired row.
            return 1
        now = time.time()
        if not self.client.set(self._redis_key(token),
                               _session_payload(data, csrf, 1, self._cipher, token,
                                                created_at=now, last_seen=now),
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
            stored = _decode_session(raw, self._cipher, token)
            if stored["version"] != version:
                raise SessionConflict("session changed or expired")
            # Rotation never moves the absolute clock; an update must not
            # either — preserve the original created_at (S-P1-9).
            created_at = _lifetime_timestamp(stored.get("created_at"), now=time.time())
            pipe.multi()
            if ttl_ms <= 0:
                pipe.delete(key)
            else:
                pipe.set(key, _session_payload(data, csrf, version + 1, self._cipher, token,
                                               created_at=created_at, last_seen=time.time()), px=ttl_ms)
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
            stored = _decode_session(raw, self._cipher, previous_token)
            if stored["version"] != version:
                raise SessionConflict("session changed or expired")
            created_at = _lifetime_timestamp(stored.get("created_at"), now=time.time())
            pipe.multi()
            pipe.delete(old_key)
            if ttl_ms > 0:
                pipe.set(new_key, _session_payload(data, csrf, 1, self._cipher, token,
                                                   created_at=created_at, last_seen=time.time()), px=ttl_ms)
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
        """Delete a session; returns ``True`` when revoked, ``False`` when queued.

        Under ``fail_open`` a transport error queues the token hash in
        :attr:`pending_revocations` for revocation on recovery instead
        of silently leaving the old token usable until its TTL (S-P1-20);
        under ``fail_closed`` the error raises :class:`SessionUnavailable`.
        """
        try:
            await asyncio.to_thread(self.client.delete, self._redis_key(token))
        except Exception as exc:
            # fail_open: logout is best-effort, but the revocation is
            # queued rather than dropped; fail_closed raises.
            self._handle_redis_error("delete", exc)
            self._queue_revocation(token)
            return False
        await self._maybe_flush_pending()
        return True

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
    session and clear the cookie. As a backstop, the middleware rotates
    the id automatically whenever a request authenticates a previously
    anonymous session (see :meth:`SessionMiddleware.__call__`, H-6), so
    a login handler that forgets to rotate cannot leave a fixed id
    authenticated — but call it explicitly anyway: the automatic
    rotation runs after the handler, so a token issued before it no
    longer validates.

    Lifetime (S-P1-9): the middleware bounds every session twice — an
    absolute lifetime (``absolute_max_age``, default 7 days) measured
    from creation, and an idle timeout (``idle_timeout``, default
    ``max_age``) measured from the last request. Rotation does not move
    the absolute deadline.

    Sensitive actions (S-P1-21): use :meth:`issue_single_use_token` /
    :meth:`verify_single_use_token` for one-shot confirmation tokens
    (delete-account, password change, transfers), and
    :meth:`recently_authenticated` to demand a fresh login for
    high-value actions. Both live under the reserved
    ``__pynext_meta__`` key, which is hidden from the mapping
    interface (iteration, ``len``, ``in``, item access) and can never
    be set or deleted by application code.
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
        # Lifetime anchors, filled in by SessionMiddleware from the
        # loaded SessionRecord (None for a fresh session — the middleware
        # substitutes "now"). Not part of the persisted data.
        self._created_at: float | None = None
        self._last_seen: float | None = None

    def __getitem__(self, key):
        if key == _META_KEY:
            raise KeyError(key)
        return self._data[key]

    def __setitem__(self, key, value):
        if key == _META_KEY:
            raise ValueError("session key '__pynext_meta__' is reserved for single-use tokens and auth timestamps")
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        # Fail here, at assignment time, with the key named — not after
        # the handler returns, when the middleware save would raise a
        # bare TypeError and the request would answer 500.
        _check_json_serializable(key, value)
        self._data[key] = value

    def __delitem__(self, key):
        if key == _META_KEY:
            raise KeyError(key)
        del self._data[key]

    def __iter__(self):
        return (key for key in self._data if key != _META_KEY)

    def __len__(self):
        return len(self._data) - (1 if _META_KEY in self._data else 0)

    def csrf_token(self):
        """Return a CSRF token for this session (masked, Django-style).

        Every call returns a different value — a fresh random one-time
        mask over the stored secret — so the token embedded in a page
        or JSON response changes per request and cannot be recovered by
        BREACH-style compression analysis. Submit the masked value (or
        the raw secret) in the ``X-CSRF-Token`` header or the
        ``csrf_token`` form field; validation unmasks it and compares
        the secret in constant time. The stored secret itself is only
        replaced by :meth:`rotate` (login, privilege change). Fetch the
        token after any rotation: a token issued before rotation no
        longer validates.
        """
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        if self._csrf is None:
            self._csrf = secrets.token_urlsafe(32)
        return mask_csrf_token(self._csrf)

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
        It also records that credentials were verified right now (see
        :meth:`recently_authenticated`), so sensitive actions can demand
        a fresh login instead of trusting an hours-old session.
        """
        self.rotate()
        self.mark_authenticated()

    # -- single-use tokens and re-authentication (S-P1-21) ------------

    def _meta(self):
        """The reserved metadata record, creating it on first write.

        ``{"single_use": {purpose: {"hash", "exp"}}, "auth_at": float}``.
        Written straight into ``_data`` (never through ``__setitem__``,
        which rejects the reserved key); the middleware persists it with
        the rest of the session data, so tokens and ``auth_at`` survive
        rotation and load like any other session state, and die with
        :meth:`invalidate`.
        """
        meta = self._data.get(_META_KEY)
        if not isinstance(meta, dict):
            meta = {"single_use": {}, "auth_at": None}
            self._data[_META_KEY] = meta
        else:
            meta.setdefault("single_use", {})
            meta.setdefault("auth_at", None)
        return meta

    def _meta_read(self):
        meta = self._data.get(_META_KEY)
        if not isinstance(meta, dict):
            return {"single_use": {}, "auth_at": None}
        return meta

    def issue_single_use_token(self, purpose, *, max_age=_DEFAULT_SINGLE_USE_MAX_AGE):
        """Issue a purpose-bound single-use token for a sensitive action.

        Render the returned token into the confirmation form (hidden
        field or header) for actions such as deleting the account,
        changing the password or confirming a transfer, and check it
        with :meth:`verify_single_use_token` before performing the
        action. The token is good for one verification attempt within
        ``max_age`` seconds (default 600); issuing a new token for the
        same purpose invalidates the previous one. Only the SHA-256
        hash is stored server-side, so a stolen session dump does not
        reveal the token — and the token alone is useless without the
        session cookie that carries it.
        """
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        if not isinstance(purpose, str) or not _SINGLE_USE_PURPOSE.fullmatch(purpose):
            raise ValueError("purpose must be 1-64 chars of [A-Za-z0-9_-]")
        if isinstance(max_age, bool) or not isinstance(max_age, int) \
                or not 1 <= max_age <= _MAX_SINGLE_USE_MAX_AGE:
            raise ValueError("max_age must be an integer from 1 to 86400")
        token = secrets.token_urlsafe(32)
        meta = self._meta()
        meta["single_use"][purpose] = {
            "hash": hashlib.sha256(token.encode("ascii")).hexdigest(),
            "exp": time.time() + max_age,
        }
        return token

    def verify_single_use_token(self, purpose, submitted):
        """Verify — and consume — a single-use token (see :meth:`issue_single_use_token`).

        Returns ``True`` only on the first verification attempt that
        presents the right value inside its lifetime. The entry is
        deleted on any verification attempt for that purpose, match or
        not, so a token cannot be replayed or brute-forced across
        attempts; an expired or unknown token also returns ``False``.
        """
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        if not isinstance(purpose, str):
            return False
        meta = self._meta_read()
        single_use = meta.get("single_use")
        entry = single_use.get(purpose) if isinstance(single_use, dict) else None
        # Consume first, then judge: one attempt, whatever the outcome.
        stored = self._data.get(_META_KEY)
        if isinstance(stored, dict) and isinstance(stored.get("single_use"), dict):
            stored["single_use"].pop(purpose, None)
            if not stored["single_use"] and stored.get("auth_at") is None:
                del self._data[_META_KEY]
        if not isinstance(entry, dict):
            return False
        exp = entry.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or time.time() > exp:
            return False
        expected = entry.get("hash")
        if not isinstance(expected, str) or not isinstance(submitted, str):
            return False
        try:
            candidate = hashlib.sha256(submitted.encode("ascii")).hexdigest()
        except UnicodeError:
            return False
        return hmac.compare_digest(expected, candidate)

    def mark_authenticated(self):
        """Record that the user's credentials were verified right now.

        Called automatically by :meth:`rotate_on_login`. Call it
        directly only when credentials were verified without a rotation
        (for example an API-key login that keeps the session id).
        :meth:`recently_authenticated` reads this timestamp.
        """
        if self._invalidated:
            raise RuntimeError("session has been invalidated")
        self._meta()["auth_at"] = time.time()

    @property
    def authenticated_at(self):
        """Epoch seconds when credentials were last verified, or ``None``."""
        auth_at = self._meta_read().get("auth_at")
        if isinstance(auth_at, bool) or not isinstance(auth_at, (int, float)):
            return None
        return auth_at

    def recently_authenticated(self, max_age=900):
        """True when credentials were verified within the last ``max_age`` seconds.

        Default window is 900 seconds (15 minutes). Gate destructive or
        high-value actions on this — a session that has been alive for
        hours must re-authenticate first — instead of trusting the
        session cookie alone. Returns ``False`` when no login was ever
        recorded (for example a session that only ever held anonymous
        data, or one authenticated before this feature existed).
        """
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= _MAX_SINGLE_USE_MAX_AGE:
            raise ValueError("max_age must be an integer from 1 to 86400")
        auth_at = self.authenticated_at
        return auth_at is not None and time.time() - auth_at <= max_age

    def cycle_key(self):
        """Rotate the session id, keeping the data (Django ``cycle_key``).

        Alias for :meth:`rotate`: call it on login and whenever
        privilege changes, so a session id fixed before authentication
        cannot become an authenticated session. The old id is deleted
        server-side when the response is sent; the CSRF secret rotates
        with it.
        """
        self.rotate()

    def invalidate(self):
        self._data.clear()
        self._csrf = None
        self._invalidated = True

    def flush(self):
        """Delete the session entirely — data and id (Django ``flush``).

        Alias for :meth:`invalidate`: call it on logout. The server-side
        entry is deleted and the cookie cleared when the response is
        sent; the next request starts a fresh anonymous session.
        """
        self.invalidate()

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


def _auto_rotate_on_login(session, previous_user_id):
    """Rotate the session id when this request authenticated it (H-6).

    ``previous_user_id`` is who (if anyone) the session authenticated
    before the handler ran; the current ``session["user_id"]`` is who
    it authenticates now. When the session was anonymous — or another
    user's — and is authenticated afterwards, force a rotation, so an
    id an attacker fixed before login cannot survive as the
    authenticated session. An app that already called
    :meth:`Session.rotate_on_login` is unaffected (``_rotate`` is
    already set); a session that stays anonymous, or keeps the same
    identity, is left alone. The rotation preserves the data and
    issues a fresh CSRF secret, exactly like an explicit
    :meth:`Session.rotate`.
    """
    current_user_id = session.get("user_id")
    if not isinstance(current_user_id, str) or not current_user_id:
        return
    was_authenticated = isinstance(previous_user_id, str) and bool(previous_user_id)
    if (not was_authenticated or current_user_id != previous_user_id) and not session._rotate:
        log.debug("Rotating session id on authentication transition")
        session.rotate()


class SessionMiddleware:
    """Attach ``request.state['session']`` and persist changes before sending headers.

    The session cookie is ``HttpOnly`` and ``Secure`` by default with
    ``SameSite=Lax`` — the safe defaults (S-P0-4): a cookie issued over
    plain HTTP would be sent in cleartext, so ``Secure`` stays on
    unless the app opts out explicitly. For plain-``http://`` local
    development pass ``secure=False`` (and only there: never ship a
    production app with it). Tokens are stored server-side under their
    SHA-256 hash, never in cleartext. A request that presents the cookie name more than
    once (duplicate ``Cookie`` headers or a duplicated name in one
    header) is treated as having no session at all — fail-closed, so an
    attacker cannot smuggle a chosen session id past the single-value
    check by sending two values. Login handlers must rotate the session
    id after authentication; see :meth:`Session.rotate_on_login`.

    Session fixation (H-6): ``check_authorization`` runs before the
    handler, so it cannot see the login transition — this middleware,
    which wraps the whole request, is the only place that can guarantee
    rotation. When the handler authenticates a session that was
    anonymous (or another user's) before the request, the id is rotated
    automatically even when the login handler forgot
    :meth:`Session.rotate_on_login`: the pre-login id stops working as
    soon as the response is sent. A session that stays anonymous, or
    keeps the same identity, is left alone.
    Lifetime (S-P1-9, S-P1-21): a session dies at the earlier of two
    deadlines. ``idle_timeout`` (default ``max_age``, 12 hours) is the
    sliding deadline — it is refreshed while the user is active (the
    middleware re-saves an unchanged session at most every
    ``touch_interval`` seconds, default 300, and refreshes the cookie
    with it). ``absolute_max_age`` (default 7 days) is the hard cap
    measured from creation: a stolen cookie stays usable for at most a
    week even if the thief keeps it active, and rotation does not move
    the cap. ``max_age`` itself is both the default idle timeout and
    the cookie ``Max-Age`` (default 43200 seconds, 12 hours; at most
    30 days — a stolen session must never stay valid for a year).
    Pass ``touch_interval=None`` to disable the rolling refresh (then
    the idle deadline runs from the last save, not the last request),
    or ``0`` to refresh on every request.

    When ``purge_interval`` is set (seconds, default 3600), the middleware
    runs the store's ``purge_expired`` in a background task while requests
    are being served, so expired sessions are cleaned up without a separate
    maintenance job. Pass ``purge_interval=None`` to disable it, and call
    :meth:`close` to stop the task when the middleware is retired. Redis
    stores expire keys with TTL, so their purge is a no-op.

    Third-party stores that return a plain ``(data, csrf, version)``
    3-tuple from ``load`` get no lifetime enforcement (their timestamps
    are unknown); return a :class:`SessionRecord` — or a 4-tuple with a
    ``{"created_at", "last_seen"}`` metadata dict — to opt in.
    """

    def __init__(self, store, *, cookie_name="pynext_session", secure=True, same_site="Lax", max_age=43200,
                 idle_timeout=None, absolute_max_age=_DEFAULT_ABSOLUTE_MAX_AGE,
                 touch_interval=_DEFAULT_TOUCH_INTERVAL, purge_interval=3600):
        if not isinstance(cookie_name, str) or not _COOKIE_NAME.fullmatch(cookie_name):
            raise ValueError("invalid session cookie name")
        if not isinstance(secure, bool):
            raise TypeError("secure must be a bool")
        if same_site not in {"Lax", "Strict", "None"} or (same_site == "None" and not secure):
            raise ValueError("SameSite must be Lax, Strict, or None with Secure")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 0 < max_age <= _MAX_SESSION_LIFETIME:
            raise ValueError("max_age must be an integer from 1 to 2592000")
        if idle_timeout is None:
            idle_timeout = max_age
        elif isinstance(idle_timeout, bool) or not isinstance(idle_timeout, int) \
                or not 0 < idle_timeout <= _MAX_SESSION_LIFETIME:
            raise ValueError("idle_timeout must be None or an integer from 1 to 2592000")
        if isinstance(absolute_max_age, bool) or not isinstance(absolute_max_age, int) \
                or not 0 < absolute_max_age <= 31_536_000:
            raise ValueError("absolute_max_age must be an integer from 1 to 31536000")
        if touch_interval is not None and (isinstance(touch_interval, bool)
                                           or not isinstance(touch_interval, (int, float))
                                           or touch_interval < 0):
            raise ValueError("touch_interval must be None or a number greater than or equal to 0")
        if purge_interval is not None and (isinstance(purge_interval, bool)
                                           or not isinstance(purge_interval, (int, float))
                                           or purge_interval <= 0):
            raise ValueError("purge_interval must be None or a number greater than 0")
        if not all(inspect.iscoroutinefunction(getattr(store, name, None))
                   for name in ("load", "save", "delete")):
            raise TypeError("session store must implement async load, save and delete")
        self.store, self.cookie_name = store, cookie_name
        self.secure, self.same_site, self.max_age = secure, same_site, max_age
        self.idle_timeout, self.absolute_max_age = idle_timeout, absolute_max_age
        self.touch_interval = touch_interval
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

    def _expires_at(self, session, now):
        """Store expiry for a save: idle deadline, capped by the absolute deadline.

        Rotation does not move the absolute deadline: a session id fixed
        before login still dies at the same absolute time, so stealing a
        fresh id does not buy a fresh lifetime.
        """
        created_at = session._created_at or now
        return min(now + self.idle_timeout, created_at + self.absolute_max_age)

    async def _best_effort_delete(self, token):
        """Delete an expired session entry without ever failing the request."""
        if token is None:
            return
        try:
            await self.store.delete(token)
        except Exception:
            log.debug("Could not delete an expired session entry", exc_info=True)

    def _report_store(self, request, *, healthy):
        """Report a shared session-store touch to ``/readyz`` health.

        Only stores with a failure policy (Redis) are watched; local
        stores have no backend to go down. A fail_closed outage counts
        as a refused activation; under fail_open the outage is absorbed
        inside the store, so it cannot be seen here and is not reported.
        The request may not belong to an app (synthetic pre-flight), in
        which case there is nothing to report to.
        """
        policy = getattr(self.store, "failure_policy", None)
        if policy is None:
            return
        app = getattr(request, "_app", None)
        health = getattr(app, "backend_health", None)
        if health is None:
            return
        health.report("sessions", healthy=healthy, policy=policy)
        if not healthy:
            metrics = getattr(app, "metrics", None)
            if metrics is not None:
                metrics.observe_degraded("sessions", policy, "refused")

    async def __call__(self, request, call_next):
        self._ensure_purge_task()
        token = self._cookie(request)
        try:
            loaded = await self.store.load(token) if token is not None else None
        except SessionUnavailable:
            # A fail_closed store reports the outage instead of serving
            # a request with no session state; fail_open stores never
            # raise here (they report a miss instead).
            self._report_store(request, healthy=False)
            return Response("Session unavailable", status=503,
                            headers=[(b"retry-after", b"1"), (b"cache-control", b"no-store")])
        now = time.time()
        created_at, last_seen = _loaded_lifetime(loaded)
        if loaded is not None and created_at is not None and now - created_at >= self.absolute_max_age:
            log.info("Session expired: absolute lifetime reached; starting a fresh session")
            await self._best_effort_delete(token)
            loaded = None
        elif loaded is not None and last_seen is not None and now - last_seen >= self.idle_timeout:
            log.info("Session expired: idle timeout reached; starting a fresh session")
            await self._best_effort_delete(token)
            loaded = None
        if token is not None:
            self._report_store(request, healthy=True)
        session = Session(*(loaded[:2]), token=token, version=loaded[2]) if loaded else Session()
        # Unknown timestamps (a third-party store without lifetime
        # metadata) mean "now": no lifetime is enforced for that store.
        session._created_at = created_at if created_at is not None else now
        session._last_seen = last_seen if last_seen is not None else now
        request.state["session"] = session
        # H-6: remember who (if anyone) this session authenticated
        # before the handler ran, so a login transition can be rotated
        # even when the handler forgets rotate_on_login().
        previous_user_id = session.get("user_id")
        response = await call_next(request)
        if request.state.get(WS_PREFLIGHT_STATE_KEY):
            # WebSocket authorization pre-flight: the synthetic response
            # is discarded, so persisting here could only destroy state.
            # A rotation would delete the old token while its Set-Cookie
            # never reaches the client, an invalidation would delete a
            # live session, and even a plain mutation would store data
            # (for example a fresh CSRF token) the client never learns
            # about. The pre-flight is therefore read-only: the session
            # was loaded for the authorization check, and any mutation
            # the chain made is dropped with the response.
            return response
        if not session._invalidated:
            _auto_rotate_on_login(session, previous_user_id)
        now = time.time()
        changed = session._changed() if not session._invalidated else False
        if session._invalidated:
            # Delete the session id the request presented, even when the
            # load missed (fail_open outage): the handler invalidated an
            # ephemeral session whose _token is None, but the browser
            # still holds a real server-side id. Without this the logout
            # would never even attempt the delete, so nothing would be
            # queued for revocation on recovery (S-P1-20).
            token_to_delete = session._token or token
            if token_to_delete:
                try:
                    revoked = await self.store.delete(token_to_delete)
                except SessionUnavailable:
                    self._report_store(request, healthy=False)
                    return Response("Session unavailable", status=503,
                                    headers=[(b"retry-after", b"1"), (b"cache-control", b"no-store")])
                if revoked is False:
                    # Redis fail_open queued the revocation for recovery
                    # (S-P1-20): the logout cleared the cookie, but the
                    # old token stays valid until Redis recovers.
                    pending = getattr(self.store, "pending_revocations", ())
                    log.warning("Logout could not revoke the server session "
                                "(fail_open); revocation is queued for recovery "
                                "(%d pending)", len(pending))
                self._report_store(request, healthy=True)
            self._set_cookie(response, "", 0)
        elif changed:
            old_token = session._token
            new_token = secrets.token_urlsafe(32) if old_token is None or session._rotate else old_token
            expires = self._expires_at(session, now)
            try:
                version = await self.store.save(new_token, session._data, session._csrf, session._version,
                                                expires,
                                                previous_token=old_token if session._rotate and old_token else None)
            except SessionConflict:
                if isinstance(response, StreamingResponse):
                    await response.aclose()
                return Response("Session changed; retry the request", status=409,
                                headers=[(b"cache-control", b"no-store")])
            except SessionUnavailable:
                self._report_store(request, healthy=False)
                if isinstance(response, StreamingResponse):
                    await response.aclose()
                return Response("Session unavailable", status=503,
                                headers=[(b"retry-after", b"1"), (b"cache-control", b"no-store")])
            self._report_store(request, healthy=True)
            session._version = version
            session._last_seen = now
            # The cookie age is the idle timeout, not the remaining
            # absolute time: the cookie is only a client-side hint, and
            # the server enforces the absolute deadline on load. Using
            # the remaining absolute time here would make the browser
            # drop the cookie almost immediately whenever the absolute
            # deadline is near, killing the session long before the
            # server would.
            self._set_cookie(response, new_token, self.idle_timeout)
        elif loaded is not None and self.touch_interval is not None \
                and now - (session._last_seen or now) >= self.touch_interval:
            # Rolling idle refresh: the handler did not change the
            # session, but the user is active, so extend the idle
            # deadline (and the cookie) instead of letting it die at the
            # last save. A failed touch never fails the request — the
            # session simply keeps its previous expiry.
            expires = self._expires_at(session, now)
            try:
                version = await self.store.save(session._token, session._data, session._csrf,
                                                session._version, expires)
                session._version = version
                session._last_seen = now
                self._set_cookie(response, session._token, self.idle_timeout)
            except SessionConflict:
                log.debug("Session touch lost a fencing race; keeping the original response",
                          exc_info=True)
            except SessionUnavailable:
                log.warning("Session touch failed; the session keeps its previous expiry",
                            exc_info=True)
        if loaded or changed or session._invalidated:
            response.headers = [(name, value) for name, value in response.headers if name != b"cache-control"]
            response.headers.append((b"cache-control", b"private, no-store"))
            _add_vary(response, "Cookie")
        return response
