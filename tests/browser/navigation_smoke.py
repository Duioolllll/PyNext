"""Client navigation and prefetch checks; normally started by tests/browser/run_smoke.py."""
import sys

from playwright.sync_api import expect, sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    page.goto(base + "/", wait_until="networkidle")
    assert page.get_by_text("Hello PyNext!").count() == 1
    assert page.evaluate("typeof window.__pynextNav === 'object'")
    # A marker that only survives if the page is NOT fully reloaded.
    page.evaluate("window.__navMarker = 'still-here'")
    link = page.get_by_role("link", name="Read a post")

    # Hovering prefetches the target; the click then swaps content in place.
    link.hover()
    page.wait_for_function("window.__pynextNav.isPrefetched('/blog/hello') === true")
    link.click()
    expect(page).to_have_url(base + "/blog/hello")
    assert page.get_by_text("Blog post").count() == 1
    assert page.get_by_text("hello", exact=True).count() == 1
    assert page.evaluate("window.__navMarker") == "still-here"
    assert not errors, errors

    # Back/forward uses the same partial navigation, not a reload.
    page.go_back()
    expect(page).to_have_url(base + "/")
    assert page.get_by_text("Hello PyNext!").count() == 1
    assert page.evaluate("window.__navMarker") == "still-here"
    page.go_forward()
    expect(page).to_have_url(base + "/blog/hello")
    assert page.get_by_text("Blog post").count() == 1
    assert page.evaluate("window.__navMarker") == "still-here"
    context.close()

    # Hover prefetch works even without IntersectionObserver (no viewport-entry
    # prefetch in this context), so the hover path is exercised on its own.
    no_io = browser.new_context()
    page = no_io.new_page()
    page.add_init_script("window.IntersectionObserver = undefined;")
    page.goto(base + "/", wait_until="networkidle")
    assert page.evaluate("window.__pynextNav.isPrefetched('/blog/hello')") is False
    page.get_by_role("link", name="Read a post").hover()
    page.wait_for_function("window.__pynextNav.isPrefetched('/blog/hello') === true")
    no_io.close()

    # A failed partial navigation (404) falls back to a full document load:
    # the marker is gone and the server's 404 body is shown.
    context = browser.new_context()
    page = context.new_page()
    page.goto(base + "/", wait_until="networkidle")
    page.evaluate("window.__navMarker = 'still-here'")
    page.evaluate("window.__pynextNav.navigate('/missing')")
    expect(page).to_have_url(base + "/missing")
    assert page.get_by_text("Not Found").count() == 1
    assert page.evaluate("window.__navMarker === undefined")
    context.close()
    browser.close()

print("Browser: client navigation, prefetch, history, and 404 fallback passed")
