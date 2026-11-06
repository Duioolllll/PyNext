# PyNext v0.2.0 — zh-CN

此实验版本根据保留的开发构建重建，并回移了后来的请求验证修复。它不兼容 Next.js 或 React，请勿直接暴露到互联网。

要求：CPython 3.13、Rust/Cargo 和 uv。 构建 island 还需要 Node.js 18+ 和 npm；请在构建前运行 `npm ci`。

**Features / 기능 / 機能 / 功能:** 文件路由、SSR、SSG、公共文件和进程内 ISR, JavaScript/TypeScript island 挂载.

尚未支持：hydration 和延迟激活。

~~~bash
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

SSG/ISR 仅用于公共数据，ISR 缓存按进程隔离。Axum 在 30 秒后返回 504，但已运行的同步函数仍可能继续执行。WebSocket、流式响应和分布式 ISR 尚未实现。

~~~python
from pynext import h, page

@page()
def Page():
    return h("h1", {}, "Hello PyNext")
~~~
