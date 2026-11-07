# 구현 로드맵

## 현재 구현: M0 수직 단면

- Next.js `sorted-routes.ts` 기반 경로 트리의 정렬·일부 충돌 규칙을 Rust로 옮김.
- Python 페이지 및 API → PyO3 → Rust 렌더러 연결.
- ASGI 어댑터와 Axum HTTP 서버 → Python asyncio 브리지.
- 경로 manifest, 소스 변경 검사, 기본 예제 및 양쪽 전송 경로의 테스트.
- 공개 페이지 SSG, 동적 경로 생성과 무결성 확인, Axum의 Python 호출 없는 SSG 응답.
- `public` 파일의 빌드 스냅샷, 경로·충돌 검사 및 Axum 정적 응답.
- 프로세스별 Rust ISR 캐시, 단일 재생성 및 stale 응답, 인증 요청의 캐시 우회.
- 명시적 JS/TS island 번들·클라이언트 mount, 서버 DOM hydration, load·visible·idle 활성화와 정리 함수.

라우터는 Next.js 전체 문법을 옮긴 것이 아닙니다. 특히 선택적 catch-all은 의도적으로 거부합니다. Axum은 생성된 SSG 페이지, 공개 파일 및 신선한 익명 ISR 결과의 GET/HEAD에서 Python을 건너뜁니다. ISR 캐시는 워커별로 독립적입니다.

## 다음 작업

새로운 프로덕션 요구사항의 설계 제약, 검증 순서와 완료 기준은 [프로덕션 로드맵](PRODUCTION_ROADMAP.md)에 정리한다. 아래 항목과 해당 로드맵의 기능은 현재 릴리스에서 미구현이다.

1. 비동기 API 스트리밍, 연결 해제 전파와 제한된 실행 대기열을 구현했다. 스트리밍 SSR, 워커 종료와 더 넓은 장애 상황의 계약 및 테스트는 남아 있다.
2. 공개 파일·SSG·island 번들의 ETag 조건부 요청과 단일 바이트 범위를 구현했다. 다중 범위 및 추가 정적 산출물 배포 정책은 남아 있다.
3. ISR 공유 저장소, 워커 간 재생성 조정과 명시적 무효화를 구현한다.
4. 상호작용 시 island 활성화, hydration 전 이벤트 재생과 클라이언트 탐색을 별도 계약으로 구현한다.
5. 프로세스 워커, 제한·로그·보안 기본값, 벤치마크 및 운영 문서를 보강한다.

Next.js 원본을 추가로 옮길 때는 관련 원본 경로와 커밋을 기록하고 라이선스 고지를 유지한다. React Server Components 또는 React 호환성은 별도 설계 없이는 주장하지 않는다.
