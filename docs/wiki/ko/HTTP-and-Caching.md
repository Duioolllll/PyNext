# HTTP, 요청 제한, 캐싱

[홈](Home.md) · [English](../en/HTTP-and-Caching.md)

## 네이티브 HTTP 기본 제한

네이티브 서버는 Axum/Hyper 기반으로 HTTP/1.1과 **HTTP/2 prior knowledge(h2c)**를 처리합니다. `pynext start/dev`의 기본값은 다음과 같습니다.

| 설정 | 기본값 | 의미 |
| --- | --- | --- |
| `--max-concurrent` | 64 | Python 실행 슬롯 |
| `--acquire-timeout-ms` | 1000 | 슬롯 대기 후 503 |
| `--max-waiting` | `min(--max-concurrent, 64)` | 실행 슬롯 대기 요청 수; 0이면 대기 없음 |
| `--body-capacity` | 16 | 동시 요청 본문 수신 슬롯 |
| `--body-timeout-ms` | 10000 | 본문 타임아웃 시 408 |
| 본문 크기 | 1 MiB | 초과 시 413 |
| HTTP/1.1 헤더 | 10초 | 헤더 수신 마감 시간 |

실행 대기 요청의 최대 개수는 `--max-waiting`으로 정합니다(기본값은 실행 제한과 64 중 작은 값; 0이면 즉시 503). 과부하 정책은 제한된 대기 후 거절입니다. 슬롯을 기다리다 `--acquire-timeout-ms`가 지나면 **503**과 `Retry-After`(대기 타임아웃을 올림한 초, 최소 1초)를 반환하고, 대기열이 가득 차서 즉시 거절될 때는 `Retry-After: 1`입니다. 대기 중인 요청은 이미 버퍼링된 본문을 들고 있을 수 있어 대기열을 무한히 늘리거나 확률적으로 일찍 거절하지 않습니다. 정적 파일·SSG·신선한 익명 ISR의 일부 GET/HEAD 요청은 Python 실행 슬롯을 우회합니다. **네이티브 서버는 전체 연결 수(기본 1024)와 IP당 연결 수(기본 128)도 제한합니다(`--max-connections` / `--max-connections-per-ip`로 조정).** 외부 ASGI 서버는 자체 타임아웃 정책을 사용합니다.

## 정적 에셋 HTTP 처리

빌드된 공개 파일·SSG·island 번들은 콘텐츠 기반 강력한 ETag 및 `If-Match`/`If-None-Match` 조건부 요청을 지원합니다. 해당 GET 자원의 단일 `Range` 및 강력한 ETag 기반 `If-Range`도 지원합니다. 잘못되거나 여러 범위 요청은 전체 본문으로 처리합니다. 날짜 기반 검증과 ISR 응답의 범위 요청은 미구현입니다.

- 콘텐츠 해시가 포함된 island 번들: `public, max-age=31536000, immutable`.
- 공개 파일 / SSG HTML: `public, max-age=0, must-revalidate`.
- `public/path` 파일은 `/path` URL로 노출됩니다. 숨김 파일과 심볼릭 링크는 거부합니다.

## ISR 범위

ISR은 크기가 제한된 캐시와 single-flight 재생성을 사용합니다. 기본 `memory` 백엔드는 프로세스별이지만, `sqlite`·`redis` 백엔드는 만료되는 lease와 fencing된 쓰기로 워커 간 항목을 공유하므로, 저장소가 정상일 때는 다른 워커의 무효화가 다음 조회에 반영됩니다(장애 중 로컬 폴백은 오래된 값을 줄 수 있습니다). 신선한 익명 결과는 Rust가 직접 전달할 수 있고, 오래된 응답은 백그라운드 재생성 중 제공될 수 있습니다. `Cookie` 또는 `Authorization`은 공유 캐시를 우회합니다. 인증을 거친 API에서 `await request.revalidate("/news/example")` 또는 `await app.revalidate(path)`를 사용할 수 있습니다. `revalidate(path, type="page")`는 `"/news/[slug]"` 같은 동적 라우트 패턴의 캐시 항목을 전부 무효화하고(`type` 없는 동적 패턴은 경고만 하고 아무것도 무효화하지 않습니다), `revalidate(path, type="layout")`는 해당 경로에 정의된 레이아웃을 쓰는 ISR 페이지를 전부 무효화합니다(`"/"`는 루트 레이아웃이라 전체 페이지가 대상입니다). `"/"`와 `"/index"`는 별칭입니다.

태그 기반 무효화는 경로가 아니라 페이지가 의존하는 데이터 단위로 묶습니다. ISR 페이지에 `@page(render="isr", revalidate=30, stale=60, tags=("post:42", "author:7"))`처럼 태그를 선언합니다(페이지당 최대 64개, 태그는 `[A-Za-z0-9:_-./]` 1–256자). `await app.revalidate_tag("post:42")`를 호출하면 해당 태그를 가진 항목이 모두 즉시 만료되고, `await app.revalidate_tag("post:42", expire=60)`은 60초 동안은 stale 상태로(한 번 더 서빙하면서 백그라운드로 재생성) 유지한 뒤 만료시킵니다. 호출 이후에 생성된 항목은 영향받지 않으며, `sqlite`/`redis` 백엔드에서는 무효화 기록이 공유 저장소에 저장돼 다른 워커도 봅니다(`memory` 백엔드는 프로세스별입니다). 프로덕션에서는 페이지 태그를 바꾸면 다시 빌드해야 합니다.

ISR 응답에는 정책 기반 `Cache-Control` 헤더가 붙어 PyNext 앞단의 CDN이 stale-while-revalidate를 수행할 수 있습니다: `public, s-maxage={revalidate}`에 SWR 구간이 0이 아닌 페이지는 `stale-while-revalidate={stale}`이 추가됩니다(2축 `{revalidate, stale}` 정책에 대한 Next.js `getCacheControlHeader` 포팅). 최초 생성·신선 히트·stale 서빙·스트리밍 ISR 응답과 L1 캐시에서 직접 나가는 네이티브 신선 히트에 모두 적용되며, private(쿠키/인증) 응답은 그대로 `private, no-store`입니다.

stale 재생성이 실패하면 이전 항목을 유지합니다. 신선 구간을 3~30초로 조이고 stale 구간을 최소 3초로 두어(Next.js `retainPreviousCacheEntry`), 원래 stale 구간이 끝나도 이전 본문이 계속 서빙되면서 다음 재시도까지 호흡을 둡니다. 이전 항목이 없는 최초 생성 실패는 그대로 500입니다.

## fetch/데이터 캐시와 요청 내 메모이제이션

페이지 핸들러는 `pynext.fetch_cached(url, fetcher, ...)`로 외부 데이터를
가져옵니다(API 핸들러에서는 `request.app.fetch_cache.get_or_fetch(...)`도
가능). `fetcher`는 `FetchResult`를 돌려주는 async callable입니다. 실제
HTTP에는 `pynext.urllib_fetcher`를, 테스트에는 스텁을 씁니다.

- **캐시 키**: `sha256(프래그먼트 제외 URL, 메서드, 헤더, 본문)`입니다.
  `Authorization`·`Cookie`·`Proxy-Authorization`·`Set-Cookie` 헤더는
  키에서 빠져서, 캐시된 응답이 다른 자격 증명에 제공되지 않습니다.
  본문은 타입 태그(`str:`/`bytes:`/폼 필드)가 붙어 `["a", "b"]`와
  `["a,b"]`가 충돌하지 않습니다.
- **정책**(`resolve_fetch_policy`, Next.js `patch-fetch.ts`에서 가져옴):
  명시적 `revalidate`(초, `False`는 무한)가 우선하고,
  `revalidate` 없는 `cache="force-cache"`는 무한,
  `cache="no-store"`/`"no-cache"`는 저장 안 함, 명시값이 없으면
  `Authorization`/`Cookie` 헤더나 GET/HEAD 외 메서드일 때 저장 안 함
  (자동 no-cache, 프리렌더 시 `build_time=True`로 해제), 그 외에는
  호출자의 `default_revalidate`(알면 페이지 ISR `revalidate`, 아니면
  무한)가 적용됩니다. 충돌하는 옵션(`force-cache` + `revalidate=0`,
  `no-store` + `revalidate > 0`)은 경고 후 둘 다 버립니다.
- **저장**: 항목은 ISR 백엔드(memory / SQLite / Redis)에 별도
  `\0fetch:` 키로 저장됩니다. 2xx 응답만 저장되고 `revalidate`초 동안
  신선합니다(`False`는 백엔드 최대 7일). stale-while-revalidate 구간은
  없어서, 만료된 항목은 다음 요청에서 동기적으로 다시 가져옵니다.
  2 MiB를 넘는 본문은 제공되지만 저장되지 않습니다. 같은 키에 대한
  동시 요청은 한 프로세스 안에서 한 번의 upstream fetch를 공유합니다.
  `await fetch_cache.invalidate(url, ...)`로 항목 하나를 지웁니다.
- **요청 내 메모이제이션**: 모든 요청은 새 메모 안에서 실행됩니다
  (`pynext.memoize(key, compute)` / `memoize_sync`). 레이아웃과 페이지가
  같은 데이터를 읽거나 한 핸들러에서 같은 DB 조회를 두 번 해도 한 번만
  계산하고, TTL 없이 요청이 끝나면 사라집니다. `fetch_cached` 결과도
  메모이제이션됩니다(`no-store` 포함). 요청 안 무효화
  (`App.revalidate` / `Request.revalidate`)는 메모 전체를 비워서,
  변경 뒤 읽기가 변경 전 데이터를 보지 않습니다.

## 안전 주의

네이티브 어댑터가 Python 요청을 504로 마무리해도 실행 중인 동기 함수는 계속 실행될 수 있습니다. 상태 변경 작업에는 DB 타임아웃과 멱등성을 적용하세요. 관련 문서: [배포와 보안](Deployment-and-Security.md).
