# PyNext 공식 위키

**PyNext 0.1.3 — 실험적인 Python + Rust 풀스택 웹 프레임워크**

[English](../en/Home.md) · [GitHub 저장소](https://github.com/Duioolllll/PyNext) · [릴리스](https://github.com/Duioolllll/PyNext/releases/tag/v0.1.3) · [라이선스](../../../LICENSE)

PyNext는 Next.js의 파일 기반 개발 경험에서 영감을 받아, **Python 페이지·API**와 **Rust 라우터·HTML 렌더러·Axum/Tokio HTTP 엔진**을 결합한 독립적인 실험 프로젝트입니다. **Next.js, React 또는 React Server Components 호환 구현이 아닙니다.**

> **주의 — 실험 버전:** 검증된 Python은 CPython 3.13과 3.14입니다. 인터넷에 직접 노출하는 프로덕션 서버로 사용하거나 신뢰할 수 없는 앱 코드를 실행하지 마세요.

## 문서 찾아보기

| 시작하기 | 앱 개발 | 내부 구조 |
| --- | --- | --- |
| [설치와 첫 실행](Getting-Started.md) | [파일 라우팅](Routing.md) | [Rust/Python 아키텍처](Architecture.md) |
| [컴포넌트와 레이아웃](Components-and-Layouts.md) | [SSR / SSG / ISR](Rendering.md) | [HTTP와 캐시](HTTP-and-Caching.md) |
| [API와 스트리밍](API-Routes-and-Streaming.md) | [Islands와 Hydration](Islands-and-Hydration.md) | [벤치마크](Benchmarks.md) |
| [테스트와 CI](Testing-and-CI.md) | [배포와 보안](Deployment-and-Security.md) | [문제 해결](Troubleshooting.md) |

구현 상태, 남은 과제 및 **향후 검토 중인** Django 통합은 [로드맵과 기여 가이드](Roadmap-and-Contributing.md)를 참고하세요.

## 핵심 기능

- **라우팅:** `app/page.py`, `app/blog/[slug]/page.py`, `app/api/users/route.py`.
- **렌더링:** Python → Rust 버퍼형/스트리밍 SSR, 빌드 시 SSG, 프로세스별 메모리 또는 SQLite/Redis 공유 ISR.
- **UI:** esbuild로 번들링하는 선택적 JS/TS islands(`load`/`visible`/`idle`/제한된 `interaction`).
- **HTTP:** Axum/Hyper HTTP/1.1 및 h2c, ASGI 어댑터, 제한된 비동기 API 스트리밍.
- **검증:** Python·Rust·Chromium 테스트 및 Linux/macOS/Windows CI.

이 위키의 사실관계는 저장소 `main`의 [d8560f8 커밋](https://github.com/Duioolllll/PyNext/commit/d8560f828a13e6ae5bb82bf4496fed6b4d45ecb8)을 기준으로 합니다. 로드맵의 제안 기능을 구현 완료로 표기하지 않습니다.
