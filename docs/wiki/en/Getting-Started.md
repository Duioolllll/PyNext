# Getting started

[Home](Home.md) · [한국어](../ko/Getting-Started.md)

## Prerequisites

- CPython **3.13 or 3.14**
- Rust toolchain and Cargo
- [uv](https://docs.astral.sh/uv/)
- Node.js **18+** and npm **only if your app contains JS/TS islands**

PyNext is published on PyPI as `pynext-framework` (`pip install pynext-framework==0.1.0`); the steps below use this source repository.

## Run the included example

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
uv sync --locked --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

Visit `http://127.0.0.1:3000/` and these sample routes: `/about` (SSG), `/blog/hello` (SSR), `/guides/intro` (dynamic SSG), `/news/example` (ISR), `/api/users`, `/api/stream` and `/hydrate`. Stop with Ctrl+C.

For development **without building**:

~~~bash
uv run pynext dev --dir examples/basic --host 127.0.0.1 --port 3000
~~~

**There is no automatic reload/HMR.** Restart the server after changing source files. Before using `start` again, rerun `build` when tracked app/public/island files change.

## Build a minimal application

Create `my-site/app/page.py`:

~~~python
from pynext import h, page

@page()
def Page():
    return h("main", {}, h("h1", {}, "Hello PyNext"))
~~~

From the checked-out repository with dependencies installed:

~~~bash
uv run pynext build --dir my-site
uv run pynext start --dir my-site --port 3000
~~~

A project without `islands/` needs neither esbuild nor Node. The framework builds a versioned `.pynext/manifest.json`, pre-renders SSG routes and snapshots assets.

## CLI reference

| Command | Purpose |
| --- | --- |
| `pynext build --dir PATH` | Validate routes, compile islands, generate SSG/manifest |
| `pynext dev --dir PATH` | Run from source (manual restart) |
| `pynext start --dir PATH` | Start using a validated build |

Both server commands accept `--host`, `--port`, `--max-concurrent` (64), `--acquire-timeout-ms` (1000), `--body-capacity` (16), and `--body-timeout-ms` (10000). Defaults are in parentheses. Bind to loopback by default; production exposure is not supported.

Next: [Routing](Routing.md) and [Components and layouts](Components-and-Layouts.md).
