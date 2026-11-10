# ASGI 제한 판정 후속 구현 (0.1.2)

검토 #6의 참고 R1·R4에 대한 0.1.2 구현이다. 기존 0.1.1에는 포함되지 않는다.
ASGI HTTP·WebSocket은 앱 인스턴스별로 진행 중 판정 최대 64개와 판정별 2초
기한을 공유한다. 초당 또는 2초당 64요청의 속도 제한이라는 뜻은 아니다.

판정 용량은 본문 수신·핸들러 실행 용량과 별개다. 잘못된 HTTP 경로는 제한을
검사하기 전에 400으로 거부하고 토큰을 소비하지 않는다. 정상 HTTP·WebSocket
판정은 같은 limiter의 버킷을 사용하며 한 번만 차감한다. 헬스 경로는 HTTP 판정
예산을 우회해 포화 중에도 생존 상태를 확인할 수 있다.

판정에는 예외 경로 훅, 클라이언트 키 계산, limiter 호출을 포함한다. `blocking`
힌트와 무관하게 호스트의 기본 실행기로 보내므로 사용자 정의 동기 콜백이
ASGI 이벤트 루프를 멈추지 않는다. 기본 실행기에서 기다리는 시간도 2초 기한에
포함되고, 호스트의 실행기 설정에 따라 동시에 실행되는 콜백 수는 64보다 작을 수 있다.
따라서 사용자 정의 콜백도 스레드에서 안전하게 실행되는 동기 함수여야 한다.

- 용량 포화·기한 초과·콜백 예외·잘못된 반환값은 `503 Retry-After: 1`이다.
  판정 게이트의 이름은 "rate-limit decision"이라, 이 503은 Rust 슬롯 대기
  503과 메트릭·로그에서 구분된다(둘 다 503이지만 단계가 다르다).
- 정상적인 제한 거부는 429이며, 누락됐거나 0인 재시도 시간은 1초로 정한다.
- 판정 실패는 HTTP 본문이나 WebSocket connect 메시지를 읽기 전에 응답한다.
- 호출자가 취소되거나 기한을 넘겨도 이미 실행 중인 스레드의 용량은 실제
  종료할 때까지 유지한다. 아직 실행하지 않은 작업은 실행기에서 차례가 오면
  취소·기한 상태를 확인해 limiter 호출 없이 정리한다.
- `wait_for_inflight()`도 남은 ASGI 판정을 추적한다. ASGI 호스트 전체의 종료
  시간과 요청 기한은 호스팅 서버의 정책을 함께 설정해야 한다.

이미 실행한 외부 요청을 중단하거나 토큰을 되돌리는 보장은 없다. 실행 중인
콜백이 2초 뒤 늦게 토큰을 소비하는 R3 경계는 남는다. Redis·외부 서비스에는
명시적 I/O timeout을 두고, 늦은 완료를 고려한 운영 정책을 적용한다.

## 단계별 타임아웃 (C-P1-7)

ASGI 경로의 타임아웃도 본문 읽기 / 핸들러 실행 / 응답 전송으로 분리한다
(네이티브 경로는 Rust 본문 타임아웃 + 브릿지 기한 + 청크별 기한으로 이미
분리돼 있다).

| 단계 | 기본값 | 초과 시 응답 |
|---|---|---|
| 본문 읽기 (`body_timeout_ms`) | 10초 (전체 본문) | 408 — 클라이언트가 느린 것이지 서버 수용 능력이 아니다 |
| 핸들러 실행 (`handler_timeout_ms`) | 30초 | 504 — 서버가 일을 끝내지 못함 |
| 응답 전송 (`response_timeout_ms`) | 30초 (일반 응답 전체·스트리밍 청크당) | 헤더가 이미 나갔을 수 있어 상태 코드를 바꿀 수 없으면 스트림 중단 + 기록 |

수용 능력 초과는 503 + `Retry-After`이고 핸들러 시간 초과는 504이다 —
둘을 섞지 않는다. 각 단계의 타임아웃은 메트릭
`stage_timeouts{stage=body|handler|response|admission}`으로 구분해 셀 수
있고, 판정 게이트 실패는 `admission` 단계로 센다. 0은 해당 단계의 타임아웃을
끈다. 설정은 `create_app(..., body_timeout_ms=..., handler_timeout_ms=...,
response_timeout_ms=...)` 또는 CLI `--body-timeout-ms` /
`--handler-timeout-ms` / `--response-timeout-ms`로 준다.

## 검증

회귀 검사는 잘못된 경로, 표시되지 않은 blocking 콜백, HTTP·WebSocket 혼합
64개 포화와 65번째 거부, 타임아웃 뒤 용량 유지·회복, 취소된 대기 작업의 미실행,
잘못된 결과의 fail-closed, 메모리·실제 Redis 버킷의 단일 차감을 검사한다.
기존 0.1.1에서 경로 사례 4건의 실패를 재현한 뒤 수정 후 통과했다.

```bash
uv run --locked --all-extras pytest -q tests/test_asgi_admission.py tests/test_http_admission.py tests/test_ratelimit.py tests/test_websocket.py tests/test_asgi_conformance.py
```

전체 운영 적합성은 [프로덕션 로드맵](PRODUCTION_ROADMAP.md)의 별도 기준을 따른다.
