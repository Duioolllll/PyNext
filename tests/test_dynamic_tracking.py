"""Dynamic-access tracking + bailout (Phase 1, track B P0-1).

Next.js parallel: ``server/app-render/dynamic-rendering.ts`` — "dynamic
intent vs dynamic data read", digest-identified bailout, and a record of
*why* a page is not static. Also covers the companion gap §3.2 #4: SSG
dev/prod behaviour must agree (dev warns, ``--strict-ssg`` 404s).
"""

import asyncio
import datetime
import logging
import time

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.dynamic import (
    StaticGenBailoutError,
    format_dynamic_accesses,
    is_prerender_bailout,
    mark_dynamic_access,
    no_store,
    now,
    track_prerender,
)


def write_page(root, source, pattern="/"):
    parts = [part for part in pattern.strip("/").split("/") if part]
    directory = root / "app" / "/".join(parts) if parts else root / "app"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "page.py"
    path.write_text(source, encoding="utf-8")
    return path


CLOCK_AUTO = (
    "from pynext import h, page\n"
    "import time\n"
    "@page(render=\"ssg\")\n"
    "async def Page():\n"
    "    return h(\"p\", {}, str(time.time() > 0))\n"
)

CLOCK_ERROR = CLOCK_AUTO.replace(
    "@page(render=\"ssg\")", "@page(render=\"ssg\", dynamic=\"error\")")

DATETIME_ERROR = (
    "from pynext import h, page\n"
    "import datetime\n"
    "@page(render=\"ssg\", dynamic=\"error\")\n"
    "async def Page():\n"
    "    return h(\"p\", {}, datetime.datetime.now().strftime(\"%Y\"))\n"
)

NO_STORE_ERROR = (
    "from pynext import h, page, no_store\n"
    "@page(render=\"ssg\", dynamic=\"error\")\n"
    "async def Page():\n"
    "    no_store()\n"
    "    return h(\"p\", {}, \"x\")\n"
)

MARK_ERROR = (
    "from pynext import h, page, mark_dynamic_access\n"
    "@page(render=\"ssg\", dynamic=\"error\")\n"
    "async def Page():\n"
    "    mark_dynamic_access(\"external fetch\")\n"
    "    return h(\"p\", {}, \"x\")\n"
)

SYNC_CLOCK_ERROR = (
    "from pynext import h, page\n"
    "import time\n"
    "@page(render=\"ssg\", dynamic=\"error\")\n"
    "def Page():\n"
    "    return h(\"p\", {}, str(time.time() > 0))\n"
)

STREAMING_CLOCK_ERROR = (
    "from pynext import h, page\n"
    "import time\n"
    "@page(render=\"ssg\", dynamic=\"error\")\n"
    "async def Page():\n"
    "    yield h(\"p\", {}, \"first\")\n"
    "    time.time()\n"
    "    yield h(\"p\", {}, \"second\")\n"
)

STREAMING_CLEAN = (
    "from pynext import h, page\n"
    "@page(render=\"ssg\")\n"
    "async def Page():\n"
    "    yield h(\"p\", {}, \"first\")\n"
    "    yield h(\"p\", {}, \"second\")\n"
)


def test_auto_mode_records_access_and_builds(tmp_path, caplog):
    write_page(tmp_path, CLOCK_AUTO)
    with caplog.at_level(logging.WARNING, logger="pynext"):
        manifest = write_manifest(tmp_path)
    assert manifest["dynamic_modes"] == {"/": "auto"}
    accesses = manifest["dynamic_accesses"]["/"]
    assert [entry["expression"] for entry in accesses] == ["time.time()"]
    assert accesses[0]["kind"] == "data"
    assert "page.py" in accesses[0]["location"]
    assert "time.time()" in caplog.text
    # The patches are restored: later reads return plain builtin types.
    assert type(datetime.datetime.now()) is datetime.datetime


def test_error_mode_fails_the_build_with_expression_and_location(tmp_path):
    write_page(tmp_path, CLOCK_ERROR)
    with pytest.raises(StaticGenBailoutError) as excinfo:
        write_manifest(tmp_path)
    assert is_prerender_bailout(excinfo.value)
    message = str(excinfo.value)
    assert "time.time()" in message
    assert "/" in message
    assert "page.py" in message
    assert "force-dynamic" in message


def test_error_mode_catches_datetime_now_and_no_store_and_manual_marks(tmp_path):
    for source, expression in ((DATETIME_ERROR, "datetime.now()"),
                               (NO_STORE_ERROR, "no_store()"),
                               (MARK_ERROR, "external fetch")):
        write_page(tmp_path, source)
        with pytest.raises(StaticGenBailoutError) as excinfo:
            write_manifest(tmp_path)
        assert expression in str(excinfo.value)


def test_error_mode_catches_sync_pages_on_worker_threads(tmp_path):
    write_page(tmp_path, SYNC_CLOCK_ERROR)
    with pytest.raises(StaticGenBailoutError) as excinfo:
        write_manifest(tmp_path)
    assert "time.time()" in str(excinfo.value)


def test_error_mode_catches_late_chunks_of_streaming_pages(tmp_path):
    write_page(tmp_path, STREAMING_CLOCK_ERROR)
    with pytest.raises(StaticGenBailoutError) as excinfo:
        write_manifest(tmp_path)
    assert "time.time()" in str(excinfo.value)


def test_streaming_shell_marker_is_not_a_dynamic_access(tmp_path):
    # The framework's own uuid4 marker must not count as the page being
    # dynamic: a clean streaming SSG page builds with an empty report.
    write_page(tmp_path, STREAMING_CLEAN)
    manifest = write_manifest(tmp_path)
    assert manifest["dynamic_accesses"] == {}
    assert manifest["static_pages"]["/"] is not None


def test_invalid_dynamic_modes_are_rejected(tmp_path):
    cases = [
        ("from pynext import h, page\n"
         "@page(render=\"ssr\", dynamic=\"error\")\n"
         "async def Page(request):\n"
         "    return h(\"p\", {}, \"x\")\n"),
        ("from pynext import h, page\n"
         "@page(render=\"isr\", revalidate=30, dynamic=\"force-dynamic\")\n"
         "async def Page():\n"
         "    return h(\"p\", {}, \"x\")\n"),
        ("from pynext import h, page\n"
         "@page(render=\"ssg\", dynamic=\"bogus\")\n"
         "async def Page():\n"
         "    return h(\"p\", {}, \"x\")\n"),
    ]
    for source in cases:
        write_page(tmp_path, source)
        with pytest.raises(ValueError):
            create_app(tmp_path)


def test_force_dynamic_ssg_is_served_dynamically_in_production(tmp_path):
    write_page(tmp_path,
               "from pynext import h, page\n"
               "calls = 0\n"
               "@page(render=\"ssg\", dynamic=\"force-dynamic\")\n"
               "async def Page():\n"
               "    global calls\n"
               "    calls += 1\n"
               "    return h(\"p\", {}, str(calls))\n")
    manifest = write_manifest(tmp_path)
    assert manifest["static_pages"] == {}
    assert manifest["dynamic_modes"] == {"/": "force-dynamic"}

    async def run():
        app = create_app(tmp_path, production=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/")
            second = await client.get("/")
        assert first.status_code == 200 and second.status_code == 200
        assert first.text != second.text  # rendered per request, not baked

    asyncio.run(run())


def test_force_dynamic_pages_are_not_tracked(tmp_path, caplog):
    # A force-dynamic page is dynamic by design: rendering it per request
    # must not warn that a "static page read dynamic data", and nothing
    # is recorded into the dynamic-access report.
    write_page(tmp_path,
               "from pynext import h, page\n"
               "import time\n"
               "calls = 0\n"
               "@page(render=\"ssg\", dynamic=\"force-dynamic\")\n"
               "async def Page():\n"
               "    global calls\n"
               "    calls += 1\n"
               "    return h(\"p\", {}, str(time.time() > 0) + str(calls))\n")
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/")
            second = await client.get("/")
        assert first.status_code == 200 and second.status_code == 200
        assert first.text != second.text
        return app

    with caplog.at_level(logging.WARNING, logger="pynext"):
        app = asyncio.run(run())
    assert app.dynamic_accesses == {}
    assert "read dynamic data" not in caplog.text


def test_isr_error_mode_fails_generation_without_caching(tmp_path):
    write_page(tmp_path, CLOCK_ERROR.replace("ssg", "isr").replace(
        "@page(render=\"isr\", dynamic=\"error\")",
        "@page(render=\"isr\", revalidate=30, dynamic=\"error\")"))
    write_manifest(tmp_path)  # ISR pages are not rendered at build time.

    async def run():
        app = create_app(tmp_path, production=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/")
            second = await client.get("/")
        assert first.status_code == 500
        assert second.status_code == 500  # failed generation is not cached
        assert app.isr.store.lookup("/")[0] == "miss"
        assert [entry["expression"] for entry in app.dynamic_accesses["/"]] == ["time.time()"]

    asyncio.run(run())


def test_dev_warns_about_ungenerated_ssg_paths(tmp_path, caplog):
    write_page(tmp_path,
               "from pynext import h, page\n"
               "@page(render=\"ssg\")\n"
               "async def Page(params):\n"
               "    return h(\"p\", {}, params[\"slug\"])\n"
               "def generate_static_params():\n"
               "    return [{\"slug\": \"a\"}]\n",
               pattern="/blog/[slug]")

    async def run(strict):
        app = create_app(tmp_path, strict_ssg=strict)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            known = await client.get("/blog/a")
            unknown = await client.get("/blog/b")
        return known.status_code, unknown.status_code

    with caplog.at_level(logging.WARNING, logger="pynext"):
        known, unknown = asyncio.run(run(False))
    assert known == 200
    assert unknown == 200  # dev still renders it, but warns it will 404 in prod
    assert "will 404 in production" in caplog.text
    assert "/blog/b" in caplog.text

    known, unknown = asyncio.run(run(True))
    assert known == 200
    assert unknown == 404  # --strict-ssg: dev agrees with production


def test_tracking_primitives():
    assert mark_dynamic_access("x") is None
    assert no_store() is None
    assert not is_prerender_bailout(ValueError("boom"))

    with track_prerender("/unit", "auto") as state:
        assert now() is not None
        assert time.time() > 0
        assert [access.expression for access in state.accesses] == [
            "datetime.now()", "time.time()"]
        # Only user-code frames survive: no framework or stdlib frames.
        stack = format_dynamic_accesses(state.accesses)
        assert "test_dynamic_tracking.py" in stack
        assert "dynamic.py" not in stack
        assert "traceback" not in stack

    # A from-import binding escapes the datetime swap: documented limit.
    # Pages that bind the class must use pynext.dynamic.now() instead.
    from datetime import datetime as bound_datetime
    with track_prerender("/unit", "auto") as state:
        bound_datetime.now()
    assert state.accesses == []

    with pytest.raises(StaticGenBailoutError) as excinfo:
        with track_prerender("/unit", "error"):
            mark_dynamic_access("external fetch")
    assert is_prerender_bailout(excinfo.value)
    assert "external fetch" in str(excinfo.value)


def test_prerender_datetime_swap_does_not_break_other_threads():
    """Regression: the datetime swap must not break ``isinstance`` in
    concurrent requests. During a prerender, a plain datetime created
    elsewhere (for example in another thread's request) must still
    pass ``isinstance(plain_dt, datetime.datetime)``, and ``json.dumps``
    with an ``isinstance``-dispatching ``default`` must keep working.
    The bug broke the *other* request's values, not the tracked ones."""
    import json
    import threading

    plain_dt = datetime.datetime(2026, 1, 2, 3, 4, 5)
    plain_date = datetime.date(2026, 1, 2)
    results = {}

    def check_in_other_thread():
        results["isinstance_dt"] = isinstance(plain_dt, datetime.datetime)
        results["isinstance_date"] = isinstance(plain_date, datetime.date)

        def enc(obj):
            if isinstance(obj, datetime.datetime):
                return obj.isoformat()
            if isinstance(obj, datetime.date):
                return obj.isoformat()
            raise TypeError(f"not serializable: {obj!r}")

        results["json"] = json.dumps(
            {"at": plain_dt, "on": plain_date}, default=enc)

    with track_prerender("/unit", "auto"):
        # The swap is active here: datetime.datetime is the tracked subclass.
        thread = threading.Thread(target=check_in_other_thread)
        thread.start()
        thread.join()
        # The same must hold in this thread while the swap is active.
        assert isinstance(plain_dt, datetime.datetime)
        assert isinstance(plain_date, datetime.date)

    assert results["isinstance_dt"] is True
    assert results["isinstance_date"] is True
    assert json.loads(results["json"]) == {
        "at": "2026-01-02T03:04:05", "on": "2026-01-02"}


def test_spawned_plain_thread_misses_dynamic_access():
    """A1-M6 (documented limit): a plain threading.Thread starts with an
    empty context, so a clock read inside it is silently missed by
    prerender tracking. This test pins the behaviour so the limit is
    visible, not silently fixed by accident."""
    import threading

    with track_prerender("/unit", "auto") as state:
        def read_clock():
            time.time()

        thread = threading.Thread(target=read_clock)
        thread.start()
        thread.join()
    assert state.accesses == []


def test_run_in_prerender_context_records_spawned_access():
    """A1-M6: the helper copies the tracking context into the spawned
    thread, so its clock read is recorded."""
    from pynext.dynamic import run_in_prerender_context

    with track_prerender("/unit", "auto") as state:
        result = run_in_prerender_context(lambda: time.time() > 0)
    assert result is True
    assert [a.expression for a in state.accesses] == ["time.time()"]


def test_run_in_prerender_context_reraises_bailout():
    """A1-M6: in dynamic='error' mode a bailout raised in the spawned
    thread is re-raised in the caller, so the prerender still fails."""
    from pynext.dynamic import run_in_prerender_context

    with pytest.raises(StaticGenBailoutError) as excinfo:
        with track_prerender("/unit", "error"):
            run_in_prerender_context(lambda: time.time() > 0)
    assert "time.time()" in str(excinfo.value)


def test_prerender_context_covers_executor_work():
    """A1-M6: prerender_context() lets an executor's worker record
    accesses too (ThreadPoolExecutor does not copy the context)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from pynext.dynamic import prerender_context

    with track_prerender("/unit", "auto") as state:
        ctx = prerender_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ctx.run, lambda: time.time() > 0).result()
    assert [a.expression for a in state.accesses] == ["time.time()"]


def test_exec_and_eval_clock_reads_are_tracked():
    """A1-L2: a clock read inside exec()/eval() code is recorded against
    the exec/eval call site, not silently missed."""
    with track_prerender("/unit", "auto") as state:
        exec("time.time()")
        eval("time.time()")
    assert [a.expression for a in state.accesses] == ["time.time()", "time.time()"]
    # The location is the real file that ran the exec, not "<string>".
    for access in state.accesses:
        assert "test_dynamic_tracking.py" in access.location
        assert "<string>" not in access.location


def test_stdlib_clock_read_still_not_tracked():
    """A1-L2 guard: walking past "<string>" frames must not turn a
    standard-library clock read into a dynamic access — logging reading
    the clock for a record still does not count."""
    import logging

    logger = logging.getLogger("pynext-test-a1l2")
    with track_prerender("/unit", "auto") as state:
        logger.info("a log line during a prerender")
    assert state.accesses == []


def test_tracked_datetime_returns_original_class():
    """A1-L3: during a prerender, datetime.now()/utcnow()/today() and
    date.today() return plain datetime/date instances — not the tracking
    subclass — so concurrent requests see no leaked type."""
    import datetime as dt

    with track_prerender("/unit", "auto"):
        now = dt.datetime.now()
        utc = dt.datetime.utcnow()
        today = dt.datetime.today()
        date_today = dt.date.today()
    assert type(now) is dt.datetime
    assert type(utc) is dt.datetime
    assert type(today) is dt.datetime
    assert type(date_today) is dt.date
    assert now.tzinfo is None and utc.tzinfo is None
    # Values are still correct datetimes.
    assert isinstance(now, dt.datetime) and isinstance(date_today, dt.date)


def test_tracked_datetime_returns_original_class_in_other_thread():
    """A1-L3: the leak also shows in a concurrent thread's request — a
    plain datetime created there must be exactly datetime.datetime."""
    import datetime as dt
    import threading

    results = {}

    def read_clock():
        results["now_type"] = type(dt.datetime.now())
        results["today_type"] = type(dt.date.today())

    with track_prerender("/unit", "auto"):
        thread = threading.Thread(target=read_clock)
        thread.start()
        thread.join()
    assert results["now_type"] is dt.datetime
    assert results["today_type"] is dt.date
