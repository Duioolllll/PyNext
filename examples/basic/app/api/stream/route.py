import asyncio

from pynext import StreamingResponse, route


@route("GET")
async def stream():
    async def events():
        yield '{"phase":"started"}\n'
        await asyncio.sleep(0.05)
        yield '{"phase":"finished"}\n'

    return StreamingResponse(events(), media_type="application/x-ndjson")
