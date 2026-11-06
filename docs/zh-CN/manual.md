# PyNext 使用手册 — 简体中文

[English](../en/manual.md) · [한국어](../ko/manual.md) · [日本語](../ja/manual.md)

本手册适用于 **v0.4.0**。PyNext 是基于 Python 和 Rust 的实验性全栈框架，不兼容 Next.js、React 或 React Server Components。当前原型不应直接暴露到公共互联网，也不应运行不受信任的应用。请参阅[版本记录](../../CHANGELOG.md)和[后续计划](../PRODUCTION_ROADMAP.md)。

## 环境要求与首次运行

需要 CPython 3.13、Rust/Cargo 和 [uv](https://docs.astral.sh/uv/)。其他 Python 版本及部署平台尚未得到充分验证。示例使用 island，因此还需要 Node.js 18 或更高版本和 npm，以安装 esbuild 0.28.1。

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout v0.4.0
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

可访问 http://127.0.0.1:3000/、/about、/guides/intro、/news/example、/hydrate、/strategies 和 /api/users。使用 Ctrl+C 停止服务器。

`pynext dev --dir examples/basic` 不经构建直接运行源码，但**不会自动重载文件**，修改后须重启。`pynext start` 使用构建 manifest；受跟踪的源码发生变化后必须重新构建。不使用 island 的应用不需要 Node 和 esbuild。以上说明通过仓库源码安装，并不表示项目已发布到 PyPI。

## 项目结构和路由

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

`page.py` 导出带装饰器的 `Page`，`route.py` 导出 HTTP 处理器，`layout.py` 包裹下级页面。优先级为静态路径 > `[slug]` > `[...path]`。单个参数是字符串，catch-all 参数是字符串列表。可选 catch-all、路由组、并行/拦截路由和客户端导航尚未实现。

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)` 构造 HTML 树并转义字符串。SSR 会缓冲整个响应；框架不提供插入任意原始 HTML 的 API。

## API 请求与错误

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

`request.query` 中每个键对应一个值列表，例如 `request.query.get("q", [""])[0]`。原始查询和百分号解码结果中的无效 UTF-8、超过 1,000 个查询字段、无效 JSON 及非有限 JSON 常量会引发 `BadRequest`；ASGI 和 Axum 返回不含内部详情的 HTTP 400。只有在**读取对应值时**才进行解析。其他未处理异常返回 HTTP 500。

响应默认使用 Content Security Policy `script-src 'self'; object-src 'none'`。该策略限制浏览器可加载的脚本，添加客户端代码时应予以考虑。响应头和媒体类型中的控制字符会被拒绝。

## SSR、SSG 与 ISR

- `@page()`：每次请求时渲染。
- `@page(render="ssg")`：构建时渲染。动态 SSG 路由须导出返回字典列表的 `generate_static_params()`；生产模式下未生成的路径返回 404。
- `@page(render="isr", revalidate=30, stale=120)`：按需生成公共页面，在新鲜期内复用；过期后在 stale 窗口内返回旧内容，同时只运行一次重新生成。时间单位为秒。

~~~python
# app/guides/[slug]/page.py
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

SSG 和 ISR 页面函数不能接收 `request`。它们仅适合公共内容；请确认环境变量和外部数据可以共享。ISR 不把匿名请求的查询字符串计入缓存键。带 Cookie 或 Authorization 的请求会绕过共享 ISR 缓存并返回 `private, no-store`。缓存大小有限，且**只存在于各自进程的内存中**，不在工作进程之间共享或失效。更新 SSG 内容需要重新构建。

## 公共文件与 islands

`public/styles/site.css` 通过 `/styles/site.css` 公开。构建时会生成快照并校验完整性，但生产启动时仍需要原始文件。隐藏文件、符号链接和冲突路径会被拒绝。开发模式下修改公共文件后也需要重启服务器。

Island 将 Python 声明与 `islands/` 内的 JavaScript 或 TypeScript 模块结合：

~~~python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island(
        "counter", props={"initial": 1},
        fallback=h("p", {}, "Enable JavaScript to use the counter."),
    ))
~~~

对应的 `islands/counter.ts` 导出 `mount(element, props)`，并可返回清理函数。Props 是公开的 JSON 对象，最大 16 KiB，请勿放入秘密信息。构建需要 esbuild；不支持嵌套 island、npm 包导入、外部导入和 CSS 导入。`/_pynext` 是保留路径。

如需复用现有服务端 DOM，请提供服务端 fallback、设置 `hydrate=True`，并在模块中导出 `render`、`hydrate` 和 `mount`。参见 [hydrated-counter.ts](../../examples/basic/islands/hydrated-counter.ts)。如果两侧的树不一致，框架会重新 mount 该 island，输入状态或焦点可能丢失。

默认激活策略为 `load`。`visible` 在进入视口时激活；`idle` 请求空闲回调，并请求 2 秒 timeout，但实际执行可能更晚。相关 API 不可用时会退回 idle 或定时器。激活前的点击或输入事件**不会重放**。

## ASGI、超时与安全性

通过 Uvicorn 以开发模式启动示例 ASGI 应用：

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

已构建的应用需使用 `create_app(project_path, production=True)`。ASGI 请求超时由所使用的服务器管理。Axum 适配器等待 Python 最多 30 秒，之后请求取消并返回 504。已经在线程中运行的同步函数可能在 504 之后继续执行并产生写入等副作用。请确保写入操作幂等，并为数据库与外部服务设置单独超时。当前没有强制进程隔离；ISR 取消也有相同限制。

流式响应、WebSocket、分布式 ISR、分布式限流、OpenTelemetry 和生产环境加固尚未实现。请参阅[生产路线图](../PRODUCTION_ROADMAP.md)中的计划与验收条件。

## 测试与故障排查

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

- 若提示源码已更改，请重新执行 `pynext build`。
- 若找不到 esbuild，请在仓库根目录运行 `npm ci`，或通过 `PYNEXT_ESBUILD` 指定可执行文件。
- 若动态 SSG 路径返回 404，请将其加入 `generate_static_params()` 并重新构建。
- 若 hydration 不匹配，请确保服务端 fallback 与客户端 `render` 产生一致且确定的结果。
- 若工作进程之间的 ISR 结果不同，请检查进程内缓存的限制。
