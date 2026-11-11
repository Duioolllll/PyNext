"""Opt-in authorization markers for application-managed identities.

Authorization is declared at registration time and enforced at exactly
one place per entry point (S-P1-33, FastAPI lesson 2): the dispatcher
(:meth:`pynext.app.App.dispatch`) and the WebSocket pre-flight
(:meth:`pynext.app.App._authorize_websocket`) both decide from the
policy snapshotted when the route was registered
(:func:`authorization_policy`), never from the handler's live
attributes — so a decorator lost to wrapping, or a handler replaced
after registration, cannot silently drop the check. The default is
deny: a route that declares permissions or scopes without a usable
identity is refused, and a 403 never says which permission or scope
was missing.

Login rate limiting (S-P1-6) lives here too: :class:`LoginRateLimiter`
is the framework primitive an application's login handler calls around
its credential check, so a credential-stuffing run cannot hammer one
account (or one client) without bound.
"""

from dataclasses import dataclass


def _validate_token_name(value, label):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{label} must be a nonempty string of at most 128 characters")
    return value


@dataclass(frozen=True)
class AuthorizationPolicy:
    """The authorization policy for one route handler, snapshotted at registration.

    ``requires_auth`` is True when the handler was marked with
    :func:`require_auth`, :func:`require_permission` or
    :func:`require_scope`. ``permissions`` and ``scopes`` are the
    required names, in declaration order; an unmarked handler has an
    empty policy and is public.
    """

    requires_auth: bool = False
    permissions: tuple = ()
    scopes: tuple = ()


def authorization_policy(fn):
    """Snapshot ``fn``'s authorization markers into an :class:`AuthorizationPolicy`.

    Called once when the route is registered. A handler that declares
    permissions or scopes without the auth flag is a configuration
    error and fails loudly here, at startup — it must never silently
    become public because a marker was set without its flag. Names are
    validated (nonempty strings of at most 128 characters) and must be
    given as a tuple/list, never a bare string (whose characters would
    otherwise be read as separate names).
    """
    permissions = getattr(fn, "__pynext_permissions__", ())
    scopes = getattr(fn, "__pynext_scopes__", ())
    requires_auth = bool(getattr(fn, "__pynext_auth__", False))
    if (permissions or scopes) and not requires_auth:
        raise ValueError(
            f"{getattr(fn, '__qualname__', fn)!r} declares permissions/scopes "
            "without @require_auth/@require_permission/@require_scope; "
            "refusing to register it as public")
    for label, names in (("permission", permissions), ("scope", scopes)):
        if isinstance(names, str) or not isinstance(names, (tuple, list)):
            raise ValueError(f"{label} names on {getattr(fn, '__qualname__', fn)!r} "
                             "must be a tuple/list of strings")
        for name in names:
            _validate_token_name(name, label)
    return AuthorizationPolicy(requires_auth=requires_auth,
                               permissions=tuple(permissions), scopes=tuple(scopes))

import math
import threading
import time
from collections import OrderedDict

#: Default consecutive failures before a (client IP, account) pair is
#: blocked: 5, per ROADMAP S-P1-6.
LOGIN_MAX_FAILURES = 5
#: Default block duration after the limit is hit: 15 minutes, in seconds.
LOGIN_BLOCK_SECONDS = 900
#: Default bound on tracked (IP, account) pairs, mirroring the
#: rate-limit bucket table in :mod:`pynext.ratelimit`.
LOGIN_MAX_KEYS = 10_000


def _login_key(client_ip, account):
    """Normalize the (IP, account) pair into one limiter key.

    The account is stripped and case-folded so ``User@example`` and
    ``user@example`` share one counter — otherwise an attacker could
    dodge the limit by varying the case. The IP is taken as given: the
    caller passes the already-resolved client IP (the direct peer, or
    the address resolved through trusted proxies — see
    :meth:`pynext.ratelimit.RateLimiter.key_for`); this module does no
    forwarded-header interpretation of its own, so a spoofed
    ``X-Forwarded-For`` can never mint fresh buckets here. An unknown
    peer shares the ``"unknown"`` IP.
    """
    ip = "unknown" if client_ip is None else str(client_ip).strip() or "unknown"
    return (ip, str(account).strip().casefold())


class LoginRateLimiter:
    """Consecutive-failure login limiter: 5 failures, then a 15-minute block.

    One instance is held by the application (typically module level, next
    to the login handler) and shared by every worker thread in the
    process; the state is process-local, like :class:`pynext.ratelimit.RateLimiter`.
    A multi-worker deployment that needs one shared counter across
    processes should front this with a shared store (the same Redis
    ``INCR`` + ``EXPIRE`` shape the Litestar lesson calls for); the
    per-process limiter still bounds each worker on its own.

    The key is the (client IP, account) **pair** (ROADMAP S-P1-6):

    - The same account failing from another IP gets its own 5 attempts,
      so one attacker's failures cannot lock the legitimate owner out
      from their own IP (a pure per-account lockout would let anyone
      deny service to any account with 5 bad passwords).
    - The same IP failing against another account gets its own 5
      attempts, so a legitimate user on a shared NAT is not blocked by
      a stranger's failures.

    Residual risk, stated plainly: an attacker rotating both IPs *and*
    accounts still gets 5 tries per pair, so this limiter is one layer,
    not the whole defence — pair it with the general per-IP
    :class:`~pynext.ratelimit.RateLimiter` (S-P0-8) and with
    password-reset throttling. A successful login resets the pair's
    counter, so a user who mistypes 4 times and then remembers the
    password is never blocked. Failures also stop counting after
    ``block_seconds`` of inactivity, so four typos in January plus one
    in March do not add up to a block — the window that matters is the
    recent run of failures, not the pair's lifetime total.

    A failure recorded while the pair is already blocked does **not**
    extend the block: the block always ends ``block_seconds`` after the
    failure that tripped it, so a continuous probe stream cannot hold
    an account blocked forever.

    Usage — inside the application's login handler, around the
    credential check (which stays the application's responsibility;
    this class never sees a password)::

        limiter = LoginRateLimiter()  # one instance per process

        @route("POST", "/login")
        async def login(request):
            form = await request.form()
            account = form.get("username", "")
            client_ip = request.client.host  # or RateLimiter.key_for(...)
            if (blocked := limiter.check_login(client_ip, account)) is not None:
                return blocked  # 429 + Retry-After
            if not verify_password(account, form.get("password", "")):
                limiter.record_failure(client_ip, account)
                return Response("Invalid credentials", status=401)
            limiter.record_success(client_ip, account)
            session.rotate_on_login()
            ...

    Blocking (rather than a CAPTCHA challenge after 5 failures) is the
    flow implemented here: a CAPTCHA needs a provider and a challenge
    round-trip the framework does not own. An application that prefers
    CAPTCHA can use :meth:`is_blocked` as the trigger — when it returns
    true, serve the challenge instead of the 429 — and call
    :meth:`record_success` once the challenge is passed.

    All state changes happen under a single lock, so the
    check-then-record sequence is atomic (Litestar lesson 4: a
    process-local limiter must not split get and set across a lock
    boundary, or two concurrent failures can both slip under the
    limit). Times are monotonic; pass ``now`` (a monotonic timestamp)
    to any method only in tests, for deterministic expiry.
    """

    def __init__(self, max_failures=LOGIN_MAX_FAILURES, block_seconds=LOGIN_BLOCK_SECONDS, *,
                 max_keys=LOGIN_MAX_KEYS):
        if isinstance(max_failures, bool) or not isinstance(max_failures, int) or max_failures < 1:
            raise ValueError("max_failures must be an integer >= 1")
        if isinstance(block_seconds, bool) or not isinstance(block_seconds, (int, float)) \
                or not math.isfinite(block_seconds) or block_seconds <= 0:
            raise ValueError("block_seconds must be a number > 0 seconds")
        if max_keys is not None:
            if isinstance(max_keys, bool) or not isinstance(max_keys, int) or max_keys < 1:
                raise ValueError("max_keys must be an integer >= 1")
        self.max_failures = max_failures
        self.block_seconds = float(block_seconds)
        self.max_keys = max_keys if max_keys is not None else LOGIN_MAX_KEYS
        self._lock = threading.Lock()
        # key -> [consecutive failures, blocked_until (monotonic) or None, last_seen]
        # Most-recently-used last, so eviction takes the stalest entry.
        self._records = OrderedDict()

    @staticmethod
    def _now(now):
        return time.monotonic() if now is None else float(now)

    def _evict_one(self, now):
        """Evict one record to make room for a new key.

        Prefer a record that is not currently blocked (its owner only
        loses a failure count, never a block); if every record is
        blocked, evict the stalest blocked one. Eviction can therefore
        unblock a pair early under extreme table pressure, but only
        after all 10,000 tracked pairs are simultaneously blocked —
        the same bounded-table tradeoff as
        :class:`pynext.ratelimit.RateLimiter`.
        """
        for key, (_, blocked_until, _) in self._records.items():
            if blocked_until is None or blocked_until <= now:
                del self._records[key]
                return
        self._records.popitem(last=False)

    def retry_after(self, client_ip, account, *, now=None):
        """Seconds until this (IP, account) pair may try again, or ``None``.

        Returns ``None`` when the pair is not blocked. An expired block
        is dropped, so the next failure starts a fresh count.
        """
        now = self._now(now)
        key = _login_key(client_ip, account)
        with self._lock:
            record = self._records.get(key)
            if record is None or record[1] is None:
                return None
            remaining = record[1] - now
            if remaining <= 0:
                del self._records[key]
                return None
            return max(1, math.ceil(remaining))

    def is_blocked(self, client_ip, account, *, now=None):
        """Whether this (IP, account) pair is currently blocked."""
        return self.retry_after(client_ip, account, now=now) is not None

    def check_login(self, client_ip, account, *, now=None):
        """Return a 429 response when the pair is blocked, else ``None``.

        Mirrors :func:`check_authorization`'s response-or-``None``
        contract so a login handler can early-return it. The response
        carries ``Retry-After`` in whole seconds and is marked
        ``private, no-store`` so it is never cached.
        """
        retry = self.retry_after(client_ip, account, now=now)
        if retry is None:
            return None
        from .http import Response
        return Response("Too Many Login Attempts", status=429,
                        headers=((b"retry-after", str(int(retry)).encode("ascii")),
                                 (b"cache-control", b"private, no-store")))

    def record_failure(self, client_ip, account, *, now=None):
        """Record one failed login for this (IP, account) pair.

        Returns the ``Retry-After`` seconds when this failure trips the
        block (the ``max_failures``-th consecutive failure), or ``None``
        when the pair may still try. A failure while already blocked
        returns the remaining block time without extending it.
        """
        now = self._now(now)
        key = _login_key(client_ip, account)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                if len(self._records) >= self.max_keys:
                    self._evict_one(now)
                record = [0, None, now]
                self._records[key] = record
            elif record[1] is not None:
                if now >= record[1]:
                    # Block expired: fresh count from this failure.
                    record[0] = 0
                    record[1] = None
                else:
                    record[2] = now
                    self._records.move_to_end(key)
                    return max(1, math.ceil(record[1] - now))
            elif now - record[2] >= self.block_seconds:
                # Idle longer than the block window: stale failures do
                # not count towards the next block.
                record[0] = 0
            record[0] += 1
            record[2] = now
            self._records.move_to_end(key)
            if record[0] >= self.max_failures:
                record[1] = now + self.block_seconds
                return int(math.ceil(self.block_seconds))
            return None

    def record_success(self, client_ip, account):
        """Clear the consecutive-failure record after a successful login.

        A success resets the count even if the pair had failures, so a
        user who mistypes and then remembers the password is never
        blocked. (A success cannot normally arrive while blocked — the
        handler returns the 429 before checking credentials — but if
        one does, clearing is the safe direction.)
        """
        key = _login_key(client_ip, account)
        with self._lock:
            self._records.pop(key, None)

    def stats(self):
        """Live counters: tracked pairs, the cap, and pairs now blocked."""
        with self._lock:
            now = time.monotonic()
            blocked = sum(1 for _, blocked_until, _ in self._records.values()
                          if blocked_until is not None and blocked_until > now)
            return {"keys": len(self._records), "max_keys": self.max_keys,
                    "blocked": blocked}

    def reset(self):
        """Drop all records. Tests only; production records expire on their own."""
        with self._lock:
            self._records.clear()


def require_auth(fn):
    """Require a nonempty string ``user_id`` in the server-side session.

    Applies to HTTP handlers and WebSocket handlers alike: a WebSocket
    upgrade is rejected before the handshake when the session has no
    ``user_id``.
    """
    fn.__pynext_auth__ = True
    return fn


def require_permission(permission):
    """Require a permission from the server-side session's permissions list."""
    _validate_token_name(permission, "permission")

    def decorate(fn):
        fn.__pynext_auth__ = True
        fn.__pynext_permissions__ = (*getattr(fn, "__pynext_permissions__", ()), permission)
        return fn
    return decorate


def require_scope(*scopes):
    """Require one or more scopes from the server-side session's scopes list.

    The scopes are declared on the handler at registration time and
    enforced by the dispatcher (and the WebSocket pre-flight) against
    ``session["scopes"]`` — the handler itself never checks them, so a
    forgotten check cannot turn a bare token into full access (S-P1-33).
    Every listed scope must be present; a session without a ``scopes``
    list, or with a malformed one, is denied. Declaring a scope also
    implies :func:`require_auth`.
    """
    if not scopes:
        raise ValueError("require_scope needs at least one scope")
    for scope in scopes:
        _validate_token_name(scope, "scope")

    def decorate(fn):
        fn.__pynext_auth__ = True
        fn.__pynext_scopes__ = (*getattr(fn, "__pynext_scopes__", ()), *scopes)
        return fn
    return decorate


def check_authorization(request, fn, *, policy=None):
    """Return 401/403, or None when the handler's policy is satisfied.

    Session fixation (H-6) is not decided here: this runs before the
    handler, so it cannot see the login transition — there is no
    rotation to check yet at this point. The guarantee lives in
    :class:`pynext.sessions.SessionMiddleware`, which rotates the
    session id whenever a request authenticates a previously anonymous
    (or different-user) session, even when the login handler forgot
    ``rotate_on_login()`` — so by the time any later request reaches
    this check with a ``user_id``, its id is one issued after
    authentication, never one fixed before login.

    The decision is made from ``policy`` — the snapshot taken at
    registration (see :func:`authorization_policy`) — and defaults to
    deriving that snapshot from ``fn`` when no policy is given, so
    callers that predate the snapshot keep working. The default is
    deny: an unmarked handler is public, but any marked handler without
    a usable identity (no session, no ``user_id``), with a malformed
    permissions/scopes list, or missing even one required permission or
    scope is refused. The 403 body is a bare ``"Forbidden"``: it never
    says which permission or scope was missing.
    """
    if policy is None:
        policy = authorization_policy(fn)
    if not policy.requires_auth:
        return None
    from .http import Response
    from .sessions import Session

    session = request.state.get("session")
    if not isinstance(session, Session) or not isinstance(session.get("user_id"), str) or not session["user_id"]:
        return Response("Unauthorized", status=401, headers=[(b"cache-control", b"private, no-store")])
    permissions = session.get("permissions", [])
    if (not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions) or
            not all(item in permissions for item in policy.permissions)):
        return Response("Forbidden", status=403, headers=[(b"cache-control", b"private, no-store")])
    scopes = session.get("scopes", [])
    if (not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes) or
            not all(item in scopes for item in policy.scopes)):
        return Response("Forbidden", status=403, headers=[(b"cache-control", b"private, no-store")])
    return None
