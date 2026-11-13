"""Dynamic-access tracking for static generation (SSG/ISR).

Next.js parallel: ``packages/next/src/server/app-render/dynamic-rendering.ts``.

Next.js distinguishes two kinds of dynamic marking:

* a **dynamic intent** — ``unstable_noStore()`` asking for a scope to be
  treated as dynamic, and
* a **dynamic data read** — actually reading a dynamic source such as
  ``cookies()`` or ``headers()`` (stronger: an error inside ``unstable_cache``).

PyNext keeps the same distinction (``kind="intent"`` vs ``kind="data"``) and
the same four-part shape: track the access, record *why* static generation
is impossible, interrupt with a digest-identified error, and format the
recorded accesses for the build log.

What is tracked
----------------
While a prerender scope is active (an SSG build render or an ISR first
generation), these calls are recorded:

* :func:`mark_dynamic_access` / :func:`no_store` — explicit marks. Page
  authors call these for anything the automatic hooks cannot see (an
  external fetch, a hand-rolled clock, ...).
* ``time.time()``, ``random.*``, ``uuid.uuid4()``, ``os.urandom()`` and
  ``secrets.token_*()`` — installed as temporary monkeypatches for the
  duration of the prerender only, then restored.
* ``datetime.datetime.now()`` / ``utcnow()`` / ``today()`` and
  ``datetime.date.today()`` — the ``datetime`` module attributes are swapped
  for subclasses that record and delegate, for the prerender only.
* ``Database.session()`` — any database access through the framework
  resource is flagged.

Why ``request`` reads need no hook: SSG/ISR pages cannot accept a
``request`` parameter at all (``App`` raises ``ValueError`` at startup,
"cannot accept request"), and layouts only receive ``children`` — so a
request object can never reach page code during a prerender. The
signature ban is the enforcement; the hooks above cover what page code
can actually reach.

Two honest limits of the automatic hooks, by design:

* A call is only recorded when its *direct* caller is user code (not the
  standard library and not ``pynext`` itself). Framework code that reads
  the clock internally — logging, CSP nonces, the streaming shell marker —
  therefore never counts as a page being dynamic.
* ``from datetime import datetime`` binds the original class at import time,
  so ``datetime.now()`` through such a binding escapes the module-attribute
  swap. Use :func:`now` (or :func:`mark_dynamic_access`) for those reads;
  the same applies to ``from time import time`` and friends.

What happens on a recorded access depends on the page's ``dynamic`` mode
(``@page(dynamic=...)``):

* ``"auto"`` (default) — the render proceeds, the access is recorded in
  ``App.dynamic_accesses`` and in the build manifest's ``dynamic_accesses``
  report, and a warning is logged the first time each expression is seen.
* ``"error"`` — the first recorded access raises
  :class:`StaticGenBailoutError`, interrupting static generation. The build
  fails for SSG pages; an ISR generation fails instead of caching a baked
  value. The error carries the ``PYNEXT_PRERENDER_BAILOUT`` digest (the
  ``NEXT_PRERENDER_INTERRUPTED`` analogue) so "cannot be static" stays
  distinguishable from a page bug in logs and metrics.
* ``"force-dynamic"`` (SSG only) — the page is never generated at build
  time and is served dynamically in production, like an SSR page.

Tracking state lives in a :class:`DynamicTrackingState` object held by a
context variable, so a synchronous page function running on a worker thread
(``asyncio.to_thread`` copies the context) still records into the same
shared state. The monkeypatches are process-global while installed, but
their wrappers only record when a tracking state is active in the current
context, so concurrent dynamic requests are unaffected.

One honest limit of the context-variable design (A1-M6): a plain
``threading.Thread`` starts with an *empty* context, so dynamic accesses
inside a thread a page spawns itself are silently missed — the clock read
happens, but no access is recorded and the page can be baked static with
a dynamic value inside. Spawn prerender work with
:func:`run_in_prerender_context` (or ``asyncio.to_thread``, or run it
under :func:`prerender_context`) so the tracking state travels with it.
"""

import contextvars
import datetime as _datetime
import inspect
import os
import random as _random
import secrets as _secrets
import sysconfig
import threading
import time as _time
import traceback
import uuid as _uuid
from pathlib import Path

__all__ = [
    "DYNAMIC_DIGEST",
    "DYNAMIC_MODES",
    "MAX_DYNAMIC_ACCESSES",
    "DynamicAccess",
    "DynamicTrackingState",
    "PrerenderTracker",
    "StaticGenBailoutError",
    "format_dynamic_accesses",
    "is_prerender_bailout",
    "mark_dynamic_access",
    "no_store",
    "now",
    "prerender_context",
    "run_in_prerender_context",
    "suppress_dynamic_tracking",
    "track_prerender",
    "utcnow",
]

#: Digest identifying a prerender interruption, mirroring Next.js
#: ``NEXT_PRERENDER_INTERRUPTED``. A page bug is a 500; this is not.
DYNAMIC_DIGEST = "PYNEXT_PRERENDER_BAILOUT"

#: Valid values for ``@page(dynamic=...)``.
DYNAMIC_MODES = ("auto", "error", "force-dynamic")

#: Most accesses kept per prerender; further ones are counted, not stored.
MAX_DYNAMIC_ACCESSES = 32

#: Most user stack frames kept per access.
MAX_DYNAMIC_STACK_FRAMES = 5

_PY_NEXT_DIR = Path(__file__).resolve().parent
_STDLIB_DIR = Path(sysconfig.get_path("stdlib") or "").resolve()
_SITE_DIRS = {
    Path(sysconfig.get_path("purelib") or "").resolve(),
    Path(sysconfig.get_path("platlib") or "").resolve(),
}


def _is_user_file(filename):
    """True when ``filename`` is user code (app or third-party), not the
    standard library and not ``pynext`` itself."""
    if not filename or filename.startswith("<"):
        return False
    resolved = os.path.realpath(filename)
    if resolved.startswith(str(_PY_NEXT_DIR) + os.sep):
        return False
    # site-packages lives under the stdlib tree in a venv, so check it first.
    for site in _SITE_DIRS:
        if str(site) and resolved.startswith(str(site) + os.sep):
            return True
    if str(_STDLIB_DIR) and resolved.startswith(str(_STDLIB_DIR) + os.sep):
        return False
    return True


def _user_stack():
    """User-code frames for the current stack, oldest first.

    Framework frames (``pynext`` internals) and standard-library frames are
    cut, like Next.js ``formatDynamicAPIAccesses``.
    """
    frames = []
    for frame in traceback.extract_stack():
        if _is_user_file(frame.filename):
            frames.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
    return frames[-MAX_DYNAMIC_STACK_FRAMES:]


class DynamicAccess:
    """One recorded dynamic access: what was read, where, and how."""

    __slots__ = ("expression", "kind", "location", "stack")

    def __init__(self, expression, kind, location, stack):
        self.expression = expression
        self.kind = kind
        self.location = location
        self.stack = list(stack)

    def to_dict(self):
        return {"expression": self.expression, "kind": self.kind,
                "location": self.location, "stack": list(self.stack)}

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"DynamicAccess({self.expression!r}, kind={self.kind!r}, at {self.location})"


def format_dynamic_accesses(accesses):
    """Render recorded accesses for a log line or build error.

    Only user-code positions are shown; framework and stdlib frames were
    already cut when the access was recorded.
    """
    lines = []
    for access in accesses:
        lines.append(f"  - {access.expression} ({access.kind}) at {access.location}")
        for frame in access.stack[:-1]:
            lines.append(f"      called from {frame}")
    return "\n".join(lines)


class StaticGenBailoutError(ValueError):
    """Static generation was interrupted by a dynamic access.

    A ``ValueError`` so ``pynext build`` reports it as a build failure
    instead of crashing, and it carries :data:`DYNAMIC_DIGEST` so the
    interruption stays distinguishable from a page bug (a plain 500).
    """

    digest = DYNAMIC_DIGEST

    def __init__(self, pattern, expression, accesses):
        self.pattern = pattern
        self.expression = expression
        self.accesses = list(accesses)
        super().__init__(self._message())

    def _message(self):
        lines = [
            f"Static generation for {self.pattern} bailed out: "
            f"dynamic access {self.expression!r} during prerender.",
            "Recorded dynamic accesses:",
            format_dynamic_accesses(self.accesses),
            "Remove the dynamic access, or serve this page dynamically "
            "with @page(dynamic='force-dynamic').",
        ]
        return "\n".join(lines)


def is_prerender_bailout(error):
    """True when ``error`` is a prerender interruption, not a page bug."""
    return getattr(error, "digest", None) == DYNAMIC_DIGEST


class DynamicTrackingState:
    """The shared, thread-safe record of one prerender's dynamic accesses.

    One instance is created per prerender and shared (via a context
    variable) with every thread the render runs on. Recording never raises
    except in ``dynamic="error"`` mode, where the first access interrupts
    the render with :class:`StaticGenBailoutError`.
    """

    def __init__(self, pattern, dynamic_mode):
        if dynamic_mode not in DYNAMIC_MODES:
            raise ValueError(f"dynamic must be one of {DYNAMIC_MODES}, got {dynamic_mode!r}")
        self.pattern = pattern
        self.dynamic_mode = dynamic_mode
        self.accesses = []
        self.dropped = 0
        self.closed = False
        self._lock = threading.Lock()

    @property
    def should_error(self):
        return self.dynamic_mode == "error"

    def record(self, expression, *, kind="data"):
        """Record one access; returns it, or None when closed or capped.

        In ``dynamic="error"`` mode the first recorded access raises
        :class:`StaticGenBailoutError` instead of returning.

        The cap is checked before the stack is extracted: once 32
        accesses are stored, further calls (for example a page calling
        ``random.random()`` in a loop) skip the expensive
        ``traceback.extract_stack()`` entirely.
        """
        with self._lock:
            if self.closed:
                return None
            if len(self.accesses) >= MAX_DYNAMIC_ACCESSES:
                self.dropped += 1
                snapshot = list(self.accesses)
                access = None
            else:
                stack = _user_stack()
                location = stack[-1] if stack else "<unknown>"
                access = DynamicAccess(expression, kind, location, stack)
                self.accesses.append(access)
                snapshot = list(self.accesses)
        if self.should_error:
            raise StaticGenBailoutError(self.pattern, expression, snapshot)
        return access

    def close(self):
        with self._lock:
            self.closed = True


_current_state = contextvars.ContextVar("pynext_dynamic_tracking_state", default=None)
_suppress_depth = contextvars.ContextVar("pynext_dynamic_suppress_depth", default=0)


def _active_state():
    state = _current_state.get()
    if state is None or state.closed or _suppress_depth.get():
        return None
    return state


def mark_dynamic_access(expression, *, kind="data"):
    """Record an explicit dynamic access in the current prerender, if any.

    Page authors (and framework resources) call this for dynamic reads the
    automatic hooks cannot see — an external fetch, a hand-rolled clock, a
    ``from datetime import datetime`` binding. Outside a prerender this is
    a no-op. In ``dynamic="error"`` mode it raises
    :class:`StaticGenBailoutError`.
    """
    if kind not in ("data", "intent"):
        raise ValueError(f"dynamic access kind must be 'data' or 'intent', got {kind!r}")
    state = _active_state()
    if state is None:
        return None
    return state.record(expression, kind=kind)


def no_store():
    """Declare that the current scope must be treated as dynamic.

    The ``unstable_noStore()`` analogue: a dynamic *intent*, weaker than a
    data read — it marks the scope dynamic without naming a source. In
    ``dynamic="error"`` mode it still interrupts static generation.
    """
    return mark_dynamic_access("no_store()", kind="intent")


def now(tz=None):
    """``datetime.now()`` that records itself when prerendering.

    Use this instead of ``datetime.now()`` when the page binds the class
    with ``from datetime import datetime`` — that binding escapes the
    prerender monkeypatch, so only this wrapper (or
    :func:`mark_dynamic_access`) can record the read.
    """
    mark_dynamic_access("datetime.now()")
    return _datetime.datetime.now(tz)


def utcnow():
    """Timezone-aware UTC ``datetime.now()`` that records itself when prerendering."""
    mark_dynamic_access("datetime.now(datetime.timezone.utc)")
    return _datetime.datetime.now(_datetime.timezone.utc)


class _suppress_dynamic_tracking:
    """Context manager: framework-internal calls never count as dynamic."""

    def __enter__(self):
        self._token = _suppress_depth.set(_suppress_depth.get() + 1)
        return self

    def __exit__(self, exc_type, exc, tb):
        _suppress_depth.reset(self._token)
        return False


def suppress_dynamic_tracking():
    """Suppress recording for framework-internal nondeterministic calls.

    The streaming shell marker (``uuid4``) and CSP nonces are generated by
    the framework during a prerender; without suppression they would be
    misreported as the page being dynamic.
    """
    return _suppress_dynamic_tracking()


def _record_call(expression):
    """Record a monkeypatched call, but only when its direct caller is user code.

    The direct caller decides: a page calling ``time.time()`` counts, while
    ``logging`` (which reads the clock for every record) or framework code
    does not. The call chain is user code -> wrapper -> this function, so
    the user frame is two hops up.

    ``exec()``/``eval()`` code runs in a ``"<string>"`` frame with no real
    file (A1-L2): the walk skips those frames to reach the user frame that
    ran the exec, so a clock read inside dynamically executed code is
    recorded against the exec call site. Only ``"<string>"`` frames are
    skipped — a standard-library frame in between still stops the walk,
    so ``logging`` reading the clock never counts.
    """
    state = _active_state()
    if state is None:
        return
    frame = inspect.currentframe()
    try:
        wrapper_frame = frame.f_back if frame is not None else None
        caller = wrapper_frame.f_back if wrapper_frame is not None else None
        while caller is not None and caller.f_code.co_filename == "<string>":
            caller = caller.f_back
        if caller is None or not _is_user_file(caller.f_code.co_filename):
            return
    finally:
        del frame
        del wrapper_frame
        del caller
    state.record(expression, kind="data")


def _wrap_simple(expression, original):
    def wrapper(*args, **kwargs):
        _record_call(expression)
        return original(*args, **kwargs)
    return wrapper


_ORIGINAL_DATETIME = _datetime.datetime
_ORIGINAL_DATE = _datetime.date


class _TrackedDatetimeMeta(type):
    """Metaclass making ``isinstance`` accept the original class too.

    The prerender swap replaces ``datetime.datetime`` with a subclass
    for tracking. Without this, ``isinstance(plain_dt,
    datetime.datetime)`` is False during a prerender for plain
    datetimes created elsewhere (for example in another thread's
    request), because a plain instance is not an instance of the
    subclass — breaking concurrent requests (notably ``json.dumps``
    with a ``default`` that dispatches on ``isinstance``). Accepting
    any instance of the original class covers both plain and tracked
    datetimes, since a tracked instance is also an original instance.
    """

    def __instancecheck__(cls, instance):
        return isinstance(instance, _ORIGINAL_DATETIME)


class _TrackedDateMeta(type):
    """Metaclass making ``isinstance`` accept the original class too."""

    def __instancecheck__(cls, instance):
        return isinstance(instance, _ORIGINAL_DATE)


class _TrackedDatetime(_ORIGINAL_DATETIME, metaclass=_TrackedDatetimeMeta):
    """``datetime.datetime`` that records ``now``/``utcnow``/``today`` reads."""

    @classmethod
    def now(cls, tz=None):
        _record_call("datetime.now()")
        return super().now(tz)

    @classmethod
    def utcnow(cls):
        _record_call("datetime.now()")
        return super().now(_datetime.timezone.utc).replace(tzinfo=None)

    @classmethod
    def today(cls):
        _record_call("datetime.today()")
        return super().today()


class _TrackedDate(_ORIGINAL_DATE, metaclass=_TrackedDateMeta):
    """``datetime.date`` that records ``today()`` reads."""

    @classmethod
    def today(cls):
        _record_call("date.today()")
        return super().today()


_PATCH_TARGETS = (
    (_time, "time", "time.time()"),
    (_random, "random", "random.random()"),
    (_random, "randint", "random.randint()"),
    (_random, "randrange", "random.randrange()"),
    (_random, "uniform", "random.uniform()"),
    (_random, "choice", "random.choice()"),
    (_random, "shuffle", "random.shuffle()"),
    (_random, "sample", "random.sample()"),
    (_random, "randbytes", "random.randbytes()"),
    (_uuid, "uuid4", "uuid.uuid4()"),
    (os, "urandom", "os.urandom()"),
    (_secrets, "token_bytes", "secrets.token_bytes()"),
    (_secrets, "token_hex", "secrets.token_hex()"),
    (_secrets, "token_urlsafe", "secrets.token_urlsafe()"),
)

_patch_lock = threading.Lock()
_patch_depth = 0
_saved_attributes = {}


def _install_patches():
    """Install the prerender monkeypatches (reference-counted)."""
    global _patch_depth
    with _patch_lock:
        if _patch_depth == 0:
            _saved_attributes.clear()
            for module, name, expression in _PATCH_TARGETS:
                _saved_attributes[(module, name)] = getattr(module, name)
                setattr(module, name, _wrap_simple(expression, getattr(module, name)))
            _saved_attributes[(_datetime, "datetime")] = _datetime.datetime
            _saved_attributes[(_datetime, "date")] = _datetime.date
            _datetime.datetime = _TrackedDatetime
            _datetime.date = _TrackedDate
        _patch_depth += 1


def _remove_patches():
    """Undo :func:`_install_patches` once the last prerender ends."""
    global _patch_depth
    with _patch_lock:
        _patch_depth -= 1
        if _patch_depth > 0:
            return
        _patch_depth = 0
        for (module, name), original in _saved_attributes.items():
            setattr(module, name, original)
        _saved_attributes.clear()


class PrerenderTracker:
    """One prerender's tracking scope.

    Enter it around the render of an SSG/ISR page; exit it when the render
    (including a streamed body) is fully consumed. Entering installs the
    monkeypatches and sets the tracking state; exiting restores both and
    calls ``on_finish`` with the state. Ownership may be handed to a
    streaming response: the tracker is entered in the rendering task and
    exited in the stream's ``finally`` block, which runs in the async
    generator's own context — the state is therefore closed with a plain
    context-variable restore rather than a token reset, and recording into
    a closed state is a no-op, so a stale value in the rendering task's
    context after hand-off is harmless.
    """

    def __init__(self, pattern, dynamic_mode, *, on_finish=None):
        self.pattern = pattern
        self.dynamic_mode = dynamic_mode
        self.state = DynamicTrackingState(pattern, dynamic_mode)
        self._on_finish = on_finish
        self._active = False
        self._previous = None

    def __enter__(self):
        if self._active:
            raise RuntimeError("PrerenderTracker is already active")
        self._active = True
        self._previous = _current_state.get()
        _current_state.set(self.state)
        _install_patches()
        return self.state

    def __exit__(self, exc_type, exc, tb):
        if not self._active:
            return False
        self._active = False
        self.state.close()
        _current_state.set(self._previous)
        self._previous = None
        _remove_patches()
        if self._on_finish is not None:
            self._on_finish(self.state)
        return False


def track_prerender(pattern, dynamic_mode, *, on_finish=None):
    """Create a :class:`PrerenderTracker` for one SSG/ISR render.

    Work the render spawns on plain ``threading.Thread`` threads does not
    inherit the tracking context (A1-M6): run it with
    :func:`run_in_prerender_context`, or under :func:`prerender_context`.
    ``asyncio.to_thread`` copies the context on its own.
    """
    return PrerenderTracker(pattern, dynamic_mode, on_finish=on_finish)


def prerender_context():
    """Return a copy of the current context, for spawning threads.

    A plain ``threading.Thread`` starts with an empty context, so the
    prerender tracking state (a context variable) is invisible inside
    it and dynamic accesses there are silently missed (A1-M6). Run
    spawned work under a copied context instead::

        ctx = prerender_context()
        threading.Thread(target=ctx.run, args=(work,)).start()

    or use :func:`run_in_prerender_context` for a single spawned call.
    Outside a prerender the copy is still valid — it just carries no
    tracking state.
    """
    return contextvars.copy_context()


def run_in_prerender_context(func, *args, **kwargs):
    """Run ``func`` in a new thread with the prerender context copied.

    The spawned thread records dynamic accesses into the current
    prerender (A1-M6); a plain ``threading.Thread`` would miss them
    because a new thread starts with an empty context. The thread is
    joined before returning, so the result is available and any
    exception — including a :class:`StaticGenBailoutError` in
    ``dynamic="error"`` mode — is re-raised in the caller.
    """
    ctx = contextvars.copy_context()
    outcome = {}

    def _run():
        try:
            outcome["value"] = ctx.run(func, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]
