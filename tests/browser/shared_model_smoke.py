"""A single data model generates and hydrates the same server/browser DOM."""
import sys

from playwright.sync_api import expect, sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page()
    page.goto(base + "/shared-model", wait_until="networkidle")
    first = page.locator('#shared-first [data-pynext-island="shared-model"]')
    second = page.locator('#shared-second [data-pynext-island="shared-model"]')
    expect(first).to_have_attribute("data-pynext-state", "waiting")
    page.evaluate("window.originalSharedButton = document.querySelector('#shared-first button')")
    first.locator("button").click()
    expect(first).to_have_attribute("data-pynext-state", "hydrated")
    expect(first.locator("button")).to_have_text("Shared: Ada <safe> & Co (1)")
    assert page.evaluate("window.originalSharedButton === document.querySelector('#shared-first button')")
    expect(second).to_have_attribute("data-pynext-state", "waiting")

    # A mutated server DOM is replaced only inside that island; the queued
    # interaction is deliberately not replayed on the replacement.
    second.locator("button").evaluate("button => { window.oldSharedButton = button; button.textContent = 'changed'; }")
    second.locator("button").click()
    expect(second).to_have_attribute("data-pynext-mismatch", "true")
    expect(second).to_have_attribute("data-pynext-state", "mounted")
    expect(second.locator("button")).to_have_text("Shared: Ada <safe> & Co")
    assert page.evaluate("window.oldSharedButton !== document.querySelector('#shared-second button')")
    expect(first.locator("button")).to_have_text("Shared: Ada <safe> & Co (1)")

    page.reload(wait_until="networkidle")
    first = page.locator('#shared-first [data-pynext-island="shared-model"]')
    first.evaluate("""element => {
      const model = JSON.parse(element.dataset.pynextModel);
      model.tree.tag = 'script';
      element.dataset.pynextModel = JSON.stringify(model);
    }""")
    first.locator("button").click()
    expect(first).to_have_attribute("data-pynext-error", "activation-failed")
    assert first.locator("script").count() == 0
    browser.close()

print("Browser: shared-model DOM reuse, interaction, escaping, and island-local mismatch recovery passed")
