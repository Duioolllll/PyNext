# 기여 가이드

PyNext에 기여해 주셔서 감사합니다. 이 문서는 기여 절차와 개발 환경을
설명한다. 큰 변경은 코드 작성 전에 이슈로 먼저 논의해 달라.

## 먼저 읽을 것

- `README.md` — 설치, 실행, 지원 환경
- `docs/ROADMAP.md` — 구현 현황과 후속 개발 방향. React / React Server
  Components 호환은 현재 비목표이며, 1.0 이후 목표로 미뤄져 있다.
  이 범위의 기능은 PR로 받지 않는다.
- `docs/ARCHITECTURE_SPEC.md` — 아키텍처와 보안 경계
- `SECURITY.md` — 취약점 신고 방법. **보안 문제는 공개 이슈나 PR로
  올리지 말고 `SECURITY.md`의 비공개 신고 경로를 쓸 것.**
- `docs/SUPPORT.md` — 지원 버전과 릴리스 주기

## 기여 절차

1. 저장소를 포크하고, `main`에서 설명이 분명한 브랜치를 만든다.
   (예: `fix/csrf-cookie-secure`, `docs/support-policy`)
2. 변경 범위를 작게 유지한다. 관련 없는 리팩터링이나 서식 변경을
   같은 PR에 섞지 않는다.
3. 테스트와 문서를 함께 바꾼다. 동작이 바뀌면 `CHANGELOG.md`의
   해당 버전 절에도 항목을 추가한다. 단, CHANGELOG의 과거 버전
   기록(재구성 스냅샷 설명)은 고치지 않는다.
4. 아래 "확인 사항"을 전부 통과시킨 뒤 PR을 연다.
5. PR 본문에 변경 내용, 관련 이슈, 실행한 테스트를 적는다
   (PR 템플릿 참고). 리뷰에서 요청한 수정은 같은 PR에 반영한다.

큰 기능, 설계 변경, 공개 API 변경은 먼저 이슈를 열어 방향을 합의한다.
합의 없이 올린 대형 PR은 받지 않을 수 있다.

## 개발 환경

필요한 도구: Rust 툴체인(안정판), [uv](https://docs.astral.sh/uv/),
Node.js 18 이상과 npm. 예제의 islands를 빌드하려면 Node.js가 필요하고,
브라우저 스모크에는 Playwright와 Chromium이 필요하다.

```bash
# 의존성 설치
uv sync --locked --extra dev --extra images
npm ci

# 예제 빌드·실행
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic

# 개발 서버 (소스 변경 시 같은 포트로 재시작)
uv run pynext dev --dir examples/basic
```

## 확인 사항

PR을 열기 전에 아래를 전부 실행한다. CI(`.github/workflows/ci.yml`)도
같은 명령을 Ubuntu/macOS/Windows × Python 3.13/3.14 × Axum/Uvicorn
매트릭스로 실행한다.

```bash
# Rust 서식·린트·테스트
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo test --locked --workspace --all-features

# Python 테스트
uv run --locked --extra dev --extra images pytest -q

# 예제 프로덕션 빌드
uv run pynext build --dir examples/basic
```

새 기능이나 버그 수정에는 회귀 테스트를 함께 넣는다. 테스트가 flaky하면
원인을 밝히고, 재시도나 대기 시간 늘리기로 덮지 않는다.

## 코딩 규칙

- 공개 API를 바꾸면 문서와 타입 힌트를 함께 바꾼다.
- 민감 정보(헤더·쿠키·본문 값, 토큰)를 로그·지표·에러 메시지에
  남기지 않는다. 지표 라벨에는 라우트 패턴을 쓰고 원본 URL을 쓰지 않는다.
- 자원 상한(연결 수, 본문 크기, 캐시 용량, 큐 길이)이 있는 경로는
  상한을 넘는 경우의 동작(거절, 대기, 폴백)을 테스트와 문서로 명시한다.
- "더 빠르다"는 이유만으로 위험한 경로를 기본값으로 바꾸지 않는다.
  성능 주장은 재현 가능한 측정과 함께 제시한다.
- Rust 확장을 바꾼 뒤에는 `uv sync`가 확장을 다시 빌드하지 않을 수
  있다. 필요하면 `--reinstall-package pynext-framework`로 강제한다.

## 문서 기여

- README·위키·매뉴얼이 서로 모순되지 않게 한다. 코드와 문서가 다르면
  코드를 기준으로 문서를 고친다(로드맵의 문서 운영 원칙).
- 버전 표기는 `pyproject.toml`의 현재 버전(0.1.0)을 따른다.
  폐기된 버전 계열(0.6.0a2, 0.7.0a1, 0.1.0a0)은 문서에 다시
  등장시키지 않는다.
- 번역(영문·한문·일문 매뉴얼)은 원문과 같은 내용을 유지한다.
  한 언어만 바꾸는 PR은 원문과의 불일치를 함께 적는다.

## 릴리스·버전

릴리스는 메인테이너가 `main`에서 만든다. 기여자는 릴리스를 직접
만들지 않는다. 버전 정책과 주기는 `docs/SUPPORT.md`를 참고한다.

## 행동 규칙

기여자는 서로를 존중하고, 기술적 논의에 집중한다. 괴롭힘, 차별,
인신공격은 받지 않는다. 문제가 있으면 메인테이너에게 비공개로 알린다.
별도의 행동 강령 파일은 없으며, 이 절이 그 역할을 한다.
