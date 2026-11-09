# 런타임·배포 후속 구현 (개발 버전)

이 문서는 2026-11-09에 요청된 7개 항목의 구현 범위와 검증 방법을 정리한다.
이 내용은 PR #17의 0.1.1 릴리스 후보에 포함하며, 게시된 0.1.0에는 없다. 적용 범위는 계속 **소규모 웹과
통제된 파일럿**이다. 공개 프로덕션 적합성은 아직 확보하지 않았으며, 남은
구체화·개선 방향은 [전체 로드맵](PRODUCTION_ROADMAP.md)에서 관리한다.

| 요청 | 구현과 회귀 검증 | 남은 범위 |
| --- | --- | --- |
| P1 정적 CORS | Axum의 Origin 포함 요청은 ASGI와 같은 Python 정책을 사용한다. 공개 파일·SSG·ISR의 GET/HEAD, Range, 304, preflight, 거부·와일드카드·자격 증명을 비교한다. Origin 없는 정적 요청의 빠른 경로는 유지한다. | 전용 러너에서 경로별 비용 측정 |
| P1 탐색 경합 | 새 탐색이 이전 fetch를 abort하고, 이전 성공·실패는 토큰으로 차단한다. abort를 무시하는 전송과 뒤로 가기까지 Chromium에서 Axum/Uvicorn 양쪽으로 검사한다. | Firefox/WebKit 확대 |
| P2 CSRF Origin | 스킴·정규화한 호스트·유효 포트를 함께 비교한다. 기본 포트, IPv6, 중복·비ASCII 헤더, 신뢰 프록시의 외부 주소를 처리한다. | 운영 프록시별 정책 검증 |
| P2 반복 측정 | 7개 시나리오 × Axum/Uvicorn × 3회, 회당 2,000요청·워밍업 100·동시성 10·프로덕션 빌드. 로그 파이프 교착과 SQLite 픽스처의 동시 접근을 수정했다. | 별도 물리 머신·TLS·실제 DB 풀·장시간·분산 환경 |
| P2 동기 작업 취소 | 요청 취소 신호, 협력적 부작용 검사, 실제 스레드 종료까지 유지되는 용량 상한과 종료 추적을 추가했다. 504 및 ASGI 연결 해제 뒤 후속 쓰기·용량 회복을 검사한다. | 실행 중 스레드 강제 중단·외부 쓰기 롤백은 제공하지 않음 |
| P3 Docker·공급망 | 다단계 이미지, UID 10001, 읽기 전용 실행, readiness·SIGTERM 검사, Python CycloneDX·이미지 SPDX SBOM, Python/npm/Rust/이미지 취약점 CI를 추가했다. | 이미지 서명·재현 빌드·다중 아키텍처·클러스터 배포 |
| P3 공개 타입 | 공개 재수출 API의 `.pyi`와 `py.typed`를 wheel/sdist에 배포한다. 별도 설치 환경의 mypy·pyright에서 정상/오류 예제를 검사한다. | 내부 모듈 전체 strict 검사와 앱별 동적 상태 스키마 |

## 요청 취소와 외부 부작용

```python
from pynext import Request, Response, route

@route("POST")
def submit(request: Request) -> Response:
    request.check_cancelled()
    # 외부 호출에는 별도 deadline/timeout과 멱등성 키를 설정한다.
    # DB 트랜잭션 commit 등 새로운 부작용 직전에도 다시 검사한다.
    return Response("accepted")
```

`request.cancelled`는 스레드에서 읽을 수 있는 bool이고,
`check_cancelled()`는 `RequestCancelled`를 발생시킨다. Axum의 30초 핸들러
제한·연결 해제와 ASGI 연결 해제에 취소 신호를 설정한다. ASGI 요청 기한은
호스팅 서버가 설정한다. `request.revalidate()`도 취소된 요청에서는 시작하지
않는다. 확인과 쓰기는 원자적이지 않으므로, 검사 직후 발생한 취소나 이미
실행한 쓰기를 막거나 되돌릴 수는 없다. 응답 504는 미실행·롤백의 증거가 아니다.

`create_app(..., sync_workers=32)`가 동기 핸들러와 일반 페이지 레이아웃의
진행 작업 수를 제한한다(1–1024, 워커별). 꽉 차면 추가 동기 호출은
`503 Retry-After: 1`이다. 시간 초과 후에도 스레드가 끝날 때까지 용량을
반환하지 않으며, 아직 실행 전인 취소 작업은 핸들러를 호출하지 않는다.
`wait_for_inflight()`도 이 작업들을 기다린다. 애플리케이션이 직접 만든
스레드·`asyncio.to_thread`·별도 실행기는 이 상한에 포함되지 않는다.
공유 ISR 재생성은 별도의 캐시 작업이며 클라이언트 한 명의 취소로 중단하지
않는 기존 정책을 유지한다. 자세한 결정은 [ADR-0002](adr/0002-sync-cancellation.md)에 있다.

## 검증 실행

```bash
uv sync --locked --all-extras
uv run --locked --all-extras pytest -q
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo test --locked --workspace --all-features
uv run --locked --all-extras pynext build --dir examples/basic
uv run --locked --all-extras --with playwright==1.62.0 python tests/browser/run_smoke.py --mode axum
uv run --locked --all-extras --with playwright==1.62.0 python tests/browser/run_smoke.py --mode uvicorn
uv build --wheel --sdist
uv run --locked --all-extras python scripts/check_typing.py --wheel dist/*.whl
```

현재 측정의 원본·조건·한계는 [벤치마크 문서](BENCHMARKING.md), 컨테이너와
검사 명령은 [배포 문서](CONTAINERS.md), 타입 경계는 [타입 문서](TYPING.md)에 있다.

## 헤더 단계 제한 판정의 경계

네이티브 HTTP·WebSocket은 **동시 판정 64개**의 공통 예산을 쓰고 각 판정은
최대 2초를 기다린다. 초당/2초당 64요청의 속도 제한이라는 뜻이 아니다.
시간이 초과돼도 진행 중 동기 콜백이 끝날 때까지 판정 용량을 유지한다.
콜백 자체를 강제 종료하지 않으므로, 503 응답 뒤에 늦은 콜백이 토큰을
소비할 수 있다. 승인한 요청은 업로드 중단·413·속도 미달 408·실행 용량 503 뒤에도
환급하지 않는다. 메모리/Redis와 HTTP·WebSocket 혼합 포화를 회귀 검사한다.

게시된 0.1.1의 ASGI에는 네이티브와 같은 판정 예산·타이머가 없고 잘못된 경로도
제한 검사 후 400을 반환할 수 있다. 0.1.2 후속 구현은 이 두 차이를 수정한다.
공통 예산·취소·실행기 및 남은 늦은 완료 경계는 [ASGI 판정 계약](ASGI_ADMISSION.md)을 따른다.
