# 테스트와 CI

[홈](Home.md) · [English](../en/Testing-and-CI.md)

## 로컬 검증

~~~bash
uv sync --locked --extra dev
npm ci
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo test --locked --workspace --all-features
uv run pytest -q
uv run pynext build --dir examples/basic
uv run python tests/smoke_wheel.py
~~~

`tests/smoke_wheel.py`는 독립 설치 환경에서 wheel을 검증하며 관련 배포 파일이 준비되어 있어야 합니다. 정확한 배포 빌드 순서는 [CI 워크플로](../../../.github/workflows/ci.yml)를 참고하세요.

## Chromium 브라우저 검사

~~~bash
uv run --with playwright==1.62.0 python -m playwright install chromium
uv run --with playwright==1.62.0 python tests/browser/run_smoke.py
~~~

실제 Chromium을 통해 Island mount/hydration, 상호작용, DOM 불일치 폴백, 탐색 및 CSP 동작을 검사합니다.

## GitHub Actions

CI는 **Ubuntu·macOS·Windows**에서 Python 3.13·3.14, Node.js 20, Rust, uv를 사용합니다. 포맷·Clippy·Rust/Python 테스트, 프로덕션 예제 빌드, sdist/wheel 패키징, 독립 wheel 검증, Axum/Uvicorn HTTP 스모크 벤치마크, 브라우저 테스트를 수행합니다. 의존성은 `uv sync --locked`, `npm ci`, Rust `--locked`로 검증합니다.

플랫폼별 배포 파일과 서버별 벤치마크 JSON이 아티팩트로 남습니다. **CI 성공은 프로덕션 보안·대규모 처리량 보증이 아닙니다.**

## 테스트 위치

- Rust 라우트/캐시: `crates/pynext-router`, `crates/pynext-cache`.
- Python/ASGI·ISR·정적 응답·스트리밍: `tests/test_*.py`.
- 네이티브 HTTP 포화/연결 처리: `tests/test_native_admission.py`, `tests/test_streaming_native.py`.
- 브라우저: `tests/browser/`.

동작을 바꾼 PR에는 대응되는 회귀 테스트와 호환성 변경 설명을 추가하는 것이 좋습니다. [로드맵과 기여](Roadmap-and-Contributing.md)도 참고하세요.
