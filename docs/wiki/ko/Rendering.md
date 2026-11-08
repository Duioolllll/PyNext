# 렌더링: SSR, SSG, ISR

[홈](Home.md) · [English](../en/Rendering.md)

PyNext는 세 가지 페이지 렌더링 모드를 제공합니다. **페이지 SSR은 버퍼형이며 HTML 청크 스트리밍을 지원하지 않습니다.**

## SSR (기본값)

~~~python
from pynext import h, page

@page()
async def Page(request):
    note = request.query.get("note", [""])[0]
    return h("p", {}, note)
~~~

요청마다 Python 핸들러가 실행됩니다. Python이 UI 트리를 만들고 JSON으로 직렬화하면 Rust가 이스케이프된 HTML을 생성합니다. React SSR/RSC와는 다릅니다.

## SSG (빌드 타임)

`app/guides/[slug]/page.py`에 다음 코드를 둡니다.

~~~python
from pynext import h, page

def generate_static_params():
    return [{"slug": "intro"}, {"slug": "routing"}]

@page(render="ssg")
def Page(params):
    return h("h1", {}, params["slug"])
~~~

`pynext build --dir PROJECT`를 실행하면 정적 HTML을 생성합니다. 동적 SSG 경로 중 생성하지 않은 URL은 프로덕션에서 **404**입니다. 정적 경로에는 생성기가 필요 없습니다. 빌드당 **최대 10,000 페이지**까지 생성하며 **SSG 페이지 함수는 `request`를 받을 수 없습니다.**

## ISR (공개 캐시)

~~~python
from pynext import h, page

@page(render="isr", revalidate=30, stale=120)
def Page(params):
    return h("h1", {}, params["slug"])
~~~

`app/news/[slug]/page.py`처럼 쓰면 최초 요청에 생성하고, 신선한 HTML은 재사용하며, 만료 후에는 한 개의 백그라운드 재생성 중 오래된 HTML을 제공할 수 있습니다. 캐시는 크기가 제한됩니다. 기본 `memory` 백엔드는 프로세스별이지만 `sqlite`·`redis` 백엔드는 워커 간 항목을 공유합니다. ISR은 `request` 인자를 받거나 `generate_static_params()`를 내보낼 수 없습니다.

- 익명 요청의 **쿼리 문자열은 ISR 캐시 키에 포함되지 않습니다.**
- `Cookie` 또는 `Authorization` 헤더가 있으면 공유 캐시를 우회하며 `private, no-store`로 응답합니다.
- ISR 공개 페이지에 비밀값·사용자별 데이터를 넣으면 안 됩니다.
- `revalidate`는 양수, `stale`은 0 이상이며 상한도 존재합니다.

## 명시적 캐시 무효화

인증을 거친 API 핸들러에서:

~~~python
success = await request.revalidate("/news/example")
~~~

앱 인스턴스라면 `await app.revalidate(path)`를 호출합니다. 쿼리 문자열이 없는 **디코딩된 ISR 경로**를 전달해야 합니다. 오래된 생성 작업이 최신 캐시를 덮어쓰지 못하도록 막지만 **현재 프로세스에서만** 동작합니다. 캐시 무효화 API는 반드시 접근 제어가 필요합니다.

관련 문서: [HTTP와 캐시](HTTP-and-Caching.md), [아키텍처](Architecture.md).
