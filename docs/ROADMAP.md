# 구현 현황과 로드맵

현재 체크아웃은 **v0.7.0a1 미출시 실험 후보**다. 파일 기반 라우터는 정적·동적·필수/선택 catch-all과 route group을 지원하며, Python 페이지·API와 Rust 렌더러, ASGI/Axum 어댑터를 연결한다. SSG, 공개 파일, 프로세스 로컬 ISR, 선택적 SQLite 공유 ISR, JS/TS island, 제한된 버튼 클릭 재생, 비동기 스트리밍 API/SSR, WebSocket, 개발 서버 파일 감시가 구현되어 있다. Axum은 일부 정적/캐시 응답에서 Python을 건너뛴다.

지원 범위와 현재 제한, 요청·캐시·브리지의 계약, 남은 위험과 단계별 검증 기준은 [아키텍처와 개발 명세](ARCHITECTURE_SPEC.md)를 참고한다. 더 장기적인 공유 버퍼, React/RSC, 분산 캐시, 관측성, 복구력 및 standalone 후보의 수용 조건은 [프로덕션 로드맵](PRODUCTION_ROADMAP.md)에 있다.

Next.js 원본을 추가로 옮길 때는 관련 원본 경로와 커밋을 기록하고 라이선스 고지를 유지한다. React Server Components 또는 React 호환성은 별도 구현과 검증 없이는 주장하지 않는다.
