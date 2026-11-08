"""Same-feature Starlette reference app for the benchmark comparison.

Finding #55 (spec v0.2 section 14.1): the comparison groups must include a
Python ASGI framework implementing the same features. This app mirrors the
subset of the PyNext example app that the comparison measures:

* ``GET /`` -- a small SSR-style HTML page with the same heading and link
  as the example home page (the PyNext page additionally embeds an island
  placeholder and its client runtime script tag; the bodies are therefore
  close in shape but not byte-identical, and ``response_bytes`` in the
  results shows the difference).
* ``GET /about`` -- a small static HTML page.
* ``GET /blog/{slug}`` -- a dynamic HTML page echoing the slug.
* ``GET /api/users`` -- ``{"users": ["Alice", "Bob"]}``.
* ``POST /api/users`` -- parses ``{"name": ...}`` and returns it with 201.

The app is served with Uvicorn over loopback HTTP by ``bench.py``
(``--mode starlette``), exactly like the PyNext ``uvicorn`` group, so the
transport is comparable. It is a reference implementation, not a claim
about Starlette in general: same process shape, one worker, no database.
"""

import html

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


def _page(title, body):
    return (
        "<!DOCTYPE html><html><head><title>"
        + html.escape(title)
        + "</title></head><body><main><h1>"
        + html.escape(title)
        + "</h1><p>"
        + body
        + "</p></main></body></html>"
    )


async def home(request):
    return HTMLResponse(_page(
        "Hello PyNext!",
        'Python pages, Rust routing and HTML rendering. '
        '<a href="/blog/hello">Read a post</a>',
    ))


async def about(request):
    return HTMLResponse(_page("About", "A small static page."))


async def blog_post(request):
    slug = html.escape(request.path_params["slug"])
    return HTMLResponse(_page("Blog post", f"<p>{slug}</p>"))


async def list_users(request):
    return JSONResponse({"users": ["Alice", "Bob"]})


async def create_user(request: Request):
    data = await request.json()
    return JSONResponse({"name": data["name"]}, status_code=201)


app = Starlette(routes=[
    Route("/", home, methods=["GET"]),
    Route("/about", about, methods=["GET"]),
    Route("/blog/{slug}", blog_post, methods=["GET"]),
    Route("/api/users", list_users, methods=["GET"]),
    Route("/api/users", create_user, methods=["POST"]),
])
