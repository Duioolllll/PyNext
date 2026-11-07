# PyNext 사용 설명서 — 한국어

[English](../en/manual.md) · [日本語](../ja/manual.md) · [简体中文](../zh-CN/manual.md)

이 설명서는 개발 브랜치 기준이며 최신 공개 릴리스는 **v0.6.0a1**입니다. PyNext는 Python과 Rust로 만드는 실험적 풀스택 프레임워크이며 Next.js, React, React Server Components와 호환되지 않습니다. 현재 프로토타입을 인터넷에 직접 공개하거나 신뢰할 수 없는 앱 실행에 사용하지 마세요. [버전별 변경 내용](../../CHANGELOG.md)과 [향후 작업](../PRODUCTION_ROADMAP.md)을 참고하세요.

## 준비와 첫 실행

CPython 3.13, Rust/Cargo, [uv](https://docs.astral.sh/uv/)가 필요합니다. 다른 Python 버전과 배포 플랫폼은 충분히 검증되지 않았습니다. 예제는 island를 사용하므로 Node.js 18 이상과 npm으로 esbuild 0.28.1도 설치해야 합니다.

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

`pynext dev --dir examples/basic`는 빌드 없이 소스를 제공하지만 **파일 변경을 자동 반영하지 않으므로** 수정 후 재시작해야 합니다. `pynext start`는 빌드 manifest를 사용하며 추적하는 소스가 바뀌면 재빌드를 요구합니다. island가 없는 앱에는 Node와 esbuild가 필요하지 않습니다. 설치 안내의 기준은 저장소 소스이며 PyPI 배포를 의미하지 않습니다.

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

`page.py`는 데코레이터가 적용된 `Page`를, `route.py`는 HTTP 핸들러를 내보냅니다. `layout.py`는 하위 페이지를 감쌉니다. 우선순위는 정적 경로 > `[slug]` > `[...path]`입니다. 단일 매개변수는 문자열이고 catch-all은 문자열 리스트입니다. 선택적 catch-all, 라우트 그룹, 병렬·인터셉트 라우트와 클라이언트 탐색은 미구현입니다.

~~~python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
~~~

`h(tag, attrs, *children)`는 HTML 트리를 만들며 문자열을 이스케이프합니다. SSR은 전체 응답을 모아 반환하고 임의의 raw HTML 삽입 API는 제공하지 않습니다.

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

## API 응답 스트리밍

`StreamingResponse`에 문자열 또는 바이트를 내보내는 비동기 iterable을 전달합니다. ASGI와 Axum이 청크를 순서대로 전송하며 완료·연결 해제·오류 시 반복자를 닫습니다. 청크당 최대 1 MiB이며 `Content-Length`와 `Transfer-Encoding`은 직접 지정하지 않습니다. Axum은 청크 하나를 대기시키고 다음 청크 생성과 송신 대기에 각각 30초 제한을 적용합니다. 헤더 전송 후 오류는 상태 코드를 바꾸지 못하고 스트림을 중단합니다. HEAD 요청은 스트림을 소비하지 않습니다. 페이지 SSR은 여전히 버퍼링하며 React/RSC 스트리밍은 미구현입니다.

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield "first\n"
        yield "second\n"
    return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8")
~~~

## SSR, SSG, ISR

- `@page()`: 요청할 때마다 렌더링합니다.
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

SSG와 ISR 페이지 함수는 `request`를 받을 수 없습니다. 공개 콘텐츠에만 사용하고 환경 변수와 외부 데이터가 공유 가능한지 확인하세요. ISR은 익명 요청의 쿼리를 캐시 키에 포함하지 않습니다. Cookie 또는 Authorization 헤더가 있으면 공유 ISR 캐시를 우회하고 `private, no-store`로 응답합니다. 캐시는 크기가 제한된 **프로세스별 메모리 캐시**이므로 워커 간 공유·무효화되지 않습니다. SSG 내용을 바꾸려면 재빌드해야 합니다.

SSG 빌드는 최대 10,000페이지를 생성하며 개발 모드에서는 요청마다 SSG 페이지를 렌더링합니다. ISR 페이지는 `generate_static_params()`를 내보낼 수 없고 프로세스별 동시 생성 작업은 최대 128개입니다.

권한을 확인한 API 핸들러에서는 `await request.revalidate("/news/example")`, 앱 인스턴스를 가진 코드에서는 `await app.revalidate(path)`를 사용할 수 있습니다. 쿼리 문자열이 없는 디코딩된 ISR 경로를 전달합니다. 캐시나 진행 중인 생성을 무효화했다면 `True`를 반환합니다. 기존 요청은 이전 생성 결과를 받을 수 있지만 그 결과가 캐시를 다시 덮어쓰지는 않습니다. 무효화는 프로세스별이며 다른 워커에 전파되지 않습니다. 외부에 노출하는 엔드포인트는 인증·권한 검사가 필요합니다.

## 공개 파일과 islands

공개 파일·SSG 페이지·island 번들은 SHA-256 내용 해시 기반 강한 ETag를 사용합니다. GET/HEAD는 `If-Match`와 `If-None-Match`를, GET은 단일 바이트 `Range`와 강한 ETag `If-Range`를 지원하며 필요한 경우 206 또는 416을 반환합니다. 다중 또는 잘못된 범위는 무시하고 전체를 반환합니다. 날짜 기반 검증과 ISR 응답의 범위 요청은 미구현입니다.

`public/styles/site.css`는 `/styles/site.css`로 공개됩니다. 빌드는 파일을 스냅샷하고 무결성을 검사하지만 운영 서버 시작에도 원본 파일이 필요합니다. 숨김 파일, 심볼릭 링크, 충돌 경로는 거부합니다. 개발 모드에서 공개 파일을 바꾼 뒤에도 서버를 재시작하세요.

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

활성화 기본 전략은 `load`입니다. `visible`은 화면에 들어올 때, `idle`은 유휴 콜백을 요청해 활성화합니다. idle 콜백에는 2초 timeout을 요청하지만 실제 실행은 더 늦을 수 있습니다. 관련 API가 없으면 idle 또는 타이머로 대체합니다. 활성화 전 클릭·입력 이벤트는 **재생되지 않습니다**.

## ASGI, 시간 초과와 안전성

예제 ASGI 앱을 Uvicorn에서 개발 모드로 실행합니다.

~~~bash
uv run --extra dev uvicorn --app-dir examples/basic asgi:app
~~~

빌드된 앱은 `create_app(project_path, production=True)`로 만듭니다. ASGI 서버의 시간 제한은 해당 서버가 관리합니다. Axum 어댑터는 Python 응답을 최대 30초 기다린 뒤 취소를 요청하고 504를 반환합니다. 이미 스레드에서 실행 중인 동기 함수는 504 이후에도 계속 실행되어 쓰기 등의 부작용을 일으킬 수 있습니다. 쓰기 작업의 멱등성과 데이터베이스·외부 서비스의 별도 시간 제한을 확보하세요. 강제 프로세스 격리는 없으며 ISR 취소에도 같은 제한이 있습니다.

스트리밍 SSR, WebSocket, 분산 ISR, 분산 요청 제한, OpenTelemetry와 운영 환경 보안 강화는 미구현입니다. 계획과 완료 기준은 [프로덕션 로드맵](../PRODUCTION_ROADMAP.md)을 참고하세요.

두 어댑터의 요청 본문은 최대 1 MiB이며 초과 시 413입니다. Axum의 기본 본문 수신 슬롯은 16개, 본문 수신 제한은 10초(408), Python 실행 슬롯은 64개입니다. 대기열은 실행 슬롯 수와 64 중 작은 값으로 제한하며 가득 차면 503입니다. 실행 및 본문 설정은 CLI에서 조정할 수 있습니다. HTTP/1.1 헤더 수신은 10초로 제한하지만 전체 연결 수는 제한하지 않습니다. 외부 ASGI 서버의 시간 제한은 해당 서버가 관리합니다. 루트가 아닌 경로의 마지막 `/`는 308로 리디렉션됩니다. 컴파일된 island는 개당 2 MiB, 전체 32 MiB 이하입니다.

## 테스트와 문제 해결

~~~bash
cargo test -p pynext-router -p pynext-cache
uv run --extra dev pytest -q
~~~

예제 빌드 후 Chromium 검사를 실행합니다.

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

`uv run --extra dev python benchmarks/bench.py --mode axum --requests 100 --concurrency 4`로 작은 폐쇄형 부하 검사를 실행합니다. 전체 시도와 성공한 2xx 요청의 초당 처리량, 오류·상태 코드, 전체·성공 요청의 p50/p95/p99, 응답 크기, CPU와 최대 RSS를 따로 기록합니다. 기본 모드의 CPU와 프로세스 생애 최대 RSS에는 서버와 부하 생성기가 모두 포함됩니다. 별도 로컬 서버는 `--base-url http://127.0.0.1:3000`으로 지정하며 Linux에서는 `--server-pid <PID>`로 해당 프로세스와 스레드의 CPU를 따로 측정할 수 있습니다. 자식 워커는 포함되지 않습니다. CI는 프로덕션 모드(`--production`, 빌드된 예제 사용)로 측정한 작은 부하 결과를 저장하지만 성능·메모리 회귀의 자동 임계값은 아직 적용하지 않습니다.

- 소스가 변경되었다는 오류가 나면 `pynext build`를 다시 실행합니다.
- esbuild가 없으면 저장소 루트에서 `npm ci`를 실행하거나 `PYNEXT_ESBUILD`에 실행 파일을 지정합니다.
- Windows에서는 npm esbuild를 Node로 실행합니다. `PYNEXT_ESBUILD`를 직접 설정할 때는 `.cmd`·`.bat` 대신 네이티브 `esbuild.exe`를 지정하세요.
- 동적 SSG 경로가 404이면 `generate_static_params()`에 경로를 추가하고 재빌드합니다.
- hydration이 일치하지 않으면 서버 fallback과 클라이언트 `render` 결과를 결정적으로 동일하게 만듭니다.
- ISR 결과가 워커마다 다르면 프로세스별 캐시인지 확인합니다.
