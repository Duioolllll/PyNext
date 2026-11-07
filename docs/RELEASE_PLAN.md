# 릴리스 계획

## 버전 결정 (2026-11-07, 사용자 결정)

버전은 `main`을 따른다. 현재 `main` 버전은 **0.6.0a1**(`pyproject.toml`)이다.

폐기된 구 계획:

- 패키지 버전 **0.1.0a0** 설정 — 버린다. `main`의 0.6.0a1을 쓴다.
- 태그 **v0.1.0-alpha.prealpha** 계획 — 폐기한다. 이 태그는 만들지 않는다.

## 정리 대상 브랜치

### `patch/503-wait-trie-r2` (3c48222) — 코드 흡수 후 폐기 예정

PR #2의 대체본. 담고 있는 코드 가치는 `fix/review-3-rust`로 흡수한 뒤 브랜치를 폐기한다.

- 슬롯 대기: 슬롯이 차면 즉시 503이 아니라 최대 1초 대기, 그래도 안 되면 503 + `Retry-After: 1`
- 정적 파일·SSG·신선한 ISR 캐시 적중은 동시성 슬롯을 쓰지 않고 우회
- 트라이 라우터: 선형 스캔 대신 트라이 매칭 (static > dynamic > catch-all, 백트래킹)
- `--max-concurrent` / `--acquire-timeout-ms` CLI 설정
- ISR `CacheBusy` 503에도 `Retry-After: 1`
- 테스트: `tests/test_bad_request.py`(신규), `tests/test_router.py`에 트라이 매칭 테스트

### `release/0.1.0-alpha.prealpha` (500030f) — 폐기 예정

0.1.0a0 기준이라 통째로 폐기한다. 머지하면 `main` 버전이 0.6.0a1에서 0.1.0a0으로 되돌아간다.

- 버전만 바꾼 부분(`pyproject.toml`, `uv.lock`, CHANGELOG·README·로드맵의 버전 표기)은 버린다.
- 매뉴얼 4종에 섞인 문구 수정(공개 파일 URL 규칙, 외부 import 표현, `--host`/`--port` 안내)은 main 기준으로 다시 검토할 때만 살릴 수 있다.
- 주의: 이 브랜치 매뉴얼의 503 관련 문구는 r2 이전 동작("즉시 503, 설정 불가")을 서술하므로, r2 흡수 후에는 맞지 않는다. 그대로 살리지 않는다.

## 새 태그 규칙

새 태그는 `main` 버전 계열로 만든다. 다음 릴리스는 **v0.6.0 계열**(예: `v0.6.0a1` 이후 정식 `v0.6.0`)이다.

## 원격 PR #2

원격 PR #2는 OPEN 상태로 `main`과 충돌 중이며, r2로 대체됐으므로 superseded로 닫아야 한다. 원격 쓰기라 이 브랜치에서 직접 닫지 않는다. 사용자 승인 후 처리한다.

## `main`에 남은 구 계획 참조

`main`(d0bf3fc4) 전체에서 `prealpha`, `0.1.0a0`, `0.1.0-alpha`는 **없다**. `0.1.0`만 아래에 남아 있으나 구 계획 참조가 아니므로 그대로 둔다.

- `CHANGELOG.md:3` — v0.1.0–v0.3.0 재구성 스냅샷 설명 (과거 버전 기록)
- `CHANGELOG.md:35` — `## v0.1.0` 절 제목 (과거 버전 기록)
- `crates/pynext-cache/Cargo.toml:3`, `crates/pynext-native/Cargo.toml:3`, `crates/pynext-router/Cargo.toml:3`, `Cargo.lock:293,300,313` — 내부 크레이트 버전 0.1.0 (프레임워크 버전과 무관)
- `package.json:3`, `package-lock.json:3,9` — JS 도구 패키지 버전 0.1.0 (프레임워크 버전과 무관)
