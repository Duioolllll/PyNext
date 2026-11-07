# PyNext manual — English

[한국어](../ko/manual.md) · [日本語](../ja/manual.md) · [简体中文](../zh-CN/manual.md)

This manual describes the development branch; the latest published release is **v0.6.0a1**. PyNext is an experimental Python + Rust full-stack framework. It is not compatible with Next.js, React, or React Server Components. Do not expose this prototype directly to the public internet or run untrusted applications with it. See the [version history](../../CHANGELOG.md) and [future work](../PRODUCTION_ROADMAP.md).

## Requirements and first run

Use CPython 3.13, a Rust toolchain with Cargo, and [uv](https://docs.astral.sh/uv/). Other Python versions and deployment platforms have not been fully verified. The example uses islands and additionally requires Node.js 18 or newer and npm to install esbuild 0.28.1.

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout main
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

Open http://127.0.0.1:3000/, /about, /guides/intro, /news/example, /hydrate, /strategies, and /api/users. Stop the server with Ctrl+C.

`pynext dev --dir examples/basic` serves source without a build, but **does not reload changed files**; restart it after edits. `pynext start` uses the build manifest and fails if tracked source files changed; rebuild first. For an app without islands, Node and esbuild are not required. The repository is the installation source; no PyPI publication is implied.

## Project structure and routing

~~~text
app/
  layout.py
  page.py
  blog/[slug]/page.py
  docs/[...path]/page.py
  api/users/route.py
public/
  styles/site.css
islands/
  counter.ts
~~~

A `page.py` exports a decorated `Page`; a `route.py` exports decorated HTTP handlers. `layout.py` wraps descendant pages. Static segments take precedence over `[slug]`, which takes precedence over `[...path]`. A single parameter is a string; catch-all parameters are lists of strings. Optional catch-all routes, route groups, parallel/intercepting routes and client-side navigation are not implemented.

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)` builds an HTML tree; strings are escaped. It is buffered SSR, not an arbitrary raw-HTML API.

## Application imports and migration

Page, layout and API modules may use package-relative imports, such as `from .helpers import title`. Each app receives its own module namespace; the framework no longer prepends the project root to process-wide `sys.path`. If a root-based absolute import stops working after upgrading from v0.5, change it to a relative import or install your project as a Python package. Third-party packages must be installed in the environment.

## API requests and errors

~~~python
# app/api/echo/route.py
from pynext import BadRequest, JSONResponse, route

@route("POST")
async def echo(request):
    data = await request.json()
    if not isinstance(data, dict) or "name" not in data:
        raise BadRequest("name is required")
    return JSONResponse({"name": data["name"]}, status=201)
~~~

`request.query` returns a mapping to lists of values, for example `request.query.get("q", [""])[0]`. Reading it rejects invalid UTF-8, including malformed percent-decoded UTF-8, and more than 1,000 query fields. `await request.json()` rejects invalid JSON and non-finite JSON constants. These operations raise `BadRequest`, which the ASGI and Axum adapters return as HTTP 400 with a generic body. Parsing is lazy: a handler that never reads the value does not trigger validation. Other unhandled errors become HTTP 500.

Responses default to the Content Security Policy `script-src 'self'; object-src 'none'`. This limits which scripts a browser may load; account for it when adding client code. Response headers and media types reject control characters.

## Streaming API responses

Return `StreamingResponse` from an API handler with an asynchronous iterable of strings or bytes. ASGI and Axum send chunks incrementally and close the iterator on completion, disconnect, or error. Each chunk is limited to 1 MiB. Do not set `Content-Length` or `Transfer-Encoding`; the server selects protocol framing. Axum allows one queued chunk and stops a stalled chunk or backpressure wait after 30 seconds. Once response headers are sent, an error ends the stream rather than changing its status. HEAD does not iterate the stream. Page SSR is still buffered; this is not React/RSC streaming.

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield "first\n"
        yield "second\n"
    return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")
~~~

## SSR, SSG and ISR

- `@page()` renders on each request.
- `@page(render="ssg")` renders during build. Dynamic SSG routes must export `generate_static_params()` returning a list of dictionaries. Ungenerated paths return 404 in production.
- `@page(render="isr", revalidate=30, stale=120)` generates a public page on demand, reuses it while fresh, and serves stale content while one regeneration runs. Times are seconds.

~~~python
# app/guides/[slug]/page.py
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

SSG and ISR page functions cannot accept `request`. Use them only for public content: ensure environment variables and external data are safe to share. ISR ignores anonymous query strings in its cache key. Requests with a Cookie or Authorization header bypass the shared ISR cache and receive `private, no-store`. The cache is bounded and **process-local**; workers do not share or invalidate entries. Rebuild SSG pages to update them.

A build produces at most 10,000 SSG pages. Development mode renders SSG pages for each request. ISR pages cannot export `generate_static_params()` and permit at most 128 concurrent generation tasks per process.

## Public files and islands

Files under `public/` are public at the corresponding URL, e.g. `public/styles/site.css` is `/styles/site.css`. The build snapshots and checks these files; source files are still needed at production startup. Hidden files, symlinks and conflicting paths are rejected. Restart the development server after changing a public file.

An island pairs a Python declaration with a JavaScript or TypeScript module in `islands/`:

~~~python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island(
        "counter", props={"initial": 1},
        fallback=h("p", {}, "Enable JavaScript to use the counter."),
    ))
~~~

The matching `islands/counter.ts` exports `mount(element, props)` and may return a cleanup function. Props are public JSON objects limited to 16 KiB; never put secrets in them. The build uses esbuild; nested islands, npm-package imports, external imports and CSS imports are not supported. `/_pynext` is reserved.

Use `hydrate=True` with server-rendered fallback and a module exporting `render`, `hydrate` and `mount` to reuse matching DOM. See [hydrated-counter.ts](../../examples/basic/islands/hydrated-counter.ts). If the trees differ, the island is mounted again and input state or focus can be lost.

The default activation strategy is `load`. `visible` activates when the island enters the viewport; `idle` requests an idle callback with a 2-second timeout. Unsupported observer/idle APIs fall back to idle/timer scheduling. An idle callback can run later than requested, and clicks or input before activation are **not replayed**.

## ASGI, timeouts and safety

To use the included development ASGI app with Uvicorn:

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

For a built app, create the ASGI app with `create_app(project_path, production=True)`. ASGI server timeout settings are managed by that server. The Axum adapter waits up to 30 seconds for Python and requests cancellation before returning 504. A synchronous function already running in a thread can continue after the 504, including writes. Use idempotent writes and explicit database/upstream timeouts; this release provides no forced process isolation. ISR cancellation has the same limitation.

Streaming SSR, WebSockets, distributed ISR, distributed rate limiting, OpenTelemetry and production hardening are not implemented. See [production roadmap](../PRODUCTION_ROADMAP.md) for proposed work and its acceptance criteria.

Both adapters limit request bodies to 1 MiB and return 413 when exceeded. By default Axum permits 16 simultaneous body reads with a 10-second body deadline (408), 64 Python handlers, and at most the smaller of the handler limit and 64 waiting requests; full queues return 503. The execution and body settings can be changed with the CLI. HTTP/1.1 header reads have a 10-second deadline, but total connections remain unlimited. External ASGI servers manage their own timeouts. Non-root paths ending in a slash redirect with 308. Each compiled island is limited to 2 MiB, with 32 MiB total.

## Tests and troubleshooting

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

After building the example, run the Chromium check with:

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

Run `uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4` for a small closed-loop smoke benchmark. It reports attempted and successful 2xx requests per second separately, error and status counts, p50/p95/p99 for all attempts and successes, response sizes, CPU and peak RSS. In the default mode the CPU and lifetime peak RSS cover both the server and the load generator; they are not server-only measurements. For a separately running local server, use `--base-url http://127.0.0.1:3000`. On Linux, add `--server-pid <PID>` to sample that process and its threads, excluding child workers. CI saves a bounded benchmark result measured in production mode (`--production`, using the built example) but does not yet enforce performance or memory regression thresholds.

- If startup says sources changed, rerun `pynext build`.
- If esbuild is missing, run `npm ci` at the repository root or set `PYNEXT_ESBUILD` to an executable.
- On Windows, npm's esbuild wrapper runs through Node. If setting `PYNEXT_ESBUILD` directly, use the native `esbuild.exe`, not a `.cmd` or `.bat` file.
- If an SSG dynamic path returns 404, add it to `generate_static_params()` and rebuild.
- If hydration reports a mismatch, make server fallback and client `render` output deterministic and equivalent.
- If an ISR page looks different across workers, remember each process has its own cache.
