"""S-P1-25 (M-16): session payload AEAD encryption + store hardening.

Covers: AES-256-GCM sealing for both stores, no plaintext leakage in the
stored rows, SQLite 0600 permissions, plaintext-legacy migration,
key rotation, wrong-key/tamper fail-closed, env-var key, and the
plaintext redis:// warning.
"""
import asyncio
import base64
import os
import secrets
import sqlite3
import stat
import threading
import time

import pytest

from pynext.sessions import (
    RedisSessionStore,
    SessionPayloadCipher,
    SQLiteSessionStore,
    _key,
)

KEY_OLD = secrets.token_bytes(32)
KEY_NEW = secrets.token_bytes(32)
KEY_B64 = base64.b64encode(KEY_NEW).decode("ascii")
TOKEN = secrets.token_urlsafe(32)
TOKEN2 = secrets.token_urlsafe(32)


def _run(coro):
    return asyncio.run(coro)


def _raw_sqlite_payload(path):
    with sqlite3.connect(str(path)) as db:
        row = db.execute("SELECT payload FROM pynext_sessions").fetchone()
    return row[0]


# ---------------------------------------------------------------- SQLite


def test_sqlite_encrypted_roundtrip_stores_no_plaintext(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(path, encryption_key=KEY_NEW)
    _run(store.save(TOKEN, {"user_id": "alice", "permissions": ["read"]},
                    "csrf-token", None, time.time() + 3600))
    raw = _raw_sqlite_payload(path)
    assert raw.startswith("v1:")
    assert "alice" not in raw and "user_id" not in raw and "csrf-token" not in raw
    data, csrf, version = _run(store.load(TOKEN))
    assert data == {"user_id": "alice", "permissions": ["read"]}
    assert csrf == "csrf-token" and version == 1


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX file mode bits")
def test_sqlite_file_permissions_0600(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    SQLiteSessionStore(path, encryption_key=KEY_NEW)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX file mode bits")
def test_sqlite_tightens_preexisting_wide_file(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    SQLiteSessionStore(path)
    os.chmod(path, 0o644)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    SQLiteSessionStore(path, encryption_key=KEY_NEW)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_sqlite_plaintext_legacy_row_loads_and_migrates(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    plain = SQLiteSessionStore(path)
    _run(plain.save(TOKEN, {"user_id": "alice"}, "csrf", None, time.time() + 3600))
    assert not _raw_sqlite_payload(path).startswith("v1:")
    keyed = SQLiteSessionStore(path, encryption_key=KEY_NEW)
    data, csrf, version = _run(keyed.load(TOKEN))
    assert data == {"user_id": "alice"} and csrf == "csrf"
    _run(keyed.save(TOKEN, data, csrf, version, time.time() + 3600))
    raw = _raw_sqlite_payload(path)
    assert raw.startswith("v1:") and "alice" not in raw
    assert _run(keyed.load(TOKEN))[0] == {"user_id": "alice"}


def test_sqlite_wrong_key_is_invalid_session_data(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    _run(SQLiteSessionStore(path, encryption_key=KEY_OLD).save(
        TOKEN, {"user_id": "alice"}, None, None, time.time() + 3600))
    with pytest.raises(ValueError, match="invalid session data"):
        _run(SQLiteSessionStore(path, encryption_key=KEY_NEW).load(TOKEN))


def test_sqlite_key_rotation_reseals_on_next_save(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    _run(SQLiteSessionStore(path, encryption_key=KEY_OLD).save(
        TOKEN, {"user_id": "alice"}, None, None, time.time() + 3600))
    rotating = SQLiteSessionStore(path, encryption_keys=[KEY_NEW, KEY_OLD])
    data, csrf, version = _run(rotating.load(TOKEN))
    assert data == {"user_id": "alice"}
    _run(rotating.save(TOKEN, data, csrf, version, time.time() + 3600))
    # Old key alone can no longer read the re-sealed row; new key can.
    with pytest.raises(ValueError, match="invalid session data"):
        _run(SQLiteSessionStore(path, encryption_key=KEY_OLD).load(TOKEN))
    assert _run(SQLiteSessionStore(path, encryption_key=KEY_NEW).load(TOKEN))[0] == {
        "user_id": "alice"}


def test_sqlite_env_var_key_and_key_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNEXT_SESSION_KEY", KEY_B64)
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(path)
    _run(store.save(TOKEN, {"user_id": "bob"}, None, None, time.time() + 3600))
    assert _raw_sqlite_payload(path).startswith("v1:")
    assert _run(SQLiteSessionStore(path, encryption_key=KEY_NEW).load(TOKEN))[0] == {
        "user_id": "bob"}
    # Hex form is accepted too.
    SQLiteSessionStore(tmp_path / "hex.sqlite3", encryption_key=KEY_NEW.hex())
    with pytest.raises(ValueError):
        SQLiteSessionStore(tmp_path / "bad.sqlite3", encryption_key=b"short")
    with pytest.raises(ValueError):
        SQLiteSessionStore(tmp_path / "bad2.sqlite3", encryption_key="not-a-key")
    with pytest.raises(ValueError):
        SQLiteSessionStore(tmp_path / "bad3.sqlite3", encryption_keys=[])


def test_sqlite_plaintext_store_warns_once(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="pynext"):
        SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    assert "stored in plaintext" in caplog.text


def test_sqlite_rotate_path_stays_encrypted(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(path, encryption_key=KEY_NEW)
    _run(store.save(TOKEN, {"user_id": "alice"}, "csrf", None, time.time() + 3600))
    _run(store.save(TOKEN2, {"user_id": "alice"}, "csrf2", 1, time.time() + 3600,
                    previous_token=TOKEN))
    assert _run(store.load(TOKEN)) is None
    assert _run(store.load(TOKEN2))[0] == {"user_id": "alice"}
    assert _raw_sqlite_payload(path).startswith("v1:")


# ---------------------------------------------------------------- cipher


def test_cipher_nonce_unique_tamper_and_transplant_rejected():
    cipher = SessionPayloadCipher([KEY_NEW])
    sealed1 = cipher.seal(b'{"user_id": "alice"}', TOKEN)
    sealed2 = cipher.seal(b'{"user_id": "alice"}', TOKEN)
    assert sealed1 != sealed2  # fresh random nonce per seal
    assert cipher.unseal(sealed1, TOKEN) == b'{"user_id": "alice"}'
    tampered = sealed1[:-2] + ("AA" if sealed1[-2:] != "AA" else "BB")
    with pytest.raises(ValueError, match="invalid session data"):
        cipher.unseal(tampered, TOKEN)
    with pytest.raises(ValueError, match="invalid session data"):
        cipher.unseal(sealed1, TOKEN2)  # AAD binds the seal to its token


# ---------------------------------------------------------------- Redis


class _FakeRedis:
    """Minimal in-memory Redis for the store's get/set/delete/pipeline use."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            row = self._data.get(key)
            return None if row is None else row[0]

    def set(self, key, value, nx=False, xx=False, px=None, ex=None):
        with self._lock:
            if isinstance(value, str):
                value = value.encode("utf-8")
            if nx and key in self._data:
                return None
            if xx and key not in self._data:
                return None
            self._data[key] = [value, self._data.get(key, [None, 0])[1] + 1]
            return True

    def delete(self, *keys):
        with self._lock:
            return sum(1 for k in keys if self._data.pop(k, None) is not None)

    def pipeline(self):
        return _FakePipeline(self)

    def close(self):
        pass


class _FakePipeline:
    def __init__(self, client):
        self._client = client
        self._watched = None
        self._queued = []
        self._in_multi = False

    def watch(self, *keys):
        with self._client._lock:
            self._watched = {k: (None if k not in self._client._data
                                 else self._client._data[k][1]) for k in keys}
        return True

    def get(self, key):
        return self._client.get(key)

    def multi(self):
        self._in_multi = True
        self._queued = []

    def set(self, key, value, px=None, ex=None, nx=False, xx=False):
        if self._in_multi:
            self._queued.append(("set", key, value, px, ex))
            return self
        return self._client.set(key, value, nx=nx, xx=xx, px=px, ex=ex)

    def delete(self, *keys):
        if self._in_multi:
            self._queued.append(("delete", keys))
            return self
        return self._client.delete(*keys)

    def execute(self):
        with self._client._lock:
            if self._watched is not None:
                for key, version in self._watched.items():
                    current = None if key not in self._client._data else self._client._data[key][1]
                    if current != version:
                        raise RuntimeError("watched key changed")
            count = len(self._queued)
            for op in self._queued:
                if op[0] == "set":
                    self._client._data[op[1]] = [
                        op[2].encode("utf-8") if isinstance(op[2], str) else op[2],
                        self._client._data.get(op[1], [None, 0])[1] + 1]
                else:
                    for k in op[1]:
                        self._client._data.pop(k, None)
            self._queued = []
            self._in_multi = False
            self._watched = None
            return [True] * count

    def reset(self):
        self._queued = []
        self._in_multi = False
        self._watched = None


def _raw_redis_value(client, token):
    return client.get(f"pynext:session:{_key(token)}").decode("utf-8")


def test_redis_encrypted_roundtrip_update_and_rotate():
    client = _FakeRedis()
    store = RedisSessionStore(client=client, encryption_key=KEY_NEW)
    _run(store.save(TOKEN, {"user_id": "alice", "permissions": ["read"]},
                    "csrf", None, time.time() + 3600))
    raw = _raw_redis_value(client, TOKEN)
    assert raw.startswith("v1:")
    assert "alice" not in raw and "user_id" not in raw
    data, csrf, version = _run(store.load(TOKEN))
    assert data == {"user_id": "alice", "permissions": ["read"]} and version == 1
    # Update path (WATCH/MULTI) re-seals under the same key.
    _run(store.save(TOKEN, {"user_id": "alice", "permissions": ["read", "write"]},
                    "csrf", version, time.time() + 3600))
    assert _run(store.load(TOKEN))[0]["permissions"] == ["read", "write"]
    # Rotate path moves the sealed payload to the new token.
    _run(store.save(TOKEN2, {"user_id": "alice"}, "csrf2", 2, time.time() + 3600,
                    previous_token=TOKEN))
    assert _run(store.load(TOKEN)) is None
    assert _run(store.load(TOKEN2))[0] == {"user_id": "alice"}
    assert _raw_redis_value(client, TOKEN2).startswith("v1:")


def test_redis_wrong_key_and_legacy_plaintext():
    client = _FakeRedis()
    _run(RedisSessionStore(client=client, encryption_key=KEY_OLD).save(
        TOKEN, {"user_id": "alice"}, None, None, time.time() + 3600))
    with pytest.raises(ValueError, match="invalid session data"):
        _run(RedisSessionStore(client=client, encryption_key=KEY_NEW).load(TOKEN))
    # A plaintext row written before encryption still loads, then migrates.
    plain_client = _FakeRedis()
    _run(RedisSessionStore(client=plain_client).save(
        TOKEN, {"user_id": "carol"}, None, None, time.time() + 3600))
    keyed = RedisSessionStore(client=plain_client, encryption_key=KEY_NEW)
    assert _run(keyed.load(TOKEN))[0] == {"user_id": "carol"}


def test_redis_plaintext_url_warns(caplog):
    with caplog.at_level("WARNING", logger="pynext"):
        store = RedisSessionStore("redis://127.0.0.1:6379/0")
    assert "plaintext redis://" in caplog.text
    store.close()
