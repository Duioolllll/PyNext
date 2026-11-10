# PyNext 아키텍처와 개발 명세

이 문서는 **0.1.2 코드**를 기준으로 실제 구현과 후속 설계를 구분한다. Next.js·React·RSC 호환성을 약속하지 않는다. 이 명세의 목표는 변경의 소유권, 실패 시 동작과 검증 기준을 결정하는 것이다. 일정과 성능은 측정 결과에 따라 정한다.

## 1. 현재 구조와 지원 범위

```text
브라우저 ─ HTTP/WS ─ Axum/Tokio ─ 정적/신선한 L1 ISR ─ 응답
                            └─ PyO3 브리지 ─ Python 이벤트 루프 ─ 페이지/API
브라우저 ─ HTTP/WS ─ ASGI 서버 ─────────────────── Python 앱
빌드: 파일 탐색 → 라우트 manifest / SSG / esbuild islands → 검증된 산출물
```

- `pynext-router`: 정적 > 동적 > catch-all > optional catch-all 우선순위를 갖는 라우트 trie. 경로 충돌은 빌드/시작 시 거부한다.
- `pynext-cache`: 크기와 항목 수가 제한된 프로세스 내부 ISR 메모리 캐시. `pynext-native`는 Axum, PyO3, 정적 응답 및 요청 브리지를 제공한다.
- Python `app.py`/`routing.py`는 핸들러와 렌더링, `server.py`는 Axum 브리지, `http.py`는 ASGI 모델·보안 도구, `build.py`/`client.py`는 빌드·산출물을 담당한다. `cache.py`, `ratelimit.py`, `observability.py`, `supervisor.py`는 각각 ISR, 요청 제한, 계측, 워커 감독을 담당한다.
- 현재 island는 명시적 JS/TS 번들이다. 선택적 공유 island 모델 v1은 서버와 브라우저에서 같은 제한된 노드 트리와 문자열 props를 렌더링한다. `client_dsl.py`는 제한된 Python 부분집합을 직접 JS로 출력하는 실험이다. 아래의 전체 라우트 공통 IR과 React 컴파일러는 아직 없다.
- 앱은 신뢰하는 코드로 실행한다. 현재 설계는 신뢰할 수 없는 Python/JS 코드 격리 또는 인터넷에 직접 공개 가능한 운영 환경을 제공하지 않는다.

### 현재 Python API 예

`app/catalog/[slug]/page.py`는 경로 매개변수와 서버 렌더링 결과를 사용한다. 참조하는 island의 JS 번들은 앱에서 별도로 제공해야 한다.

```python
from pynext import h, island, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]),
             island("cart", props={"slug": params["slug"]},
                    fallback=h("p", {}, "Loading cart")))
```

`app/api/items/route.py`의 예는 JSON 입력 오류를 `BadRequest`로 처리하는 현재 API를 사용한다.

```python
from pynext import JSONResponse, route

@route("POST")
async def create(request):
    payload = await request.json()
    return JSONResponse({"name": payload["name"]}, status=201)
```

공개 ISR 페이지에는 `@page(render="isr", revalidate=60, stale=120)`을 사용하고 페이지 매개변수로 `request`를 받지 않는다. `await request.revalidate("/catalog/example")`을 호출하는 관리 API는 앱에서 인증·권한을 검사해야 한다.

### 후속 모듈 경계 — 제안

현행 3개 crate와 Python API를 유지하면서 필요할 때만 분리한다. `pynext-ir`은 버전·제한·직렬화/검증만 소유하고 컴파일러가 디스크나 네트워크를 직접 다루지 않게 한다. `pynext-router`는 경로 정규화와 충돌·매칭, `pynext-cache`는 L1 및 조건부 쓰기 인터페이스, 장래 `pynext-transport`는 연결·본문·역압력, `pynext-observability`는 bounded 계측 경계를 소유한다. PyO3 공개 경계는 `pynext-native`에 모으고 Python 쪽 ASGI·개발 서버는 공통 계약을 사용한다. 이 이름과 분리는 구현 전 제안이며 현재 존재하지 않는 crate가 있다.

## 2. 요청과 실행 계약

1. 전송 계층은 유효하지 않은 경로와 과도한 본문을 거부한다. Axum은 기본적으로 본문 1 MiB, 본문 수신 16개/10초, Python 실행 64개를 사용하고, 실행 대기열은 기본 `min(실행 슬롯, 64)`개이며 `--max-waiting`으로 바꿀 수 있다(0이면 즉시 503). 연결 제한(A4)도 기본값으로 켜져 있다: 총 TCP 연결 1024개, IP당 128개, 헤더 읽기(느린 헤더와 유휴 keep-alive 포함) 10초, 본문 최소 평균 수신 속도 1024 B/s(2초 유예 후 408). 초과 연결은 수락 시점에 닫히고, 느린 본문은 408 + `Retry-After: 1`로 거부된다. 과부하 정책은 제한된 대기 후 거절이다: 슬롯을 기다리다 `acquire-timeout`이 지나면 503을 반환하고, `Retry-After`는 대기 타임아웃을 올림한 초(최소 1초)이며, 대기열이 가득 차서 즉시 거절될 때는 `Retry-After: 1`이다. 대기 중인 요청은 이미 버퍼링된 본문을 들고 있을 수 있어 대기열을 무한히 늘리거나 확률적으로 일찍 거절하지 않는다. 실제 ASGI 서버의 연결·수신 제한은 그 서버 설정으로 따로 적용한다.
2. 라우트는 동일한 trie 우선순위로 결정한다. 정적 응답은 Python을 건너뛸 수 있다. 미들웨어나 계측이 이 빠른 경로에도 적용되는지는 기능별로 따로 명시한다.
3. 동적 요청은 소유 이벤트 루프에서 실행한다. 동기 핸들러는 스레드로 이동한다. 요청 취소/시간 초과는 취소를 *요청*하지만 이미 실행 중인 동기 함수, DB 커밋 또는 외부 호출의 중단을 보장하지 않는다. 호출별 기한과 멱등성은 앱의 책임이다.
4. 스트리밍은 생성된 청크를 순차적으로 송신하고 느린 수신자에 역압력을 전달한다. 연결 종료 시 async 생성기를 닫고, 헤더 송신 뒤 발생한 예외는 상태 코드를 바꿀 수 없으므로 스트림을 중단하고 기록한다.
5. WebSocket은 기본 64 KiB의 메시지 상한을 최대 16 MiB까지 조정할 수 있다. Axum/Python 사이의 각 큐는 32개로 제한하고 수신 큐가 1초 동안 가득 차면 1013으로 연결을 닫는다. 프로세스 내 pubsub의 구독 큐도 제한한다. WebSocket 연결도 HTTP 연결과 같은 총/IP당 연결 상한에 포함된다. h2c 연결의 유휴 타임아웃은 hyper HTTP/1.1 헤더 읽기 타임아웃과 달리 별도로 강제되지 않으며 총/IP당 개수 상한으로만 제한된다.

요청 제한을 켜면 개발 버전의 양 전송 경로는 본문 수신 전 헤더로 판정하며, HTTP·WebSocket·정적 파일·SSG·ISR에 적용한다. 통과한 요청은 이후 업로드 중단이나 수신·실행 실패에도 토큰을 환급하지 않는다. Axum의 판정 작업은 본문·실행 슬롯과 별도로 합계 64개/2초로 제한하고, 포화·시간 초과·오류는 `Retry-After: 1`을 포함한 503으로 거부한다. 이 예산은 저장소 장애 시 `fail_open` 여부와 독립적이다.

## 3. 빌드, 컴파일러 IR 및 hydration 설계

### 현재

파일 탐색은 경로 manifest와 원본 해시를 만들고 SSG HTML, 공개 파일, island 번들을 빌드한다. 프로덕션 시작 시 manifest와 산출물을 검증한다. 서버는 HTML 및 island 마커/직렬화된 props를 렌더링하고 브라우저 런타임이 load/visible/idle/interaction 전략으로 island를 활성화한다. 버튼 클릭 일부만 제한적으로 재생하며 일반 이벤트의 정확한 재생을 약속하지 않는다.

공유 island 모델은 element·text·fragment와 문자열 props 치환만 제공하며, 모델을 서버 HTML과 함께 내보내 브라우저에서 동일한 데이터로 분리된 DOM을 구성해 비교한다. 일치하면 기존 DOM에 이벤트를 연결하고 불일치하면 해당 island만 mount한다. 모델 16 KiB·512노드·깊이 32 및 안전한 태그·속성을 강제한다. 동적 속성, 반복, 상태 계산과 전체 라우트 IR은 범위 밖이다.

### 목표 IR v1 — 미구현

컴파일러 프런트엔드(JS/TS 또는 제한된 Python DSL)와 백엔드(서버 HTML, 클라이언트 번들)를 분리한다. IR은 버전이 붙은 명시적 데이터이며 임의 코드를 실행하지 않는다.

```json
{
  "ir_version": 1,
  "build_id": "<content hash>",
  "route": "/products/[id]",
  "nodes": [
    {"kind": "element", "tag": "main", "attrs": {}, "children": [1]},
    {"kind": "island", "module": "cart", "export": "default",
     "strategy": "visible", "props_schema": "cart-props-v1",
     "fallback": 2}
  ],
  "modules": [{"id": "cart", "content_hash": "<sha256>", "exports": ["default"]}]
}
```

노드 범위는 `element`, `text`, `fragment`, `island`, `boundary`로 시작한다. 참조 인덱스, 최대 노드 수·깊이·props 바이트, 태그/속성 허용 규칙을 빌드에서 검증하고 텍스트와 속성은 문맥별로 이스케이프한다. 원본 위치는 별도 source map에 둔다. 빌드 ID, IR 버전, JS 모듈 해시가 맞지 않으면 hydration을 중단하고 정적 fallback을 유지한다. 서버 전용 이름이나 비밀은 클라이언트 의존성 그래프에 들어갈 수 없다.

Hydration 상태는 `unseen → scheduled → loading → active → disposed`이며 실패는 `failed`로 끝낸다. 동일 island의 중복 활성화·정리와 탐색 중 취소를 멱등으로 처리한다. 서버 DOM과 클라이언트 출력 불일치의 복구 정책을 명시하고 테스트한다. CSP 아래에서 동작해야 하며 props 직렬화는 스크립트 종료 문자열과 순환 객체를 안전하게 처리한다. React/RSC는 Flight 프로토콜·모듈 경계·버전 호환성·Suspense를 별도로 구현하고 시험하기 전까지 이 IR의 기능으로 간주하지 않는다.

## 4. FFI, 프로세스 및 수명

현재 요청/응답의 Python `bytes` 경계에서 복사와 객체 생성이 발생한다. Tokio I/O 스레드와 Python 루프를 분리해도 단일 CPython 인터프리터의 GIL 경쟁은 사라지지 않는다. 다중 워커는 프로세스 격리를 제공하지만 강제 종료는 부분 완료된 외부 작업을 되돌리지 않는다. 공유 리스너 워커의 실행과 재시작은 Linux·macOS·Windows CI에서 검사한다. 신호 기반 수퍼바이저 검사는 Linux·macOS에서 수행한다.

후속 버퍼 API는 읽기 전용 소유 객체가 Rust 참조를 보유하고 Python memoryview의 마지막 참조가 사라질 때까지 메모리를 유지한다. 변경 가능한 공유 버퍼는 초기 범위에서 금지한다. 요청마다 최대 보유 바이트와 수명, 연결 종료와 소비자 취소, 뷰 보유 중 워커 종료 및 오류 처리를 정의한다. Arrow IPC는 컬럼형 데이터의 별도 프로토콜 후보이며 일반 HTTP 본문에 필수로 적용하지 않는다. zero-copy 주장은 해당 경로의 할당·복사 측정값과 함께만 한다. sub-interpreter/free-threaded Python은 PyO3와 각 확장 모듈의 안전성을 검증하기 전까지 옵션 제안이다.

## 5. ISR, 무효화 및 장애 모델

기본 L1은 워커별 메모리이며 SQLite L2는 **같은 파일을 안전하게 공유하는 워커**를 대상으로 한다. 공개 콘텐츠만 공유한다. 쿠키/Authorization 요청은 공유 ISR을 우회하고 `private, no-store`로 처리한다. `Set-Cookie`, `private`, `no-store` 응답은 캐시하지 않는다.

최소 수명 하한: ISR 정책의 전체 수명(`revalidate + stale`)이 `MIN_PRERENDERABLE_EXPIRE`(300초)보다 짧은 페이지는 동적이다. 동적 페이지는 프리렌더/정적 대상에서 제외된다. 이 하한은 분류 규칙이며 프로덕션 동작은 그대로 둔다 — 프로덕션에서도 동적 페이지는 선언한 정책대로 ISR 캐시를 거친다. 개발(`pynext dev`)에서는 stale 항목을 `expire`까지 서빙하고 항목을 최소 300초간 유지하므로 다시 로드하면 캐시를 맞는다. 이 하한은 Next.js의 `MIN_PRERENDERABLE_EXPIRE`과 같은 규칙이다.

SQLite에서 재생성자는 키별 소유 토큰·만료시각이 있는 lease를 획득한다. 저장 트랜잭션은 현재 버전과 만료되지 않은 lease 소유권을 함께 확인한다. 무효화는 공유 항목과 lease를 함께 제거하므로 오래된 생성자는 공유 캐시를 다시 채울 수 없다. 이미 진행 중인 요청은 자신의 이전 결과를 받을 수 있다. 공유 백엔드가 정상일 때는 매 캐시 조회가 공유 저장소를 확인하므로 다른 워커의 무효화가 다음 조회에 반영된다. L2 장애 시 로컬 생성/캐시로 저하되며 워커 간 single-flight와 즉시 일관성은 보장하지 않는다. SQLite DB 전체 저장 용량, 만료 항목의 주기적 정리 및 WAL 디스크 사용량에는 아직 전역 상한이 없다.

SQLite에서 재생성자는 키별 소유 토큰·만료시각이 있는 lease를 획득한다. 저장 트랜잭션은 현재 버전과 만료되지 않은 lease 소유권을 함께 확인한다. 무효화는 공유 항목과 lease를 함께 제거하므로 오래된 생성자는 공유 캐시를 다시 채울 수 없다. 이미 진행 중인 요청은 자신의 이전 결과를 받을 수 있다. 공유 백엔드가 정상일 때는 매 캐시 조회가 공유 저장소를 확인하므로 다른 워커의 무효화가 다음 조회에 반영된다. L2 장애 시 로컬 생성/캐시로 저하되며 워커 간 single-flight와 즉시 일관성은 보장하지 않는다. 수명은 단조 시계(백엔드 시작 시 wall-clock 앵커 + `time.monotonic()` 경과)로 판정해 wall-clock 점프에 흔들리지 않으며, `entries` 테이블은 기본 10,000개·128MiB 상한을 두고 삽입 시 만료 행 정리와 가장 오래된 항목부터의 축출로 유지한다. WAL 파일 자체의 디스크 사용량에는 아직 전역 상한이 없다.

다중 노드 후속 설계는 `(build_id, route, locale, policy_version)` 키, 원자적 세대 번호, 만료 가능한 lease와 저장소 측 fencing, invalidation 이벤트 및 L1 세대 검증을 필요로 한다. 이벤트 유실·노드 재시작·네트워크 분할 시 일관성 정책을 정하고, Redis/객체 스토어의 장애와 조건부 쓰기를 별도로 시험한다. 응답 시간 1ms 미만은 배포 환경의 측정 목표이며 API 보장이 아니다.

## 6. 보안과 관측성 계약

입력 경로는 한 번만 엄격하게 디코딩하며 인코딩된 구분자와 상위 경로 이동을 거부한다. CORS·CSRF·CSP는 `SecurityConfig`와 앱 정책에 따라 동작한다. 선택적 세션 미들웨어는 세션에 CSRF 토큰을 결합하고 인증·권한 검사 도구를 제공한다. 개발 버전의 WebSocket은 핸드셰이크 전에 기본적으로 같은 출처만 허용하며 추가 출처와 Origin 없는 클라이언트는 명시적으로 설정한다. 같은 시점에 전역 미들웨어 체인(세션 미들웨어 포함)과 핸들러의 `@require_auth`/`@require_permission` 정책도 핸드셰이크 전에 강제되며, 거부된 업그레이드는 101 전에 차단된다. 신원 확립(로그인·세션에 사용자 식별자 설정)과 세션 외 자격 증명 방식의 검증은 앱의 책임이다. 신뢰 프록시는 명시적 IP/CIDR만 설정하고 전달 주소 체인은 신뢰한 바로 앞 홉에서 역방향으로 해석한다. 기본 제한은 프로세스별이고, Redis 공유 제한을 사용하면 워커 간 제한을 공유한다. 제한 활성화 시 정적·ISR 요청도 Python 경유로 검사한다.

구조화 로그는 요청 ID·경로 패턴·상태·시간을 기록하고 본문, 인증 값과 생 쿼리를 기록하지 않는다. Python 지표는 Rust 빠른 경로 전체를 대표하지 않는다. 선택적 OpenTelemetry 훅은 전체 Rust→Python→외부 서비스의 추적 완성을 뜻하지 않는다. 후속 구현은 trace context 검증·샘플링·exporter 장애 시 제한된 큐·민감정보 제거를 계약으로 삼는다. 연결 제한은 워커별이다: 슈퍼바이저가 N개 워커를 띄우면 유효 총 상한은 N × 설정값이 되며, IP당 상한도 워커별로 적용된다.

## 7. 확인된 남은 간극과 목표 결정

| 영역 | 현재 확인된 간극 | 목표 동작 |
| --- | --- | --- |
| WebSocket | 연결별 큐·메시지 크기는 제한하지만 h2c 유휴 타임아웃은 없음 | h2c 유휴 연결 타임아웃과 추가 장애·종료 테스트 |
| 브라우저 탐색 | 명시적 공개 응답만 prefetch 캐시에 보관하며 크기와 동시 요청을 제한 | 취소·로그아웃/개인화와 프록시 캐시 장애에 대한 추가 회귀 테스트 |
| 브라우저 문서 | 본문과 제목만 교체하고 응답 head/CSP를 전체 문서처럼 갱신하지 않음 | 안전하게 동기화할 head 범위를 명세하고 불가능하면 전체 탐색으로 폴백 |
| SQLite | 공유 저장소 장애 시 로컬 복사본의 일관성 미보장, WAL 디스크 사용량 상한 부재 | 장애 정책과 성능 개선, WAL 디스크 quota, 장애 주입과 운영 지표 |
| 요청 제한 | 개발 버전은 양 서버에서 본문 수신 전 검사, Axum 판정은 HTTP·WebSocket 합계 64개/2초로 제한 | 신뢰 프록시·예외 경로·실패 정책을 유지하고 정상 트래픽 영향과 과부하 지연을 측정 |
| 연결/실행 | 동기 작업 강제 종료 미보장 | upstream별 기한, 프로세스 격리와 종료 시 부분 쓰기 계약 |
| 계측 | 빠른 경로·프로세스 간 지표와 전 구간 trace 미완성 | Rust→Python→upstream의 단일 context 및 bounded exporter, 라벨 상한 |
| RSC/배포 | React/RSC와 circuit breaker는 미구현, standalone은 Linux에 한정 | React/RSC는 현재 비목표다. 복구력·패키징은 별도 장애·호환성 검증 후 확대한다. |

## 8. 검증 및 완료 기준

- **PR 품질:** Linux/macOS/Windows의 CPython 3.13·3.14 × Axum/Uvicorn CI, Rust fmt/clippy/test, Python 전체 테스트, 양 전송 경로 브라우저 테스트, wheel 설치·예제 빌드가 통과해야 한다.
- **라우팅/보안:** 정적·동적·두 종류 catch-all 충돌/우선순위, 인코딩·경로 탐색, ASGI/Axum 패리티, 프록시 스푸핑, 제한 버킷 상한과 실패 모드를 재현한다.
- **수명/캐시:** 취소·동기 작업 지속, 느린 수신자, 동시 재생성, lease 만료/인수, 무효화 중 생성, 장애 후 로컬 대체, 다중 프로세스 및 정리 경합을 검증한다.
- **브라우저:** Chromium에서 hydration 각 전략, CSP, 탐색·history·에러 폴백, 개인화/캐시, 중복 활성화와 정리를 시험한다. 브라우저별 지원 주장은 해당 엔진을 실제 실행한 뒤에만 한다.
- **부하:** 동일한 머신·운영체제·런타임·네트워크·요청 크기·동시성·워커 수를 기록하고 전체/성공 RPS, 오류율, p50/p95/p99, CPU 및 RSS를 함께 측정한다. 짧은 CI 부하는 성능 보장이 아니다.

권장 우선순위는 보안·네트워크, 신뢰성·분산 운영, 성능·실행 모델, 배포·공급망 순이며 개발자 경험은 병행할 수 있다. 각 단계는 설계, 구현, 회귀·장애 테스트, 문서와 실측이 완료되어야 완료로 표시한다. React/RSC는 1.0 범위에 포함하지 않는다. 자세한 후보와 게이트는 [프로덕션 로드맵](PRODUCTION_ROADMAP.md)을 따른다.
