# 로드맵, 릴리스, 기여

[홈](Home.md) · [English](../en/Roadmap-and-Contributing.md)

## 현재 릴리스

이 위키가 설명하는 공개 실험 릴리스는 **[v0.6.0a2](https://github.com/Duioolllll/PyNext/releases/tag/v0.6.0a2)**입니다. 위키는 릴리스 준비 이후 벤치마크·CI 개선이 병합된 `main`의 [aa256bf](https://github.com/Duioolllll/PyNext/commit/aa256bf2b03a175d96f9f084a3b8cfaeca883ceb)를 기준으로 작성했습니다. 업데이트할 때는 [CHANGELOG](../../../CHANGELOG.md)와 [릴리스 계획](../../RELEASE_PLAN.md)을 함께 확인하세요.

## 현재 구현됨

- Rust trie 라우팅, Axum HTTP 엔진, Python/ASGI 통합
- 버퍼형 SSR, 빌드 시 SSG, 프로세스별 ISR
- ETag 및 단일 byte-range 공개 파일 응답
- 비동기 API 스트리밍
- load/visible/idle/제한된 interaction 활성화가 가능한 JS/TS Islands
- 플랫폼별 CI 및 전송 방식이 표시되는 벤치마크

## 계획 및 미구현

- 스트리밍 페이지 SSR. React/RSC 호환은 별도의 대규모 설계 과제입니다.
- 분산 ISR 및 워커 간 조정
- 앱별 프로세스 격리와 안정적인 워커 수명주기
- 프로덕션 관측성, 속도 제한, 보안 기본값, standalone 패키징
- Rust/Python 데이터 전달 최적화 및 프로파일링
- **Django 통합**(ORM·인증·Admin 상호운용)은 추후 검토할 수 있는 아이디어이지만, **현재 구현되지 않았고 호환성을 약속하는 기능도 아닙니다.**

자세한 목표와 완료 기준은 [프로덕션 로드맵](../../PRODUCTION_ROADMAP.md)에 있습니다. 일정이나 기능 제공을 보장하는 문서는 아닙니다.

## 기여 가이드

1. 최신 `main`을 기준으로 포크 후 작업 브랜치를 만듭니다.
2. CPython 3.13·uv·Rust/Cargo 및 필요 시 npm을 준비합니다.
3. `cargo fmt --all -- --check`, `cargo test --locked --workspace --all-features`, `uv run pytest -q`를 실행합니다.
4. 브라우저 기능 변경 시 예제 빌드와 Chromium 스모크 테스트를 수행합니다.
5. 공개 API/동작을 바꿨다면 회귀 테스트와 **영문·한국어 위키를 함께** 수정합니다.
6. PR에는 구현한 기능, 직접 검증한 결과, 성능 측정의 한계, 향후 계획을 분리해 적습니다.

인증 정보·비밀 키를 올리지 말고 작은 스모크 테스트로 프레임워크 성능 우위를 주장하지 마세요. 사용한 Next.js 일부 경로 로직의 [MIT 표기](../../../third_party/NEXTJS.md) 및 [PyNext MIT 라이선스](../../../LICENSE)도 준수해야 합니다.

[GitHub Issues](https://github.com/Duioolllll/PyNext/issues) · [Pull Requests](https://github.com/Duioolllll/PyNext/pulls)
