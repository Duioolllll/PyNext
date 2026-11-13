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

정적 렌더링은 동적 접근을 추적합니다. SSG 페이지가 빌드 중 렌더링되거나 ISR 페이지가 생성되는 동안 `time.time()`, `datetime.now()`, `random.*`, `uuid.uuid4()`, `Database.session()` 같은 읽기는 읽은 페이지 위치와 함께 기록됩니다. 기본값 `@page(dynamic="auto")`은 빌드를 계속하고 접근 내역을 빌드 매니페스트의 `dynamic_accesses` 리포트에 경고와 함께 남깁니다. `@page(dynamic="error")`는 처음 기록된 접근에서 빌드(또는 ISR 생성)를 실패시키고, `@page(render="ssg", dynamic="force-dynamic")`는 빌드 시 생성을 건너뛰고 운영에서는 페이지를 동적으로 제공합니다. `no_store()`는 스코프를 의도적으로 동적으로 표시하고, `mark_dynamic_access("...")`은 자동 후크가 볼 수 없는 나머지(외부 fetch나, 프리렌더 패치를 피해 가는 `from datetime import datetime` 바인딩)를 기록합니다. 개발 모드에서 `generate_static_params()`가 만들지 않을 SSG 경로를 요청하면 운영에서 404가 될 것이라는 경고를 남깁니다. `pynext dev --strict-ssg`(또는 `create_app(..., strict_ssg=True)`)를 쓰면 개발 모드에서도 404로 응답합니다.

생성하지 않은 동적 경로는 404 대신 fallback을 선택할 수 있습니다. `@page(render="ssg", fallback="blocking")`은 첫 요청 때 페이지를 생성한 뒤 정적으로 제공하고, `fallback="prerender"`는 레이아웃 셸을 먼저 스트리밍한 뒤 생성된 페이지를 보내고 이후에는 정적으로 제공합니다. 기본값은 `fallback="not_found"`입니다. `dynamic_params=False`이면 fallback을 지정해도 생성하지 않은 경로는 404입니다. 첫 요청이 동시에 여러 개 와도 생성은 한 번만 일어나며, 생성에 실패하면 500을 반환하고 다음 요청에서 다시 시도합니다.

## ISR (공개 캐시)

~~~python
from pynext import h, page

@page(render="isr", revalidate=30, stale=120)
def Page(params):
    return h("h1", {}, params["slug"])
~~~

`app/news/[slug]/page.py`처럼 쓰면 최초 요청에 생성하고, 신선한 HTML은 재사용하며, 만료 후에는 한 개의 백그라운드 재생성 중 오래된 HTML을 제공할 수 있습니다. 캐시는 크기가 제한됩니다. 기본 `memory` 백엔드는 프로세스별이지만 `sqlite`·`redis`·`filesystem` 백엔드는 워커 간 항목을 공유합니다. ISR은 `request` 인자를 받을 수 없습니다.

동적 ISR 페이지는 `generate_static_params()`를 내보낼 수 있습니다(정적 ISR 경로는 불가). 나열된 경로는 빌드 시점에 열거되고, 시작 시 백그라운드에서 캐시를 미리 채우므로 배포 직후 첫 요청이 전체 렌더 비용을 치르지 않습니다. 웜업은 베스트 에포트입니다. 웜업에 실패한 경로는 로그를 남기고 요청 시점에 그대로 렌더링됩니다.
- 익명 요청의 **쿼리 문자열은 ISR 캐시 키에 포함되지 않습니다.**
- `Cookie` 또는 `Authorization` 헤더가 있으면 공유 캐시를 우회하며 `private, no-store`로 응답합니다.
- ISR 공개 페이지에 비밀값·사용자별 데이터를 넣으면 안 됩니다.
- `revalidate`는 양수, `stale`은 0 이상이며 상한도 존재합니다. `expire`은 생성 시점부터 재는 절대 상한입니다. `revalidate`이 지난 뒤에는 `expire`까지 오래된 HTML을 제공하면서 백그라운드로 한 번 재생성하고, `expire`이 지나면 다시 제공하지 않습니다(`expire`은 `revalidate`보다 커야 하며, `float("inf")`는 1년과 같습니다). `stale`과 `expire`은 같은 구간을 다르게 쓴 것입니다(`expire == revalidate + stale`). 둘 중 하나만 주거나, 일치한다면 둘 다 줄 수 있습니다. 숫자 대신 이름 있는 프로파일도 쓸 수 있습니다. `@page(render="isr", cache_life="hours")` — `default`, `seconds`, `minutes`, `hours`, `days`, `weeks`, `max`(`pynext.CACHE_LIFE_PROFILES` 참조).
- `revalidate`는 양수, `stale`은 0 이상이며 상한도 존재합니다.
- 레이아웃은 `@layout(revalidate=60, stale=120)`으로 구간 정책을 정할 수 있습니다. 하위 ISR 페이지는 정하지 않은 값을 상속하고, 숫자끼리는 가장 짧은 `revalidate`(가장 작은 `stale`)가 이깁니다. 레이아웃 체인 어디에도 `revalidate`가 없는 ISR 페이지는 로드 시 실패하고, 레이아웃보다 긴 값을 요구한 페이지는 빌드 시 경고와 함께 레이아웃 값으로 덮어씁니다. 레이아웃 정책은 빌드 매니페스트에 기록되므로 변경 시 다시 빌드해야 합니다.

## 명시적 캐시 무효화

인증을 거친 API 핸들러에서:

~~~python
success = await request.revalidate("/news/example")
~~~

앱 인스턴스라면 `await app.revalidate(path)`를 호출합니다. 쿼리 문자열이 없는 **디코딩된 ISR 경로**를 전달해야 합니다. 오래된 생성 작업이 최신 캐시를 덮어쓰지 못하도록 막지만 **현재 프로세스에서만** 동작합니다. 캐시 무효화 API는 반드시 접근 제어가 필요합니다.

관련 문서: [HTTP와 캐시](HTTP-and-Caching.md), [아키텍처](Architecture.md).
