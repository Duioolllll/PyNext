"""Dev-mode file watching: stdlib polling watcher plus `pynext dev` reload."""
import asyncio
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request

from pynext.watch import Watcher

PAGE_V1 = (
    "from pynext import h, page\n"
    "@page()\n"
    "async def Page():\n"
    "    return h(\"main\", {}, h(\"p\", {}, \"version-one\"))\n"
)

PAGE_V2 = PAGE_V1.replace("version-one", "version-two")
PAGE_V3 = PAGE_V1.replace("version-one", "version-three")

LAYOUT = (
    "from pynext import h, layout\n"
    "@layout\n"
    "def Layout(children):\n"
    "    return h(\"html\", {}, h(\"body\", {}, children))\n"
)


def test_watcher_detects_content_new_and_deleted_files(tmp_path):
    root = tmp_path
    (root / "app").mkdir()
    page = root / "app" / "page.py"
    page.write_text("one", encoding="utf-8")
    watcher = Watcher(root)
    assert watcher.changed(watcher._baseline, watcher.snapshot()) == []
    page.write_text("two", encoding="utf-8")
    assert watcher.changed(watcher._baseline, watcher.snapshot()) == ["app/page.py"]
    (root / "app" / "new.py").write_text("x", encoding="utf-8")
    changed = watcher.changed(watcher._baseline, watcher.snapshot())
    assert changed == ["app/new.py", "app/page.py"]
    page.unlink()
    assert watcher.changed(watcher._baseline, watcher.snapshot()) == ["app/new.py", "app/page.py"]


def test_watcher_detects_same_size_edit_with_unchanged_timestamp(tmp_path):
    (tmp_path / "app").mkdir()
    page = tmp_path / "app" / "page.py"
    page.write_bytes(b"one")
    watcher = Watcher(tmp_path)
    before = page.stat()
    page.write_bytes(b"two")
    os.utime(page, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert page.stat().st_mtime_ns == before.st_mtime_ns
    assert watcher.changed(watcher._baseline, watcher.snapshot()) == ["app/page.py"]


def test_watcher_ignores_unwatched_and_generated_files(tmp_path):
    root = tmp_path
    (root / "app").mkdir()
    page = root / "app" / "page.py"
    page.write_text("one", encoding="utf-8")
    watcher = Watcher(root)
    (root / "notes.txt").write_text("not watched", encoding="utf-8")
    cache = root / "app" / "__pycache__"
    cache.mkdir()
    (cache / "page.pyc").write_bytes(b"bytecode")
    build = root / ".pynext"
    build.mkdir()
    (build / "manifest.json").write_text("{}", encoding="utf-8")
    assert watcher.changed(watcher._baseline, watcher.snapshot()) == []
    (root / "pynext.config.py").write_text("config = None\n", encoding="utf-8")
    assert watcher.changed(watcher._baseline, watcher.snapshot()) == ["pynext.config.py"]


def test_watcher_wait_for_change_debounces_rapid_edits(tmp_path):
    async def run():
        root = tmp_path
        (root / "app").mkdir()
        page = root / "app" / "page.py"
        page.write_text("one", encoding="utf-8")
        watcher = Watcher(root, interval=0.05, debounce=0.1)
        task = asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0.05)
        page.write_text("two", encoding="utf-8")
        await asyncio.sleep(0.05)
        page.write_text("three", encoding="utf-8")
        assert await asyncio.wait_for(task, 3) == ["app/page.py"]
    asyncio.run(run())


def _fetch(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read()


def _wait_for_content(url, marker, timeout, lines=None):
    deadline = time.time() + timeout
    last = b""
    while time.time() < deadline:
        try:
            last = _fetch(url)
        except Exception:
            time.sleep(0.2)
            continue
        if marker in last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"{marker!r} not served within {timeout}s; last body: {last!r}")


def _next_listening_address(lines, proc, seen, timeout=30):
    """Read dev-server output until the next listening line; return its address.

    A restart with ``--port 0`` binds a new ephemeral port, so the address
    must be re-read after every reload. Every line read is appended to
    ``seen`` so the test can assert on the reload messages afterwards.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            assert proc.poll() is None, "dev server exited before listening"
            continue
        seen.append(line)
        match = re.search(r"PyNext listening on http://(\S+)", line)
        if match:
            return match.group(1)
    raise AssertionError("dev server never printed its listening address")


def test_dev_reloads_edited_page_and_survives_broken_edit(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.py").write_text(PAGE_V1, encoding="utf-8")
    (root / "app" / "layout.py").write_text(LAYOUT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(root), "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    lines = queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=reader, daemon=True).start()
    seen = []
    try:
        address = _next_listening_address(lines, proc, seen)
        base = f"http://{address}"
        _wait_for_content(base + "/", b"version-one", timeout=10)

        (root / "app" / "page.py").write_text(PAGE_V2, encoding="utf-8")
        address = _next_listening_address(lines, proc, seen)
        _wait_for_content(f"http://{address}/", b"version-two", timeout=15)

        # A broken edit must not take the server down: the old version keeps
        # serving until a fixed edit arrives.
        (root / "app" / "page.py").write_text("this is not valid python (\n", encoding="utf-8")
        time.sleep(2)
        assert b"version-two" in _fetch(f"http://{address}/")
        (root / "app" / "page.py").write_text(PAGE_V3, encoding="utf-8")
        address = _next_listening_address(lines, proc, seen)
        _wait_for_content(f"http://{address}/", b"version-three", timeout=15)

        while not lines.empty():
            seen.append(lines.get())
        assert any("change detected" in line and "reloading" in line for line in seen)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
