# PyNext — 초기 프로토타입

**Manual:** [English](docs/en/manual.md) · [한국어](docs/ko/manual.md) · [日本語](docs/ja/manual.md) · [简体中文](docs/zh-CN/manual.md)

**Version:** v0.4.0 · [변경 이력](CHANGELOG.md) · [프로덕션 로드맵](docs/PRODUCTION_ROADMAP.md)

PyNext는 Next.js의 파일 기반 라우팅 경험을 참고해 Python과 Rust로 구현하는 실험적 풀스택 프레임워크입니다. 이 저장소는 구현의 출발점이며, Next.js나 React의 호환 구현은 아닙니다.

PyNext 코드는 [MIT 라이선스](LICENSE)를 따릅니다. 제3자 코드 고지는 [third_party/NEXTJS.md](third_party/NEXTJS.md)에 유지합니다.

현재는 Next.js 원본의 경로 정렬·충돌 검사를 참고해 Rust 라우터를 구현하고, Python 페이지·API 및 Rust HTML 렌더러와 연결했습니다. 참고한 원본의 고정 커밋과 MIT 라이선스는 [third_party/NEXTJS.md](third_party/NEXTJS.md)에 있습니다.

## 실행

현재 검증된 Python 3.13, Rust 툴체인, [uv](https://docs.astral.sh/uv/)가 필요합니다. 예제의 islands를 빌드하려면 Node.js 18 이상과 npm도 필요합니다. 다른 Python 버전의 지원은 추후 검증할 예정입니다.

```bash
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
```

브라우저에서 `http://127.0.0.1:3000/`, `/blog/hello`, `/about`, `/guides/intro`, `/news/example`, `/hydrate`, `/strategies`, `/api/users`를 확인할 수 있습니다. `dev` 명령은 빌드 없이 소스에서 시작합니다. **현재 파일 변경 자동 반영은 없으므로 변경 후 재시작해야 합니다.**

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

`request.query`를 읽을 때 잘못된 UTF-8 쿼리 문자열과 `await request.json()`을 읽을 때 잘못된 JSON 본문은 `BadRequest` 예외가 발생하며 HTTP 400으로 응답합니다. 쿼리의 퍼센트 인코딩을 푼 결과도 UTF-8로 검사합니다. JSON의 `NaN`과 `Infinity`도 허용하지 않습니다. 파싱은 해당 속성이나 메서드를 읽을 때 이루어지므로, 읽지 않는 요청에는 적용되지 않습니다. 앱에서 직접 `from pynext import BadRequest`로 불러와 입력 검증 오류에 사용할 수도 있습니다. 클라이언트에는 예외의 상세 내용 대신 `Bad Request`만 전송합니다.

경로는 정적 경로, `[slug]`, 마지막 위치의 `[...path]`를 지원합니다. 우선순위는 정적 > 단일 동적 > catch-all입니다. catch-all 파라미터는 문자열 리스트이고 단일 동적 파라미터는 문자열입니다. 선택적 catch-all, 라우트 그룹, 병렬·인터셉트 라우트는 아직 지원하지 않습니다. 빌드 시 경로 충돌과 잘못된 핸들러를 검사하고 `.pynext/manifest.json`에 소스 해시를 기록합니다. `start`는 빌드 후 소스가 바뀌면 재빌드를 요구합니다.

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

`strategy`로 각 island의 활성화 시점을 선택할 수 있습니다. mount와 hydration 모두에서 사용할 수 있습니다.

```python
island("chart", props={"value": 42}, strategy="visible",
       fallback=h("p", {}, "차트를 불러오는 중입니다."))
```

| 전략 | 동작 |
|---|---|
| `load` | 기본값. 문서가 준비되면 활성화합니다. |
| `visible` | 요소가 화면에 들어오면 모듈을 불러옵니다. IntersectionObserver를 사용할 수 없으면 idle 방식으로 대체합니다. |
| `idle` | 브라우저가 한가할 때 활성화하며, idle 콜백에 2초 timeout을 요청합니다. 실제 실행은 브라우저 상황에 따라 늦어질 수 있고, 해당 API가 없으면 타이머를 사용합니다. |

대기 중인 요소가 제거되면 예약을 취소합니다. 지연된 island는 활성화 전의 클릭이나 입력 이벤트를 재생하지 않으므로 중요한 상호작용에는 기본 `load` 전략을 사용하세요.

- 프로젝트 루트의 `islands`에 최상위 `.js` 또는 `.ts` 파일을 두고 이름이 `mount`인 함수를 내보냅니다. 보조 모듈은 하위 디렉터리에 둘 수 있습니다.
- 초기 버전은 `islands` 밖의 모듈, npm 패키지, 외부 URL과 동적 경로 import를 묶지 않습니다. CSS import와 중첩 island도 지원하지 않습니다.
- island 진입점은 최대 256개, 소스 파일은 10,000개·총 10 MiB 이하이며 파일 하나는 1 MiB 이하입니다.
- `props`는 공개되는 JSON 객체입니다. 비밀 값을 넣지 마세요. 크기는 16 KiB 이하이고 숫자는 JavaScript에서 안전하게 표현할 수 있어야 합니다.
- islands가 있는 앱을 빌드하거나 개발 서버로 시작할 때 esbuild가 필요합니다. 프로젝트의 `node_modules/.bin/esbuild`, PATH 또는 `PYNEXT_ESBUILD` 환경 변수에서 찾습니다. 빌드가 끝난 운영 서버에는 esbuild가 필요하지 않습니다.
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

이 구현은 프로세스별 메모리 캐시입니다. 최대 1,024개 항목·32 MiB, 항목당 2 MiB까지 저장하고 오래 사용하지 않은 항목을 제거합니다. 더 큰 페이지는 응답하지만 캐시하지 않습니다. 서버를 재시작하면 캐시가 비워지고, 여러 워커는 결과를 공유하지 않습니다. 개발 모드에서는 캐시 없이 매번 렌더링합니다. 동적 ISR 경로는 요청 시 생성하며 `generate_static_params()`로 미리 생성하는 기능은 아직 없습니다.

### 공개 파일

`public/styles/site.css` 같은 파일은 `/styles/site.css`로 제공됩니다. 공개 파일은 누구나 읽을 수 있으므로 비밀 값이나 공개하지 않을 파일을 넣으면 안 됩니다. 빌드 시 파일을 해시와 함께 저장하고, 운영 시작 시 소스와 산출물의 무결성을 확인합니다. Axum은 GET/HEAD를 Python 호출 없이 처리하고, ASGI 모드도 같은 내용을 반환합니다.

- 파일은 정확히 일치하는 동적 페이지보다 우선합니다. 동일한 정적 페이지·API 경로와 충돌하거나 생성된 SSG 페이지를 가리는 경우 빌드가 실패합니다.
- 경로 이탈을 막기 위해 `public` 내의 심볼릭 링크와 숨김 파일·디렉터리를 거부합니다. 파일은 16 MiB 이하, 전체는 128 MiB 및 10,000개 이하로 제한합니다.
- 파일은 프로세스 시작 시 메모리에 적재합니다. 개발 모드에서 파일을 바꾼 뒤에도 재시작해야 하며, 운영 모드에서는 재빌드가 필요합니다.
- 알려진 확장자는 해당 콘텐츠 형식으로, 그 외에는 `application/octet-stream`으로 제공합니다. `nosniff`와 재검증 캐시 헤더를 설정합니다. 범위 요청과 조건부 요청의 최적화는 아직 지원하지 않습니다.

## 현재 상태와 제한

- SSR은 Python에서 UI 트리를 만들고 Rust가 이스케이프해 직렬화하는 buffered 렌더링입니다. 문자열을 raw HTML로 삽입하는 API는 없습니다.
- Axum 서버는 Python asyncio 루프에 요청을 전달하며 동시에 최대 64개 요청을 처리합니다. Python 응답을 30초 동안 기다린 뒤에는 504를 반환하고 해당 코루틴의 취소를 요청합니다. 이미 스레드에서 실행 중인 동기 핸들러나 렌더링 함수는 강제로 중단되지 않아 504 이후에도 쓰기 등의 부작용을 일으키거나 자원을 점유할 수 있습니다. ISR 생성의 시간 초과나 취소에도 같은 제한이 있습니다. 쓰기 작업은 멱등성을 보장하고 DB·외부 서비스에 별도의 시간 제한을 설정해야 합니다. 강제 종료가 필요하면 별도의 프로세스 격리가 필요하며 현재 구현되어 있지 않습니다. 외부 ASGI 서버의 요청 시간 제한은 해당 서버가 관리합니다.
- ASGI HTTP와 기본 lifespan을 지원합니다. WebSocket, 스트리밍, 요청 처리 중 연결 해제 감지와 완전한 ASGI 적합성 검증은 아직 구현되지 않았습니다.
- ISR은 단일 프로세스 메모리 구현입니다. 워커 간 공유 캐시·무효화, 상호작용 시 island 로딩과 hydration 전 이벤트 재생, 자동 리로드, 프로덕션 보안·관측성 기능은 **미구현**입니다. PyNext의 hydration은 React나 React Server Components와 호환되지 않습니다. 현재 버전을 인터넷에 직접 공개하거나 신뢰할 수 없는 앱을 실행하는 데 사용하지 마세요.
- 빌드할 때 앱 모듈을 import하므로 개발자가 작성한 Python 코드가 실행됩니다. 프로젝트를 신뢰할 수 있는 환경에서 빌드해야 합니다.
- Python 파일의 앱 루트 기준 절대 import를 지원합니다. 파일 시스템 기반 로더의 상대 import는 아직 지원하지 않습니다.
- `start`의 manifest는 앱 디렉터리의 Python 파일과 `public`·`islands` 파일 변화를 검사하지만 앱 밖 의존성, 외부 데이터나 환경 변수 변화까지 추적하지 않습니다. 현재 운영 시작에도 원본 `public`과 `islands` 디렉터리가 필요합니다. 정적 산출물은 프로세스마다 메모리에 적재하며, 이전 빌드의 사용하지 않는 파일을 자동 정리하지 않습니다.

## 테스트

```bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest
```

실제 Chromium에서의 mount·클릭·CSP·정리 동작 검사는 Playwright와 브라우저가 설치된 환경에서 `tests/browser/islands_smoke.py`를 예제 서버와 함께 실행할 수 있습니다.

기초 부하 측정은 `uv run --extra dev python benchmarks/bench.py --mode axum` 또는
`--mode asgi`로 실행합니다. 이 스크립트는 같은 프로세스에서 클라이언트도
실행하는 간단한 회귀 측정 도구이며, 프레임워크 간 성능 비교 결과가 아닙니다.
빌드 후 `--production --path /about`을 추가하면 SSG 경로를 측정합니다.

개발 단계와 Next.js 원본을 참조하는 범위는 [구현 로드맵](docs/ROADMAP.md)을 참고하세요.
