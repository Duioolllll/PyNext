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

실행 대기 요청의 최대 개수는 `--max-waiting`으로 정합니다(기본값은 실행 제한과 64 중 작은 값; 0이면 즉시 503). 과부하 정책은 제한된 대기 후 거절입니다. 슬롯을 기다리다 `--acquire-timeout-ms`가 지나면 **503**과 `Retry-After`(대기 타임아웃을 올림한 초, 최소 1초)를 반환하고, 대기열이 가득 차서 즉시 거절될 때는 `Retry-After: 1`입니다. 대기 중인 요청은 이미 버퍼링된 본문을 들고 있을 수 있어 대기열을 무한히 늘리거나 확률적으로 일찍 거절하지 않습니다. 정적 파일·SSG·신선한 익명 ISR의 일부 GET/HEAD 요청은 Python 실행 슬롯을 우회합니다. **전체 연결 개수 제한은 없습니다.** 외부 ASGI 서버는 자체 타임아웃 정책을 사용합니다.

## 정적 에셋 HTTP 처리

빌드된 공개 파일·SSG·island 번들은 콘텐츠 기반 강력한 ETag 및 `If-Match`/`If-None-Match` 조건부 요청을 지원합니다. 해당 GET 자원의 단일 `Range` 및 강력한 ETag 기반 `If-Range`도 지원합니다. 잘못되거나 여러 범위 요청은 전체 본문으로 처리합니다. 날짜 기반 검증과 ISR 응답의 범위 요청은 미구현입니다.

- 콘텐츠 해시가 포함된 island 번들: `public, max-age=31536000, immutable`.
- 공개 파일 / SSG HTML: `public, max-age=0, must-revalidate`.
- `public/path` 파일은 `/path` URL로 노출됩니다. 숨김 파일과 심볼릭 링크는 거부합니다.

## ISR 범위

ISR은 크기가 제한된 **프로세스별 캐시**와 single-flight 재생성을 사용합니다. 신선한 익명 결과는 Rust가 직접 전달할 수 있고, 오래된 응답은 백그라운드 재생성 중 제공될 수 있습니다. `Cookie` 또는 `Authorization`은 공유 캐시를 우회합니다. 인증을 거친 API에서 `await request.revalidate("/news/example")` 또는 `await app.revalidate(path)`를 사용할 수 있습니다. 워커 간 캐시 동기화는 없습니다.

## 안전 주의

네이티브 어댑터가 Python 요청을 504로 마무리해도 실행 중인 동기 함수는 계속 실행될 수 있습니다. 상태 변경 작업에는 DB 타임아웃과 멱등성을 적용하세요. 관련 문서: [배포와 보안](Deployment-and-Security.md).
