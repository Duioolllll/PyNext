# 문제 해결

[홈](Home.md) · [English](../en/Troubleshooting.md)

| 증상 | 확인할 내용 |
| --- | --- |
| `pynext start`에서 소스 변경 오류 | `uv run pynext build --dir YOUR_APP` 재실행; public/islands 확인 |
| `.pynext/manifest.json`이 없음 | `start` 전에 `pynext build` 실행 |
| 코드 수정 후 적용 안 됨 | `dev`에도 **HMR/자동 리로드가 없음**; 재시작 필요 |
| `esbuild`를 찾지 못함 | 루트에서 `npm ci` 또는 `PYNEXT_ESBUILD` 실행 파일 지정 |
| Windows에서 esbuild 경로 오류 | `.cmd`/`.bat`가 아닌 네이티브 `esbuild.exe` 지정 |
| 동적 SSG URL이 404 | `generate_static_params()` 목록에 추가 후 빌드 |
| Island 미발견 | `island("name")`과 `islands/name.js` / `.ts` 이름 일치 확인 |
| Hydration mismatch | 서버 fallback과 JS `render` 결과 동일성 검사; 포커스 유실 가능 |
| API 400 | 쿼리 UTF-8/1,000필드 제한/JSON 파싱 검토 |
| API 413 | 요청 본문 1 MiB 초과 |
| 부하 상황 503 | 실행·본문 슬롯 용량 및 대기열 원인 진단 |
| 요청 504 | Python 핸들러 시간 초과; 동기 스레드가 계속 실행될 수 있음 |
| 프로세스마다 ISR 결과 차이 | 캐시/무효화는 **프로세스별**임 |

## 점검 순서

1. CPython **3.13**, Rust/Cargo, uv, islands 사용 시 Node/npm 버전을 확인합니다.
2. `app/page.py`가 `@page()`를 사용한 `Page`를 내보내고, API는 `Response` 파생 객체를 반환하며 경로 충돌이 없는지 검사합니다.
3. `uv run pytest -q`와 `cargo test --locked --workspace --all-features`를 실행합니다.
4. 신뢰하는 소스에서 재빌드하고 서버를 재시작한 뒤 경로를 하나씩 확인합니다.
5. GitHub 이슈에는 최소 재현 코드와 개인정보·비밀이 제거된 오류 로그를 첨부합니다.

관련 문서: [홈](Home.md), [배포와 보안](Deployment-and-Security.md).
