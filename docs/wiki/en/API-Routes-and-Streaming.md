# API routes and streaming

[Home](Home.md) · [한국어](../ko/API-Routes-and-Streaming.md)

An `app/api/users/route.py` exports functions decorated with `@route("METHOD")`. API routes share the file router but are not wrapped by page layouts.

## JSON API

~~~python
from pynext import BadRequest, JSONResponse, route

@route("GET")
async def users():
    return JSONResponse({"users": ["Alice", "Bob"]})

@route("POST")
async def create_user(request):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        raise BadRequest("name is required")
    return JSONResponse({"name": body["name"]}, status=201)
~~~

- `request.method`, `request.path`, `request.params`, `request.headers`, `request.body` and `request.query` expose request data.
- `request.query` maps keys to lists of strings: `request.query.get("q", [""])[0]`. Invalid UTF-8 and more than 1,000 query fields raise `BadRequest` on access.
- `await request.json()` validates JSON; malformed JSON and non-finite constants raise `BadRequest`.
- `BadRequest` becomes a generic HTTP **400**. Unhandled exceptions become **500**.
- Successful handlers must return `Response`, `JSONResponse` or `StreamingResponse`, not bare Python dictionaries.

## Streaming API responses

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield '{"phase":"started"}\n'
        yield '{"phase":"finished"}\n'
    return StreamingResponse(chunks(), media_type="application/x-ndjson")
~~~

ASGI and Axum support async-iterable streaming with a maximum **1 MiB per chunk**; the native adapter bounds pending chunks and manages backpressure. Do not manually set `Content-Length` or `Transfer-Encoding` on streaming responses. Disconnects request iterator cancellation/cleanup; a streaming failure after headers have been sent terminates the stream rather than changing the HTTP status. `HEAD` does not consume the stream.

## HTTP response safety

`Response("ok", status=200, media_type="text/plain; charset=utf-8")` accepts string or bytes bodies. The default Content Security Policy is `script-src 'self'; object-src 'none'`. Headers and media types are validated. The body size limit for incoming requests is **1 MiB**.

For dynamic API examples see [the users endpoint](../../../examples/basic/app/api/users/route.py) and [stream endpoint](../../../examples/basic/app/api/stream/route.py). **WebSockets and streaming page SSR are not available.**
