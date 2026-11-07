import asyncio

from pynext import h, page


@page()
async def Page():
    yield h("h1", {}, "Streaming SSR")
    await asyncio.sleep(0.05)
    yield h("p", {}, "The layout shell arrived first; this chunk was rendered after it.")
    await asyncio.sleep(0.05)
    yield h("p", {}, "Each chunk is serialized by Rust and sent as it is produced.")
