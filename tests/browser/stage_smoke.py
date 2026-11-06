"""Browser smoke test for the reconstructed mount/hydration releases."""
import os

from playwright.sync_api import sync_playwright

base = os.environ.get("PYNEXT_TEST_BASE_URL", "http://127.0.0.1:3174")
hydration = os.environ.get("PYNEXT_TEST_HYDRATION") == "1"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(base + "/", wait_until="domcontentloaded")
    button = page.get_by_role("button", name="Count: 1")
    button.wait_for()
    button.click()
    assert page.get_by_role("button", name="Count: 2").count() == 1

    if hydration:
        def delay_modules(route):
            response = route.fetch()
            route.fulfill(response=response, body="await new Promise(r => setTimeout(r, 500));\n" + response.text())

        page.route("**/_pynext/static/*.js", delay_modules)
        page.goto(base + "/hydrate", wait_until="commit")
        page.wait_for_selector('[data-pynext-mode="hydrate"] button', state="attached")
        page.evaluate("window.savedButton = document.querySelector('[data-pynext-mode=hydrate] button')")
        page.wait_for_function("document.querySelector('[data-pynext-mode=hydrate]').dataset.pynextState === 'hydrated'")
        assert page.evaluate("window.savedButton === document.querySelector('[data-pynext-mode=hydrate] button')")
        page.get_by_role("button", name="Hydrated: 7").click()
        assert page.get_by_role("button", name="Hydrated: 8").count() == 1
    browser.close()

print("Browser mount" + (" and hydration" if hydration else "") + " passed")
