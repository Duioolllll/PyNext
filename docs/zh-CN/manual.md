# PyNext 使用手册 — 简体中文

[English](../en/manual.md) · [한국어](../ko/manual.md) · [日本語](../ja/manual.md)

本手册适用于当前检出的 **v0.7.0a1** 实验版代码。PyNext 是基于 Python 和 Rust 的实验性全栈框架，不兼容 Next.js、React 或 React Server Components。当前原型不应直接暴露到公共互联网，也不应运行不受信任的应用。请参阅[版本记录](../../CHANGELOG.md)和[后续计划](../PRODUCTION_ROADMAP.md)。

## 环境要求与首次运行

需要 CPython 3.13、Rust/Cargo 和 [uv](https://docs.astral.sh/uv/)。受支持的环境由 CI 矩阵验证：操作系统为 Ubuntu、macOS 与 Windows（最新版本），Python 仅支持 CPython 3.13（3.14 未经验证，不在矩阵中），部署模式为独立 Axum 与 Uvicorn 之上的 ASGI（进程内 ASGI 模式由每个 CI 任务中的 pytest 覆盖）。浏览器冒烟测试在三个操作系统上都会运行。示例使用 island，因此还需要 Node.js 18 或更高版本和 npm，以安装 esbuild 0.28.1。

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout main
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

可访问 http://127.0.0.1:3000/、/about、/guides/intro、/news/example、/hydrate、/strategies 和 /api/users。使用 Ctrl+C 停止服务器。

`pynext dev --dir examples/basic` 不经构建直接运行源码，并在 `app/`、`islands/`、`public/` 或 `pynext.config.py` 变化时在同一端口重启服务器。`pynext start` 使用构建 manifest；受跟踪的源码发生变化后必须重新构建。不使用 island 的应用不需要 Node 和 esbuild。以上说明通过仓库源码安装，并不表示项目已发布到 PyPI。

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

`page.py` 导出带装饰器的 `Page`，`route.py` 导出 HTTP 处理器，`layout.py` 包裹下级页面。优先级为静态路径 > `[slug]` > `[...path]` > `[[...path]]`。单个参数是字符串，catch-all 参数是字符串列表。可选 catch-all `[[...path]]` 匹配零个或多个路径段，因此 `/docs/[[...path]]` 也会以空列表提供 `/docs`。必需 catch-all 与可选 catch-all 不能处于同一层级，否则构建会因冲突失败。并行/拦截路由尚未实现。

括号目录是路由组：`app/(marketing)/page.py` 提供 `/`，组内的布局仍会生效。组名只能包含字母、数字、`_` 和 `-`；两个组映射到同一 URL 时构建失败。

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)` 构造 HTML 树并转义字符串。SSR 默认缓冲整个响应；页面也可以流式传输 UI 数据块（见下文流式 SSR）。框架不提供插入任意原始 HTML 的 API。

## 应用导入与迁移

页面、布局和 API 模块可使用 `from .helpers import title` 等相对导入。每个应用有独立的模块命名空间，框架不再自动将项目根目录加入进程级 `sys.path`。从 v0.5 升级后，如果原先基于根目录的绝对导入失效，请改为相对导入或将项目安装为 Python 包。第三方包需安装在运行环境中。

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

状态变更类 API 请求可以选择启用 CSRF 保护（规范 v0.2 §11）。由于目前还没有认证系统，保护采用 double-submit 模式：应用签发一个不可预测的令牌，存入 `pynext_csrf` Cookie，客户端再以 `X-CSRF-Token` 请求头（或 `csrf_token` 表单字段）回传相同的值。GET、HEAD 和 OPTIONS 不受检查；其他方法若缺少令牌或令牌不匹配，将返回 HTTP 403。

~~~python
from pynext import JSONResponse, csrf_protect, new_csrf_token, route, set_csrf_cookie

@route("GET")
async def get_token():
    token = new_csrf_token()
    return set_csrf_cookie(JSONResponse({"token": token}), token)

@route("POST")
@csrf_protect
async def update(request):
    return JSONResponse({"ok": True})
~~~

除逐个标记处理器外，也可向 `create_app` 传入 `security=SecurityConfig(csrf_enabled=True)`，保护全部状态变更类 API 路由。由于目前没有会话，令牌不与用户会话绑定：它仅证明请求来自能够读取该 Cookie 的页面。与其他 double-submit 方案一样，能够通过 XSS 读取 Cookie 的页面可以绕过它。Cookie 默认使用 `SameSite=Lax`，且不设 `HttpOnly`（JavaScript 需要读取它）；生产环境请设置 `csrf_cookie_secure=True`，使 Cookie 仅通过 HTTPS 发送。

跨源请求默认一律拒绝（规范 v0.2 §11）：未配置来源时，不会发送 `Access-Control-Allow-Origin` 请求头。请通过 `SecurityConfig` 将策略传给 `create_app`：

~~~python
from pynext import SecurityConfig, create_app

app = create_app(".", security=SecurityConfig(
    cors_allowed_origins=("https://example.com",),
    cors_allowed_methods=("GET", "POST"),
    cors_allowed_headers=("Content-Type", "X-CSRF-Token"),
    cors_allow_credentials=False,
    cors_max_age=600,
))
~~~

来自允许来源的预检请求（携带 `Origin` 和 `Access-Control-Request-Method` 的 `OPTIONS`）会返回 204 以及 `Allow` 和 `Access-Control-Allow-*` 请求头；来源、方法或请求头不在允许范围内的预检请求返回 403。来自允许来源的实际跨源请求会带有 `Access-Control-Allow-Origin`（如已配置，还会带有 `Access-Control-Allow-Credentials: true`）；其他来源则不带 CORS 请求头，浏览器会拒绝向调用方页面暴露响应。所有携带 `Origin` 的请求的响应还会带有 `Vary: Origin`。同源请求永远不会被 CORS 拦截，因此即使未配置来源，同源页面也可以携带自定义请求头调用应用自身的 API。`cors_allowed_origins=("*",)` 表示允许任意来源；当 `cors_allow_credentials=True` 时，将原样回显确切来源，而不是 `*`。

静态文件、SSG 页面和已缓存的 ISR 响应由原生服务器直接提供，不带 CORS 请求头；CORS 仅适用于经 Python 分发产生的响应（API 路由和动态渲染页面）。

默认 CSP 为 `script-src 'self'; object-src 'none'`。设置 `SecurityConfig(csp_nonce_enabled=True)` 后，每个动态渲染页面都会获得一个新的按请求生成的 nonce：响应头变为 `script-src 'self' 'nonce-<值>'; object-src 'none'`，同一个 nonce 也会添加到 island 运行时 `<script>` 标签上。nonce 每次响应都不同，请勿自行缓存带有 nonce 的页面。预渲染的 SSG 页面、已缓存的 ISR 响应和静态文件保持不带 nonce 的静态策略：嵌入共享 HTML 的 nonce 会在多个响应中被重复使用，从而失去 nonce 的意义。携带 Cookie 或 Authorization 请求头的 ISR 请求会按请求重新渲染，因此会获得 nonce。

## API 响应流

向 `StreamingResponse` 传入产生字符串或字节的异步 iterable。ASGI 和 Axum 会依次发送数据块，并在完成、断开或出错时关闭迭代器。单块最多 1 MiB；不要手动设置 `Content-Length` 或 `Transfer-Encoding`，协议帧由服务器管理。Axum 最多排队一个数据块，下一块生成与发送等待分别有 30 秒超时。响应头发出后出错会中断流，无法更改状态码。HEAD 不会消费流。尚不支持 React/RSC 流式渲染。

## 流式 SSR

页面函数可以返回 UI 节点的异步 iterable，而不是一棵完整的树。服务器先发送布局外壳，再由 Rust 逐块序列化并按生成顺序发送，最后发送布局的闭合部分。缓冲渲染仍为默认方式。流式传输仅适用于 SSR：SSG 与 ISR 页面需要完整正文，指定流式传输会失败。布局必须恰好渲染一次 children。断开连接时关闭生成器；响应头发出后出错会中断流，而不是更改状态码。[示例页面](../../examples/basic/app/stream/page.py)可在 `/stream` 查看。

~~~python
from pynext import h, page

@page()
async def Page():
    yield h("h1", {}, "Streaming SSR")
    yield h("p", {}, "Each chunk is sent as it is produced.")
~~~

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield "first\n"
        yield "second\n"
    return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")
~~~

## WebSocket

`route.py` 可以在 HTTP 处理函数之外再导出一个 `@websocket` 处理函数。处理函数接收一个 `WebSocket`，在收发消息之前必须调用 `await websocket.accept()`。支持文本和二进制消息往返；对方断开连接时 `receive()` 会抛出 `WebSocketDisconnect`，因此每个连接的清理工作应放在 `finally` 块中。

~~~python
# app/ws/echo/route.py
from pynext import WebSocket, WebSocketDisconnect, websocket

@websocket
async def echo(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            kind, data = await websocket.receive()
            if kind == "text":
                await websocket.send_text(data)
            else:
                await websocket.send_bytes(data)
    except WebSocketDisconnect:
        pass
~~~

参见 [echo 示例](../../examples/basic/app/ws/echo/route.py)。没有 WebSocket 处理函数的路由会以 close 代码 1008 拒绝握手；未调用 accept 就返回的处理函数会以代码 1000 关闭连接。单条消息上限为 64 KiB，更大的消息会以 close 代码 1009 拒绝。close 代码须在 1000~4999 之间，且不能是保留的 1005、1006、1015；reason 不超过 123 个 UTF-8 字节。不支持子协议协商、广播或 pub/sub，也没有连接间共享状态：每个连接只能收到自己的消息。仅含 WebSocket 的 `route.py` 对普通 HTTP 请求返回 405。ASGI 适配器与 Axum 独立服务器均支持 WebSocket；Axum 会在处理函数运行前完成握手，因此那里的 `accept()` 仅起标记作用。

## SSR、SSG 与 ISR

- `@page()`：每次请求时渲染。返回 UI 节点的异步 iterable 时改为流式传输，不再缓冲整个响应（见流式 SSR）。
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

SSG 和 ISR 页面函数不能接收 `request`。它们仅适合公共内容；请确认环境变量和外部数据可以共享。ISR 不把匿名请求的查询字符串计入缓存键。带 Cookie 或 Authorization 的请求会绕过共享 ISR 缓存并返回 `private, no-store`。默认缓存大小有限，且**只存在于各自进程的内存中**，不在工作进程之间共享或失效。使用 `--cache-backend sqlite` 启动，或用 `create_app(..., cache_backend="sqlite", cache_path=...)` 创建，即可让所有工作进程共享一个 SQLite 文件（WAL）：过期内容的重新生成通过带过期时间和属主令牌的租约实现每个键仅一个进程执行，版本围栏可防止缓慢的生成覆盖较新的结果。每个工作进程在共享文件前仍保留一份进程内缓存，因此其他进程的失效会立即作用于共享条目，但本地副本可能保留至剩余 TTL 结束。带 `Set-Cookie` 或 `Cache-Control: private/no-store` 的响应不会存入共享缓存。Redis 适配器是未来的可选方案，尚未实现。更新 SSG 内容需要重新构建。

SSG 构建最多生成 10,000 个页面；开发模式下每次请求都会渲染 SSG 页面。ISR 页面不能导出 `generate_static_params()`，每个进程最多并发执行 128 个生成任务。

已授权的 API 处理函数可调用 `await request.revalidate("/news/example")`；持有应用实例的代码可调用 `await app.revalidate(path)`。请传入不含查询字符串、已解码的 ISR 路径。如果移除了缓存或使进行中的生成失效，返回 `True`。已有请求仍可能收到旧生成结果，但该结果不会重新覆盖缓存。内存后端下失效只作用于当前进程，不会传播到其他工作进程；SQLite 后端下也会删除共享条目，但其他工作进程的进程内副本可能保留至剩余 TTL 结束。对外提供此操作的端点需要认证和授权。

## 公共文件与 islands

公共文件、SSG 页面和 island 包使用基于 SHA-256 内容哈希的强 ETag。GET/HEAD 支持 `If-Match` 与 `If-None-Match`；GET 支持单个字节 `Range` 和强 ETag `If-Range`，适用时返回 206 或 416。多个或格式错误的范围会被忽略并返回完整内容。暂不支持基于日期的验证或 ISR 响应的范围请求。

`public/styles/site.css` 通过 `/styles/site.css` 公开。构建时会生成快照并校验完整性，但生产启动时仍需要原始文件。隐藏文件、符号链接和冲突路径会被拒绝。开发模式下修改公共文件后服务器会自动重启。

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

默认激活策略为 `load`。`visible` 在进入视口时激活；`idle` 请求空闲回调，并请求 2 秒 timeout，但实际执行可能更晚。相关 API 不可用时会退回 idle 或定时器。这三种策略不会重放激活前的事件。

设置 `hydrate=True, strategy="interaction"` 后，用户点击 `<button type="button">` 会激活 island。参见[示例页面](../../examples/basic/app/interaction/page.py)和[模块](../../examples/basic/islands/interaction-counter.js)。加载器为该 island 注册监听器后，最多缓存 16 次受支持的点击，在 hydration 成功后按顺序重放；超出的点击会被丢弃并产生警告。链接、表单提交、输入、label 和可编辑区域的事件不会被拦截或重放；监听器注册前的事件也不会被捕获。DOM 不匹配、元素被移除或激活失败时会丢弃待处理事件。重放事件是 `isTrusted=false` 的合成事件，不能保证需要浏览器用户激活的操作；island 外的捕获监听器可能同时观察到原始事件和重放事件。

## 客户端导航

在加载客户端运行时的页面（带有 island 的页面）上，点击同源链接时无需整页刷新即可跳转：运行时会获取目标文档、替换页面主体，并通过 `pushState` 更新历史记录；浏览器的后退/前进按钮也是同样的工作方式。链接会在悬停、键盘聚焦和进入视口时预取（最多 20 个页面，每个 1 MiB）。任何失败——非 200 响应、非 HTML 响应或获取错误——都会回退到整页导航，因此页面永远不会损坏。带有 `target="_blank"` 或 `download` 的链接、跨源链接和同页哈希链接保持正常的浏览器行为，禁用 JavaScript 时也是如此。`window.__pynextNav` 提供用于调试的 `navigate`、`prefetch` 和 `isPrefetched`。没有 island 的页面不加载运行时，因此部分导航在那里不起作用。

## Python 客户端 DSL（实验性）

`pynext.client_dsl.compile_client_dsl(source)` 可将一小部分固定的 Python 子集编译为 JavaScript（规范 §7.3）。支持的内容：字面量（JavaScript 安全整数范围内的 int、float、str、bool、None、list、tuple、dict）、算术运算（`+ - * / // % **`）、比较、`and`/`or`/`not`、`if`/`else`、对 `range()` 或列表的 `for`、`while`、函数定义、`len`、`str`、`int`、`float`、`bool`、`abs`、`min`、`max`、`list`、`print`、`.append()`、索引、切片以及单子句列表推导式。除此之外的所有写法都会抛出 `ClientDSLCompileError`：import、反射、文件 I/O、类、lambda、`try`、f-string，以及 `open`、`os`、`Request`、`h` 等服务器专用名称都会在编译时被拒绝，因此服务器代码无法混入浏览器包。完整列表与已知的语义差异（例如 `str(1.0)` 会丢掉末尾的 `.0`）见 `pynext.client_dsl.SUPPORTED` 与 `KNOWN_DIFFERENCES`，另见[示例](../../examples/basic/client_dsl_example.py)。该 DSL 是独立的编译器，不会自动打包进 island。

## ASGI、超时与安全性

通过 Uvicorn 以开发模式启动示例 ASGI 应用：

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

已构建的应用需使用 `create_app(project_path, production=True)`。ASGI 请求超时由所使用的服务器管理。Axum 适配器等待 Python 最多 30 秒，之后请求取消并返回 504。已经在线程中运行的同步函数可能在 504 之后继续执行并产生写入等副作用。请确保写入操作幂等，并为数据库与外部服务设置单独超时。进程隔离可通过 `--workers`（见下文）获得；单进程没有该能力。ISR 取消也有相同限制。

工作进程间即时失效传播、分布式限流和生产环境加固尚未实现。安装可选包并启用追踪后会生成 OpenTelemetry span（见可观测性一节）。ASGI 一致性由 `tests/test_asgi_conformance.py` 验证（HTTP 事件顺序、分块请求体、断开连接、包含失败路径的 lifespan）。请参阅[生产路线图](. 请参阅[生产路线图](../PRODUCTION_ROADMAP.md)中的计划与验收条件。

两个适配器的请求体上限均为 1 MiB，超出返回 413。Axum 默认同时接收 16 个请求体、接收期限 10 秒（408），Python 执行槽为 64 个，等待队列上限是执行槽数与 64 中较小的值；队列满返回 503。执行和请求体设置可通过 CLI 调整。HTTP/1.1 请求头读取期限为 10 秒，但连接总数没有限制。外部 ASGI 服务器的超时由其自身管理。非根路径末尾的 `/` 会以 308 重定向。编译后的 island 单个上限为 2 MiB，总计 32 MiB。

## 可观测性

结构化日志默认开启：`pynext` 日志器每行输出一个 JSON 对象，包含 `timestamp`、`level`、`message`、`request_id`、`trace_id`（收到 `traceparent` 请求头时）、`route`、`status` 和 `latency_ms`。每个请求都会获得一个请求 ID：传入的 `X-Request-Id` 请求头有效时会被原样回显，否则生成新 ID，在整个请求处理过程中传播，并通过 `x-request-id` 响应头返回。切勿在日志消息中放入请求头、Cookie、正文或 `Authorization` 值：JSON 格式化器会丢弃名称类似凭据的附加字段，访问日志只记录路由模式（例如 `/blog/[slug]`），绝不记录原始 URL。

指标在内存中收集，默认开启。`app.metrics.snapshot()` 返回按路由模式统计的请求数与状态码、按路由统计的延迟 p50/p95/p99、Python 执行队列长度与等待时间、Python 执行时间与 Rust 渲染时间对比、ISR 缓存 hit/miss/stale 与再生成成功/失败/已开始、Worker 启动/超时/不可用、响应大小与 island 包大小。`app.metrics.prometheus_text()` 以 Prometheus 文本格式暴露相同的计数器。延迟样本按路由上限为 2048 个。Rust 不经过 Python 直接提供的全新 ISR 命中不会计入此处；仅统计到达 Python 分发的 ISR 结果。

OpenTelemetry 追踪默认关闭且为可选项：安装 `opentelemetry-api`（以及自选的 SDK/导出器），并用 `--tracing` 或 `PYNEXT_TRACING=1` 启用。启用后会创建 `pynext.request` 与 `pynext.render` span，并传播传入的 W3C `traceparent`/`tracestate` 上下文。未安装相关包时，追踪为 no-op，不会破坏任何功能。

可通过 CLI 标志 `--structured-logs` / `--no-structured-logs`、`--metrics` / `--no-metrics`、`--tracing` / `--no-tracing`，环境变量 `PYNEXT_STRUCTURED_LOGS`、`PYNEXT_METRICS`、`PYNEXT_TRACING`、`PYNEXT_LOG_LEVEL`，或在代码中向 `create_app(..., observability=ObservabilityConfig(...))` 传入配置来进行设置。

默认可观测性设置（结构化日志 + 指标开启，追踪关闭）的实测开销已用 `benchmarks/bench.py --mode axum --requests 1000 --concurrency 10 --path / --production` 在已构建的示例上各测 2 次。由于同一 VM 上还有其他工作在并行构建与测试，绝对延迟被放大，运行间波动较大。开启：25.4 / 14.2 req/s，p50 309 / 586 ms，进程 CPU 4.79 / 5.02 秒。关闭（`PYNEXT_STRUCTURED_LOGS=0 PYNEXT_METRICS=0`）：18.7 / 18.5 req/s，p50 437 / 478 ms，CPU 5.01 / 4.69 秒。均值分别为开启 19.8 req/s、p50 447 ms，关闭 18.6 req/s、p50 457 ms，未观测到超出运行间波动的开销（开启的运行还向 stderr 写了约 1000 行 JSON 日志）。如需更严格的数值，请在空闲机器上重新测量。

## 限流

限流是可选项，采用按进程的令牌桶：`pynext start --rate-limit 100 --rate-limit-window 60` 表示每个客户端 IP 每 60 秒允许 100 个请求（突发量即上限）。超限的动态请求返回 429，并带有以秒为单位的 `Retry-After` 头。键为客户端 IP；只有 `--trusted-proxy`（IP 或 CIDR，可重复指定）列出的对端发来的 `X-Forwarded-For` 才会被信任，不受信任代理的该头部会被忽略，因为它可以被伪造。代码中使用 `create_app(path, rate_limit=RateLimiter(100, 60, trusted_proxies=["10.0.0.0/8"]))`。桶按进程划分，重启后消失；分布式限流尚未实现。静态文件和新鲜的 ISR 页面在到达 Python 桥接之前已由 Rust 层返回，因此不受限流。

多工作进程：`pynext start --workers 4` 会运行一个监管器，启动 4 个共享同一端口的工作进程（SO_REUSEPORT，仅限 Unix，需要固定 `--port`）。监管器会重启已退出或心跳停止的工作进程，并将 SIGINT/SIGTERM 转发给全部工作进程以实现优雅关闭（最多 5 秒，之后 SIGKILL）。每个工作进程都是独立进程，拥有各自的解释器、事件循环、ISR 缓存和限流桶，状态不在进程间共享。若工作进程在处理请求中途死亡，该请求会失败，客户端需要重试。默认为 1 个工作进程，与此前的单进程行为完全相同。阻塞事件循环的处理函数不会停止工作进程的心跳线程，因此卡住的处理函数由 30 秒请求超时处理，而不会触发重启。

## 测试与故障排查

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

路由器与 HTML 序列化除人工挑选的用例外，还有基于属性的测试：Python 侧用 hypothesis（`tests/test_property_router_render.py`，固定种子、40 个用例以保持 CI 快速），Rust 侧用 proptest（`crates/pynext-router` 与 `crates/pynext-native` 的单元测试，各 64 个用例），覆盖路由匹配往返、转义无泄漏、void 标签处理与任意树渲染。`embed` 虽在 void 列表中，但也在屏蔽标签列表中，因此无法渲染，已排除在测试之外。

ASGI 一致性由 `tests/test_asgi_conformance.py` 验证：HTTP 事件顺序、多分块请求体与 1 MiB 大小边界、读取中与慢处理执行期间的断开连接、包含失败时 `lifespan.startup.failed` / `lifespan.shutdown.failed` 的 lifespan 启动/关闭，以及流式响应帧顺序。lifespan 失败时发送 `.failed` 消息是随该套件一并修复的行为。

构建示例后，可运行 Chromium 检查：

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

运行 `uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4` 可执行小规模闭环负载检查。结果分别记录所有尝试和成功的 2xx 请求每秒数量、错误与状态码、全部及成功请求的 p50/p95/p99、响应大小、CPU 和峰值 RSS。默认模式的 CPU 和进程生命周期峰值 RSS 同时包含服务器与负载生成器，并非仅服务器数据。对单独运行的本地服务器可指定 `--base-url http://127.0.0.1:3000`；在 Linux 上还可用 `--server-pid <PID>` 单独采样该进程及其线程的 CPU，不包括子工作进程。CI 会按 OS × 部署模式（Axum/Uvicorn）矩阵任务分别保存以生产模式（`--production`，使用已构建的示例）测得的小规模负载结果，但尚未设置性能或内存回归自动阈值。`--compare` 会以相同条件依次运行 axum、uvicorn 与 starlette 三个 loopback HTTP 组；2026-11-08 记录的一次比较（`/api/users`，1000 个请求，并发 10）在 `benchmarks/results-2026-11-08.json` 中。模式与注意事项见 `docs/BENCHMARKING.md`。

- 若提示源码已更改，请重新执行 `pynext build`。
- 若找不到 esbuild，请在仓库根目录运行 `npm ci`，或通过 `PYNEXT_ESBUILD` 指定可执行文件。
- Windows 上的 npm esbuild 通过 Node 运行。直接设置 `PYNEXT_ESBUILD` 时，请指定原生 `esbuild.exe`，不要使用 `.cmd` 或 `.bat` 文件。
- 若动态 SSG 路径返回 404，请将其加入 `generate_static_params()` 并重新构建。
- 若 hydration 不匹配，请确保服务端 fallback 与客户端 `render` 产生一致且确定的结果。
- 若工作进程之间的 ISR 结果不同，请检查缓存后端：内存后端按进程隔离；SQLite 后端下各工作进程的进程内副本也可能在共享失效后保留至 TTL 结束。
