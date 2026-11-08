## 요약

<!-- 변경 내용을 한두 문단으로 설명해 주세요. -->

## 관련 이슈

<!-- 예: Closes #123 -->

## 변경 내용

-

## 테스트

<!-- 실행한 명령과 결과를 적어 주세요. 예: -->

<!-- - `cargo test --locked --workspace --all-features` — 통과 -->
<!-- - `uv run --locked --extra dev --extra images pytest -q` — 통과 -->
<!-- - 새로 추가한 테스트: `tests/test_...py` -->

## 체크리스트

- [ ] `cargo fmt --all -- --check` 통과
- [ ] `cargo clippy --locked --workspace --all-targets --all-features -- -D warnings` 통과
- [ ] `cargo test --locked --workspace --all-features` 통과
- [ ] `uv run --locked --extra dev --extra images pytest -q` 통과
- [ ] `uv run pynext build --dir examples/basic` 통과
- [ ] 동작 변경이 있으면 테스트와 문서를 함께 바꿨다
- [ ] 동작 변경이 있으면 `CHANGELOG.md`에 항목을 추가했다

## 보안 고려 사항

<!-- 보안 관련 변경이면 영향 범위를 적어 주세요. 취약점 자체는 이 PR에 올리지 말고 SECURITY.md의 비공개 신고 경로를 사용해 주세요. -->
