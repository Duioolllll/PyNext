"""Runtime ISR policy overrides (Next.js SharedCacheControls port).

The override map is consulted before the build-time policy, so an ISR
policy can change without rewriting the manifest or rebuilding. These
tests pin that contract: base policies and the manifest file stay
untouched, overrides are validated like page policies, and a new
generation after an override stores the overridden lifetimes.
"""
import asyncio

import httpx
import pytest
from conftest import page_html

from pynext import create_app
from pynext.build import write_manifest


def make_project(root):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"{params[\'slug\']}:{calls}")\n'
    )
    about = root / "app" / "about"
    about.mkdir(parents=True)
    (about / "page.py").write_text(
        'from pynext import h, page\n'
        '@page()\n'
        'def Page():\n'
        '    return h("p", {}, "about")\n'
    )


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_runtime_override_defaults_to_manifest_policy(tmp_path):
    make_project(tmp_path)
    manifest = write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)
    assert app.get_isr_policy("/blog/[slug]") == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}
    assert app.isr_policy_overrides == {}
    assert manifest["isr_policies"]["/blog/[slug]"] == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}


def test_set_isr_policy_overrides_without_rebuild_or_manifest_change(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)
    manifest_path = tmp_path / ".pynext" / "manifest.json"
    before = manifest_path.read_bytes()
    app = create_app(tmp_path, production=True)
    assert app.set_isr_policy("/blog/[slug]", revalidate=120, stale=10) == {
        "revalidate": 120.0, "stale": 10.0, "expire": 130.0}
    assert app.get_isr_policy("/blog/[slug]") == {"revalidate": 120.0, "stale": 10.0, "expire": 130.0}
    # Base policy and manifest are untouched: no rebuild was needed.
    assert app.isr_policies["/blog/[slug]"] == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}
    assert app.manifest["isr_policies"]["/blog/[slug]"] == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}
    assert manifest_path.read_bytes() == before
    # A fresh production app still passes the manifest check.
    create_app(tmp_path, production=True)


def test_set_isr_policy_partial_override_inherits_current(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path)
    assert app.set_isr_policy("/blog/[slug]", revalidate=5) == {
        "revalidate": 5.0, "stale": 1.0, "expire": 6.0}
    # The second call inherits the overridden revalidate, not the base one.
    assert app.set_isr_policy("/blog/[slug]", stale=2) == {
        "revalidate": 5.0, "stale": 2.0, "expire": 7.0}


@pytest.mark.parametrize("kwargs", [
    {},
    {"revalidate": 0},
    {"revalidate": -1},
    {"revalidate": True},
    {"revalidate": 31_536_001},
    {"revalidate": float("nan")},
    {"stale": -1},
    {"stale": float("inf")},
])
def test_set_isr_policy_rejects_invalid_values(tmp_path, kwargs):
    make_project(tmp_path)
    app = create_app(tmp_path)
    with pytest.raises(ValueError):
        app.set_isr_policy("/blog/[slug]", **kwargs)
    assert app.isr_policy_overrides == {}


@pytest.mark.parametrize("pattern", ["/nope", "/about", "/"])
def test_set_isr_policy_rejects_non_isr_pattern(tmp_path, pattern):
    make_project(tmp_path)
    app = create_app(tmp_path)
    with pytest.raises(ValueError):
        app.set_isr_policy(pattern, revalidate=5)


def test_set_and_get_isr_policy_reject_non_string_pattern(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path)
    with pytest.raises(TypeError):
        app.set_isr_policy(123, revalidate=5)
    with pytest.raises(TypeError):
        app.get_isr_policy(123)
    with pytest.raises(TypeError):
        app.clear_isr_policy(123)


def test_get_isr_policy_returns_a_copy(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path)
    policy = app.get_isr_policy("/blog/[slug]")
    policy["revalidate"] = 0
    assert app.get_isr_policy("/blog/[slug]") == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}
    app.set_isr_policy("/blog/[slug]", revalidate=9)
    override = app.get_isr_policy("/blog/[slug]")
    override["stale"] = 0
    assert app.get_isr_policy("/blog/[slug]") == {"revalidate": 9.0, "stale": 1.0, "expire": 10.0}


def test_clear_isr_policy_restores_base(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path)
    app.set_isr_policy("/blog/[slug]", revalidate=9)
    assert app.clear_isr_policy("/blog/[slug]") is True
    assert app.get_isr_policy("/blog/[slug]") == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}
    assert app.clear_isr_policy("/blog/[slug]") is False
    with pytest.raises(ValueError):
        app.clear_isr_policy("/about")
    app.set_isr_policy("/blog/[slug]", revalidate=9)
    assert app.clear_isr_policies() == 1
    assert app.clear_isr_policies() == 0
    assert app.get_isr_policy("/blog/[slug]") == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}


def test_runtime_override_applies_to_next_generation(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:1</p>")
                assert app.isr.store.lookup("/blog/alpha")[0] == "fresh"
                assert module.calls == 1

                app.set_isr_policy("/blog/[slug]", revalidate=0.2, stale=5)
                # Already-cached entries keep the lifetimes they were
                # stored with, so this is still a fresh base-policy hit.
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:1</p>")
                assert module.calls == 1

                assert await app.revalidate("/blog/alpha") is True
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:2</p>")
                assert app.isr.store.lookup("/blog/alpha")[0] == "fresh"
                # Under the base 30s policy this would stay fresh; under
                # the 0.2s override it goes stale, then regenerates.
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "stale")
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:2</p>")
                await until(lambda: "/blog/alpha" not in app.isr._inflight)
                assert app.isr.store.lookup("/blog/alpha") == (
                    "fresh", page_html("<p>alpha:3</p>").encode())
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:3</p>")

                # Clearing the override restores base-policy generations.
                assert app.clear_isr_policy("/blog/[slug]") is True
                assert await app.revalidate("/blog/alpha") is True
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:4</p>")
                await asyncio.sleep(0.5)
                assert app.isr.store.lookup("/blog/alpha")[0] == "fresh"
        finally:
            await app.isr.close()

    asyncio.run(run())
