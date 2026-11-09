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
    expect(page.get_by_text("Hello PyNext!")).to_have_count(1)
    assert page.evaluate("typeof window.__pynextNav === 'object'")
    # A marker that only survives if the page is NOT fully reloaded.
    page.evaluate("window.__navMarker = 'still-here'")
    link = page.get_by_role("link", name="Read a post")

    # The uncached response is fetched and the click swaps content in place.
    link.hover()
    link.click()
    expect(page).to_have_url(base + "/blog/hello")
    expect(page.get_by_text("Blog post")).to_have_count(1)
    expect(page.get_by_text("hello", exact=True)).to_have_count(1)
    assert page.evaluate("window.__navMarker") == "still-here"
    assert not errors, errors

    # Back/forward uses the same partial navigation, not a reload.
    page.go_back()
    expect(page).to_have_url(base + "/")
    expect(page.get_by_text("Hello PyNext!")).to_have_count(1)
    assert page.evaluate("window.__navMarker") == "still-here"
    page.go_forward()
    expect(page).to_have_url(base + "/blog/hello")
    expect(page.get_by_text("Blog post")).to_have_count(1)
    assert page.evaluate("window.__navMarker") == "still-here"
    context.close()

    # A superseded request may ignore AbortSignal (cache/transport adapters
    # can do this). Its late failure must not cause a full document load.
    for outcome in ("failure", "success", "history"):
        context = browser.new_context()
        page = context.new_page()
        page.goto(base + "/", wait_until="networkidle")
        page.evaluate("""async () => {
            window.__navMarker = 'race-survived';
            const originalFetch = window.fetch.bind(window);
            window.slowResponse = await originalFetch('/?race-slow=1');
            window.fetch = (url, options) => String(url).includes('race-slow')
                ? new Promise((resolve, reject) => {
                    window.resolveSlow = resolve;
                    window.rejectSlow = reject;
                    window.slowSignal = options.signal;
                }) : originalFetch(url, options);
            window.__pynextNav.navigate('/?race-slow=1').then(result => window.slowResult = result);
        }""")
        page.wait_for_function("typeof window.rejectSlow === 'function'")
        assert page.evaluate("window.__pynextNav.navigate('/blog/hello')") is True
        assert page.evaluate("window.slowSignal.aborted") is True
        if outcome == "history":
            page.go_back()
            expect(page.get_by_text("Hello PyNext!")).to_have_count(1)
        page.evaluate("window.rejectSlow(new Error('late failure'))" if outcome != "success"
                      else "window.resolveSlow(window.slowResponse)")
        page.wait_for_function("window.slowResult === false")
        expect(page).to_have_url(base + ("/" if outcome == "history" else "/blog/hello"))
        expect(page.get_by_text("Hello PyNext!" if outcome == "history" else "Blog post")).to_have_count(1)
        assert page.evaluate("window.__navMarker") == "race-survived"
        context.close()

    # Hover prefetch works even without IntersectionObserver (no viewport-entry
    # prefetch in this context), so the hover path is exercised on its own.
    no_io = browser.new_context()
    page = no_io.new_page()
    page.add_init_script("window.IntersectionObserver = undefined;")
    page.goto(base + "/", wait_until="networkidle")
    assert page.evaluate("window.__pynextNav.isPrefetched('/blog/hello')") is False
    with page.expect_response(lambda response: response.url == base + "/blog/hello"):
        page.get_by_role("link", name="Read a post").hover()
    assert page.evaluate("window.__pynextNav.isPrefetched('/blog/hello')") is False
    no_io.close()

    # Pages with no islands also start the navigation runtime.
    context = browser.new_context()
    page = context.new_page()
    page.goto(base + "/blog/hello", wait_until="networkidle")
    assert page.evaluate("typeof window.__pynextNav === 'object'")
    page.evaluate("window.__navMarker = 'still-here'")
    page.get_by_role("link", name="Home").click()
    expect(page.get_by_text("Hello PyNext!")).to_have_count(1)
    assert page.evaluate("window.__navMarker") == "still-here"
    assert page.evaluate("window.__pynextNav.navigate('https://example.com/')") is False
    context.close()

    # Prefetch only retains explicitly public, fresh, non-personal responses.
    context = browser.new_context()
    page = context.new_page()
    page.add_init_script("window.IntersectionObserver = undefined;")
    page.goto(base + "/", wait_until="networkidle")
    page.route("**/blog/hello?cacheable=1", lambda route: route.fulfill(
        status=200, body="<p>public</p>", headers={"content-type": "text/html", "cache-control": "public, max-age=30"}))
    with page.expect_response(lambda response: response.url.endswith("?cacheable=1")):
        page.evaluate("window.__pynextNav.prefetch('/blog/hello?cacheable=1')")
    page.wait_for_function("() => window.__pynextNav.isPrefetched('/blog/hello?cacheable=1')")
    page.route("**/blog/hello?private=1", lambda route: route.fulfill(
        status=200, body="<p>private</p>", headers={"content-type": "text/html", "cache-control": "private, no-store"}))
    with page.expect_response(lambda response: response.url.endswith("?private=1")):
        page.evaluate("window.__pynextNav.prefetch('/blog/hello?private=1')")
    assert not page.evaluate("window.__pynextNav.isPrefetched('/blog/hello?private=1')")
    context.close()

    # A failed partial navigation (404) falls back to a full document load:
    # the marker is gone and the server's 404 body is shown.
    context = browser.new_context()
    page = context.new_page()
    page.goto(base + "/", wait_until="networkidle")
    page.evaluate("window.__navMarker = 'still-here'")
    page.evaluate("window.__pynextNav.navigate('/missing')")
    expect(page).to_have_url(base + "/missing")
    expect(page.get_by_text("Not Found")).to_have_count(1)
    assert page.evaluate("window.__navMarker === undefined")
    context.close()
    browser.close()

print("Browser: client navigation, prefetch, history, and 404 fallback passed")
