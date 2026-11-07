# Rust/Python 아키텍처

[홈](Home.md) · [English](../en/Architecture.md)

PyNext는 Python 앱 레이어와 **maturin/PyO3**로 빌드하는 Rust 네이티브 확장으로 구성됩니다.

~~~text
클라이언트
   |
   v
Tokio + Hyper (HTTP/1.1 / h2c)
   |
   v
Axum 요청 처리 / 본문 슬롯 (Rust)
   |
   +--> 공개 파일 / SSG / 신선한 익명 ISR --> HTTP 응답
   |
   v
실행 세마포어 / 제한된 대기열
   |
   v
PyO3 브리지 --> Python asyncio --> 라우트 디스패치
                                   |
                           +-------+-------+
                           |               |
                        Page/UI 트리     API 응답
                           |               |
                       JSON 직렬화      JSON/Stream
                           |
                       Rust HTML 렌더러
                           |
                           v
                        HTTP 응답
~~~

## 소스 구조

| 기능 | 소스 |
| --- | --- |
| Rust HTTP 엔진 / HTML 렌더러 | [`crates/pynext-native/src/lib.rs`](../../../crates/pynext-native/src/lib.rs) |
| Rust trie 라우터 | [`crates/pynext-router/src/lib.rs`](../../../crates/pynext-router/src/lib.rs) |
| Rust 캐시 | [`crates/pynext-cache/src/lib.rs`](../../../crates/pynext-cache/src/lib.rs) |
| Python 앱 / 요청 라우팅 | [`python/pynext/app.py`](../../../python/pynext/app.py) |
| 비동기 브리지 / 네이티브 서버 연결 | [`python/pynext/server.py`](../../../python/pynext/server.py) |
| 빌드 / SSG | [`python/pynext/build.py`](../../../python/pynext/build.py) |
| 브라우저 런타임 | [`python/pynext/browser/runtime.js`](../../../python/pynext/browser/runtime.js) |

## 성능 경계

버퍼형 페이지 렌더링은 Python 딕셔너리 UI 트리 → JSON 직렬화 → Rust JSON 파싱·HTML 이스케이프 과정을 거칩니다. 따라서 **GIL·JSON·복사 비용이 존재**하며 Rust 서버만으로 임의 Python 핸들러가 GIL 없이 실행되는 것은 아닙니다.

동기 핸들러는 스레드에서 실행됩니다. 요청 타임아웃/취소가 이미 실행 중인 동기 작업(DB 쓰기 포함)을 강제로 중단하지는 못합니다. 멱등성 설계와 외부 작업별 타임아웃이 필요합니다.

## 구현과 계획 구분

구현됨: 파일 라우팅, 버퍼형 SSR, SSG, 프로세스별 ISR, 네이티브 HTTP 서버, ASGI, API 스트리밍, 선택적 islands.

미구현: React/RSC, 스트리밍 SSR, 워커 공유 ISR, 앱별 프로세스 격리, standalone 패키징, 내장 OpenTelemetry.

다음 문서: [HTTP와 캐시](HTTP-and-Caching.md), [로드맵](Roadmap-and-Contributing.md).
