# PyNext 개발용 프롬프트

AI 에이전트에게 PyNext 개발을 맡길 때 쓰는 프롬프트다. 이 문서 전체를 복사해 AI에게 주고, 뒤에 할 일을 덧붙인다. 예를 들면 "B-1을 고쳐줘" 같은 식으로 쓴다.

## 대상

- 레포: https://github.com/Duioolllll/PyNext (main `acffbbf2`, 버전 **0.1.1**, 2026-11-09 릴리즈)

## 먼저 읽을 Docs (이 순서대로)

1. `README.md` — 개요, 사용 범위, 실행 방법
2. `docs/ko/manual.md` (또는 `docs/en/manual.md`) — 사용 설명서 (준비·라우팅·API·세션·WebSocket·SSR/SSG/ISR·islands·속도 제한·테스트)
3. `docs/FEATURES.md` — 코드에서 자동 생성된 기능 목록 (**Docs와 코드가 다르면 이 파일이 우선한다**)
4. `docs/ROADMAP.md` — 구현 현황과 후속 개발 방향
5. `docs/PRODUCTION_ROADMAP.md` — 프로덕션 게이트와 체크리스트
6. `docs/ARCHITECTURE_SPEC.md` — 아키텍처 명세
7. `CHANGELOG.md` — `## 0.1.1` 섹션 (최신 변경 내용)
8. `docs/DATABASE.md`, `docs/DJANGO.md` — DB 리소스 (SQLAlchemy / Django ORM)
9. `docs/wiki/ko/Home.md` — 위키 목차 (필요한 주제만 골라 읽는다)

## PyNext란 (Docs 요약)

- Next.js의 파일 기반 라우팅 경험을 참고해 **Python과 Rust**로 구현하는 **실험적 풀스택 프레임워크**다.
- **Next.js나 React의 호환 구현이 아니다.** React/RSC는 명시적 비목표다.
- 현재 사용 범위: **소규모 웹 프로젝트의 학습·개발·기능 검증**에 한정한다. 0.1.1은 보안과 장애 대응이 완성되지 않아 **공개 운영 서비스나 중요한 데이터를 처리하는 서비스에 부적합**하다. 인터넷에 직접 공개하거나 신뢰할 수 없는 앱을 실행하지 않는다.
- 구조: Rust 라우터(Axum, Next.js 경로 정렬·충돌 검사 참고) + Python 페이지·API (PyO3) + Rust HTML 렌더러. 실행 모드는 독립 실행 Axum과 Uvicorn 위의 ASGI.
- 렌더링: SSR·SSG·ISR. **SSG·ISR 페이지는 `request`를 받을 수 없고 인증도 걸 수 없다** (공개 콘텐츠 전용).
- 프론트: JS/TS island (esbuild, 하이드레이션 load/visible/idle). 공유 island 모델은 제한된 부분집합이다.

## 공개 API (Docs/FEATURES.md 기준, `pynext`에서 export)

`App`, `create_app`, `h`, `fragment`, `island`, `layout`, `model_prop`, `page`, `route`, `websocket`, `Database`, `DjangoResource`, `DjangoBusyError`, `Request`, `Response`, `JSONResponse`, `StreamingResponse`, `SecurityConfig`, `csrf_protect`, `new_csrf_token`, `sign_csrf_token`, `validate_csrf`, `content_security_policy`, `security_headers`, `RateLimiter`, `UploadFile`, `Session`, `SessionMiddleware`, `SQLiteSessionStore`, `RedisSessionStore`, `require_auth`, `require_permission`, `compile_client_dsl`, `WebSocket`, `WebSocketDisconnect`.

## 프로젝트 구조

~~~text
app/
  layout.py
  page.py
  blog/[slug]/page.py
  docs/[...path]/page.py
  api/users/route.py
public/
  styles/site.css
islands/
  <island>.ts
pynext.config.py
~~~

## 개발 환경

- CPython 3.13·3.14, Rust/Cargo, `uv`. islands를 빌드하려면 Node.js 18 이상 + npm(esbuild 0.28.1).
- OS: Ubuntu·macOS·Windows (CI 매트릭스 기준).

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

## 코드 짤 때 지켜야 할 규칙 (Docs + 리뷰 기준)

- **Docs를 먼저 읽는다.** 기능이 Docs와 코드에서 다르면 **코드(FEATURES.md)가 우선**한다. 추측으로 API를 만들지 않는다.
- **공개 API를 바꾸지 않는다.** 새 기능은 기존 `pynext` export와 일관되게 추가한다. `.pyi` 스텁이 있는 모듈은 스텁도 함께 고친다.
- **제약 (알려진 것)**:
  - 요청 본문 상한 **1 MiB** (초과 시 413). `--body-capacity`는 슬롯 수이지 바이트 상한이 아니다.
  - islands: **npm import 불가, 외부 import 불가, CSS import 불가, 중첩 island 불가**. Props는 공개 JSON, **16 KiB 이하**, 비밀값 금지. 공유 모델은 16 KiB·512노드·깊이 32 제한.
  - SSG 비스트리밍 페이지 **8 MiB 상한**, SSG/ISR 스트림 30초 제한.
  - 세션: 쿠키 HttpOnly·Secure·SameSite=Lax. 로그인 후 **`session.rotate_on_login()` 호출 필수** (세션 고정 방지, 문서상 요구사항 — 프레임워크가 강제하지 않으니 앱에서 호출한다). 세션 값은 JSON 직렬화 가능해야 한다.
  - WebSocket: 전역 미들웨어와 `@require_auth`가 핸드셰이크 **전에** 적용된다. pre-flight `request.state`는 핸들러에 전달되지 않는다.
  - 속도 제한: HTTP와 WebSocket이 **공유 64개 판정 예산**을 쓴다. 결정당 2초 타임아웃, 포화·타임아웃 시 503 + `Retry-After: 1`. 과금된 토큰은 **환급하지 않는다** (413/408/503 포함).
  - CSRF: Origin 스킴·정식 호스트·유효 포트를 비교한다. 워커마다 서명 키가 달라지면 안 된다 (`--csrf-signing-key` 또는 `PYNEXT_CSRF_SIGNING_KEY`로 공유).
- **테스트를 돌린다.** Rust 수정 후 `cargo fmt`, `cargo clippy`, `cargo test`를 실행한다. Python 수정 후 관련 `pytest`를 실행한다 (`no_proxy`·`NO_PROXY`를 `localhost,127.0.0.1`로 설정). `/tmp`는 512MB라 standalone 테스트는 `TMPDIR`을 메인 디스크로 쓴다.
- **확인하지 않은 성공을 성공으로 말하지 않는다.** 테스트를 돌리지 않았으면 "테스트 못 돌렸다"고 명시한다.
- **문서와 코드를 함께 고친다.** 기능을 바꾸면 `docs/ko/manual.md`·`docs/en/manual.md`·`docs/wiki/`와 `CHANGELOG.md`의 해당 부분을 함께 고친다. `docs/FEATURES.md`는 손으로 고치지 않고 `python scripts/generate_feature_table.py`로 재생성한다.

## 현재 상태 (2026-11-09 기준)

- 0.1.1 릴리즈 완료 (태그 `v0.1.1` → `acffbbf2`). PyPI에는 아직 안 올라가 있다 (GitHub 릴리즈 휠만 있음).
- 리뷰 #7 (main `acffbbf2` 대상) 결과: 치명 0건, 중요 1건, 참고 5건.
  - 중요 1건: **커스텀 리미터의 `super().is_exempt()` 위임 시 인코딩된 `?`로 속도 제한 우회** (`python/pynext/ratelimit.py`). 네이티브 회귀, 재현 확인됨. 0.1.2에서 고치는 것이 권장된다.
  - 참고 5건: `pynext.__version__` 없음, 네이티브 크레이트 `Cargo.toml` 여전히 0.1.0, 빈 `## Unreleased` 제목, 인코딩된 헬스 프로브가 정적 에셋에 가려짐, 취소된 동기 핸들러 500 기록.
- 다음 릴리즈(0.1.2) 후보: 위 중요 1건 수정이 1순위다.

## 작업 방식

1. 할 일을 먼저 말하고 (무엇을, 어떤 파일에서, 어떻게), 그다음 코드를 짠다.
2. 한 번에 한 가지씩 고친다. 여러 기능을 한 커밋에 섞지 않는다.
3. 고친 뒤에는 관련 테스트를 돌리고 결과를 말한다 (통과 수·실패 수·스킵 수).
4. 막히는 부분(예: Docs에 없는 기능, 상충하는 요구)은 우회하지 말고 그대로 말하고 물어본다.
5. 커밋 메시지와 PR 본문에 "Prepared by Muse"를 넣는다. 작성자는 Muse로 한다.
