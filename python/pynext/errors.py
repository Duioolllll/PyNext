"""Signal errors and error digests (Next.js error taxonomy, adapted).

Next.js treats a page raising ``notFound()`` / ``forbidden()`` / ``redirect()``
as a *signal*, not a failure: ``server/app-render/create-error-handler.tsx``
returns the digest for well-known errors (``NEXT_REDIRECT``,
``NEXT_HTTP_ERROR_FALLBACK;404|403|401``) and does not log them, while a real
error gets a ``stringHash(message + stack)`` digest so the boundary can
restore and dedup the original error. PyNext follows the same split:

* :class:`SignalError` subclasses (``NotFoundError``, ``ForbiddenError``,
  ``UnauthorizedError``, :class:`RedirectError`) are control flow. The
  dispatcher converts them to their response and never logs them as errors.
* Any other exception gets a stable digest from its type, message and
  traceback (:func:`compute_digest`). :func:`digest_for_error` registers the
  first sighting in a bounded in-process registry, so a digest printed in a
  500 response can be resolved back to the original error record with
  :func:`lookup_error` — the same error raised again reuses its digest and
  bumps the count instead of creating a new entry.

The registry is in-process only: it is never logged, never exposed over
HTTP, and holds at most 512 digests (oldest evicted first). Messages are
truncated to 200 characters because an exception message can carry user
input; never pass a digest entry to a logger.
"""

import hashlib
import threading
import time
import traceback

from .http import Response

__all__ = [
    "SignalError",
    "NotFoundError",
    "ForbiddenError",
    "UnauthorizedError",
    "RedirectError",
    "not_found",
    "forbidden",
    "unauthorized",
    "redirect",
    "is_signal_error",
    "get_digest_for_well_known_error",
    "compute_digest",
    "digest_for_error",
    "error_count",
    "lookup_error",
]

#: Well-known digest codes, mirroring Next.js ``NEXT_REDIRECT`` and
#: ``NEXT_HTTP_ERROR_FALLBACK``. Only these fixed strings are ever used as
#: Prometheus label values; computed digests stay in the snapshot.
REDIRECT_ERROR_CODE = "PYNEXT_REDIRECT"
HTTP_ERROR_FALLBACK_ERROR_CODE = "PYNEXT_HTTP_ERROR_FALLBACK"

#: Redirect statuses a page may signal. Anything else is a programming
#: error, not a redirect.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Registry bound: distinct digests remembered per process. Past this,
#: the oldest entry is evicted.
_MAX_REGISTRY_ENTRIES = 512

#: Registry entries keep a truncated message only; see the module docstring.
_MAX_MESSAGE_CHARS = 200


class SignalError(Exception):
    """Base for control-flow signals raised inside a page or middleware.

    A signal is not a failure: the dispatcher answers with
    :meth:`to_response` and records a metric, but writes no error log.
    Subclasses fix :attr:`status` and :attr:`digest`; the digest is a
    constant string so it is safe as a metric label.
    """

    status = 500
    digest = ""

    @property
    def label_digest(self):
        """Digest value safe as a Prometheus label (a fixed string)."""
        return self.digest

    def to_response(self):
        """Convert the signal into the response the client sees."""
        raise NotImplementedError


class NotFoundError(SignalError):
    """Signal a 404 from inside a page (``notFound()`` in Next.js)."""

    status = 404
    digest = f"{HTTP_ERROR_FALLBACK_ERROR_CODE};404"

    def __init__(self, message="Not Found"):
        super().__init__(message)

    def to_response(self):
        return Response("Not Found", status=404)


class ForbiddenError(SignalError):
    """Signal a 403 from inside a page (``forbidden()`` in Next.js)."""

    status = 403
    digest = f"{HTTP_ERROR_FALLBACK_ERROR_CODE};403"

    def __init__(self, message="Forbidden"):
        super().__init__(message)

    def to_response(self):
        return Response("Forbidden", status=403)


class UnauthorizedError(SignalError):
    """Signal a 401 from inside a page (``unauthorized()`` in Next.js)."""

    status = 401
    digest = f"{HTTP_ERROR_FALLBACK_ERROR_CODE};401"

    def __init__(self, message="Unauthorized"):
        super().__init__(message)

    def to_response(self):
        return Response("Unauthorized", status=401)


class RedirectError(SignalError):
    """Signal a redirect from inside a page (``redirect()`` in Next.js).

    The digest mirrors Next.js (``NEXT_REDIRECT;<push|replace>;<url>;
    <status>;``) so the destination and status survive the boundary, but
    :attr:`label_digest` is the bare code: the URL must never become a
    metric label.
    """

    def __init__(self, location, status=307, replace=False):
        if not isinstance(location, str) or not location:
            raise ValueError("redirect location must be a non-empty string")
        if any(c in location for c in "\r\n"):
            raise ValueError("redirect location must not contain CR or LF")
        try:
            location.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("redirect location must be ASCII (percent-encode it)") from exc
        if status not in REDIRECT_STATUSES:
            raise ValueError(f"redirect status must be one of {sorted(REDIRECT_STATUSES)}")
        super().__init__(location)
        self.location = location
        self.status = status
        self.replace = bool(replace)
        kind = "replace" if self.replace else "push"
        self.digest = f"{REDIRECT_ERROR_CODE};{kind};{location};{status};"

    @property
    def label_digest(self):
        return REDIRECT_ERROR_CODE

    def to_response(self):
        return Response(b"", status=self.status,
                        headers=[(b"location", self.location.encode("ascii"))])


def not_found():
    """Raise :class:`NotFoundError`. Never returns, like Next.js ``notFound()``."""
    raise NotFoundError()


def forbidden():
    """Raise :class:`ForbiddenError`. Never returns, like Next.js ``forbidden()``."""
    raise ForbiddenError()


def unauthorized():
    """Raise :class:`UnauthorizedError`. Never returns, like Next.js ``unauthorized()``."""
    raise UnauthorizedError()


def redirect(location, status=307, replace=False):
    """Raise :class:`RedirectError`. Never returns, like Next.js ``redirect()``."""
    raise RedirectError(location, status=status, replace=replace)


def is_signal_error(exc):
    """True when ``exc`` is a navigation signal, not a failure.

    Single entry point, mirroring Next.js ``isNextRouterError()``.
    """
    return isinstance(exc, SignalError)


def get_digest_for_well_known_error(exc):
    """Return the digest for a well-known signal error, else ``None``.

    A non-``None`` result means "this error needs no error log", mirroring
    Next.js ``getDigestForWellKnownError()``.
    """
    if isinstance(exc, SignalError):
        return exc.digest
    return None


def compute_digest(exc):
    """Stable digest for a real error: ``sha256(type + message + stack)``.

    Next.js uses ``stringHash(message + stack)``; the idea is the same —
    the same error raised from the same place hashes identically, so
    repeats dedup to one digest. Tracebacks carry file paths, line numbers
    and source lines but no addresses, so the hash is stable per process
    and across processes running the same code.

    An exception whose ``__str__`` raises (A1-M5) must not kill the 500
    handler that is trying to report it: the whole computation is
    guarded, and on failure the digest falls back to the exception's
    type identity alone, so dispatch still produces a normal 500
    response with a digest and request_id.
    """
    try:
        text = (
            f"{type(exc).__name__}:{exc}\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        fallback = f"unhashable:{type(exc).__module__}.{type(exc).__qualname__}"
        return hashlib.sha256(fallback.encode("utf-8", "replace")).hexdigest()[:16]


class _ErrorRegistry:
    """Bounded digest -> original-error record, for boundary restore/dedup."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}  # digest -> {"type", "message", "count", "first_seen", "last_seen"}

    def record(self, exc, digest):
        """Store the first sighting of ``digest``; bump the count on repeats."""
        now = time.time()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                if len(self._entries) >= _MAX_REGISTRY_ENTRIES:
                    self._entries.pop(next(iter(self._entries)))
                try:
                    message = str(exc)[:_MAX_MESSAGE_CHARS]
                except Exception:
                    # A1-M5: an exception whose __str__ raises must not
                    # kill the 500 handler recording it.
                    message = f"<unprintable {type(exc).__name__}>"
                entry = {
                    "type": type(exc).__name__,
                    "message": message,
                    "count": 0,
                    "first_seen": now,
                    "last_seen": now,
                }
                self._entries[digest] = entry
            entry["count"] += 1
            entry["last_seen"] = now
            return entry["count"]

    def lookup(self, digest):
        """Return a copy of the record for ``digest``, or ``None``."""
        with self._lock:
            entry = self._entries.get(digest)
            return dict(entry) if entry is not None else None


_REGISTRY = _ErrorRegistry()


def digest_for_error(exc):
    """Return ``(digest, count)`` for ``exc``, registering it on first sight.

    The digest is cached on the exception instance, so an error re-raised
    through several layers (dispatch -> transport boundary) is registered
    exactly once and every layer sees the same digest and count. This is
    the boundary restore: the digest printed in a 500 response resolves
    back to the record :func:`lookup_error` returns.
    """
    digest = getattr(exc, "_pynext_digest", None)
    if digest is not None:
        entry = _REGISTRY.lookup(digest)
        return digest, entry["count"] if entry is not None else 1
    digest = compute_digest(exc)
    try:
        exc._pynext_digest = digest
    except Exception:
        pass
    return digest, _REGISTRY.record(exc, digest)


def error_count(digest):
    """How many times the error behind ``digest`` has been seen (0 if unknown)."""
    entry = _REGISTRY.lookup(digest)
    return entry["count"] if entry is not None else 0


def lookup_error(digest):
    """Restore the original-error record for ``digest``, or ``None``.

    The record carries the exception type, a truncated message, the sighting
    count and first/last-seen timestamps. In-process only.
    """
    return _REGISTRY.lookup(digest)
