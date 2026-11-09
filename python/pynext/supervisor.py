"""Multi-worker supervisor (spec v0.2 §8.4 and §15 M4, finding #52).

The supervisor is a plain parent process: it binds one listening socket,
spawns ``workers`` child processes that accept from duplicated copies of
that socket, and watches them. A worker that exits — crash, kill,
or a failed in-flight request that takes the process down — is restarted
automatically, so one worker's failure never takes the server down. A
worker whose heartbeat goes stale is killed and restarted the same way;
the heartbeat is updated by a dedicated thread, so it only goes stale
when the process itself stops making progress (stopped, or deadlocked
outside Python). A handler that merely blocks the event loop does not
stop the heartbeat: that case is still bounded by the 30-second request
timeout, not by a restart.

Graceful shutdown: SIGINT/SIGTERM to the supervisor signals every worker;
workers finish their current ``serve()``
shutdown (stop accepting, stop the native server, close the ISR cache)
and the supervisor waits up to ``shutdown_timeout`` seconds before
SIGKILLing stragglers.

Rolling restart (B6): SIGHUP to the supervisor replaces workers one at
a time — a replacement is started and must report ready before the old
worker is asked to drain, so the shared listener keeps accepting
throughout and a rolling restart serves no 5xx. A replacement that
fails to become ready leaves the old worker in place.

Scope and limits, stated plainly:

- Each worker is a separate process with its own Python interpreter,
  event loop and rate-limit buckets. The ISR cache is per-process with
  the memory backend, but shared across workers with
  ``--cache-backend sqlite``. State other than shared ISR entries is
  not shared between workers (see the manual) — with one exception:
  the CSRF signing key. The supervisor creates one key before spawning
  (finding #23) and shares it with every worker, via the environment
  and via the pickled worker config, so a double-submit CSRF cookie
  issued by one worker verifies on every other worker, including
  replacements started after a crash or a rolling restart.
- The supervisor does not proxy or route requests: if a worker dies
  mid-request, that request fails (the connection drops) and the client
  must retry. Only the *server* survives, not the in-flight request.
- The parent retains the listener, including a dynamically assigned port,
  so replacements accept on the same address after a restart.
- The default is one worker, which is exactly the historical
  single-process path: no supervisor process or shared listener.
"""
import logging
import multiprocessing
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass, replace

from .http import SecurityConfig, ensure_shared_csrf_signing_key

log = logging.getLogger("pynext.supervisor")

HEARTBEAT_INTERVAL = 0.5
HEARTBEAT_STALE_AFTER = 5.0
SUPERVISOR_POLL_INTERVAL = 0.2
# Default graceful-shutdown budget; WorkerConfig.shutdown_timeout
# overrides it per deployment. The old 5s default could SIGKILL a worker
# that was still draining a request the Rust layer allows 30s for.
SHUTDOWN_TIMEOUT = 30.0
_MAX_RESTART_BACKOFF = 8.0


@dataclass(frozen=True)
class WorkerConfig:
    """Everything a worker needs to (re)create its app. Picklable."""

    root: str
    host: str
    port: int
    production: bool
    max_concurrent: int
    acquire_timeout_ms: int
    body_capacity: int
    body_timeout_ms: int
    # Connection limits (A4); defaults mirror server.DEFAULT_*.
    max_connections: int = 1024
    max_connections_per_ip: int = 128
    header_read_timeout_ms: int = 10000
    body_min_rate_bytes_per_sec: int = 1024
    max_waiting: int | None = None  # None keeps the native default min(max_concurrent, 64)
    rate_limit: int = 0  # 0 disables rate limiting
    rate_limit_window: float = 60.0
    rate_limit_backend: str = "memory"
    rate_limit_exempt_paths: tuple = ()
    rate_limit_ipv6_prefix_len: int = 64  # IPv6 bucket grouping (see ratelimit.py)
    trusted_proxies: tuple = ()
    # Shared-cache / observability settings, forwarded to each worker's
    # create_app so multi-worker mode honours them (finding #42/#45).
    cache_backend: str = "memory"
    cache_path: str | None = None
    cache_failure_policy: str = "fail_open"
    rate_limit_failure_policy: str = "fail_closed"
    observability: object = None  # ObservabilityConfig or None
    security: object = None  # SecurityConfig or None
    websocket_max_message: int = 65_536
    # Graceful-shutdown budget (B6): how long a worker may take to drain
    # in-flight requests and close WebSockets after SIGTERM before the
    # supervisor SIGKILLs it. It bounds the Python drain in serve() and
    # the native connection drain; it must cover the 30s request timeout
    # or a slow request can still be cut off mid-flight.
    shutdown_timeout: float = 30.0


def _heartbeat_loop(stop, heartbeat):
    while not stop.wait(HEARTBEAT_INTERVAL):
        heartbeat.value = time.monotonic()


def _worker_main(config, ready, heartbeat, shutdown, listener):
    """Run one worker process. Top-level so the spawn start method can import it."""
    import asyncio

    from .app import create_app
    from .ratelimit import RateLimiter
    from .redis_rate import RedisRateLimiter
    from .server import serve

    limiter = None
    if config.rate_limit:
        limiter_type = RedisRateLimiter if config.rate_limit_backend == "redis" else RateLimiter
        options = ({"namespace": os.environ.get("PYNEXT_RATE_LIMIT_NAMESPACE", "default"),
                    "failure_policy": config.rate_limit_failure_policy}
                   if config.rate_limit_backend == "redis" else {})
        limiter = limiter_type(config.rate_limit, config.rate_limit_window,
                               trusted_proxies=config.trusted_proxies,
                               exempt_paths=config.rate_limit_exempt_paths,
                               ipv6_prefix_len=config.rate_limit_ipv6_prefix_len, **options)
    app = create_app(config.root, production=config.production, rate_limit=limiter,
                     cache_backend=config.cache_backend, cache_path=config.cache_path,
                     cache_failure_policy=config.cache_failure_policy,
                     observability=config.observability, debug=not config.production,
                     security=config.security, websocket_max_message=config.websocket_max_message,
                     trusted_proxies=config.trusted_proxies)

    async def run():
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # The shared shutdown event also works on Windows loops.
                pass
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(heartbeat_stop, heartbeat),
                                            daemon=True)
        heartbeat_thread.start()
        async def watch_shutdown():
            while not shutdown.is_set():
                await asyncio.sleep(0.1)
            stop_event.set()
        shutdown_task = asyncio.create_task(watch_shutdown())
        try:
            await serve(app, config.host, config.port, config.max_concurrent,
                        config.acquire_timeout_ms, config.body_capacity, config.body_timeout_ms,
                        listener=listener, stop_event=stop_event,
                        observability=config.observability,
                        max_connections=config.max_connections,
                        max_connections_per_ip=config.max_connections_per_ip,
                        header_read_timeout_ms=config.header_read_timeout_ms,
                        body_min_rate_bytes_per_sec=config.body_min_rate_bytes_per_sec,
                        shutdown_timeout=config.shutdown_timeout,
                        max_waiting=config.max_waiting,
                        on_ready=lambda _address: ready.set())
        finally:
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, return_exceptions=True)
            heartbeat_stop.set()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


class _Worker:
    def __init__(self, index, config, context, listener):
        self.index = index
        self.config = config
        self._context = context
        self._listener = listener
        self.process = None
        self.ready = context.Event()
        self.heartbeat = context.Value("d", 0.0)
        self.shutdown = context.Event()
        self.started_at = 0.0
        self.consecutive_failures = 0
        self.restart_at = None  # monotonic time of the next (re)start, if one is pending

    def schedule_restart(self, now):
        """Record a failure and the earliest time the worker may start again."""
        self.consecutive_failures += 1
        backoff = min(2 ** min(self.consecutive_failures, 3), _MAX_RESTART_BACKOFF)
        self.restart_at = now + backoff

    def start(self):
        if self.process is not None:
            if self.process.is_alive():
                raise RuntimeError("Cannot replace a running worker")
            self.process.join(timeout=0)
            self.process.close()
        self.ready = self._context.Event()
        self.heartbeat = self._context.Value("d", time.monotonic())
        self.shutdown = self._context.Event()
        self.process = self._context.Process(target=_worker_main,
                                             args=(self.config, self.ready, self.heartbeat,
                                                   self.shutdown, self._listener),
                                             name=f"pynext-worker-{self.index}")
        self.process.start()
        self.started_at = time.monotonic()
        log.info("Worker %d started (pid %d)", self.index, self.process.pid)

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    def is_alive(self):
        return self.process is not None and self.process.is_alive()

    def is_stale(self, now):
        # A worker that only just started has not had time to beat yet.
        if now - self.started_at < HEARTBEAT_STALE_AFTER:
            return False
        return now - self.heartbeat.value > HEARTBEAT_STALE_AFTER

    def terminate(self):
        if self.process is not None and self.process.is_alive():
            self.shutdown.set()

    def kill(self):
        if self.process is not None and self.process.is_alive():
            self.process.kill()

    def join(self, timeout=None):
        if self.process is not None:
            self.process.join(timeout)


def _rolling_restart(pool, config, context, listener):
    """Replace every worker one at a time; return True if all replaced.

    For each worker a replacement is started first and must report ready
    (its ``/readyz``-equivalent readiness, via the worker ``ready``
    event) before the old worker is asked to drain. The shared listener
    stays open throughout — the supervisor and every live worker hold a
    copy — so new connections keep being accepted while one worker
    drains. A replacement that fails to start or to become ready within
    ``shutdown_timeout`` leaves the old worker in place and aborts the
    roll; the old worker is untouched in that case.
    """
    for index, old in enumerate(pool):
        old_pid = old.pid
        replacement = _Worker(index, config, context, listener)
        try:
            replacement.start()
        except Exception:
            log.exception("Rolling restart: replacement worker %d failed to start", index)
            return False
        deadline = time.monotonic() + max(config.shutdown_timeout, 30.0)
        ready = False
        while time.monotonic() < deadline:
            if replacement.ready.is_set() and replacement.is_alive():
                ready = True
                break
            if not replacement.is_alive():
                break
            time.sleep(0.2)
        if not ready:
            log.error("Rolling restart: replacement worker %d did not become ready; "
                      "keeping worker %s", index, old_pid)
            if replacement.is_alive():
                replacement.kill()
            replacement.join()
            if replacement.process is not None:
                replacement.process.close()
            return False
        log.info("Rolling restart: worker %d ready (pid %d); draining old worker %s",
                 index, replacement.pid, old_pid)
        old.terminate()
        old.join(timeout=config.shutdown_timeout)
        if old.is_alive():
            log.warning("Rolling restart: old worker %d (pid %s) did not stop; killing",
                        index, old_pid)
            old.kill()
            old.join()
        if old.process is not None:
            old.process.close()
        pool[index] = replacement
        log.info("Rolling restart: worker %d replaced (pid %s -> %d)",
                 index, old_pid, replacement.pid)
    return True


MAX_WORKERS = 64
"""Most worker processes worth running: beyond this the supervisor spends
more time spawning and watching children than serving (review #4 #5:
--workers 100000 attempted tens of thousands of processes). The CLI
rejects larger values at parse time; run_supervisor enforces the same
bound for direct callers."""


def run_supervisor(config, *, workers):
    """Spawn and supervise ``workers`` processes. Returns an exit code.

    Blocks until SIGINT/SIGTERM, then shuts every worker down gracefully.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be an integer between 1 and {MAX_WORKERS}")
    # Finding #23: one CSRF signing key for every worker. Each worker
    # re-imports the framework (spawn), so a per-process fallback key
    # would differ per worker and a cookie issued by one worker would
    # fail HMAC verification on the others (intermittent 403). Resolve
    # the shared key first — an invalid configured key raises here,
    # before any socket is bound or worker spawned — then publish it
    # both ways workers can read it: the environment (inherited by
    # spawn children, so their default configs agree) and the pickled
    # worker config (so the app's own config carries it even if the
    # environment were not inherited). The same ``config`` is reused
    # for crash restarts and rolling restarts, so replacements keep
    # the key. The key itself is never logged.
    existing_key = getattr(config.security, "csrf_signing_key", None)
    shared_key = ensure_shared_csrf_signing_key(existing_key)
    if config.security is None:
        config = replace(config, security=SecurityConfig(csrf_signing_key=shared_key))
    else:
        config = replace(config, security=replace(config.security, csrf_signing_key=shared_key))
    log.info("Sharing one CSRF signing key across %d workers", workers)
    addresses = socket.getaddrinfo(config.host, config.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    family, kind, proto, _, address = addresses[0]
    listener = socket.socket(family, kind, proto)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name == "posix":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(address)
        listener.listen(1024)
        bound = listener.getsockname()
        resolved_host = bound[0]
        if family == socket.AF_INET6 and bound[3]:
            resolved_host += f"%{bound[3]}"
        config = replace(config, host=resolved_host, port=bound[1])
    except BaseException:
        listener.close()
        raise

    context = multiprocessing.get_context("spawn")
    pool = [_Worker(index, config, context, listener) for index in range(workers)]
    stopping = threading.Event()
    rolling = threading.Event()

    def _request_stop(_signum, _frame):
        stopping.set()

    def _request_roll(_signum, _frame):
        rolling.set()

    old_sigint = signal.signal(signal.SIGINT, _request_stop)
    old_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    sighup = getattr(signal, "SIGHUP", None)
    old_sighup = signal.signal(sighup, _request_roll) if sighup is not None else None
    try:
        for worker in pool:
            worker.start()
        display_host = f"[{config.host}]" if ":" in config.host else config.host
        print(f"PyNext supervisor listening on http://{display_host}:{config.port} "
              f"with {workers} workers", flush=True)
        while not stopping.is_set():
            if rolling.is_set():
                rolling.clear()
                _rolling_restart(pool, config, context, listener)
            now = time.monotonic()
            for worker in pool:
                if worker.is_alive():
                    # A worker that stays up for a while clears its failure
                    # streak, so one crash after a long healthy run does not
                    # inherit the backoff of an old crash loop.
                    if now - worker.started_at > 10:
                        worker.consecutive_failures = 0
                    if worker.is_stale(now):
                        log.warning("Worker %d (pid %d) heartbeat stale; restarting",
                                    worker.index, worker.pid)
                        worker.kill()
                        worker.join(timeout=config.shutdown_timeout)
                        worker.schedule_restart(now)
                    continue
                if worker.process is None:
                    continue
                if worker.restart_at is None:
                    # The process exited (crash, kill, or an error at startup).
                    log.warning("Worker %d (pid %d) exited with code %s; restarting",
                                worker.index, worker.pid, worker.process.exitcode)
                    worker.schedule_restart(now)
                elif now >= worker.restart_at:
                    worker.restart_at = None
                    worker.start()
            stopping.wait(SUPERVISOR_POLL_INTERVAL)
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        if sighup is not None:
            signal.signal(sighup, old_sighup)
        # Graceful shutdown: SIGTERM first, then SIGKILL stragglers.
        for worker in pool:
            worker.terminate()
        deadline = time.monotonic() + config.shutdown_timeout
        for worker in pool:
            remaining = deadline - time.monotonic()
            worker.join(timeout=max(0.0, remaining))
        for worker in pool:
            if worker.is_alive():
                log.warning("Worker %d (pid %d) did not stop; killing",
                            worker.index, worker.pid)
                worker.kill()
                worker.join()
        for worker in pool:
            if worker.process is not None:
                worker.process.close()
        listener.close()
    return 0
