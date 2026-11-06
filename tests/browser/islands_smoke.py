"""Optional real-browser check; run with the documented with_server helper."""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3173"
manifest = json.loads((Path(__file__).resolve().parents[2] / "examples/basic/.pynext/manifest.json").read_text())
counter = manifest["client"]["bundles"]["counter"]
unused = manifest["client"]["bundles"]["unused"]
hydrated = manifest["client"]["bundles"]["hydrated-counter"]
idle_counter = manifest["client"]["bundles"]["idle-counter"]
visible_counter = manifest["client"]["bundles"]["visible-counter"]
runtime = manifest["client"]["runtime"]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context()
    page = context.new_page()
    requested, errors = [], []
    page.on("request", lambda request: requested.append(request.url))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
      const pendingIdle = new Map();
      let nextIdle = 1;
      window.idleTimeouts = [];
      window.requestIdleCallback = (callback, options) => {
        const id = nextIdle++;
        window.idleTimeouts.push(options.timeout);
        pendingIdle.set(id, callback);
        return id;
      };
      window.cancelIdleCallback = (id) => pendingIdle.delete(id);
      window.pendingIdleCount = () => pendingIdle.size;
      window.flushIdle = () => {
        const callbacks = Array.from(pendingIdle.values());
        pendingIdle.clear();
        for (const callback of callbacks) callback({ didTimeout: false, timeRemaining: () => 50 });
      };
      function capture() {
        const button = document.querySelector('[data-pynext-island="hydrated-counter"] button');
        if (button && !window.serverHydratedButton) {
          window.serverHydratedButton = button;
        }
        const input = document.querySelector('[data-pynext-island="hydrated-counter"] input');
        if (input && !window.serverHydratedInput) {
          window.serverHydratedInput = input;
          input.value = "typed-before-hydration";
        }
      }
      const observer = new MutationObserver(capture);
      observer.observe(document, { childList: true, subtree: true });
      document.addEventListener("DOMContentLoaded", () => { capture(); observer.disconnect(); }, { once: true });
    })();""")

    def delay_hydration(route):
        # Give the browser time to retain the original server DOM before JS loads.
        time.sleep(0.15)
        route.continue_()

    page.route(re.compile(r"/_pynext/static/" + hydrated + r"\.js$"), delay_hydration)

    def csp(route):
        response = route.fetch()
        headers = dict(response.headers)
        headers["content-security-policy"] = "script-src 'self'; object-src 'none'"
        route.fulfill(response=response, headers=headers)

    page.route(base + "/", csp)
    page.goto(base + "/", wait_until="networkidle")
    button = page.get_by_role("button", name="Count: 1")
    button.wait_for()
    button.click()
    assert page.get_by_role("button", name="Count: 2").count() == 1
    assert any(counter in url for url in requested)
    assert any(runtime in url for url in requested)
    assert not any(unused in url for url in requested)
    assert not errors, errors

    # A detached island runs the function returned by mount as cleanup.
    page.evaluate("window.savedButton = document.querySelector('button'); window.savedButton.closest('[data-pynext-island]').remove()")
    page.wait_for_timeout(50)
    assert page.evaluate("window.savedButton.click(); window.savedButton.textContent") == "Count: 2"

    attack = '</script><img id="injected" src=x onerror="window.exploited=1">'
    page.goto(base + "/?" + urlencode({"note": attack}), wait_until="networkidle")
    assert page.locator("#injected").count() == 0
    assert page.evaluate("JSON.parse(document.querySelector('[data-pynext-island]').dataset.pynextProps).note") == attack
    assert page.evaluate("window.exploited === undefined")
    for path, initial, updated in (("/about", "Count: 5", "Count: 6"), ("/news/example", "Count: 10", "Count: 11")):
        page.goto(base + path, wait_until="networkidle")
        page.get_by_role("button", name=initial).click()
        assert page.get_by_role("button", name=updated).count() == 1
        island = page.locator('[data-pynext-island="hydrated-counter"]')
        assert island.get_attribute("data-pynext-state") == "hydrated"
        assert page.evaluate("window.serverHydratedButton === document.querySelector('[data-pynext-island=\"hydrated-counter\"] button')")
    page.goto(base + "/hydrate", wait_until="networkidle")
    island = page.locator('[data-pynext-island="hydrated-counter"]')
    assert island.get_attribute("data-pynext-state") == "hydrated"
    assert page.evaluate("window.serverHydratedButton === document.querySelector('[data-pynext-island=\"hydrated-counter\"] button')")
    assert page.locator('[data-pynext-island="hydrated-counter"] input').input_value() == "typed-before-hydration"
    page.get_by_role("button", name="Hydrated: 7").click()
    assert page.get_by_role("button", name="Hydrated: 8").count() == 1
    page.evaluate("window.savedHydratedButton = document.querySelector('[data-pynext-island=\"hydrated-counter\"] button'); window.savedHydratedButton.closest('[data-pynext-island]').remove()")
    page.wait_for_timeout(50)
    assert page.evaluate("window.savedHydratedButton.click(); window.savedHydratedButton.textContent") == "Hydrated: 8"

    def mismatch(route):
        response = route.fetch()
        body = response.text()
        assert "Hydrated: 3" in body
        headers = dict(response.headers)
        headers.pop("content-length", None)
        route.fulfill(response=response, body=body.replace("Hydrated: 3", "Stale: 3", 1), headers=headers)

    page.route(re.compile(r"/about\?mismatch=1$"), mismatch)
    page.goto(base + "/about?mismatch=1", wait_until="networkidle")
    island = page.locator('[data-pynext-island="hydrated-counter"]')
    assert island.get_attribute("data-pynext-mismatch") == "true"
    assert island.get_attribute("data-pynext-state") == "mounted"
    assert page.evaluate("window.serverHydratedButton !== document.querySelector('[data-pynext-island=\"hydrated-counter\"] button')")
    page.get_by_role("button", name="Hydrated: 3").click()
    assert page.get_by_role("button", name="Hydrated: 4").count() == 1

    requested.clear()
    page.goto(base + "/strategies", wait_until="networkidle")
    idle = page.locator('[data-pynext-island="idle-counter"]')
    lazy_hydrated = page.locator('[data-pynext-island="hydrated-counter"]')
    visible = page.locator('[data-pynext-island="visible-counter"]')
    assert idle.get_attribute("data-pynext-state") == "waiting"
    assert lazy_hydrated.get_attribute("data-pynext-state") == "waiting"
    assert visible.get_attribute("data-pynext-state") == "waiting"
    assert not any(idle_counter in url or visible_counter in url or unused in url for url in requested)
    assert page.evaluate("window.idleTimeouts.length >= 3 && window.idleTimeouts.every(value => value === 2000)")
    # Removing pending islands must cancel their loading before callbacks fire.
    page.locator('[data-pynext-island="unused"]').evaluate_all("nodes => nodes.forEach(node => node.remove())")
    page.wait_for_timeout(50)
    assert page.evaluate("window.pendingIdleCount()") == 2
    page.evaluate("window.flushIdle()")
    page.wait_for_function("document.querySelector('[data-pynext-island=\"idle-counter\"]').dataset.pynextState === 'mounted'")
    page.wait_for_function("document.querySelector('[data-pynext-island=\"hydrated-counter\"]').dataset.pynextState === 'hydrated'")
    assert page.evaluate("window.serverHydratedButton === document.querySelector('[data-pynext-island=\"hydrated-counter\"] button')")
    page.get_by_role("button", name="Idle: 1").click()
    assert page.get_by_role("button", name="Idle: 2").count() == 1
    page.get_by_role("button", name="Hydrated: 6").click()
    assert page.get_by_role("button", name="Hydrated: 7").count() == 1
    assert any(idle_counter in url for url in requested)
    assert not any(visible_counter in url or unused in url for url in requested)
    visible.scroll_into_view_if_needed()
    page.wait_for_function("document.querySelector('[data-pynext-island=\"visible-counter\"]').dataset.pynextState === 'hydrated'")
    page.get_by_role("button", name="Visible: 3").click()
    assert page.get_by_role("button", name="Visible: 4").count() == 1
    assert any(visible_counter in url for url in requested)
    assert not any(unused in url for url in requested)
    page.evaluate("""() => {
      const copy = document.querySelector('[data-pynext-island="idle-counter"]').cloneNode(true);
      copy.id = 'dynamic-idle';
      copy.removeAttribute('data-pynext-state');
      copy.setAttribute('data-pynext-props', JSON.stringify({ initial: 10 }));
      document.querySelector('main').append(copy);
    }""")
    page.wait_for_function("document.querySelector('#dynamic-idle').dataset.pynextState === 'waiting'")
    page.evaluate("window.flushIdle()")
    page.wait_for_function("document.querySelector('#dynamic-idle').dataset.pynextState === 'mounted'")
    page.get_by_role("button", name="Idle: 10").click()
    page.evaluate("window.dynamicButton = document.querySelector('#dynamic-idle button'); document.querySelector('#dynamic-idle').remove()")
    page.wait_for_timeout(50)
    assert page.evaluate("window.dynamicButton.click(); window.dynamicButton.textContent") == "Idle: 11"
    context.close()

    for observer in ("delete window.IntersectionObserver", "window.IntersectionObserver = class { constructor() { throw new Error('blocked') } }"):
        fallback = browser.new_context()
        page = fallback.new_page()
        page.add_init_script(observer)
        page.goto(base + "/strategies", wait_until="networkidle")
        page.wait_for_function("document.querySelector('[data-pynext-island=\"visible-counter\"]').dataset.pynextState === 'hydrated'", timeout=6000)
        fallback.close()
    for idle_api in ("delete window.requestIdleCallback; delete window.cancelIdleCallback", "window.requestIdleCallback = () => { throw new Error('blocked') }"):
        fallback = browser.new_context()
        page = fallback.new_page()
        page.add_init_script(idle_api)
        page.goto(base + "/strategies", wait_until="networkidle")
        page.wait_for_function("document.querySelector('[data-pynext-island=\"idle-counter\"]').dataset.pynextState === 'mounted'", timeout=6000)
        fallback.close()

    no_js = browser.new_context(java_script_enabled=False)
    page = no_js.new_page()
    page.goto(base + "/", wait_until="load")
    assert page.get_by_text("Enable JavaScript to use the counter.").count() == 1
    assert page.get_by_role("button").count() == 0
    page.goto(base + "/about", wait_until="load")
    assert page.get_by_role("button", name="Hydrated: 3").count() == 1
    no_js.close()
    browser.close()

print("Browser: SSR/SSG/ISR hydration, lazy scheduling, cancellation, API fallbacks, CSP, cleanup, and XSS escaping passed")
