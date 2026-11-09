"""Failure policies for shared backends (roadmap item B4).

A shared backend (the SQLite/Redis ISR cache, the Redis rate limiter,
the Redis session store) can become unavailable while the process
itself is healthy. Each component exposes one policy that decides what
a request does then:

* ``fail_open`` — degrade: serve the request from process-local state
  (the ISR cache falls back to its local L1 and renders locally; the
  rate limiter admits the request). Availability wins; cross-worker
  consistency (cache) or abuse protection (rate limiting) is weakened
  for the duration of the outage.
* ``fail_closed`` — refuse: fail the request with HTTP 503 instead of
  serving it from degraded state. Consistency / protection wins;
  availability is reduced for the duration of the outage.

Defaults are chosen per component for the safer side of its own risk:

* ISR cache: ``fail_open``. A degraded cache can only serve a page that
  was rendered fresh for this request or a still-valid local entry;
  private responses are never cached and requests carrying a Cookie or
  Authorization header bypass the cache entirely, so failing open
  cannot leak another user's data — it can only serve a possibly stale
  public page. This is also the historical behaviour.
* Rate limiter: ``fail_closed``. Failing open would admit unlimited
  traffic exactly when the shared limiter is down, which is when abuse
  protection is most needed. This is also the historical behaviour.
* Redis sessions: ``fail_closed``. Failing open would silently drop
  session state, so the store raises ``SessionUnavailable`` and
  the middleware answers 503 instead. Under ``fail_open`` the request
  proceeds with a fresh ephemeral session and nothing is persisted:
  reads keep working, but a login cannot complete during the outage
  (the CSRF token issued for it is never stored, so the login
  request fails CSRF with 403).

Best-effort cache writes stay best-effort in both modes: a shared-cache
insert that fails after
a page was rendered correctly does not fail the request (the page is
fresh and safe to serve once; only its caching is lost), and a lease
release failure does not fail the request either (leases expire on
their own). An explicit invalidation that cannot reach the shared
store is different: under ``fail_closed`` it raises instead of
reporting success, so callers never mistake an unapplied invalidation
for a completed one. Redis sessions apply the policy to load, save and
delete: ``fail_closed`` propagates storage failures; a ``fail_open`` save
reports success without persisting anything (the session is ephemeral),
and a rotation during the outage may leave the old session id usable
until it expires. A ``fail_open`` delete is also best-effort: logout
can clear the browser cookie while the server-side session remains
usable with the old token until its TTL expires or it is explicitly
deleted after recovery. Do not treat such a logout as confirmed
server-side revocation. A ``fail_open`` session outage also breaks
session-bound CSRF: the token issued for a login is never stored, so
the login request fails CSRF (403) even though reads keep working.
"""

FAIL_OPEN = "fail_open"
FAIL_CLOSED = "fail_closed"

_POLICIES = (FAIL_OPEN, FAIL_CLOSED)


def normalize_failure_policy(value, *, default):
    """Validate a failure policy, returning ``default`` for ``None``.

    Raises ``ValueError`` for anything other than ``"fail_open"``,
    ``"fail_closed"`` or ``None`` so a typo fails at startup instead of
    silently picking a behaviour.
    """
    if value is None:
        value = default
    if value not in _POLICIES:
        raise ValueError(
            f"failure policy must be 'fail_open' or 'fail_closed', got {value!r}")
    return value
