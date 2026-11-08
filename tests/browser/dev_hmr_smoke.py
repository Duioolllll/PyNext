"""Check development island replacement without a full document reload."""
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
PAGE = (
    'from pynext import h, island, page\n'
    '@page()\n'
    'def Page(): return h("main", {}, h("p", {"id": "outside"}, "outside-one"), island("counter"))\n'
)


def module(version):
    return (
        'export function mount(element) {\n'
        '  window.hmrEvents = window.hmrEvents || [];\n'
        f'  window.hmrEvents.push("mount-{version}");\n'
        f'  element.textContent = "module-{version}";\n'
        f'  return () => window.hmrEvents.push("cleanup-{version}");\n'
        '}\n'
    )


with tempfile.TemporaryDirectory(prefix="pynext-hmr-browser-") as directory:
    project = Path(directory)
    (project / "app").mkdir()
    (project / "islands").mkdir()
    page_file = project / "app" / "page.py"
    client_file = project / "islands" / "counter.js"
    page_file.write_text(PAGE, encoding="utf-8")
    client_file.write_text(module("one"), encoding="utf-8")
    env = os.environ.copy()
    if os.name == "nt":
        candidates = list((ROOT / "node_modules" / "@esbuild").glob("win32-*/esbuild.exe"))
        if len(candidates) != 1:
            raise RuntimeError("The Windows esbuild executable is unavailable")
        env["PYNEXT_ESBUILD"] = str(candidates[0])
    else:
        env["PYNEXT_ESBUILD"] = str(ROOT / "node_modules" / ".bin" / "esbuild")
    process = subprocess.Popen(
        [sys.executable, "-m", "pynext.cli", "dev", "--dir", str(project), "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
    )
    lines = queue.Queue()
    seen = []

    def read_output():
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=read_output, daemon=True).start()
    try:
        deadline = time.monotonic() + 30
        address = None
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            seen.append(line)
            match = re.search(r"PyNext listening on http://(\S+)", line)
            if match:
                address = match.group(1)
                break
        if not address:
            raise AssertionError("Development server did not start: " + "".join(seen))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(f"http://{address}/", wait_until="networkidle")
            counter = page.locator('[data-pynext-island="counter"]')
            expect(counter).to_have_text("module-one")
            page.evaluate("window.hmrMarker = 'preserved'; window.originalOutside = document.querySelector('#outside')")

            client_file.write_text(module("two"), encoding="utf-8")
            expect(counter).to_have_text("module-two", timeout=20000)
            assert page.evaluate("window.hmrMarker") == "preserved"
            assert page.evaluate("window.originalOutside === document.querySelector('#outside')")
            assert page.evaluate("window.hmrEvents") == ["mount-one", "cleanup-one", "mount-two"]

            # Changes to Python page code still restart the server and cause
            # the browser to reload the document.
            page_file.write_text(PAGE.replace("outside-one", "outside-two"), encoding="utf-8")
            expect(page.locator("#outside")).to_have_text("outside-two", timeout=20000)
            assert page.evaluate("typeof window.hmrMarker") == "undefined"
            browser.close()
    except BaseException:
        while not lines.empty():
            seen.append(lines.get_nowait())
        print("Development server output:\n" + "".join(seen), file=sys.stderr)
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

print("Browser: development island replacement and server-code reload passed")
