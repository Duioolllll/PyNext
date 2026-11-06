# PyNext v0.2.0 — ko

보관된 개발 빌드에서 재구성한 실험판이며 이후의 요청 검증 수정이 백포트되었습니다. Next.js·React와 호환되지 않습니다. 인터넷에 직접 공개하지 마세요.

준비물: CPython 3.13, Rust/Cargo, uv. Island 빌드에는 Node.js 18 이상과 npm이 필요하며 빌드 전 `npm ci`를 실행합니다.

**Features / 기능 / 機能 / 功能:** 파일 라우팅, SSR, SSG, 공개 파일 및 프로세스별 ISR, JavaScript/TypeScript island mount.

아직 지원하지 않음: hydration, 지연 활성화.

~~~bash
uv sync --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

SSG/ISR은 공개 데이터 전용이며 ISR 캐시는 프로세스별입니다. Axum은 30초 뒤 504를 반환하지만 이미 실행 중인 동기 함수는 계속 실행될 수 있습니다. WebSocket, 스트리밍, 분산 ISR은 미구현입니다.

~~~python
from pynext import h, page

@page()
def Page():
    return h("h1", {}, "Hello PyNext")
~~~
