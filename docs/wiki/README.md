# PyNext Wiki / PyNext 위키

**Official bilingual developer guides for the experimental PyNext v0.6.0a2 source tree.**

- **English:** [Home](en/Home.md) · [Sidebar](en/_Sidebar.md)
- **한국어:** [홈](ko/Home.md) · [사이드바](ko/_Sidebar.md)
- [Main repository README](../../README.md) · [English manual](../en/manual.md) · [한국어 설명서](../ko/manual.md)

## Topics / 목차

| English | 한국어 |
| --- | --- |
| [Getting started](en/Getting-Started.md) | [설치와 실행](ko/Getting-Started.md) |
| [Routing](en/Routing.md) | [라우팅](ko/Routing.md) |
| [Components and layouts](en/Components-and-Layouts.md) | [컴포넌트와 레이아웃](ko/Components-and-Layouts.md) |
| [SSR / SSG / ISR](en/Rendering.md) | [SSR / SSG / ISR](ko/Rendering.md) |
| [API and streaming](en/API-Routes-and-Streaming.md) | [API와 스트리밍](ko/API-Routes-and-Streaming.md) |
| [Islands and hydration](en/Islands-and-Hydration.md) | [Islands와 Hydration](ko/Islands-and-Hydration.md) |
| [Architecture](en/Architecture.md) | [아키텍처](ko/Architecture.md) |
| [HTTP and caching](en/HTTP-and-Caching.md) | [HTTP와 캐시](ko/HTTP-and-Caching.md) |
| [Benchmarks](en/Benchmarks.md) | [벤치마크](ko/Benchmarks.md) |
| [Testing and CI](en/Testing-and-CI.md) | [테스트와 CI](ko/Testing-and-CI.md) |
| [Deployment and security](en/Deployment-and-Security.md) | [배포와 보안](ko/Deployment-and-Security.md) |
| [Troubleshooting](en/Troubleshooting.md) | [문제 해결](ko/Troubleshooting.md) |
| [Roadmap and contributing](en/Roadmap-and-Contributing.md) | [로드맵과 기여](ko/Roadmap-and-Contributing.md) |

## Scope and publishing / 범위 및 게시 안내

- Verified against `main` commit [aa256bf](https://github.com/Duioolllll/PyNext/commit/aa256bf2b03a175d96f9f084a3b8cfaeca883ceb) and the published [v0.6.0a2 release](https://github.com/Duioolllll/PyNext/releases/tag/v0.6.0a2). Do not interpret the roadmap as completed functionality.
- English and Korean pages use matching filenames to keep translation review straightforward. Update both when public behavior changes.
- These are **repository Markdown files**, not automatically published into GitHub's separate **Wiki** tab. `Home.md` and `_Sidebar.md` are provided in each language as ready-to-adapt starting points for manual Wiki publishing. Relative links in this repository may require adjustment when moving files into the standalone GitHub Wiki repository.
- Current limitations include no React/RSC compatibility, no streaming page SSR, no HMR, no distributed ISR, and no standalone production packaging. Django integration is a **possible future exploration**, not a shipped integration.

## Maintenance / 문서 유지보수

1. Confirm changes against implementation and regression tests, not only the changelog or speculative plans.
2. Keep `en/` and `ko/` chapters synchronized and examples executable under CPython 3.13.
3. Link to source, relevant tests, and existing canonical documentation for technical claims.
4. Document operational and security limits explicitly; avoid unverified cross-framework performance claims.

Documentation changes are welcome through GitHub pull requests.
