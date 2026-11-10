"""Minimum ISR lifetime floor + dev/prod separation (P1-4).

Next.js keeps ``MIN_PRERENDERABLE_EXPIRE = 300`` (``use-cache/constants.ts``):
a page whose total lifetime (``revalidate + stale``) is below the floor is
dynamic, and the dev server serves stale entries until ``expire`` while
retaining them for at least the floor so reloads hit the cache
(``lib/cache-handlers/default.ts``).
"""
import asyncio
import time

import httpx
import pytest

from conftest import page_html
from pynext import create_app, page
from pynext.build import write_manifest
from pynext.cache import (
    MIN_PRERENDERABLE_EXPIRE,
    dev_retention_policy,
    is_prerenderable_policy,
)


def make_counter_page(root, revalidate, stale):
    directory = root / "app" / "counter"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        "from pynext import h, page\n"
        "calls = 0\n"
        f"@page(render=\"isr\", revalidate={revalidate}, stale={stale})\n"
        "async def Page(params):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return h(\"p\", {}, f\"count:{calls}\")\n"
    )
    return directory / "page.py"


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_min_prerenderable_expire_floor():
    assert MIN_PRERENDERABLE_EXPIRE == 300.0
    # Total lifetime below the floor is dynamic.
    assert not is_prerenderable_policy({"revalidate": 1.0, "stale": 60.0})
    assert not is_prerenderable_policy({"revalidate": 239.0, "stale": 60.0})
    # Exactly at the floor is prerenderable.
    assert is_prerenderable_policy({"revalidate": 240.0, "stale": 60.0})
    assert is_prerenderable_policy({"revalidate": 300.0, "stale": 60.0})


def test_dev_retention_policy_extends_stale_to_floor():
    # Short lifetimes are stretched so a reload hits the cache; revalidate
    # keeps its real value.
    assert dev_retention_policy({"revalidate": 1.0, "stale": 60.0}) == {
        "revalidate": 1.0, "stale": 299.0}
    assert dev_retention_policy({"revalidate": 0.5, "stale": 1.0}) == {
        "revalidate": 0.5, "stale": 299.5}
    # At or above the floor the policy is unchanged.
    assert dev_retention_policy({"revalidate": 300.0, "stale": 60.0}) == {
        "revalidate": 300.0, "stale": 60.0}
    assert dev_retention_policy({"revalidate": 240.0, "stale": 60.0}) == {
        "revalidate": 240.0, "stale": 60.0}


def test_production_short_lifetime_isr_still_cached(tmp_path):
    """Production behavior is unchanged: a short-lifetime page still uses
    the ISR cache with its declared policy (the floor is a classification
    rule, not a production enforcement)."""
    make_counter_page(tmp_path, 1, 60)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/counter/page.py")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/counter")
            assert first.status_code == 200
            assert first.text == page_html("<p>count:1</p>")
            assert module.calls == 1
            # The page is cached despite being below the floor.
            assert app.isr.store.lookup("/counter")[0] == "fresh"
            second = await client.get("/counter")
            assert second.status_code == 200
            assert second.text == page_html("<p>count:1</p>")
            assert module.calls == 1

    asyncio.run(run())


def test_production_prerenderable_isr_uses_cache(tmp_path):
    """A page at/above the floor goes through the ISR cache in prod."""
    make_counter_page(tmp_path, 300, 60)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/counter/page.py")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/counter")
            assert first.status_code == 200
            assert first.text == page_html("<p>count:1</p>")
            assert module.calls == 1
            assert app.isr.store.lookup("/counter")[0] == "fresh"
            second = await client.get("/counter")
            assert second.status_code == 200
            assert second.text == page_html("<p>count:1</p>")
            assert module.calls == 1

    asyncio.run(run())


def test_dev_isr_reload_serves_stale_until_expire(tmp_path):
    """Dev serves a stale entry until expire, so a reload hits the cache."""
    make_counter_page(tmp_path, 0.5, 1)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, debug=True)
        module = app._module("app/counter/page.py")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/counter")
            assert first.status_code == 200
            assert first.text == page_html("<p>count:1</p>")
            assert module.calls == 1
            # The dev retention floor keeps the entry: still fresh here,
            # and it stays servable long past its declared stale window.
            assert app.isr.store.lookup("/counter")[0] == "fresh"
            # Wait past revalidate (0.5s) but well inside the dev-retained
            # stale window (299.5s): the reload must serve the stale body.
            await asyncio.sleep(0.6)
            assert app.isr.store.lookup("/counter")[0] == "stale"
            second = await client.get("/counter")
            assert second.status_code == 200
            assert second.text == page_html("<p>count:1</p>")
            # The stale serve triggered exactly one background regeneration.
            await until(lambda: module.calls == 2)
            third = await client.get("/counter")
            assert third.status_code == 200
            assert third.text == page_html("<p>count:2</p>")

    asyncio.run(run())
