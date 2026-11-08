# PyNext 사용 설명서 — 한국어

[English](../en/manual.md) · [日本語](../ja/manual.md) · [简体中文](../zh-CN/manual.md)

이 설명서는 현재 체크아웃한 코드인 **v0.7.0a1** 미출시 실험판 후보를 기준으로 합니다. PyNext는 Python과 Rust로 만드는 실험적 풀스택 프레임워크이며 Next.js, React, React Server Components와 호환되지 않습니다. 현재 프로토타입을 인터넷에 직접 공개하거나 신뢰할 수 없는 앱 실행에 사용하지 마세요. [버전별 변경 내용](../../CHANGELOG.md)과 [향후 작업](../PRODUCTION_ROADMAP.md)을 참고하세요.

## 준비와 첫 실행

CPython 3.13, Rust/Cargo, [uv](https://docs.astral.sh/uv/)가 필요합니다. 지원 환경은 CI 매트릭스로 검증합니다. OS는 Ubuntu·macOS·Windows(최신), Python은 CPython 3.13만 지원합니다(3.14는 검증되지 않아 매트릭스에 없습니다). 배포 모드는 독립 실행 Axum과 Uvicorn 위의 ASGI이며, 인프로세스 ASGI 모드는 모든 CI 잡에서 pytest가 검사합니다. 브라우저 스모크는 세 OS에서 모두 실행합니다. 예제는 island를 사용하므로 Node.js 18 이상과 npm으로 esbuild 0.28.1도 설치해야 합니다.

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
git checkout main
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

http://127.0.0.1:3000/, /about, /guides/intro, /news/example, /hydrate, /strategies, /api/users를 확인하세요. Ctrl+C로 서버를 종료합니다.

`pynext dev --dir examples/basic`는 빌드 없이 소스를 제공하며 `app/`·`islands/`·`public/`·`pynext.config.py` 변경 시 같은 포트로 서버를 다시 시작합니다. `pynext start`는 빌드 manifest를 사용하며 추적하는 소스가 바뀌면 재빌드를 요구합니다. island가 없는 앱에는 Node와 esbuild가 필요하지 않습니다. 설치 안내의 기준은 저장소 소스이며 PyPI 배포를 의미하지 않습니다.

개발 감시기는 크기와 수정 시간이 같은 변경도 감지하도록 매번 파일 내용을 해시합니다. 감시 디렉터리가 크면 디스크 사용량이 늘어납니다.

## 프로젝트 구조와 라우팅

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

`page.py`는 데코레이터가 적용된 `Page`를, `route.py`는 HTTP 핸들러를 내보냅니다. `layout.py`는 하위 페이지를 감쌉니다. 우선순위는 정적 경로 > `[slug]` > `[...path]` > `[[...path]]`입니다. 단일 매개변수는 문자열이고 catch-all은 문자열 리스트입니다. 선택적 catch-all `[[...path]]`는 0개 이상의 세그먼트에 매칭되므로 `/docs/[[...path]]`는 빈 리스트로 `/docs`도 제공합니다. 필수 catch-all과 선택적 catch-all을 같은 레벨에 함께 쓸 수 없으며, 빌드 시 충돌로 실패합니다. 병렬·인터셉트 라우트는 미구현입니다.

괄호로 묶인 디렉터리는 라우트 그룹입니다. `app/(marketing)/page.py`는 `/`를 제공하고 그룹 안의 레이아웃도 그대로 적용됩니다. 그룹 이름은 영문·숫자·`_`·`-`만 쓸 수 있고, 두 그룹이 같은 URL로 매핑되면 빌드가 실패합니다.

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)`는 HTML 트리를 만들며 문자열을 이스케이프합니다. SSR은 기본적으로 전체 응답을 모아 반환하며, 페이지는 UI 청크를 스트리밍할 수도 있습니다(아래 스트리밍 SSR 참고). 임의의 raw HTML 삽입 API는 제공하지 않습니다.

## 앱 import와 이전 버전에서의 변경

페이지·레이아웃·API 모듈에서 `from .helpers import title`처럼 상대 import를 사용할 수 있습니다. 앱마다 모듈 공간이 분리되며 프로젝트 루트를 전역 `sys.path`에 자동 추가하지 않습니다. v0.5에서 사용하던 루트 기준 절대 import가 실패하면 상대 import로 바꾸거나 프로젝트를 Python 패키지로 설치하세요. 외부 패키지는 실행 환경에 설치해야 합니다.

## API 요청과 오류

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

`request.query`의 값은 리스트입니다. 예를 들어 `request.query.get("q", [""])[0]`로 첫 값을 읽습니다. 원본과 퍼센트 디코딩 결과의 잘못된 UTF-8, 1,000개를 초과한 쿼리 필드, 잘못된 JSON 및 비유한 JSON 상수는 `BadRequest`를 발생시키고 ASGI와 Axum은 상세 내용 없이 HTTP 400을 반환합니다. 파싱은 해당 값을 **읽을 때만** 수행됩니다. 처리되지 않은 다른 예외는 HTTP 500입니다.

응답의 기본 Content Security Policy는 `script-src 'self'; object-src 'none'`입니다. 브라우저에서 불러올 수 있는 스크립트가 제한되므로 클라이언트 코드를 추가할 때 이를 고려하세요. 응답 헤더와 미디어 타입의 제어 문자는 거부합니다.

상태 변경 API 요청에는 CSRF 보호를 선택적으로 적용할 수 있습니다(명세 v0.2 §11). 아직 인증 시스템이 없으므로 double-submit 방식을 사용합니다. 앱이 예측할 수 없는 토큰을 발급해 `pynext_csrf` 쿠키에 담고, 클라이언트는 같은 값을 `X-CSRF-Token` 헤더(또는 `csrf_token` 폼 필드)로 돌려보냅니다. GET, HEAD, OPTIONS는 제외되며, 토큰이 없거나 일치하지 않는 그 외의 메서드는 HTTP 403을 받습니다.

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

핸들러를 하나씩 표시하는 대신 `create_app`에 `security=SecurityConfig(csrf_enabled=True)`를 전달하면 모든 상태 변경 API 라우트를 보호할 수 있습니다. 아직 세션이 없으므로 토큰은 사용자 세션에 묶이지 않습니다. 토큰은 쿠키를 읽을 수 있는 페이지에서 요청이 왔다는 것만 증명합니다. XSS로 쿠키를 읽을 수 있는 페이지는 다른 double-submit 방식과 마찬가지로 이를 우회할 수 있습니다. 쿠키는 기본적으로 `SameSite=Lax`이며 `HttpOnly`가 아닙니다(JavaScript가 읽어야 합니다). 운영 환경에서는 `csrf_cookie_secure=True`로 설정해 HTTPS에서만 전송되게 하세요.

교차 출처 요청은 기본적으로 허용하지 않습니다(명세 v0.2 §11). 출처를 설정하지 않으면 `Access-Control-Allow-Origin` 헤더를 보내지 않습니다. `SecurityConfig`로 정책을 `create_app`에 전달하세요.

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

허용된 출처의 preflight(`Origin`과 `Access-Control-Request-Method`를 담은 `OPTIONS`)에는 204와 `Allow` 및 `Access-Control-Allow-*` 헤더를 반환하고, 허용되지 않은 출처·메서드·헤더의 preflight는 403을 받습니다. 허용된 출처의 실제 교차 출처 요청에는 `Access-Control-Allow-Origin`(설정한 경우 `Access-Control-Allow-Credentials: true`도)을 붙이고, 그 외의 출처에는 CORS 헤더를 붙이지 않으므로 브라우저가 응답을 노출하지 않습니다. `Origin`을 담은 요청에 대한 응답에는 항상 `Vary: Origin`도 붙습니다. 같은 출처 요청은 CORS로 차단하지 않으므로, 출처를 설정하지 않아도 같은 출처 페이지는 커스텀 헤더를 달아 앱 자신의 API를 호출할 수 있습니다. `cors_allowed_origins=("*",)`는 모든 출처를 허용하며, `cors_allow_credentials=True`인 경우 `*` 대신 정확한 출처를 그대로 반환합니다.

정적 파일, SSG 페이지, 캐시된 ISR 응답은 네이티브 서버가 직접 제공하므로 CORS 헤더가 붙지 않습니다. CORS는 Python 디스패치를 거치는 응답(API 라우트와 동적 렌더링 페이지)에 적용됩니다.

기본 CSP는 `script-src 'self'; object-src 'none'`입니다. `SecurityConfig(csp_nonce_enabled=True)`로 설정하면 동적 렌더링 페이지마다 요청별 새 nonce가 발급됩니다. 헤더는 `script-src 'self' 'nonce-<값>'; object-src 'none'`이 되고, 같은 nonce가 island 런타임 `<script>` 태그에도 들어갑니다. nonce는 응답마다 다르므로 nonce가 포함된 페이지를 직접 캐시하지 마세요. 미리 렌더링된 SSG 페이지, 캐시된 ISR 응답, 정적 파일은 nonce 없는 정적 정책을 유지합니다. 공유 HTML에 박힌 nonce는 여러 응답에서 재사용돼 nonce의 의미가 사라지기 때문입니다. 쿠키나 Authorization 헤더가 있는 ISR 요청은 요청마다 새로 렌더링하므로 nonce가 적용됩니다.

## API 응답 스트리밍

`StreamingResponse`에 문자열 또는 바이트를 내보내는 비동기 iterable을 전달합니다. ASGI와 Axum이 청크를 순서대로 전송하며 완료·연결 해제·오류 시 반복자를 닫습니다. 청크당 최대 1 MiB이며 `Content-Length`와 `Transfer-Encoding`은 직접 지정하지 않습니다. Axum은 청크 하나를 대기시키고 다음 청크 생성과 송신 대기에 각각 30초 제한을 적용합니다. 헤더 전송 후 오류는 상태 코드를 바꾸지 못하고 스트림을 중단합니다. HEAD 요청은 스트림을 소비하지 않습니다. React/RSC 스트리밍은 미구현입니다.

## 스트리밍 SSR

페이지 함수는 단일 트리 대신 UI 노드의 비동기 iterable을 반환할 수 있습니다. 레이아웃 셸을 먼저 보내고 각 청크를 Rust가 직렬화해 생성되는 대로 전송한 뒤 레이아웃 닫는 부분을 보냅니다. 기본값은 여전히 전체 버퍼를 만든 뒤 보내는 방식입니다. 스트리밍은 SSR 전용이며 SSG·ISR 페이지는 전체 본문이 필요하므로 스트리밍 페이지를 지정하면 실패합니다. 레이아웃은 자식을 정확히 한 번 렌더링해야 합니다. 연결이 끊기면 생성기를 닫고, 헤더 전송 후 오류가 나면 스트림을 중단합니다. [예제 페이지](../../examples/basic/app/stream/page.py)는 `/stream`에서 확인할 수 있습니다.

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

`route.py`는 HTTP 핸들러와 함께 `@websocket` 핸들러를 하나 내보낼 수 있습니다. 핸들러는 `WebSocket`을 받아 보내거나 받기 전에 `await websocket.accept()`를 호출해야 합니다. 텍스트와 바이너리 메시지를 주고받을 수 있으며, 상대가 연결을 끊으면 `receive()`에서 `WebSocketDisconnect`가 발생하므로 연결별 정리는 `finally` 블록에 둡니다.

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

[echo 예제](../../examples/basic/app/ws/echo/route.py)를 참고하세요. WebSocket 핸들러가 없는 경로는 close 코드 1008로 핸드셰이크를 거부하고, accept 없이 반환한 핸들러는 코드 1000으로 닫힙니다. 메시지 1개는 64 KiB까지이며, 더 큰 메시지는 close 코드 1009로 거부합니다. close 코드는 1005·1006·1015를 제외한 1000~4999여야 하고 reason은 UTF-8 123바이트까지입니다. 서브프로토콜 협상, 브로드캐스트·pub/sub, 연결 간 공유 상태는 없습니다. 각 연결은 자신의 메시지만 받습니다. WebSocket 전용 `route.py`는 일반 HTTP 요청에 405로 답합니다. ASGI 어댑터와 Axum 독립 서버 모두 WebSocket을 지원하며, Axum에서는 핸들러 실행 전에 서버가 핸드셰이크를 완료하므로 `accept()`는 연결을 수락됨으로 표시만 합니다.

## SSR, SSG, ISR

- `@page()`: 요청할 때마다 렌더링합니다. UI 노드의 비동기 iterable을 반환하면 전체를 모으지 않고 스트리밍합니다(스트리밍 SSR 참고).
- `@page(render="ssg")`: 빌드할 때 렌더링합니다. 동적 SSG 경로는 딕셔너리 리스트를 반환하는 `generate_static_params()`가 필요합니다. 운영 모드에서 생성하지 않은 경로는 404입니다.
- `@page(render="isr", revalidate=30, stale=120)`: 공개 페이지를 요청 시 생성하고, 신선한 동안 재사용하며, stale 기간에는 기존 응답을 제공하면서 하나의 재생성을 수행합니다. 단위는 초입니다.

~~~python
# app/guides/[slug]/page.py
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

SSG와 ISR 페이지 함수는 `request`를 받을 수 없습니다. 공개 콘텐츠에만 사용하고 환경 변수와 외부 데이터가 공유 가능한지 확인하세요. ISR은 익명 요청의 쿼리를 캐시 키에 포함하지 않습니다. Cookie 또는 Authorization 헤더가 있으면 공유 ISR 캐시를 우회하고 `private, no-store`로 응답합니다. 기본 캐시는 크기가 제한된 **프로세스별 메모리 캐시**이므로 워커 간 공유·무효화되지 않습니다. `--cache-backend sqlite`로 시작하거나 `create_app(..., cache_backend="sqlite", cache_path=...)`로 만들면 모든 워커가 하나의 SQLite 파일(WAL)을 공유합니다. stale 재생성은 만료·소유 토큰이 있는 lease로 키마다 한 워커만 수행하고, 저장할 때 버전과 유효한 lease 소유권을 원자적으로 확인합니다. 공유 저장소 장애 시 워커별 로컬 캐시로 대체합니다. 각 워커는 공유 파일 앞에 프로세스별 캐시를 두므로, 다른 워커의 무효화는 공유 항목에 즉시 반영되지만 로컬 복사본은 남은 TTL 동안 유지될 수 있습니다. `Set-Cookie`나 `Cache-Control: private/no-store` 응답은 공유 캐시에 저장하지 않습니다. Redis 어댑터는 이후 과제이며 미구현입니다. SSG 내용을 바꾸려면 재빌드해야 합니다.

SSG 빌드는 최대 10,000페이지를 생성하며 개발 모드에서는 요청마다 SSG 페이지를 렌더링합니다. ISR 페이지는 `generate_static_params()`를 내보낼 수 없고 프로세스별 동시 생성 작업은 최대 128개입니다.

권한을 확인한 API 핸들러에서는 `await request.revalidate("/news/example")`, 앱 인스턴스를 가진 코드에서는 `await app.revalidate(path)`를 사용할 수 있습니다. 쿼리 문자열이 없는 디코딩된 ISR 경로를 전달합니다. 캐시나 진행 중인 생성을 무효화했다면 `True`를 반환합니다. 기존 요청은 이전 생성 결과를 받을 수 있지만 그 결과가 캐시를 다시 덮어쓰지는 않습니다. 메모리 백엔드에서는 무효화가 프로세스별이며 다른 워커에 전파되지 않고, SQLite 백엔드에서는 공유 항목도 제거되지만 다른 워커의 프로세스별 복사본은 남은 TTL 동안 유지될 수 있습니다. 외부에 노출하는 엔드포인트는 인증·권한 검사가 필요합니다.

## 공개 파일과 islands

공개 파일·SSG 페이지·island 번들은 SHA-256 내용 해시 기반 강한 ETag를 사용합니다. GET/HEAD는 `If-Match`와 `If-None-Match`를, GET은 단일 바이트 `Range`와 강한 ETag `If-Range`를 지원하며 필요한 경우 206 또는 416을 반환합니다. 다중 또는 잘못된 범위는 무시하고 전체를 반환합니다. 날짜 기반 검증과 ISR 응답의 범위 요청은 미구현입니다.

`public/styles/site.css`는 `/styles/site.css`로 공개됩니다. 빌드는 파일을 스냅샷하고 무결성을 검사하지만 운영 서버 시작에도 원본 파일이 필요합니다. 숨김 파일, 심볼릭 링크, 충돌 경로는 거부합니다. 개발 모드에서 공개 파일을 바꾸면 서버가 자동으로 다시 시작합니다.

Island는 Python 선언과 `islands/`의 JavaScript 또는 TypeScript 모듈을 연결합니다.

~~~python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island(
        "counter", props={"initial": 1},
        fallback=h("p", {}, "Enable JavaScript to use the counter."),
    ))
~~~

`islands/counter.ts`는 `mount(element, props)`를 내보내며 정리 함수를 반환할 수 있습니다. Props는 공개되는 JSON 객체이고 16 KiB 이하이므로 비밀 값을 넣지 마세요. 빌드에 esbuild가 필요합니다. 중첩 island, npm 패키지 import, 외부 import, CSS import는 지원하지 않습니다. `/_pynext`는 예약 경로입니다.

기존 서버 DOM을 재사용하려면 서버 fallback과 `hydrate=True`를 지정하고 모듈에서 `render`, `hydrate`, `mount`를 모두 내보냅니다. [hydrated-counter.ts](../../examples/basic/islands/hydrated-counter.ts)를 참고하세요. 서버와 클라이언트 트리가 다르면 해당 island를 다시 mount하며 입력 상태나 포커스를 잃을 수 있습니다.

활성화 기본 전략은 `load`입니다. `visible`은 화면에 들어올 때, `idle`은 유휴 콜백을 요청해 활성화합니다. idle 콜백에는 2초 timeout을 요청하지만 실제 실행은 더 늦을 수 있습니다. 관련 API가 없으면 idle 또는 타이머로 대체합니다. 이 세 전략은 활성화 전 이벤트를 재생하지 않습니다.

`hydrate=True, strategy="interaction"`은 사용자가 `<button type="button">`을 클릭할 때 활성화합니다. [예제 페이지](../../examples/basic/app/interaction/page.py)와 [모듈](../../examples/basic/islands/interaction-counter.js)을 참고하세요. 로더가 해당 island에 리스너를 등록한 뒤 들어온 지원 클릭은 최대 16개를 보관해 hydration 성공 후 순서대로 재생하고 초과분은 경고 후 버립니다. 링크, 폼 제출, 입력, label 및 편집 영역의 이벤트는 가로채거나 재생하지 않습니다. 리스너 등록 전 이벤트도 보관하지 않습니다. DOM 불일치, 요소 제거 또는 활성화 실패 시 대기 이벤트는 버립니다. 재생 이벤트는 `isTrusted=false`인 합성 이벤트로 브라우저 사용자 제스처가 필요한 동작을 보장하지 않으며, island 밖 캡처 리스너는 원본과 재생 이벤트를 모두 볼 수 있습니다.

## 클라이언트 탐색

클라이언트 런타임을 로드하는 페이지(island가 있는 페이지)에서는 같은 출처 링크 클릭 시 전체 새로고침 없이 이동합니다. 런타임이 대상 문서를 가져와 페이지 본문을 교체하고 `pushState`로 히스토리를 갱신하며, 뒤로/앞으로 버튼도 같은 방식으로 동작합니다. 링크는 호버, 키보드 포커스, 뷰포트 진입 시 미리 가져옵니다(최대 20개, 개당 1 MiB). 200이 아닌 응답, HTML이 아닌 응답, 가져오기 오류 등 어떤 실패가 있어도 전체 문서 이동으로 되돌아가므로 페이지가 깨지지 않습니다. `target="_blank"`나 `download`가 있는 링크, 다른 출처 링크, 같은 페이지 해시 링크는 기존 브라우저 동작을 유지하고, JavaScript가 꺼져 있을 때도 마찬가지입니다. `window.__pynextNav`에 디버깅용 `navigate`·`prefetch`·`isPrefetched`가 있습니다. island가 없는 페이지는 런타임을 로드하지 않으므로 부분 탐색이 동작하지 않습니다.

## Python 클라이언트 DSL (실험적)

`pynext.client_dsl.compile_client_dsl(source)`은 Python의 작고 고정된 부분집합을 JavaScript로 컴파일합니다(명세 §7.3). 지원하는 것: 리터럴(JavaScript 안전 정수 범위 안의 int, float, str, bool, None, list, tuple, dict), 산술 연산(`+ - * / // % **`), 비교, `and`/`or`/`not`, `if`/`else`, `range()`나 리스트에 대한 `for`, `while`, 함수 정의, `len`, `str`, `int`, `float`, `bool`, `abs`, `min`, `max`, `list`, `print`, `.append()`, 인덱싱, 슬라이스, 한 절짜리 리스트 컴프리헨션입니다. 그 밖의 것은 모두 `ClientDSLCompileError`가 됩니다. import, 리플렉션, 파일 I/O, 클래스, 람다, `try`, f-string, `open`·`os`·`Request`·`h` 같은 서버 전용 이름은 컴파일 시점에 거부되므로 서버 코드가 브라우저 번들에 들어갈 수 없습니다. 정확한 목록과 알려진 의미 차이(예: `str(1.0)`의 `.0`이 사라짐)는 `pynext.client_dsl.SUPPORTED`와 `KNOWN_DIFFERENCES`를 참고하세요. [예제](../../examples/basic/client_dsl_example.py)도 있습니다. DSL은 독립 컴파일러이며 island 번들에 자동으로 넣어주지 않습니다.

## ASGI, 시간 초과와 안전성

예제 ASGI 앱을 Uvicorn에서 개발 모드로 실행합니다.

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

빌드된 앱은 `create_app(project_path, production=True)`로 만듭니다. ASGI 서버의 시간 제한은 해당 서버가 관리합니다. Axum 어댑터는 Python 응답을 최대 30초 기다린 뒤 취소를 요청하고 504를 반환합니다. 이미 스레드에서 실행 중인 동기 함수는 504 이후에도 계속 실행되어 쓰기 등의 부작용을 일으킬 수 있습니다. 쓰기 작업의 멱등성과 데이터베이스·외부 서비스의 별도 시간 제한을 확보하세요. 강제 프로세스 격리는 `--workers`(아래)로 제공하며, 단일 프로세스에는 없습니다. ISR 취소에도 같은 제한이 있습니다.

워커 간 즉시 무효화 전파, 분산 요청 제한, 운영 환경 보안 강화는 미구현입니다. OpenTelemetry 스팬은 선택 패키지를 설치하고 트레이싱을 켰을 때 생성됩니다(관측성 참고). ASGI 적합성은 `tests/test_asgi_conformance.py`로 검증합니다(HTTP 이벤트 순서, 청크 분할 본문, 연결 해제, 실패 경로를 포함한 lifespan). 계획과 완료 기준은 [프로덕션 로드맵](../PRODUCTION_ROADMAP.md)을 참고하세요.

두 어댑터의 요청 본문은 최대 1 MiB이며 초과 시 413입니다. Axum의 기본 본문 수신 슬롯은 16개, 본문 수신 제한은 10초(408), Python 실행 슬롯은 64개입니다. 대기열은 실행 슬롯 수와 64 중 작은 값으로 제한하며 가득 차면 503입니다. 실행 및 본문 설정은 CLI에서 조정할 수 있습니다. HTTP/1.1 헤더 수신은 10초로 제한하지만 전체 연결 수는 제한하지 않습니다. 외부 ASGI 서버의 시간 제한은 해당 서버가 관리합니다. 루트가 아닌 경로의 마지막 `/`는 308로 리디렉션됩니다. 컴파일된 island는 개당 2 MiB, 전체 32 MiB 이하입니다.

## 관측성

구조화 로그가 기본으로 켜져 있습니다. `pynext` 로거는 한 줄마다 `timestamp`, `level`, `message`, `request_id`, `trace_id`(`traceparent` 헤더를 받은 경우), `route`, `status`, `latency_ms`를 담은 JSON 객체 하나를 출력합니다. 요청마다 요청 ID가 부여됩니다. 유효한 `X-Request-Id` 헤더가 있으면 그대로 사용하고, 없으면 새로 생성해 요청 전체에 전파하고 `x-request-id` 응답 헤더로 반환합니다. 로그 메시지에는 헤더·쿠키·본문·`Authorization` 값을 절대 넣지 마세요. JSON 포맷터는 자격 증명처럼 보이는 이름의 추가 필드를 버리며, 액세스 로그는 원시 URL이 아니라 라우트 패턴(예: `/blog/[slug]`)만 기록합니다.

지표는 메모리에서 수집되며 기본으로 켜져 있습니다. `app.metrics.snapshot()`은 라우트 패턴별 요청 수·상태 코드, 라우트별 지연 p50/p95/p99, Python 실행 큐 길이와 대기 시간, Python 실행 시간 대 Rust 렌더링 시간, ISR 캐시 hit/miss/stale와 재생성 성공·실패·시작, 워커 시작·타임아웃·불가용, 응답 크기와 island 번들 크기를 반환합니다. `app.metrics.prometheus_text()`는 같은 카운터를 Prometheus 텍스트 형식으로 노출합니다. 지연 샘플은 라우트당 2048개로 제한됩니다. Rust가 Python을 거치지 않고 제공하는 신선한 ISR 히트는 여기서 집계되지 않습니다. Python dispatch에 도달한 ISR 결과만 집계됩니다.

OpenTelemetry 트레이싱은 기본으로 꺼져 있으며 선택 사항입니다. `opentelemetry-api`(및 원하는 SDK/익스포터)를 설치하고 `--tracing` 또는 `PYNEXT_TRACING=1`로 켜세요. 켜면 `pynext.request`와 `pynext.render` 스팬이 생성되고 들어온 W3C `traceparent`/`tracestate` 컨텍스트가 전파됩니다. 패키지가 설치돼 있지 않으면 트레이싱은 no-op이며 아무것도 깨지지 않습니다.

CLI 플래그 `--structured-logs` / `--no-structured-logs`, `--metrics` / `--no-metrics`, `--tracing` / `--no-tracing`, 환경 변수 `PYNEXT_STRUCTURED_LOGS`, `PYNEXT_METRICS`, `PYNEXT_TRACING`, `PYNEXT_LOG_LEVEL`, 또는 코드에서 `create_app(..., observability=ObservabilityConfig(...))`로 설정합니다.

기본 관측성 설정(구조화 로그 + 지표 켜짐, 트레이싱 꺼짐)의 측정 오버헤드는 `benchmarks/bench.py --mode axum --requests 1000 --concurrency 10 --path / --production`으로 빌드된 예제에서 각 2회씩 측정했습니다. 다른 작업이 같은 VM에서 빌드·테스트 중이어서 절대 지연이 부풀려졌고 실행 간 편차가 큽니다. 켜짐: 25.4 / 14.2 req/s, p50 309 / 586 ms, 프로세스 CPU 4.79 / 5.02초. 꺼짐(`PYNEXT_STRUCTURED_LOGS=0 PYNEXT_METRICS=0`): 18.7 / 18.5 req/s, p50 437 / 478 ms, CPU 5.01 / 4.69초. 평균은 켜짐 19.8 req/s·p50 447 ms 대 꺼짐 18.6 req/s·p50 457 ms로, 실행 간 편차를 벗어나는 오버헤드는 없습니다(켜짐 실행은 stderr에 JSON 로그 약 1000줄도 함께 썼습니다). 더 엄밀한 수치는 조용한 머신에서 다시 측정하세요.

## 요청 속도 제한

요청 속도 제한은 선택 사항이며 프로세스별 토큰 버킷입니다. `pynext start --rate-limit 100 --rate-limit-window 60`은 클라이언트 IP당 60초에 100요청을 허용합니다(버스트가 곧 제한). 제한을 초과한 동적 요청은 초 단위 `Retry-After` 헤더와 함께 429를 받습니다. 키는 클라이언트 IP이며, `X-Forwarded-For`는 `--trusted-proxy`(IP 또는 CIDR, 반복 가능)로 지정한 피어에서만 신뢰합니다. 신뢰하지 않는 프록시의 헤더는 위조가 가능하므로 무시합니다. 코드에서는 `create_app(path, rate_limit=RateLimiter(100, 60, trusted_proxies=["10.0.0.0/8"]))`처럼 사용합니다. 버킷은 프로세스별이며 재시작 시 사라지고, 분산 속도 제한은 아직 미구현입니다. 정적 파일과 신선한 ISR 페이지는 Python 브리지에 도달하기 전에 Rust 계층에서 제공되므로 속도 제한이 적용되지 않습니다.

전달된 주소는 오른쪽부터 신뢰하는 프록시를 거슬러 가장 가까운 비신뢰 주소를 사용합니다. 모든 중간 프록시를 정확히 등록해야 합니다. 프로세스당 버킷은 최대 10,000개이며 모두 사용 중이면 버킷이 완전히 충전될 때까지 새 주소에 429를 반환합니다.

여러 워커: `pynext start --workers 4`는 한 포트를 공유하는 워커 프로세스 4개를 띄우는 수퍼바이저를 실행합니다(SO_REUSEPORT, Unix 전용, 고정 `--port` 필요). 수퍼바이저는 종료된 워커나 하트비트가 멈춘 워커를 재시작하고, SIGINT/SIGTERM을 모든 워커에 전달해 정상 종료합니다(최대 5초, 이후 SIGKILL). 각 워커는 별도 프로세스로 인터프리터·이벤트 루프·ISR 캐시·속도 제한 버킷을 따로 가지며 상태를 공유하지 않습니다. 워커가 요청 처리 중 죽으면 그 요청은 실패하고 클라이언트가 재시도해야 합니다. 기본값은 워커 1개로 기존 단일 프로세스와 동일합니다. 이벤트 루프를 막는 핸들러는 하트비트 스레드를 멈추지 않으므로, 멈춘 핸들러는 재시작이 아니라 30초 요청 시간 제한으로 처리됩니다.

## 테스트와 문제 해결

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

라우터와 HTML 직렬화에는 직접 고른 예제 외에 property-based 테스트도 있습니다. Python 쪽은 hypothesis(`tests/test_property_router_render.py`, 고정 시드·40개 예제로 CI를 빠르게 유지), Rust 쪽은 proptest(`crates/pynext-router`와 `crates/pynext-native` 단위 테스트, 각 64케이스)로 라우트 매칭 왕복, 이스케이프 누수 없음, void 태그 처리, 임의 트리 렌더링을 검사합니다. `embed`은 void 목록에 있지만 차단 태그 목록에도 있어 렌더링되지 않으므로 제외합니다.

ASGI 적합성은 `tests/test_asgi_conformance.py`로 검증합니다. HTTP 이벤트 순서, 다중 청크 본문과 1 MiB 크기 경계, 본문 읽기 중·느린 핸들러 실행 중 연결 해제, 실패 시 `lifespan.startup.failed`/`lifespan.shutdown.failed`를 포함한 lifespan 시작·종료, 스트리밍 응답 프레임 순서를 다룹니다. lifespan 실패 시 `.failed` 메시지를 보내는 것은 이 스위트와 함께 고친 동작입니다.

예제 빌드 후 Chromium 검사를 실행합니다.

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

`uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4`로 작은 폐쇄형 부하 검사를 실행합니다. 전체 시도와 성공한 2xx 요청의 초당 처리량, 오류·상태 코드, 전체·성공 요청의 p50/p95/p99, 응답 크기, CPU와 최대 RSS를 따로 기록합니다. 기본 모드의 CPU와 프로세스 생애 최대 RSS에는 서버와 부하 생성기가 모두 포함됩니다. 별도 로컬 서버는 `--base-url http://127.0.0.1:3000`으로 지정하며 Linux에서는 `--server-pid <PID>`로 해당 프로세스와 스레드의 CPU를 따로 측정할 수 있습니다. 자식 워커는 포함되지 않습니다. CI는 프로덕션 모드(`--production`, 빌드된 예제 사용)로 측정한 작은 부하 결과를 OS × 배포 모드(Axum/Uvicorn) 매트릭스 잡마다 따로 저장하지만 성능·메모리 회귀의 자동 임계값은 아직 적용하지 않습니다. `--compare`는 axum·uvicorn·starlette loopback HTTP 그룹을 동일 조건으로 실행합니다. 2026-11-08에 기록한 비교(`/api/users`, 1000요청, 동시성 10)는 `benchmarks/results-2026-11-08.json`에 있습니다. 모드와 주의점은 `docs/BENCHMARKING.md`를 참고하세요.

- 소스가 변경되었다는 오류가 나면 `pynext build`를 다시 실행합니다.
- esbuild가 없으면 저장소 루트에서 `npm ci`를 실행하거나 `PYNEXT_ESBUILD`에 실행 파일을 지정합니다.
- Windows에서는 npm esbuild를 Node로 실행합니다. `PYNEXT_ESBUILD`를 직접 설정할 때는 `.cmd`·`.bat` 대신 네이티브 `esbuild.exe`를 지정하세요.
- 동적 SSG 경로가 404이면 `generate_static_params()`에 경로를 추가하고 재빌드합니다.
- hydration이 일치하지 않으면 서버 fallback과 클라이언트 `render` 결과를 결정적으로 동일하게 만듭니다.
- ISR 결과가 워커마다 다르면 캐시 백엔드를 확인합니다. 메모리 백엔드는 프로세스별이며, SQLite 백엔드에서도 각 워커의 프로세스별 복사본은 공유 무효화 후 TTL이 끝날 때까지 유지될 수 있습니다.
