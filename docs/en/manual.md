# PyNext v0.1.0 — en

This experimental release was reconstructed from a preserved development build; the later request-validation fix was backported. It is not Next.js or React compatible. Do not expose it directly to the internet.

Requirements: CPython 3.13, Rust/Cargo and uv.

**Features / 기능 / 機能 / 功能:** file routing, SSR, SSG, public files and process-local ISR.

Not yet included: islands, hydration and lazy activation.

~~~bash
uv sync --extra dev
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

SSG/ISR are for public data; ISR is process-local. Axum returns 504 after 30 seconds, but synchronous handlers already running in a thread can continue. WebSockets, streaming and distributed ISR are unavailable.

~~~python
from pynext import h, page

@page()
def Page():
    return h("h1", {}, "Hello PyNext")
~~~
