# 설치와 첫 실행

[홈](Home.md) · [English](../en/Getting-Started.md)

## 필수 환경

- **CPython 3.13** (현재 검증된 유일한 Python 버전)
- Rust 툴체인 및 Cargo
- [uv](https://docs.astral.sh/uv/)
- JS/TS island를 사용하는 앱에 한해 **Node.js 18 이상과 npm**

현재 저장소 소스에서 실행하는 방식이 안내되어 있으며, PyPI에 게시된 패키지가 있다고 가정하면 안 됩니다.

## 공식 예제 실행

~~~bash
git clone https://github.com/Duioolllll/PyNext.git
cd PyNext
uv sync --locked --extra dev
npm ci
uv run pynext build --dir examples/basic
uv run pynext start --dir examples/basic
~~~

`http://127.0.0.1:3000/`에서 접속하세요. 예시 경로는 `/about`(SSG), `/blog/hello`(SSR), `/guides/intro`(동적 SSG), `/news/example`(ISR), `/api/users`, `/api/stream`, `/hydrate`입니다. 종료는 Ctrl+C입니다.

빌드 없이 개발 모드로 실행하려면:

~~~bash
uv run pynext dev --dir examples/basic --host 127.0.0.1 --port 3000
~~~

**자동 리로드/HMR은 없습니다.** 소스를 수정한 후 재시작해야 합니다. `start`를 사용하기 전에 앱·공개 파일·island를 변경했다면 `build`를 다시 실행하세요.

## 최소 프로젝트 만들기

`my-site/app/page.py`를 생성하세요.

~~~python
from pynext import h, page

@page()
def Page():
    return h("main", {}, h("h1", {}, "Hello PyNext"))
~~~

저장소 루트에서 의존성 설치 후:

~~~bash
uv run pynext build --dir my-site
uv run pynext start --dir my-site --port 3000
~~~

`islands/`가 없다면 Node/esbuild도 필요하지 않습니다. 빌드는 버전이 있는 `.pynext/manifest.json`, SSG HTML 및 에셋 스냅샷을 생성합니다.

## CLI 명령

| 명령 | 역할 |
| --- | --- |
| `pynext build --dir PATH` | 라우트 검증, islands 번들, SSG 및 매니페스트 |
| `pynext dev --dir PATH` | 소스에서 실행(수정 시 수동 재시작) |
| `pynext start --dir PATH` | 검증된 빌드로 시작 |

두 서버 명령은 `--host`, `--port`, `--max-concurrent`(기본 64), `--acquire-timeout-ms`(1000), `--max-waiting`(`--max-concurrent`와 64 중 작은 값), `--body-capacity`(16), `--body-timeout-ms`(10000)을 지원합니다. 기본 바인딩 주소는 loopback입니다. 공개 프로덕션 노출은 아직 지원 대상으로 보장되지 않습니다.

다음 문서: [라우팅](Routing.md), [컴포넌트](Components-and-Layouts.md).
