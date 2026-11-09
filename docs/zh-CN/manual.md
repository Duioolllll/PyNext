# PyNext 使用手册 — 简体中文

[English](../en/manual.md) · [한국어](../ko/manual.md) · [日本語](../ja/manual.md)

本手册适用于当前检出的 **0.1.0** 实验版本代码。PyNext 是基于 Python 和 Rust 的实验性全栈框架，不兼容 Next.js、React 或 React Server Components。当前原型不应直接暴露到公共互联网，也不应运行不受信任的应用。请参阅[版本记录](../../CHANGELOG.md)和[后续计划](../PRODUCTION_ROADMAP.md)。

> 本手册是英文手册的翻译。如内容不一致，以英文手册为准。功能列表由代码自动生成 — 参见 [FEATURES](../FEATURES.md)。

## 环境要求与首次运行

需要 CPython 3.13 或 3.14、Rust/Cargo 和 [uv](https://docs.astral.sh/uv/)。受支持的环境由 CI 矩阵验证：操作系统为 Ubuntu、macOS 与 Windows（最新版本），Python 为 CPython 3.13 和 3.14，部署模式为独立 Axum 与 Uvicorn 之上的 ASGI（进程内 ASGI 模式由每个 CI 任务中的 pytest 覆盖）。浏览器冒烟测试在三个操作系统上都会运行。示例使用 island，因此还需要 Node.js 18 或更高版本和 npm，以安装 esbuild 0.28.1。

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

开发监视器使用原生文件通知和文件哈希，必要时回退到轮询。修改 JS/TS island 会重新构建并替换受影响的模块，无需刷新整个页面；旧清理函数会执行，该 island 的状态可能重置。修改其他源码会重启服务器并刷新打开的页面。错误修改保留上一个正常版本。`--port 0` 分配的端口在重启后保持不变。

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

状态变更类 API 请求适用 CSRF 保护（规范 v0.2 §11）。启用会话中间件后，全部状态变更类 API 路由会自动受到保护，令牌绑定到服务端会话。没有会话中间件时默认关闭，采用 double-submit 模式：应用签发一个不可预测的令牌，存入 `pynext_csrf` Cookie，客户端再以 `X-CSRF-Token` 请求头（或 `csrf_token` 表单字段）回传相同的值。Cookie 值经过 HMAC 签名，未签名的 Cookie 即使令牌相符也会被拒绝（防止子域 Cookie 注入）。同时还会检查 `Origin` 和 `Sec-Fetch-Site`：来自跨站或子域页面的请求即使令牌有效也会被拒绝。GET、HEAD 和 OPTIONS 不受检查；其他方法若缺少令牌或令牌不匹配，将返回 HTTP 403。

~~~python
from pynext import JSONResponse, csrf_protect, new_csrf_token, route, set_csrf_cookie

@route("GET")
async def get_token(request):
    token = new_csrf_token()
    return set_csrf_cookie(JSONResponse({"token": token}), token, config=request.security)

@route("POST")
@csrf_protect
async def update(request):
    return JSONResponse({"ok": True})
~~~

启用会话中间件后，无需另传安全配置即可自动开启 CSRF。若没有会话，又想保护全部状态变更类 API 路由，可向 `create_app` 传入 `security=SecurityConfig(csrf_enabled=True)`。令牌仅证明请求来自能够读取该 Cookie 的页面。与其他 double-submit 方案一样，能够通过 XSS 读取 Cookie 的页面可以绕过它。Cookie 默认使用 `SameSite=Lax`，且不设 `HttpOnly`（JavaScript 需要读取它）；CSRF Cookie 默认带 `Secure`，在明文 `http://` 本地开发时，请同时向应用配置和 `set_csrf_cookie()` 调用传入 `csrf_cookie_secure=False`（`pynext dev` 会自动这样做）。多 worker 部署时，所有 worker 共享同一个签名密钥。使用 `--workers N` 启动时，supervisor 会生成一个 32 字节密钥（若指定了 `--csrf-signing-key` / `PYNEXT_CSRF_SIGNING_KEY`，则使用该密钥），并与所有 worker 共享，包括崩溃或滚动重启后新启动的 worker，因此一个 worker 签发的 Cookie 可以在其他 worker 上通过验证。密钥可以是至少 16 字节的字符串、`hex:<hex>` 或 `base64:<base64>`；为空、过短或无法解码的密钥不会静默回退到随机密钥，而是在启动时直接失败。不使用 worker 的单进程服务器继续使用每个进程的回退密钥，因为全部配置都在同一个进程内，所以没有问题。

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

## 会话

安装 `SessionMiddleware` 后，服务器端会话位于 `request.state["session"]` 中（`SQLiteSessionStore` 用于工作进程共享的单个文件，`RedisSessionStore` 用于 Redis）。会话 Cookie 中只有不透明令牌，且为 `HttpOnly`，默认 `Secure`、`SameSite=Lax`。会话数据必须是 JSON 可序列化的：为不可序列化的值（例如任意对象或集合）赋值时，会在赋值时抛出指明键名的 `TypeError`，而不是等到保存会话时才失败。存储数据上限为 16 KiB。多次出示同一会话 Cookie 名的请求会被视为完全没有会话（fail-closed）。

登录时请轮换会话 id——这是必需的，不是可选的。验证凭据并存储已认证身份后，在返回登录响应之前调用 `session.rotate_on_login()`：

~~~python
from pynext import JSONResponse, route

@route("POST")
def login(request):
    user = authenticate(request)  # 验证凭据；失败时返回/抛出 401
    session = request.state["session"]
    session["user_id"] = user.id
    session.rotate_on_login()  # 新的会话 id + 新的 CSRF 令牌；旧 id 不再可用
    return JSONResponse({"ok": True})
~~~

跳过轮换会使会话面临固定攻击：攻击者在登录前固定的会话 id 在登录后仍然有效。权限变更时（例如修改密码后）也请再次轮换。退出登录时调用 `session.invalidate()` 以删除服务器端会话并清除 Cookie。

`RedisSessionStore` 在 Redis 不可达时遵循故障策略（见 `failure_policy`）：默认是 `fail_closed`，即抛出 `SessionUnavailable`，请求以 503 响应，而不是悄悄丢失会话状态。传入 `failure_policy="fail_open"` 则改为以一个全新的临时会话开始——读取可以继续，但不会持久化任何内容，并且故障期间无法完成登录：为其签发的 CSRF 令牌从未被存储，因此登录请求会在 CSRF（403）上失败。故障期间的轮换还可能让旧会话 id 在过期前继续可用。仅在可用性比会话连续性更重要时使用 `fail_open`。

## API 响应流

向 `StreamingResponse` 传入产生字符串或字节的异步 iterable。ASGI 和 Axum 会依次发送数据块，并在完成、断开或出错时关闭迭代器。单块最多 1 MiB；不要手动设置 `Content-Length` 或 `Transfer-Encoding`，协议帧由服务器管理。Axum 最多排队一个数据块，下一块生成与发送等待分别有 30 秒超时。响应头发出后出错会中断流，无法更改状态码。HEAD 不会消费流。尚不支持 React/RSC 流式渲染。

## 流式 SSR

页面函数可以返回 UI 节点的异步 iterable。SSR 按生成顺序发送布局和各块；SSG 在构建时合并并提供静态文件。公开 ISR 的首次请求接收流，其他并发请求等待完整结果或立即收到 stale 内容，只有完整结果才会缓存。SSG 页面限制为 8 MiB；SSG 和 ISR 流还限制为 30 秒；ISR 最多缓存 2 MiB 的正文。首次客户端断开后生成仍可能继续。布局必须恰好渲染一次 children。[示例](../../examples/basic/app/stream/page.py)。

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

开发版本在 ASGI 和 Axum 接受连接前检查 `Origin`，默认只允许同源。通过向 `create_app(..., security=...)` 传入 `SecurityConfig(websocket_allowed_origins=("https://client.example",))`，或重复指定 `--ws-allowed-origin https://client.example`，可以添加 HTTP(S) 来源。来源须包含协议及非默认端口；不接受 `*` 和 `null`。不发送 `Origin` 的现有非浏览器客户端必须通过 `websocket_allow_missing_origin=True` 或 `--ws-allow-missing-origin` 显式允许。已发布的 0.1.0 不包含此检查。客户端可以伪造或省略 `Origin`。在握手完成之前，升级请求还会经过全局中间件链（包括会话中间件）和处理函数的 `@require_auth`/`@require_permission` 策略（ASGI 和 Axum 均适用）；被拒绝的升级会在 101 响应之前被拦截（Axum 返回 HTTP 403，ASGI 返回 close 1008），处理函数不会执行。应用仍须负责建立身份（例如登录路由设置 `session["user_id"]`），会话之外的凭证方式（如令牌）须在中间件中验证。使用代理时，只通过 `--trusted-proxy` 指定实际的代理，并配置代理覆盖客户端提供的转发头。

为了让 PyNext 在 ASGI 下识别可信代理，服务器须在 `scope["client"]` 中保留原始对端地址。如果由 PyNext 解释转发头，请关闭 ASGI 服务器提前处理代理头的功能（Uvicorn 使用 `--no-proxy-headers`）。

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

参见 [echo 示例](../../examples/basic/app/ws/echo/route.py)。ASGI 和 Axum 均支持文本和二进制消息。默认上限为 64 KiB，可通过 `create_app(..., websocket_max_message=...)` 或 `--ws-max-message-bytes` 调至最多 16 MiB；接收超限以 1009 关闭。`@websocket(protocols=("chat.v1", "chat.v2"))` 选择子协议，结果可从 `websocket.subprotocol` 读取。`WebSocketPubSub` 使用 `async with broker.subscribe(topic) as sub`、`await sub.receive()` 和 `await broker.publish(topic, message)` 在单个进程中分发消息。慢订阅者会收到 `PubSubOverflow`；消息不会跨 worker 分发或持久化。Axum 在处理函数执行前完成握手。

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

SSG 和 ISR 页面不能接收 `request`，仅用于公开内容。ISR 默认忽略查询字符串。通过 `@page(render="isr", revalidate=30, query_keys=("lang",))` 和页面的 `query` 参数，只传入允许的键及其字符串列表，并按值分别缓存。使路径失效会使其所有查询变体失效。包含 Cookie 或 Authorization 的请求绕过共享缓存并返回 `private, no-store`。默认缓存仅在本进程内，也可使用 SQLite 或可选 Redis（`pynext-framework[redis]` 和 `PYNEXT_REDIS_URL`）在 worker 间共享；共享存储故障时遵循故障策略：默认（`fail_open`，`--cache-failure-policy fail_open`）回退到过期的本地缓存，`fail_closed` 则返回 503。带 `Set-Cookie` 或 private/no-store 的响应不缓存。SSG 异步生成器在构建期间消费，并作为静态文件提供。

SSG 构建最多生成 10,000 个页面；开发模式下每次请求都会渲染 SSG 页面。ISR 页面不能导出 `generate_static_params()`，每个进程最多并发执行 128 个生成任务。

已授权的 API 处理函数可调用 `await request.revalidate("/news/example")`；持有应用实例的代码可调用 `await app.revalidate(path)`。请传入不含查询字符串、已解码的 ISR 路径。如果移除了本地缓存条目或使进行中的生成失效，返回 `True`。使用共享后端时，存储可达则同时删除共享条目；但在 `fail_open` 模式下，共享存储不可达时即使返回 `True` 也不保证共享条目已被删除（共享删除仅为尽力而为，其他工作进程可能继续提供旧条目直至其过期）——如需让未生效的失效报错而非报告成功，请使用 `fail_closed`。已有请求仍可能收到旧生成结果，但该结果不会重新覆盖缓存。内存后端下失效只作用于当前进程，不会传播到其他工作进程；SQLite 后端下也会删除共享条目，共享存储正常时其他工作进程的下次查询会看到失效。对外提供此操作的端点需要认证和授权。

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

从 `pynext` 导入 `model_prop`，并使用 `island("shared-model", props={"name": "Ada"}, hydrate=True, model=h("button", {"type": "button"}, "Hello ", model_prop("name")))`，服务端与浏览器即可用同一模型创建初始 DOM。模型支持 element、text、fragment、字符串 props 占位符及静态字符串/布尔/None 属性，上限为 16 KiB、512 节点和深度 32。JS 模块导出 `hydrate`、`mount` 和 `render`，用于事件绑定与不匹配时恢复。动态属性、循环和客户端状态计算尚不支持。参见[服务端示例](../../examples/basic/app/shared-model/page.py)和[客户端模块](../../examples/basic/islands/shared-model.js)。

默认激活策略为 `load`。`visible` 在进入视口时激活；`idle` 请求空闲回调，并请求 2 秒 timeout，但实际执行可能更晚。相关 API 不可用时会退回 idle 或定时器。这三种策略不会重放激活前的事件。

设置 `hydrate=True, strategy="interaction"` 后，用户点击 `<button type="button">` 会激活 island。参见[示例页面](../../examples/basic/app/interaction/page.py)和[模块](../../examples/basic/islands/interaction-counter.js)。加载器为该 island 注册监听器后，最多缓存 16 次受支持的点击，在 hydration 成功后按顺序重放；超出的点击会被丢弃并产生警告。链接、表单提交、输入、label 和可编辑区域的事件不会被拦截或重放；监听器注册前的事件也不会被捕获。DOM 不匹配、元素被移除或激活失败时会丢弃待处理事件。重放事件是 `isTrusted=false` 的合成事件，不能保证需要浏览器用户激活的操作；island 外的捕获监听器可能同时观察到原始事件和重放事件。

## 客户端导航

所有页面都会加载客户端运行时。同源链接在文档元数据和 CSP 兼容时使用局部导航，否则回退到完整导航。预取最多并发 4 个请求，每个响应不超过 1 MiB；仅缓存明确公开且仍然新鲜的响应，最多 60 秒、20 项。带 `Vary: Cookie` 或 `Authorization` 的响应不会缓存。

## Python 客户端 DSL（实验性）

`pynext.client_dsl.compile_client_dsl(source)` 可将一小部分固定的 Python 子集编译为 JavaScript（规范 §7.3）。支持的内容：字面量（JavaScript 安全整数范围内的 int、float、str、bool、None、list、tuple、dict）、算术运算（`+ - * / // % **`）、比较、`and`/`or`/`not`、`if`/`else`、对 `range()` 或列表的 `for`、`while`、函数定义、`len`、`str`、`int`、`float`、`bool`、`abs`、`min`、`max`、`list`、`print`、`.append()`、索引、切片以及单子句列表推导式。除此之外的所有写法都会抛出 `ClientDSLCompileError`：import、反射、文件 I/O、类、lambda、`try`、f-string，以及 `open`、`os`、`Request`、`h` 等服务器专用名称都会在编译时被拒绝，因此服务器代码无法混入浏览器包。完整列表与已知的语义差异（例如 `str(1.0)` 会丢掉末尾的 `.0`）见 `pynext.client_dsl.SUPPORTED` 与 `KNOWN_DIFFERENCES`，另见[示例](../../examples/basic/client_dsl_example.py)。该 DSL 是独立的编译器，不会自动打包进 island。

## ASGI、超时与安全性

通过 Uvicorn 以开发模式启动示例 ASGI 应用：

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

已构建的应用需使用 `create_app(project_path, production=True)`。ASGI 请求超时由所使用的服务器管理。Axum 适配器等待 Python 最多 30 秒，之后请求取消并返回 504。已经在线程中运行的同步函数可能在 504 之后继续执行并产生写入等副作用。请确保写入操作幂等，并为数据库与外部服务设置单独超时。进程隔离可通过 `--workers`（见下文）获得；单进程没有该能力。ISR 取消也有相同限制。

分布式限流已通过 `--rate-limit-backend redis` 实现（共享 Redis 令牌桶）。生产环境加固仍是路线图项目。共享存储正常时 SQLite 失效会在下次查询时生效。安装可选包并启用追踪后会生成 OpenTelemetry span（见可观测性一节）。ASGI 一致性由 `tests/test_asgi_conformance.py` 验证（HTTP 事件顺序、分块请求体、断开连接、包含失败路径的 lifespan）。请参阅[生产路线图](../PRODUCTION_ROADMAP.md)中的计划与验收条件。

两个适配器的请求体上限均为 1 MiB，超出返回 413。Axum 默认同时接收 16 个请求体、接收期限 10 秒（408），Python 执行槽为 64 个，等待队列上限是执行槽数与 64 中较小的值；队列满返回 503。执行和请求体设置可通过 CLI 调整。HTTP/1.1 请求头读取期限为 10 秒。Axum 默认每个 worker 最多 1,024 个连接、每个 IP 最多 128 个连接，可通过 `--max-connections` 和 `--max-connections-per-ip` 调整。外部 ASGI 服务器的超时由其自身管理。非根路径末尾的 `/` 会以 308 重定向。编译后的 island 单个上限为 2 MiB，总计 32 MiB。

## 可观测性

结构化日志默认开启：`pynext` 日志器每行输出一个 JSON 对象，包含 `timestamp`、`level`、`message`、`request_id`、`trace_id`（收到 `traceparent` 请求头时）、`route`、`status` 和 `latency_ms`。每个请求都会获得一个请求 ID：传入的 `X-Request-Id` 请求头有效时会被原样回显，否则生成新 ID，在整个请求处理过程中传播，并通过 `x-request-id` 响应头返回。切勿在日志消息中放入请求头、Cookie、正文或 `Authorization` 值：JSON 格式化器会丢弃名称类似凭据的附加字段，访问日志只记录路由模式（例如 `/blog/[slug]`），绝不记录原始 URL。

指标在内存中收集，默认开启。`app.metrics.snapshot()` 返回按路由模式统计的请求数与状态码、按路由统计的延迟 p50/p95/p99、Python 执行队列长度与等待时间、Python 执行时间与 Rust 渲染时间对比、ISR 缓存 hit/miss/stale 与再生成成功/失败/已开始、Worker 启动/超时/不可用、响应大小与 island 包大小。`app.metrics.prometheus_text()` 以 Prometheus 文本格式暴露相同的计数器。延迟样本按路由上限为 2048 个。Rust 不经过 Python 直接提供的全新 ISR 命中不会计入此处；仅统计到达 Python 分发的 ISR 结果。

OpenTelemetry 追踪默认关闭且为可选项：安装 `opentelemetry-api`（以及自选的 SDK/导出器），并用 `--tracing` 或 `PYNEXT_TRACING=1` 启用。启用后会创建 `pynext.request` 与 `pynext.render` span，并传播传入的 W3C `traceparent`/`tracestate` 上下文。未安装相关包时，追踪为 no-op，不会破坏任何功能。

可通过 CLI 标志 `--structured-logs` / `--no-structured-logs`、`--metrics` / `--no-metrics`、`--tracing` / `--no-tracing`，环境变量 `PYNEXT_STRUCTURED_LOGS`、`PYNEXT_METRICS`、`PYNEXT_TRACING`、`PYNEXT_LOG_LEVEL`，或在代码中向 `create_app(..., observability=ObservabilityConfig(...))` 传入配置来进行设置。

默认可观测性设置（结构化日志 + 指标开启，追踪关闭）的实测开销已用 `benchmarks/bench.py --mode axum --requests 1000 --concurrency 10 --path / --production` 在已构建的示例上各测 2 次。由于同一 VM 上还有其他工作在并行构建与测试，绝对延迟被放大，运行间波动较大。开启：25.4 / 14.2 req/s，p50 309 / 586 ms，进程 CPU 4.79 / 5.02 秒。关闭（`PYNEXT_STRUCTURED_LOGS=0 PYNEXT_METRICS=0`）：18.7 / 18.5 req/s，p50 437 / 478 ms，CPU 5.01 / 4.69 秒。均值分别为开启 19.8 req/s、p50 447 ms，关闭 18.6 req/s、p50 457 ms，未观测到超出运行间波动的开销（开启的运行还向 stderr 写了约 1000 行 JSON 日志）。如需更严格的数值，请在空闲机器上重新测量。

## 限流

限流是可选项，采用按进程的令牌桶：`pynext start --rate-limit 100 --rate-limit-window 60` 表示每个客户端 IP 每 60 秒允许 100 个请求（突发量即上限）。超限的请求返回 429，并带有以秒为单位的 `Retry-After` 头。WebSocket 握手也以同样方式限流：每次握手从同一个按 IP 划分的桶中消耗一个令牌，超限的握手返回 429 并带有 `Retry-After`。键为客户端 IP；只有 `--trusted-proxy`（IP 或 CIDR，可重复指定）列出的对端发来的 `X-Forwarded-For` 才会被信任，不受信任代理的该头部会被忽略，因为它可以被伪造。代码中使用 `create_app(path, rate_limit=RateLimiter(100, 60, trusted_proxies=["10.0.0.0/8"]))`。桶按进程划分，重启后消失。使用 `--rate-limit-backend redis` 时桶在进程间共享；Redis 不可达时遵循故障策略：默认（`fail_closed`，`--rate-limit-failure-policy fail_closed`）返回 503，`fail_open` 则放行请求。配置的限流同样适用于两个适配器的静态文件、SSG 和新鲜的 ISR 页面。

开发版本在两个适配器中均先根据请求头限流，再接收请求体，包括使用 `Expect: 100-continue` 的请求。准入时扣除一个令牌；后续上传中断、大小或速率超限、执行槽不足均不退还。Axum 的 HTTP 与 WebSocket 限流判定合计最多并发 64 个，期限为 2 秒；饱和、超时或意外错误返回 503 和 `Retry-After: 1`。该预算也适用于 `fail_open`，该策略只处理在期限内返回的后端故障。带未读请求体的 HTTP/1.1 拒绝连接会关闭，HTTP/2 的其他流仍可使用。健康检查绕过限流，豁免路径在严格解码后判定。已发布的 0.1.0 不包含此改进。

转发链从右向左遍历受信任的代理，使用最近的不受信任地址。必须准确配置所有中间代理。每个进程最多保留 10,000 个桶；全部处于活跃状态时，新键会逐出最旧的桶而不是收到 429，新键的创建受全局滑动窗口限制（每个窗口最多 10,000 个新键）。桶的分数是取自 Redis `TIME` 的整数微秒；两个脚本可能读到同一微秒，此时分数相同的桶同样陈旧，按桶键确定性地选择要逐出的桶——被逐出的所有者不会被拒绝，下次请求会获得一个新的桶。分数在远超最长一年窗口的范围内仍保持为精确整数（当前纪元下最后一位的单位是 0.25 µs），因此数分钟到数小时的实际窗口不受双精度限制的影响。IPv6 地址按 /64 前缀共享桶。这种分组是一种权衡：持有整个 /48 的攻击者可以生成 65,536 个不同的 /64，填满桶表和新键窗口，在窗口滑动之前真正的新键仍会被拒绝（被逐出的已有键不会被拒绝）。`--rate-limit-ipv6-prefix-len 48` 将整个 /48 归入一个桶，可以阻止这种攻击，但共享该前缀的所有主机也将共享一个桶，繁忙的 /48 会连带限制无辜用户；默认值保持为 64。用 `--rate-limit-exempt-path`（例如 `/healthz`）指定的路径跳过速率限制。

多工作进程：`pynext start --workers 4` 启动共享监听套接字的监管器。使用 `--port 0` 时，动态分配的端口在 worker 重启后保持不变。各 worker 的解释器、内存缓存和默认限流独立；SQLite/Redis ISR 与 Redis 限流可以共享。监管器会重启故障 worker；中断的请求需要重试。CI 在 Linux、macOS 和 Windows 上检查共享监听套接字的 worker 启动和重启，并在 Linux 和 macOS 上运行基于信号的监管器测试。

## 测试与故障排查

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

路由器与 HTML 序列化除人工挑选的用例外，还有基于属性的测试：Python 侧用 hypothesis（`tests/test_property_router_render.py`，固定种子、40 个用例以保持 CI 快速），Rust 侧用 proptest（`crates/pynext-router` 与 `crates/pynext-native` 的单元测试，各 64 个用例），覆盖路由匹配往返、转义无泄漏、void 标签处理与任意树渲染。`embed` 在屏蔽标签列表中，因此无法渲染，已排除在测试之外。

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
- 若工作进程之间的 ISR 结果不同，请检查缓存后端：内存后端按进程隔离；SQLite 后端在共享存储故障时会回退到进程内副本。
