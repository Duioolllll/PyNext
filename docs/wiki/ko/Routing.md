# 파일 기반 라우팅

[홈](Home.md) · [English](../en/Routing.md)

`app/` 아래 디렉터리가 URL 경로가 됩니다. `page.py`는 HTML 페이지, `route.py`는 API 핸들러, `layout.py`는 하위 **페이지**를 감싸는 레이아웃입니다. 같은 URL에 페이지와 API를 동시에 둘 수 없습니다.

~~~text
app/
  layout.py                   # 공통 페이지 레이아웃
  page.py                     # /
  about/page.py               # /about
  blog/[slug]/page.py         # /blog/hello
  docs/[...path]/page.py      # /docs/a/b
  api/users/route.py          # /api/users
~~~

## 동적 매개변수와 우선순위

- **정적 경로 > 동적 `[slug]` > catch-all `[...path]`** 순서입니다. Rust trie가 백트래킹도 수행합니다.
- `[slug]`는 문자열 하나를 전달합니다: `params["slug"] == "hello"`.
- `[...path]`는 비어 있지 않은 문자열 목록을 전달합니다: `params["path"] == ["a", "b"]`.
- 충돌하는 경로를 검사하며 일부 파라미터 이름 표기는 같은 슬롯으로 정규화됩니다.
- **미지원:** 선택적 catch-all(`[[...slug]]`), route group, parallel/intercepting route, 클라이언트 측 페이지 탐색.

페이지 예시:

~~~python
from pynext import h, page

@page()
async def Page(params):
    return h("h1", {}, params["slug"])
~~~

반드시 `@page()`로 꾸민 `Page` 함수를 내보내야 합니다. 허용되는 명명 인자는 `request`와 `params`입니다. 가변 위치 인자나 허용되지 않은 필수 인자는 사용할 수 없습니다.

## URL과 import

- 루트 이외 URL의 마지막 `/`는 기본값으로 HTTP 308로 리디렉션합니다. 아래 "베이스 경로와 trailing slash"를 참고하세요.
- 잘못된 UTF-8이나 경로 구분자가 인코딩된 요청은 거부합니다.
- `/_pynext/`는 클라이언트 번들용 예약 경로입니다.
- `public/` 파일은 URL 루트에서 제공하며 페이지/API와 충돌하면 오류입니다.
- 앱 내부 파일 import는 `from .helpers import title`처럼 상대 import를 사용하세요. 프로젝트 루트를 전역 `sys.path`에 넣지 않습니다.

다음 문서: [렌더링](Rendering.md), [API](API-Routes-and-Streaming.md). Next.js 코드 참조 범위는 [원본 저작권 표기](../../../third_party/NEXTJS.md)에서 확인할 수 있습니다.

## 베이스 경로와 trailing slash

경로 전처리(Next.js의 `getNextPathnameInfo()`, i18n locale 단계는 제외 — PyNext에는 locale 라우팅이 없음):

- `create_app(..., base_path="/app")`는 앱 전체를 `/app` 아래에서 제공합니다. prefix는 라우트 매칭 전에 떼어내므로 라우트는 그대로 `/`, `/about`, `/blog/[slug]`로 정의하고, `url_for("/blog/[slug]", {"slug": "hi"}, base_path="/app")`는 `/app/blog/hi`를 반환합니다. prefix 밖의 경로는 404이며, prefix는 세그먼트 경계에서 끝나야 합니다(`/application`은 `/app` 아래가 아님). 같은 전처리는 `/_pynext/` 자산, public 파일, WebSocket 업그레이드(ASGI), `revalidate()` 경로에도 적용됩니다.
- `create_app(..., trailing_slash="redirect")`(기본값)는 기존 동작을 유지합니다. 루트가 아닌 경로의 마지막 `/`는 HTTP 308로 `/` 없는 같은 경로로 리디렉션되며, `Location`에 베이스 경로가 유지됩니다. `trailing_slash="ignore"`는 리디렉션 없이 그대로 제공합니다.
- 두 설정 모두 시작 시점에 검증됩니다. 베이스 경로는 `/`로 시작하고 `/`로 끝나지 않아야 하며, 빈 세그먼트나 `.`·`..` 세그먼트를 포함할 수 없습니다. `trailing_slash`는 `"redirect"` 또는 `"ignore"`여야 합니다.
- 의도적으로 prefix가 붙지 않는 경로가 두 가지 있습니다. 헬스 프로브(`/healthz`, `/readyz`)는 전처리 전에 응답하고, 속도 제한 면제 판정은 prefix가 붙은 원본 경로로 평가합니다. 로드 밸런서는 베이스 경로 아래가 아니라 루트의 `/healthz`를 프로브해야 합니다.
