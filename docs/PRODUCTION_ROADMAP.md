# 프로덕션 기능 로드맵

이 문서는 제안된 설계와 완료 기준이다. **v0.5.0에 이 기능들이 구현되어 있다는 뜻이 아니다.** PyNext는 현재 실험판이며, 각 항목은 설계 검토·구현·테스트·측정을 마친 뒤에만 릴리스한다. 버전 번호와 일정은 검증 결과에 따라 정한다.

## 출발점과 공통 측정

현재 Axum은 정적 파일과 일부 캐시 응답을 Rust에서 처리하고, 동적 요청은 Python asyncio 루프로 넘긴다. Python 응답은 버퍼링하며 ISR 캐시는 프로세스별이다. 현재 코드에는 RSC, 스트리밍, 분산 캐시, OpenTelemetry, 프로세스 격리 또는 standalone 빌드가 없다.

각 단계는 동일한 하드웨어·운영체제·런타임 버전·작업자 수·TLS 설정·요청 크기·동시성·클라이언트 위치를 기록한 재현 가능한 기준과 비교한다. 처리량뿐 아니라 p50/p95/p99/p99.9 지연, 오류율, 메모리, 할당·복사 횟수, CPU, GIL 대기 및 취소 후 남은 작업을 측정한다. 부하 생성기는 가능한 한 별도 머신에서 실행한다. 수치는 테스트 결과이며 범용 보장이 아니다.

## 1. Rust ↔ Python 버퍼와 실행 경계

### 설계 후보

- HTTP 본문을 위한 첫 후보는 소유권이 명확한 읽기 전용 Rust 바이트 버퍼와 Python buffer protocol/memoryview다. Rust 버퍼의 참조가 Python 뷰보다 오래 살아야 하며, 크기 제한·취소·예외·조기 해제 경로에서 use-after-free나 무제한 보유가 없어야 한다.
- Arrow IPC는 대형 **컬럼형 데이터** 교환에 별도 선택지로 검증한다. 모든 HTTP 요청에 Arrow 변환을 강제하지 않는다. 프로세스 간 공유 메모리는 포인터 자체가 아니라 수명·권한·크기·정리 규칙을 가진 핸들로 전달한다.
- 네트워크 스택, TLS, Python 객체 생성과 직렬화에는 여전히 복사나 할당이 생길 수 있다. “zero-copy/zero-allocation”은 범위를 한정한 경로별 측정 결과로만 주장한다.
- Tokio가 Rust I/O를 처리해도 단일 GIL 기반 CPython에서 실행되는 Python 바이트코드는 여전히 GIL의 영향을 받는다. 프로세스 워커를 우선 검증하고, sub-interpreter 및 free-threaded CPython은 PyO3와 앱의 확장 모듈 호환성·안전성 검증 후 별도 옵션으로 취급한다. Rust 스레드 풀만으로 GIL이 사라지지는 않는다. [PyO3 병렬성 안내](https://pyo3.rs/main/parallelism), [free-threading 안내](https://pyo3.rs/main/free-threading).

### 완료 기준

1. 소유권·수명·변경 가능성·프로세스 간 전달 계약과 폴백 경로를 문서화한다.
2. 취소, 연결 해제, 버퍼 보유 제한, 재사용, 동시 접근, 예외 및 워커 종료 테스트를 통과한다.
3. 현재 복사 기반 경로와 작은/큰 요청 및 스트리밍을 같은 조건에서 비교하고, 위험한 경로는 더 빠르다는 이유만으로 기본값으로 전환하지 않는다.
4. 프로세스 워커 장애·타임아웃·재시작과 앱별 호환성 테스트를 통과한다. 동기 작업 강제 종료의 트랜잭션·중복 실행 결과를 명시한다.

## 2. React/RSC와 스트리밍 SSR

V8을 내장하는 것만으로 React SSR 또는 RSC가 완성되지는 않는다. Rust에서 V8 바인딩을 쓰는 실험은 크기·보안 업데이트·빌드 시간·라이선스·격리 비용을 측정하고, 별도 프로세스 방식과 비교한다. React Server Components의 서버/클라이언트 모듈 경계, Flight 데이터, 참조 manifest, JSX 번들, React 버전 호환성, 오류와 Suspense를 별도로 구현해야 한다. React 공식 문서도 프레임워크·번들러용 RSC 내부 API의 버전 고정을 권장한다. [React Server Components](https://react.dev/reference/rsc/server-components).

### 완료 기준

1. 고정한 React·컴파일러·V8 버전과 지원하는 API 범위가 명시되고 서버/클라이언트 경계의 end-to-end 테스트가 통과한다.
2. Suspense fallback 이후 청크가 실제 브라우저에 순서대로 도착하고, backpressure·클라이언트 연결 해제·중간 오류·시간 제한·워커 종료 시 정리된다.
3. HTTP/1.1과 HTTP/2 이상은 각 서버의 프레이밍을 사용한다. 모든 응답에 `Transfer-Encoding: chunked`를 강제하지 않는다.
4. JS 비활성화, CSP, 신뢰할 수 없는 입력, 사용자별 데이터의 캐시 혼입 및 기존 island와의 공존을 검증한다.

## 3. 관측 가능성과 복구력

- OpenTelemetry는 Rust 요청 span, Python 핸들러 span, 외부 호출 span을 동일한 trace context로 연결한다. W3C `traceparent`/`tracestate`를 검증해 전파하고, 취소·오류·샘플링 및 비동기/프로세스 경계를 테스트한다. [OpenTelemetry context propagation](https://opentelemetry.io/docs/concepts/context-propagation/).
- 요청·본문·인증 정보는 기본적으로 기록하지 않는다. 경로 라벨의 카디널리티, 샘플링, exporter 장애와 큐 크기 제한을 정한다.
- 속도 제한은 토큰 버킷 등 알고리즘, 키 수명·메모리 상한, `429`/`Retry-After`, 프록시 신뢰 목록, 장애 시 정책을 정한다. IP 헤더나 토큰 값을 무조건 신뢰하지 않는다. 프로세스별 제한과 분산 제한을 구별한다.
- 서킷 브레이커는 특정 upstream 단위로 closed/open/half-open, 실패 판정, 관찰 창, 제한된 probe 및 복구를 정의한다. 전역 서버 과부하 제한과 구분한다.

### 완료 기준

Rust→Python→upstream의 trace 연결과 취소, exporter 중단 시 bounded queue, 민감정보 비노출, 제한 정확도와 프록시 스푸핑, 브레이커 open/half-open/복구 및 경쟁 상태를 자동 테스트한다. 계측 on/off의 지연과 처리량도 공개한다.

## 4. 분산 ISR 및 저장소

Redis 등에서 경로·빌드·로케일·정책 버전이 반영된 키, 소유자가 확인 가능한 재생성 lease, 만료·갱신, 무효화와 장애 시 동작을 설계한다. 콘텐츠는 크기 제한·무결성 해시를 적용하고, 필요하면 S3/GCS 같은 객체 저장소의 immutable blob과 원자적 버전 포인터를 분리한다. 오래된 워커가 lease 만료 후 새 결과를 덮어쓰지 못하도록 저장소에서 fencing 또는 조건부 갱신을 검증해야 한다. [Redis 분산 잠금의 안전성](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/).

### 완료 기준

1. 두 개 이상의 프로세스·노드에서 single-flight, stale 제공, 정확한 무효화와 인증 요청의 공유 캐시 우회를 확인한다.
2. lease 만료, 느린 생성, 워커 사망, Redis 장애·재시작, 네트워크 분할, 객체 저장소 실패와 경쟁 쓰기를 시험한다. 지원하지 않는 장애 모델은 명시한다.
3. 캐시 항목·태그·본문 크기, 보존 기간과 개인정보 정책을 제한한다.
4. “1ms 미만”은 네트워크·토폴로지·부하에 종속된 **측정 목표**다. 조건과 p50/p95/p99를 공개하고 충족하지 못한 환경에서 보장하지 않는다.

## 5. standalone 및 컨테이너 배포

`pynext build --standalone`는 아직 없다. Python 인터프리터·네이티브 확장·앱 의존성·정적 산출물을 포함한 단일 실행 파일과 slim/distroless 이미지를 각각 시제품으로 비교한다. 라이선스 고지, 플랫폼·아키텍처별 빌드, 동적 라이브러리, 인증서, 설정·비밀 주입, graceful shutdown, 보안 업데이트 및 재현 가능한 빌드를 포함한다.

### 완료 기준

깨끗한 지원 환경에서 설치·시작·정적 파일·SSR/API·island·종료를 확인한다. 최소 권한 실행, 취약점 검사, 산출물 해시/SBOM, 크기·콜드 스타트와 플랫폼 지원표를 CI에서 검증한다. 지원 불가능한 확장 모듈이나 플랫폼은 명확히 표시한다.

## 권장 진행 순서

1. 재현 가능한 부하 기준선과 요청 수명·프로세스 워커 계약.
2. trace 전파와 bounded 관측성, 과부하 제한 및 장애 테스트.
3. 분산 ISR의 정확성·무효화·장애 복구.
4. 측정 기반의 공유 버퍼와 Python 실행 격리.
5. 버전 고정된 React/RSC 및 스트리밍.
6. standalone/컨테이너 패키징과 지원 플랫폼 확대.

순서는 의존 관계를 반영한 제안이며 병렬 설계는 가능하다. 외부 의존성 추가나 성능 수치는 각 단계의 실제 검증 결과에 따라 결정한다.
