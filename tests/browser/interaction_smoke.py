"""Exercise real user clicks, hydration races, and browser defaults."""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000"
manifest = json.loads((Path(__file__).resolve().parents[2] / "examples/basic/.pynext/manifest.json").read_text())
bundle = manifest["client"]["bundles"]["interaction-counter"]


def hold_hydration(page, island):
    # Hold this example's async hydration deterministically while real browser
    # events arrive; no production timer or network behavior is changed.
    page.evaluate("""() => {
      const realSetTimeout = window.setTimeout;
      window.setTimeout = (callback, delay, ...args) => {
        if (delay === 4321) {
          window.releaseIslandDelay = () => callback(...args);
          document.documentElement.setAttribute('data-test-hydration-delay', 'ready');
          return 0;
        }
        return realSetTimeout(callback, delay, ...args);
      };
    }""")
    island.evaluate("""element => {
      const props = JSON.parse(element.dataset.pynextProps);
      props.delay_ms = 4321;
      element.dataset.pynextProps = JSON.stringify(props);
    }""")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context()
    page = context.new_page()
    requested, warnings = [], []
    page.on("request", lambda request: requested.append(request.url))
    page.on("console", lambda message: warnings.append(message.text) if message.type == "warning" else None)

    def visit():
        requested.clear()
        page.goto(base + "/interaction", wait_until="networkidle")
        first = page.locator('#first [data-pynext-island="interaction-counter"]')
        second = page.locator('#second [data-pynext-island="interaction-counter"]')
        expect(first).to_have_attribute("data-pynext-state", "waiting")
        expect(second).to_have_attribute("data-pynext-state", "waiting")
        return first, second

    first, second = visit()
    assert not any(bundle in url for url in requested)
    first.locator('button[type="button"]').evaluate("button => button.click()")
    expect(first).to_have_attribute("data-pynext-state", "waiting")
    assert not any(bundle in url for url in requested)

    # Links and form submit buttons keep their normal browser behavior.
    first.get_by_role("link", name="Normal link").click()
    page.wait_for_url(re.compile(r"/about$"))
    assert not any(bundle in url for url in requested)
    first, _ = visit()
    first.get_by_role("button", name="Normal submit").click()
    page.wait_for_url(re.compile(r"/about\??$"))
    assert not any(bundle in url for url in requested)

    first, second = visit()
    hold_hydration(page, first)
    page.evaluate("""() => {
      window.originalInteractionButton = document.querySelector('#first button[type="button"]');
      window.deliveredInteractionTrust = [];
      document.addEventListener('click', event => {
        if (event.target.closest('#first [data-count]')) window.deliveredInteractionTrust.push(event.isTrusted);
      });
    }""")
    label = first.locator("[data-count]")
    label.click()
    expect(page.locator("html")).to_have_attribute("data-test-hydration-delay", "ready")
    expect(first).to_have_attribute("data-pynext-state", "activating")
    label.click()
    assert label.inner_text() == "Interaction: 0"
    page.evaluate("window.releaseIslandDelay()")
    expect(first).to_have_attribute("data-pynext-state", "hydrated")
    expect(label).to_have_text("Interaction: 2")
    assert page.evaluate("window.originalInteractionButton === document.querySelector('#first button[type=button]')")
    assert page.evaluate("window.deliveredInteractionTrust") == [False, False]
    expect(second).to_have_attribute("data-pynext-state", "waiting")
    second.locator("[data-count]").click()
    expect(second.locator("[data-count]")).to_have_text("Interaction: 11")
    label.click()
    expect(label).to_have_text("Interaction: 3")

    # Keyboard activation produces a trusted click and follows the same path.
    first, _ = visit()
    hold_hydration(page, first)
    first.locator('button[type="button"]').focus()
    page.keyboard.press("Enter")
    expect(page.locator("html")).to_have_attribute("data-test-hydration-delay", "ready")
    page.evaluate("window.releaseIslandDelay()")
    expect(first.locator("[data-count]")).to_have_text("Interaction: 1")

    # An identical replacement tree must not receive a stale target's click.
    first, _ = visit()
    hold_hydration(page, first)
    first.locator("[data-count]").click()
    expect(page.locator("html")).to_have_attribute("data-test-hydration-delay", "ready")
    first.locator("[data-count]").evaluate("label => label.replaceWith(label.cloneNode(true))")
    page.evaluate("window.releaseIslandDelay()")
    expect(first).to_have_attribute("data-pynext-state", "hydrated")
    expect(first.locator("[data-count]")).to_have_text("Interaction: 0")
    first.locator("[data-count]").click()
    expect(first.locator("[data-count]")).to_have_text("Interaction: 1")

    # A DOM mismatch mounts a fresh tree and drops clicks aimed at the old one.
    first, _ = visit()
    first.locator("[data-count]").evaluate("label => { window.staleButton = label.closest('button'); label.textContent = 'Stale'; }")
    first.locator("[data-count]").click()
    expect(first).to_have_attribute("data-pynext-mismatch", "true")
    expect(first).to_have_attribute("data-pynext-state", "mounted")
    expect(first.locator("[data-count]")).to_have_text("Interaction: 0")
    assert page.evaluate("window.staleButton !== document.querySelector('#first button[type=button]')")
    first.locator("[data-count]").click()
    expect(first.locator("[data-count]")).to_have_text("Interaction: 1")

    # Removed islands discard pending clicks and clean up any late hydration.
    first, _ = visit()
    hold_hydration(page, first)
    first.locator("[data-count]").click()
    expect(page.locator("html")).to_have_attribute("data-test-hydration-delay", "ready")
    first.evaluate("""element => {
      const button = window.removedButton = element.querySelector('button');
      const originalRemove = button.removeEventListener.bind(button);
      button.removeEventListener = (...args) => {
        originalRemove(...args);
        document.documentElement.setAttribute('data-test-removal-cleaned', 'yes');
      };
      element.remove();
    }""")
    page.evaluate("window.releaseIslandDelay()")
    expect(page.locator("html")).to_have_attribute("data-test-removal-cleaned", "yes")
    assert page.evaluate("window.removedButton.click(); window.removedButton.textContent") == "Interaction: 0"

    # Flooding during hydration remains bounded to the first 16 clicks.
    first, _ = visit()
    hold_hydration(page, first)
    label = first.locator("[data-count]")
    label.click()
    expect(page.locator("html")).to_have_attribute("data-test-hydration-delay", "ready")
    box = label.bounding_box()
    for _ in range(19):
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.evaluate("window.releaseIslandDelay()")
    expect(first).to_have_attribute("data-pynext-state", "hydrated")
    expect(label).to_have_text("Interaction: 16")
    assert any("interaction buffer is full" in warning for warning in warnings)

    # A failed import releases its interception listener and does not retry.
    first, _ = visit()
    pattern = re.compile(r"/_pynext/static/" + bundle + r"\.js$")
    page.route(pattern, lambda route: route.fulfill(status=503, body="unavailable"))
    first.locator("[data-count]").click()
    expect(first).to_have_attribute("data-pynext-error", "activation-failed")
    attempts = sum(bundle in url for url in requested)
    page.evaluate("""() => {
      window.bubbledAfterFailure = 0;
      document.addEventListener('click', () => window.bubbledAfterFailure++);
    }""")
    first.locator("[data-count]").click()
    assert sum(bundle in url for url in requested) == attempts == 1
    assert page.evaluate("window.bubbledAfterFailure") == 1
    assert first.locator("[data-count]").inner_text() == "Interaction: 0"
    context.close()

    no_js = browser.new_context(java_script_enabled=False)
    page = no_js.new_page()
    page.goto(base + "/interaction", wait_until="load")
    assert page.locator("#first [data-count]").inner_text() == "Interaction: 0"
    page.locator("#first").get_by_role("link", name="Normal link").click()
    page.wait_for_url(re.compile(r"/about$"))
    no_js.close()
    browser.close()

print("Browser: interaction hydration, safe replay, limits, failures, cleanup, and navigation passed")
