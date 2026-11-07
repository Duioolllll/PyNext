<p align="center">
  <img src="assets/pynext-logo-refracted-light.gif" alt="PyNext" width="640" />
</p>

# PyNext — 초기 프로토타입

**Manual:** [English](docs/en/manual.md) · [한국어](docs/ko/manual.md) · [日本語](docs/ja/manual.md) · [简体中文](docs/zh-CN/manual.md)

**Wiki:** [English](docs/wiki/en/Home.md) · [한국어](docs/wiki/ko/Home.md) · [Wiki index](docs/wiki/README.md)

**최신 릴리스:** [v0.6.0a2](https://github.com/Duioolllll/PyNext/releases/tag/v0.6.0a2) · 이 브랜치는 미출시 v0.7.0a1 후보 · [변경 이력](CHANGELOG.md) · [아키텍처 명세](docs/ARCHITECTURE_SPEC.md) · [프로덕션 로드맵](docs/PRODUCTION_ROADMAP.md)

이 문서는 현재 체크아웃한 저장소의 코드를 설명합니다.

PyNext는 Next.js의 파일 기반 라우팅 경험을 참고해 Python과 Rust로 구현하는 실험적 풀스택 프레임워크입니다. 이 저장소는 구현의 출발점이며, Next.js나 React의 호환 구현은 아닙니다.

현재는 Next.js 원본의 경로 정렬·충돌 검사를 참고해 Rust 라우터를 구현하고, Python 페이지·API 및 Rust HTML 렌더러와 연결했습니다. 참고한 원본의 고정 커밋과 MIT 라이선스는 [third_party/NEXTJS.md](third_party/NEXTJS.md)에 있습니다.

## 실행

지원 환경은 CI 매트릭스로 검증합니다. OS는 Ubuntu/macOS/Windows 최신 버전,
Python은 CPython 3.13만 지원합니다(3.14는 아직 검증되지 않아 매트릭스에
없습니다). 배포 모드는 독립 실행 Axum과 Uvicorn 위의 ASGI이며, ASGI 인프로세스
모드는 pytest가 모든 CI 잡에서 검사합니다. Rust 툴체인과
[uv](https://docs.astral.sh/uv/)가 필요합니다. 예제의 islands를 빌드하려면
Node.js 18 이상과 npm도 필요합니다. 브라우저 스모크는 세 OS에서 모두
실행합니다(Linux는 `--with-deps` 포함, macOS/Windows는 제외).

```bash
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
```

브라우저에서 `http://127.0.0.1:3000/`, `/blog/hello`, `/about`, `/guides/intro`, `/news/example`, `/hydrate`, `/strategies`, `/interaction`, `/stream`, `/api/users`를 확인할 수 있습니다. `dev` 명령은 빌드 없이 소스에서 시작하고, `app/`·`islands/`·`public/`·`pynext.config.py` 변경을 감지해 같은 포트로 서버를 다시 시작합니다.

```bash
uv run pynext dev --dir examples/basic
```

외부 ASGI 서버를 사용하는 방법:

```bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
```

이 예제 ASGI 진입점은 개발 모드입니다. 운영 모드에서는 빌드 후 ASGI
진입점에서 `create_app(project_path, production=True)`로 생성해야 SSG 산출물을 사용합니다.

## 앱 작성

```text
app/
├── layout.py
├── page.py
├── blog/[slug]/page.py
└── api/users/route.py
public/
└── styles/site.css
islands/
└── counter.ts
```

```python
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
```

```python
from pynext import JSONResponse, route

@route("GET")
async def users():
    return JSONResponse({"users": ["Alice", "Bob"]})
```

`request.query`를 읽을 때 잘못된 UTF-8 쿼리 문자열과 `await request.json()`을 읽을 때 잘못된 JSON 본문은 `BadRequest` 예외가 발생하며 HTTP 400으로 응답합니다. 쿼리의 퍼센트 인코딩을 푼 결과도 UTF-8로 검사합니다. JSON의 `NaN`과 `Infinity`도 허용하지 않습니다. 파싱은 해당 속성이나 메서드를 읽을 때 이루어지므로, 읽지 않는 요청에는 적용되지 않습니다. 앱에서 직접 `from pynext import BadRequest`로 불러와 입력 검증 오류에 사용할 수도 있습니다. 클라이언트에는 예외의 상세 내용 대신 `Bad Request`만 전송합니다. 요청 본문은 최대 1 MiB이며 초과하면 413입니다.

상태 변경 API에는 CSRF 보호를 선택적으로 적용할 수 있습니다(명세 v0.2 §11). 아직 인증 시스템이 없으므로 double-submit 방식을 사용합니다. `new_csrf_token()`으로 토큰을 발급해 `set_csrf_cookie()`로 `pynext_csrf` 쿠키에 담고, 클라이언트는 같은 값을 `X-CSRF-Token` 헤더(또는 `csrf_token` 폼 필드)로 돌려보냅니다. 핸들러에 `@csrf_protect`를 붙이거나 `create_app(..., security=SecurityConfig(csrf_enabled=True))`로 모든 상태 변경 API를 보호할 수 있으며, 토큰이 없거나 일치하지 않으면 403을 반환합니다. 자세한 내용은 [사용 설명서](docs/ko/manual.md)를 참조하세요.

교차 출처 요청은 기본적으로 허용하지 않습니다(명세 v0.2 §11). `SecurityConfig(cors_allowed_origins=("https://example.com",), ...)`로 origin·method·header를 명시하면 해당 출처의 preflight에 204와 `Access-Control-Allow-*` 헤더를 반환하고, 실제 요청에도 허용 헤더를 붙입니다. 허용되지 않은 출처의 preflight는 403이며, 실제 요청에는 CORS 헤더를 붙이지 않아 브라우저가 응답을 차단합니다. 같은 출처 요청은 CORS로 차단하지 않습니다. 자세한 내용은 [사용 설명서](docs/ko/manual.md)를 참조하세요.

`SecurityConfig(csp_nonce_enabled=True)`로 설정하면 동적 렌더링 페이지마다 요청별 새 CSP nonce가 발급돼 헤더와 island 런타임 `<script>` 태그에 같은 nonce가 들어갑니다(명세 v0.2 §11). 미리 렌더링된 SSG 페이지와 캐시된 ISR 응답은 nonce 없는 정적 CSP 정책을 유지합니다. 자세한 내용은 [사용 설명서](docs/ko/manual.md)를 참조하세요.

### 비동기 응답 스트리밍

API 핸들러는 `StreamingResponse`에 비동기 iterable을 전달할 수 있습니다. 아래 예제와 [실행 가능한 NDJSON 예제](examples/basic/app/api/stream/route.py)는 ASGI와 Axum에서 청크를 순서대로 전송합니다.

```python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield "first\n"
        yield "second\n"
    return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")
```

청크는 문자열이나 바이트이며 개당 최대 1 MiB입니다. 프레이밍은 HTTP 서버가 처리하므로 `Content-Length`와 `Transfer-Encoding`을 직접 지정할 수 없습니다. 클라이언트 연결이 끊기거나 송신이 실패하면 비동기 생성기를 닫습니다. Axum은 청크 하나를 대기시키며 다음 청크 생성과 송신 대기 각각에 30초 제한을 둡니다. 헤더를 보낸 뒤 생성 중 오류가 나면 응답을 중단하며 이미 보낸 상태 코드를 바꿀 수 없습니다.

페이지 함수도 UI 노드의 비동기 iterable을 반환하면 스트리밍 SSR로 동작합니다. 레이아웃 셸을 먼저 보내고 각 청크를 생성되는 대로 직렬화해 전송한 뒤 레이아웃 닫는 부분을 보냅니다. 기본값은 여전히 전체 버퍼를 만든 뒤 보내는 방식이며, 스트리밍은 SSR 페이지 전용입니다(SSG·ISR 페이지는 전체 본문이 필요합니다). 레이아웃은 자식을 정확히 한 번 렌더링해야 합니다. 연결이 끊기면 생성기를 닫고, 헤더 전송 후 오류가 나면 스트림을 중단합니다. [실행 가능한 예제](examples/basic/app/stream/page.py)는 `/stream`에서 확인할 수 있습니다.

```python
from pynext import h, page

@page()
async def Page():
    yield h("h1", {}, "Streaming SSR")
    yield h("p", {}, "Each chunk is sent as it is produced.")
```

### 클라이언트 탐색

island가 있는 페이지에서는 같은 출처 링크를 클릭하면 전체 새로고침 없이 본문만 교체해 이동하고, 호버·포커스·뷰포트 진입 시 링크를 미리 가져옵니다(최대 20개, 개당 1 MiB). 실패 시에는 전체 문서 이동으로 되돌아갑니다. 자세한 내용은 [매뉴얼](docs/ko/manual.md#클라이언트-탐색)을 참고하세요.

### Python 클라이언트 DSL (실험적)

`pynext.client_dsl.compile_client_dsl(source)`은 Python의 작고 고정된 부분집합을 JavaScript로 컴파일합니다(명세 §7.3). 리터럴, 산술·비교·불 연산, `if`/`for`/`while`, 함수 정의, `len`·`str`·`int`·`abs`·`min`·`max`·`.append()` 등을 지원하고, import·리플렉션·파일 I/O·서버 전용 이름(`open`, `os`, `Request`, `h` 등)은 컴파일 오류로 거부합니다. 자세한 목록과 의미 차이는 `pynext.client_dsl.SUPPORTED`와 `KNOWN_DIFFERENCES`를 참고하세요.

### WebSocket

`route.py`는 `@websocket` 핸들러를 하나 내보낼 수 있습니다. 핸들러는 `WebSocket`을 받아 `await websocket.accept()` 후 텍스트·바이너리 메시지를 주고받습니다. 상대가 끊으면 `receive()`에서 `WebSocketDisconnect`가 발생합니다. 메시지 1개는 64 KiB까지이며, 서브프로토콜 협상·브로드캐스트는 없습니다. [echo 예제](examples/basic/app/ws/echo/route.py)를 참고하세요.

경로는 정적 경로, `[slug]`, 마지막 위치의 `[...path]`, 0개 이상의 세그먼트를 매칭하는 `[[...path]]`를 지원합니다. 우선순위는 정적 > 단일 동적 > catch-all > 선택적 catch-all입니다. catch-all 파라미터는 문자열 리스트이고 단일 동적 파라미터는 문자열입니다. 필수 catch-all과 선택적 catch-all을 같은 레벨에 함께 쓸 수 없습니다. 괄호 디렉터리 `app/(marketing)/` 같은 라우트 그룹은 URL에 나타나지 않고, 그룹 안의 레이아웃은 그대로 적용됩니다. 병렬·인터셉트 라우트는 아직 지원하지 않습니다. 빌드 시 경로 충돌과 잘못된 핸들러를 검사하고 `.pynext/manifest.json`에 소스 해시를 기록합니다. `start`는 빌드 후 소스가 바뀌면 재빌드를 요구합니다.

### 정적 페이지 생성

공개 페이지에 `render="ssg"`를 지정하면 빌드 시 HTML을 생성합니다. 동적 경로는 생성할 매개변수를 명시합니다.

```python
# app/guides/[slug]/page.py
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
```

`[slug]`는 문자열, `[...path]`는 비어 있지 않은 문자열 리스트를 받습니다. 중복·잘못된 값이나 우선순위가 높은 다른 경로에 가려지는 결과는 빌드 오류입니다. 빌드에 포함되지 않은 동적 경로는 운영 모드에서 `404`입니다. `dev`에서는 SSG 페이지도 요청마다 렌더링하며 생성 목록을 적용하지 않습니다.

SSG는 **모든 방문자에게 같은 내용을 제공하는 페이지에만** 사용하세요. 페이지 함수는 `request`를 받을 수 없으며 쿼리 문자열, 인증 헤더와 쿠키는 생성 결과를 바꾸지 않습니다. 앱 코드가 읽는 환경 변수나 외부 데이터가 공개해도 되는 값인지 확인해야 합니다. 데이터가 바뀌면 다시 빌드해야 합니다.

생성된 HTML은 내용 해시로 저장하며, 빌드가 실패하면 기존 manifest를 교체하지 않습니다. 운영 시작 시 파일의 해시를 확인하고 메모리에 올립니다. Axum은 생성된 경로의 GET/HEAD를 Python 호출 없이 제공하며, ASGI 모드는 Python ASGI 진입 후 같은 결과를 제공합니다.

### 브라우저 islands

상호작용이 필요한 부분은 Python의 `island()`와 명시적인 JavaScript 또는 TypeScript 모듈로 작성합니다.

```python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island("counter", props={"initial": 1},
                                 fallback=h("p", {}, "JavaScript가 필요합니다.")))
```

```typescript
// islands/counter.ts
export function mount(element: HTMLElement, props: { initial: number }) {
  let value = props.initial;
  const button = document.createElement("button");
  button.textContent = String(value);
  const click = () => { button.textContent = String(++value); };
  button.addEventListener("click", click);
  element.replaceChildren(button);
  return () => button.removeEventListener("click", click);
}
```

빌드 시 esbuild가 각 최상위 island 진입점을 독립적인 브라우저 모듈로 묶고 내용 해시로 저장합니다. 페이지는 공통 로더와 실제 사용된 island만 내려받습니다. `mount`가 반환한 함수는 해당 요소가 제거되면 정리 함수로 호출됩니다. JavaScript가 꺼져 있으면 fallback이 남습니다. 기본 방식은 클라이언트 mount입니다.

서버가 출력한 DOM을 재사용하려면 `hydrate=True`와 서버에서 그릴 `fallback`을 지정합니다.

```python
island("hydrated-counter", props={"initial": 3}, hydrate=True,
       fallback=h("button", {}, "Hydrated: 3"))
```

해당 모듈은 `render(props)`, `hydrate(element, props)`, `mount(element, props)`를 모두 내보내야 합니다. `render`는 서버의 fallback과 같은 내용을 나타내는 **분리된 DOM 노드**를 반환하고, `hydrate`는 기존 노드에 이벤트를 연결합니다. 반환한 정리 함수도 요소 제거 시 호출됩니다. 전체 예시는 [hydrated-counter.ts](examples/basic/islands/hydrated-counter.ts)에 있습니다.

브라우저는 기존 DOM과 `render` 결과를 비교합니다. 일치하면 기존 노드를 보존하고 `hydrate`를 호출합니다. 다르면 경고와 `data-pynext-mismatch` 표시를 남기고 `mount`로 해당 island만 다시 그립니다. 이 복구는 해당 영역의 입력 상태나 포커스를 잃을 수 있습니다. 시간·난수·로케일 등에 의한 차이가 생기지 않도록 양쪽 출력을 결정적으로 작성해야 합니다. 활성화 중 예외가 나면 `data-pynext-error="activation-failed"`를 표시합니다.

`strategy`로 각 island의 활성화 시점을 선택할 수 있습니다. `interaction`은 hydration 전용입니다.

```python
island("chart", props={"value": 42}, strategy="visible",
       fallback=h("p", {}, "차트를 불러오는 중입니다."))
```

| 전략 | 동작 |
|---|---|
| `load` | 기본값. 문서가 준비되면 활성화합니다. |
| `visible` | 요소가 화면에 들어오면 모듈을 불러옵니다. IntersectionObserver를 사용할 수 없으면 idle 방식으로 대체합니다. |
| `idle` | 브라우저가 한가할 때 활성화하며, idle 콜백에 2초 timeout을 요청합니다. 실제 실행은 브라우저 상황에 따라 늦어질 수 있고, 해당 API가 없으면 타이머를 사용합니다. |
| `interaction` | `hydrate=True`인 island의 `<button type="button">`에 사용자가 클릭하면 활성화합니다. [예제](examples/basic/app/interaction/page.py)를 참고하세요. |

`interaction`은 로더가 해당 island의 리스너를 등록한 후 들어온 지원 클릭을 최대 16개 보관하고 DOM이 일치해 hydration이 끝나면 순서대로 재생합니다. 넘치는 클릭은 버리고 경고합니다. 재생 이벤트는 합성 이벤트여서 `isTrusted`가 `false`이며, 브라우저의 사용자 제스처가 필요한 동작을 보장하지 않습니다. 외부 캡처 리스너는 원본과 재생 이벤트를 모두 볼 수 있습니다. 링크·폼 제출·입력 등은 가로채거나 재생하지 않습니다. DOM 불일치, 요소 제거 또는 활성화 실패 시 대기 중인 클릭을 버립니다. 리스너 등록 전 이벤트는 보관할 수 없습니다. `load`, `visible`, `idle`은 이벤트를 재생하지 않습니다.

- 프로젝트 루트의 `islands`에 최상위 `.js` 또는 `.ts` 파일을 두고 이름이 `mount`인 함수를 내보냅니다. 보조 모듈은 하위 디렉터리에 둘 수 있습니다.
- 초기 버전은 `islands` 밖의 모듈, npm 패키지, 외부 URL과 동적 경로 import를 묶지 않습니다. CSS import와 중첩 island도 지원하지 않습니다.
- island 진입점은 최대 256개, 소스 파일은 10,000개·총 10 MiB 이하이며 파일 하나는 1 MiB 이하입니다.
- `props`는 공개되는 JSON 객체입니다. 비밀 값을 넣지 마세요. 크기는 16 KiB 이하이고 숫자는 JavaScript에서 안전하게 표현할 수 있어야 합니다.
- islands가 있는 앱을 빌드하거나 개발 서버로 시작할 때 esbuild가 필요합니다. 프로젝트의 `node_modules/.bin/esbuild`, PATH 또는 `PYNEXT_ESBUILD` 환경 변수에서 찾습니다. 빌드가 끝난 운영 서버에는 esbuild가 필요하지 않습니다.
- Windows에서는 npm의 esbuild를 Node로 실행합니다. `PYNEXT_ESBUILD`를 직접 설정할 때는 `.cmd`나 `.bat` 대신 네이티브 `esbuild.exe`를 지정해야 합니다.
- 별도의 앱 프로젝트에서는 `npm install --save-dev --save-exact esbuild@0.28.1`로 예제와 같은 버전을 설치할 수 있습니다.
- `/_pynext`는 프레임워크 내부 경로로 예약되어 있습니다. 앱과 공개 파일에는 사용할 수 없습니다.

### 주기적으로 재생성하는 페이지

```python
from pynext import h, page

@page(render="isr", revalidate=30, stale=120)
async def Page(params):
    article = await load_public_article(params["slug"])
    return h("h1", {}, article.title)
```

ISR은 첫 요청에서 공개 HTML을 생성하고 `revalidate`초 동안 재사용합니다. 이후 `stale`초 동안은 이전 결과를 보내면서 백그라운드에서 재생성합니다. 같은 경로의 동시 요청은 하나의 생성 작업을 공유합니다. 재생성이 실패하면 기존 결과는 stale 기간까지만 제공하며, 그 뒤에는 새 생성이 완료될 때까지 기다립니다. `stale`의 기본값은 60초이며 0으로 설정하면 만료 후 기다립니다.

ISR은 **공개 콘텐츠 전용**입니다. 페이지 함수는 `request`를 받을 수 없고, 익명 요청의 쿼리 문자열은 캐시 키에 포함되지 않습니다. 공유 결과를 만드는 작업은 요청의 `contextvars`를 상속하지 않습니다. `Cookie`나 `Authorization` 헤더가 있는 요청은 공유 캐시를 건너뛰어 개별 렌더링하고 `private, no-store`로 응답합니다. 앱의 전역 변수나 외부 데이터에서 사용자별 정보가 공유 결과에 섞이지 않도록 개발자가 보장해야 합니다.

이 구현은 기본적으로 프로세스별 메모리 캐시입니다. 최대 1,024개 항목·32 MiB, 항목당 2 MiB까지 저장하고 오래 사용하지 않은 항목을 제거합니다. 더 큰 페이지는 응답하지만 캐시하지 않습니다. 서버를 재시작하면 캐시가 비워지고, 여러 워커는 결과를 공유하지 않습니다. 여러 워커가 결과를 공유하려면 `--cache-backend sqlite --cache-path <파일>`로 시작하거나 `create_app(..., cache_backend="sqlite", cache_path=...)`로 만듭니다. SQLite 파일(WAL)을 모든 워커가 함께 쓰며, stale 재생성은 만료·소유 토큰이 있는 lease로 한 워커만 수행하고 저장 시 버전과 유효한 lease 소유권을 원자적으로 확인합니다. 공유 저장소가 실패하면 워커별 로컬 캐시로 대체합니다. 각 워커는 프로세스별 캐시를 앞에 두므로 다른 워커의 무효화는 공유 항목에 즉시 반영되지만 로컬 복사본은 남은 TTL 동안 유지될 수 있습니다. `Set-Cookie`나 `Cache-Control: private/no-store` 응답은 공유 캐시에 저장하지 않습니다. Redis 어댑터는 이후 과제입니다. 개발 모드에서는 캐시 없이 매번 렌더링합니다. 동적 ISR 경로는 요청 시 생성하며 `generate_static_params()`로 미리 생성하는 기능은 아직 없습니다.

인증과 권한을 확인한 API 핸들러에서는 `await request.revalidate("/news/example")`로 같은 앱 인스턴스의 ISR 경로를 무효화할 수 있습니다. 앱을 직접 관리할 때는 `await app.revalidate(path)`를 사용합니다. 경로는 쿼리 문자열이 없는 디코딩된 경로이며 기존 캐시나 진행 중인 생성이 있었다면 `True`를 반환합니다. 이미 진행 중인 요청은 이전 결과를 받을 수 있지만 그 생성 결과가 무효화 후 캐시를 덮어쓰지는 않습니다. 메모리 백엔드에서는 다른 프로세스·워커에 전파되지 않으며, SQLite 백엔드에서는 공유 항목이 제거되지만 다른 워커의 프로세스별 복사본은 남은 TTL 동안 유지될 수 있습니다. 무효화 API를 외부에 노출할 때는 앱이 인증·권한 검사를 해야 합니다.

### 공개 파일

`public/styles/site.css` 같은 파일은 `/styles/site.css`로 제공됩니다. 공개 파일은 누구나 읽을 수 있으므로 비밀 값이나 공개하지 않을 파일을 넣으면 안 됩니다. 빌드 시 파일을 해시와 함께 저장하고, 운영 시작 시 소스와 산출물의 무결성을 확인합니다. Axum은 GET/HEAD를 Python 호출 없이 처리하고, ASGI 모드도 같은 내용을 반환합니다.

- 파일은 정확히 일치하는 동적 페이지보다 우선합니다. 동일한 정적 페이지·API 경로와 충돌하거나 생성된 SSG 페이지를 가리는 경우 빌드가 실패합니다.
- 경로 이탈을 막기 위해 `public` 내의 심볼릭 링크와 숨김 파일·디렉터리를 거부합니다. 파일은 16 MiB 이하, 전체는 128 MiB 및 10,000개 이하로 제한합니다.
- 파일은 프로세스 시작 시 메모리에 적재합니다. 개발 모드에서 파일을 바꾸면 서버가 자동으로 다시 시작하며, 운영 모드에서는 재빌드가 필요합니다.
- 알려진 확장자는 해당 콘텐츠 형식으로, 그 외에는 `application/octet-stream`으로 제공합니다. `nosniff`와 재검증 캐시 헤더를 설정합니다. 공개 파일·SSG·island 번들은 내용 기반 ETag, `If-Match`/`If-None-Match`, 단일 `Range` 및 ETag 기반 `If-Range`를 지원합니다. 다중 범위는 무시하고 전체를 반환하며 날짜 기반 검증은 아직 지원하지 않습니다. ISR 응답에는 이 기능이 적용되지 않습니다.

## 관측성 (구조화 로그·지표·트레이싱)

구조화 로그가 기본으로 켜져 있습니다. `pynext` 로거는 요청마다 `timestamp`, `level`, `message`, `request_id`, `trace_id`, `route`, `status`, `latency_ms`를 담은 JSON 객체 한 줄을 출력합니다. 요청 ID는 유효한 `X-Request-Id` 헤더를 그대로 쓰거나 새로 생성해 요청 전체에 전파하고 `x-request-id` 응답 헤더로 반환합니다. 로그에는 헤더·쿠키·본문·`Authorization` 값을 넣지 않으며, 액세스 로그는 원시 URL이 아니라 라우트 패턴(예: `/blog/[slug]`)만 기록합니다.

지표는 메모리에서 수집되며 기본으로 켜져 있습니다. `app.metrics.snapshot()`으로 라우트 패턴별 요청 수·상태 코드, 라우트별 지연 p50/p95/p99, Python 실행 큐 길이·대기 시간, Python 실행 시간 대 Rust 렌더링 시간, ISR 캐시 hit/miss/stale와 재생성 성공·실패·시작, 워커 시작·타임아웃·불가용, 응답 크기와 island 번들 크기를 확인할 수 있고, `app.metrics.prometheus_text()`는 같은 카운터를 Prometheus 텍스트 형식으로 노출합니다. Rust가 Python을 거치지 않고 제공하는 신선한 ISR 히트는 집계되지 않습니다.

OpenTelemetry 트레이싱은 기본으로 꺼져 있으며 선택 사항입니다. `opentelemetry-api`(및 원하는 SDK/익스포터)를 설치하고 `--tracing` 또는 `PYNEXT_TRACING=1`로 켜면 `pynext.request`·`pynext.render` 스팬이 생성되고 들어온 W3C `traceparent`/`tracestate`가 전파됩니다. 패키지가 없으면 트레이싱은 no-op이며 아무것도 깨지지 않습니다. CLI 플래그(`--structured-logs`/`--no-structured-logs`, `--metrics`/`--no-metrics`, `--tracing`/`--no-tracing`), 환경 변수(`PYNEXT_STRUCTURED_LOGS`, `PYNEXT_METRICS`, `PYNEXT_TRACING`, `PYNEXT_LOG_LEVEL`), 또는 `create_app(..., observability=ObservabilityConfig(...))`로 설정합니다. 자세한 내용은 [영문 매뉴얼](docs/en/manual.md)의 관측성 절을 참고하세요.

## 현재 상태와 제한

- SSR은 기본적으로 Python에서 UI 트리를 만들고 Rust가 이스케이프해 직렬화하는 buffered 렌더링입니다. 페이지 함수가 UI 노드의 비동기 iterable을 반환하면 레이아웃 셸을 먼저 보내고 청크 단위로 스트리밍합니다. 문자열을 raw HTML로 삽입하는 API는 없습니다.
- Axum의 기본 본문 수신 슬롯은 16개, Python 실행 슬롯은 64개이며 CLI에서 조정합니다. 본문 수신 제한은 기본 10초이고 HTTP/1.1 헤더 수신에도 10초 제한이 있습니다. 실행 대기열은 최대 실행 슬롯 수와 64 중 작은 값으로 제한합니다. 정적 파일과 신선한 ISR 캐시는 Python 슬롯을 건너뜁니다. 연결 총수는 제한하지 않으며 동기 함수는 504 또는 연결 해제 후에도 스레드에서 계속 실행될 수 있습니다. DB·외부 서비스 시간 제한과 쓰기 멱등성을 별도로 확보해야 합니다. 외부 ASGI 서버의 시간 제한은 해당 서버가 관리합니다.
- 요청 속도 제한은 선택 사항이며 프로세스별 토큰 버킷입니다. `pynext start --rate-limit 100 --rate-limit-window 60`은 클라이언트 IP당 60초에 100요청을 허용하고, 초과한 동적 요청에 429와 초 단위 `Retry-After`를 반환합니다. `X-Forwarded-For`는 `--trusted-proxy`(IP 또는 CIDR, 반복 가능)로 지정한 피어에서만 신뢰합니다. 정적 파일과 신선한 ISR 페이지는 Python 브리지에 도달하기 전에 Rust 계층에서 제공되므로 속도 제한이 적용되지 않으며, 분산 속도 제한은 아직 미구현입니다.
  전달된 주소는 오른쪽부터 신뢰하는 프록시를 거슬러 가장 가까운 비신뢰 주소를 사용합니다. 모든 중간 프록시를 정확히 등록해야 합니다. 프로세스당 버킷은 최대 10,000개이며 모두 사용 중이면 기존 버킷을 제거하지 않고 새 주소에 429를 반환합니다.
- ASGI HTTP와 기본 lifespan, 비동기 API 응답 스트리밍과 연결 해제 시 비동기 작업 취소를 지원합니다. WebSocket도 ASGI와 Axum에서 지원합니다(텍스트·바이너리, close 코드, 64 KiB 메시지 제한). ASGI 적합성은 `tests/test_asgi_conformance.py`로 검증합니다(HTTP 이벤트 순서, 다중 청크 본문, 본문 크기 경계, 연결 해제, lifespan 시작·종료와 실패 시 `lifespan.*.failed` 응답). 강제 프로세스 격리는 `--workers`로 제공합니다.
- `--workers N`으로 여러 워커 프로세스를 띄울 수 있습니다(Unix 전용, 고정 포트 필요). 상위 프로세스(수퍼바이저)가 워커를 감시하고 비정상 종료 시 재시작하며, SIGINT/SIGTERM 시 모든 워커를 정상 종료합니다. 워커는 각자 별도 프로세스·속도 제한 버킷을 가지며, ISR 캐시는 메모리 백엔드에서는 워커별이고 `--cache-backend sqlite`에서는 공유됩니다. 워커가 죽는 도중 처리하던 요청은 실패하고 클라이언트가 재시도해야 합니다. 기본값은 1(기존 단일 프로세스)입니다.
- ISR 기본값은 단일 프로세스 메모리 구현이며 `--cache-backend sqlite`로 워커 간 공유 캐시와 단일 생성을 쓸 수 있습니다. 워커 간 즉시 무효화 전파, 버튼 이외의 상호작용 재생, 모듈 단위 핫 리로드, 프로덕션 보안 기능은 **미구현**입니다. `pynext dev`는 파일 변경 시 서버를 재시작합니다. PyNext의 hydration은 React나 React Server Components와 호환되지 않습니다. 현재 버전을 인터넷에 직접 공개하거나 신뢰할 수 없는 앱을 실행하는 데 사용하지 마세요.
- 빌드할 때 앱 모듈을 import하므로 개발자가 작성한 Python 코드가 실행됩니다. 프로젝트를 신뢰할 수 있는 환경에서 빌드해야 합니다.
- 페이지·레이아웃·API 파일에서 `from .helpers import name` 같은 상대 import를 사용할 수 있습니다. **0.6 변경:** 프로젝트 루트를 전역 `sys.path`에 자동 추가하지 않습니다. 기존 루트 기준 절대 import가 실패하면 상대 import로 바꾸거나 프로젝트를 Python 패키지로 설치하세요.
- `start`의 manifest는 앱 디렉터리의 Python 파일과 `public`·`islands` 파일 변화를 검사하지만 앱 밖 의존성, 외부 데이터나 환경 변수 변화까지 추적하지 않습니다. 현재 운영 시작에도 원본 `public`과 `islands` 디렉터리가 필요합니다. 정적 산출물은 프로세스마다 메모리에 적재하며, 이전 빌드의 사용하지 않는 파일을 자동 정리하지 않습니다.

## 테스트

```bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest
```

ASGI 적합성은 `tests/test_asgi_conformance.py`가 검증합니다. HTTP 이벤트 순서,
다중 청크 본문과 1 MiB 크기 경계, 요청 중 연결 해제, lifespan 시작·종료와
실패 시 `lifespan.startup.failed`/`lifespan.shutdown.failed` 응답, 스트리밍
응답 프레임 순서를 다룹니다. lifespan 시작·종료 실패 시 `.failed` 메시지를
보내는 것은 이번에 고친 동작입니다.

라우터와 HTML 직렬화에는 손으로 고른 예제 외에 property-based 테스트도
있습니다. Python 쪽은 hypothesis(`tests/test_property_router_render.py`,
고정 시드·예제 40개로 CI를 빠르게 유지), Rust 쪽은 proptest
(`crates/pynext-router`와 `crates/pynext-native`의 단위 테스트, 케이스 64개)로
라우트 매칭 왕복, 이스케이프 누수 없음, void 태그 처리, 임의 트리 렌더링을
검사합니다. 렌더러의 void 목록에 있는 `embed`은 안전 태그 차단 목록에도 있어
실제로 렌더링되지 않으므로 해당 테스트에서는 제외합니다.

예제를 빌드한 뒤 다음 명령으로 실제 Chromium에서 mount·hydration·CSP·정리 동작을 검사할 수 있습니다.

```bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
```

기초 부하 측정은 `uv run --extra dev python benchmarks/bench.py --mode axum`,
`--mode uvicorn`, `--mode asgi` 또는 `--mode starlette`로 실행합니다. Axum·
Uvicorn·Starlette 모드는 모두 실제 loopback HTTP를 사용하지만 클라이언트와
서버가 같은 프로세스에서 실행됩니다. `asgi` 모드는 네트워크를 거치지 않는
ASGITransport이므로 HTTP 모드와 수치를 직접 비교하면 안 됩니다.
`--compare`는 loopback HTTP 그룹 세 가지(axum, uvicorn, starlette)를 동일
조건으로 차례로 실행해 결합 결과를 출력합니다. 2026-11-08에 `/api/users`
1000요청·동시성 10으로 측정한 결과는 `benchmarks/results-2026-11-08.json`에
있습니다. 같은 프로세스에서 잰 스모크 비교이므로 운영 처리량 주장으로
읽으면 안 됩니다. 측정 조건과 별도 프로세스 사용법은
[벤치마크 가이드](docs/BENCHMARKING.md)를 참고하세요.
빌드 후 `--production --path /about`을 추가하면 SSG 경로를 측정합니다.

결과에는 전체 시도와 성공한 2xx 요청의 초당 처리량, 오류 비율, 상태 코드별 건수,
전체·성공 요청의 p50/p95/p99, 응답 크기가 별도로 표시됩니다. `--warmup`으로
준비 요청 수를 바꿀 수 있으며 준비 요청 오류도 따로 집계합니다. 기본 모드의
프로세스 CPU 시간과 최대 RSS에는 부하 생성기와 서버가 함께 포함됩니다. RSS는
측정 구간만이 아닌 프로세스 생애 동안의 최대값입니다.

별도로 실행한 로컬 서버를 측정할 때는 `--base-url http://127.0.0.1:3000`을
지정합니다. Linux에서는 `--server-pid <PID>`를 더해 해당 서버 프로세스의 CPU
시간과 단일 코어 기준 사용률을 따로 확인할 수 있습니다. 이는 지정한 한 프로세스와
그 스레드만 포함하며 자식 워커는 포함하지 않습니다. CI는 프로덕션 모드(`--production`, 빌드된 예제 사용)의 loopback HTTP 스모크
측정값을 OS × 배포 모드(Axum/Uvicorn) 매트릭스 잡마다 따로 저장하지만,
환경에 민감한 성능·메모리 회귀의 자동 임계값은 아직 없습니다.

벤치마크 수치는 최소 비교 앱과 부분적으로 이식된 경로를 기준으로 하며,
데이터베이스·실제 사용자 트래픽·완성된 클라이언트 기능을 포함한 운영 서비스의
성능을 의미하지 않습니다. 처리량과 함께 offered load 대비 성공률, 4xx/5xx 및
503 비율, 응답 크기와 p50/p95/p99를 함께 기록하세요.

개발 단계와 Next.js 원본을 참조하는 범위는 [구현 로드맵](docs/ROADMAP.md)을 참고하세요.
## 라이선스

PyNext 자체 코드는 [MIT License](LICENSE)로 배포됩니다. `third_party/` 아래의
참고 자료는 각 파일에 표시된 별도 라이선스를 따릅니다.
