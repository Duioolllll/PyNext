<p align="center">
  <img src="assets/pynext-logo-refracted-light.gif" alt="PyNext" width="640" />
</p>

# PyNext

**Manual:** [English](docs/en/manual.md) · [한국어](docs/ko/manual.md) · [日本語](docs/ja/manual.md) · [简体中文](docs/zh-CN/manual.md)

**Wiki:** [English](docs/wiki/en/Home.md) · [한국어](docs/wiki/ko/Home.md) · [Wiki index](docs/wiki/README.md)

**버전:** [패키지 0.1.3](https://pypi.org/project/pynext-framework/0.1.3/) / [릴리스 v0.1.3](https://github.com/Duioolllll/PyNext/releases/tag/v0.1.3) · [변경 이력](CHANGELOG.md) · [기능 목록](docs/FEATURES.md) · [아키텍처 명세](docs/ARCHITECTURE_SPEC.md)

PyNext는 Next.js의 파일 기반 라우팅 경험을 Python과 Rust로 구현한 풀스택 프레임워크입니다. Python으로 페이지와 API를 쓰고, Rust 라우터와 렌더러가 요청을 처리합니다. 하나의 프로젝트로 SSR 페이지, API, 정적·주기 재생성 페이지를 제공하고, 독립 실행 서버(Axum)나 ASGI 서버(Uvicorn 등) 위에서 서비스로 실행합니다.

## 빠른 시작

CPython 3.13 또는 3.14에서 설치합니다.

```bash
pip install pynext-framework==0.1.3
```

```bash
pynext init --dir my-app
cd my-app
pynext dev                 # 개발 서버: 소스 변경 시 자동 재시작
pynext build               # 운영 빌드
pynext start               # 운영 서버: 수퍼바이저 + 2개 워커 (기본값)
```

브라우저에서 `http://127.0.0.1:3000/`을 확인하세요.

## 앱 작성

`app/` 아래 파일이 곧 라우트입니다. `page.py`는 페이지, `api/*/route.py`는 API, `[slug]`는 동적 경로입니다.

```text
app/
├── layout.py
├── page.py
├── blog/[slug]/page.py
└── api/users/route.py
```

```python
# app/blog/[slug]/page.py
from pynext import h, page

@page()
async def Page(params):
    return h("main", {}, h("h1", {}, params["slug"]))
```

```python
# app/api/users/route.py
from pynext import JSONResponse, route

@route("GET")
async def users():
    return JSONResponse({"users": ["Alice", "Bob"]})
```

상세 문법과 옵션은 [매뉴얼](docs/ko/manual.md)을 참고하세요.

## 기능

| 영역 | 내용 |
|---|---|
| 라우팅 | 파일 기반 라우팅, 중첩 레이아웃, 동적·catch-all 경로, 라우트 그룹 |
| 렌더링 | SSR, SSG(`render="ssg"`), ISR(`render="isr"`, `revalidate`/`stale`) |
| API | JSON·폼·파일 업로드·스트리밍 응답, `request` 검증(1 MiB 본문 상한) |
| 미들웨어 | 전역 HTTP 미들웨어(`middleware.py`), 스코프·조건부 미들웨어 |
| 인증·세션 | 서버 세션(SQLite/Redis), `@require_auth`·`@require_permission`, CSRF 보호 |
| 실시간 | WebSocket(`@websocket`, 서브프로토콜·출처 정책), 프로세스 내 pub/sub |
| 클라이언트 | JS/TS island(esbuild), hydration, 활성화 전략(load/visible/idle/interaction) |
| 캐시 | 메모리·SQLite·Redis 공유 ISR 캐시, 경로 무효화(`revalidate`) |
| 관측성 | 구조화 JSON 로그(기본 on), 메트릭(`/metrics`), OpenTelemetry 트레이싱(선택) |
| 운영 | 다중 워커 수퍼바이저, `/healthz`·`/readyz`, 속도 제한(기본 on), graceful shutdown |

## 서비스로 실행하기

### 운영 서버

`pynext start`는 기본적으로 수퍼바이저가 2개 워커 프로세스를 하나의 리스너에서 실행합니다. 워커 한 대가 죽어도 서버가 내려가지 않습니다. 단일 프로세스로 실행하려면 `--workers 1`을 명시하세요.

```bash
pynext start --dir my-app --port 3000
```

- `/healthz`: 프로세스 생존 확인
- `/readyz`: 시작 완료 및 공유 백엔드 건강 확인
- `/metrics`: `--metrics-endpoint`를 켜면 Prometheus 형식 메트릭 제공
- SIGTERM을 받으면 진행 중인 요청을 끝내고 WebSocket을 종료한 뒤 멈춥니다(`--shutdown-timeout`, 기본 30초)

### ASGI 서버 위에서 실행

```bash
uvicorn --app-dir my-app asgi:app
```

운영 모드에서는 빌드 후 `create_app(project_path, production=True)`로 앱을 생성해야 SSG 산출물을 사용합니다.

### Standalone 배포 (Linux)

```bash
pynext build --dir my-app --standalone
./my-app/.pynext/standalone/run --port 3000
```

실행 파일, Python 표준 라이브러리, 가상환경 패키지와 빌드된 앱을 하나의 디렉터리에 묶습니다. 환경 변수는 포함하지 않으므로 배포 환경에서 제공합니다.

### Docker 배포

```bash
docker build --tag pynext-example:local .
docker compose -f deploy/compose.yaml up --build -d
```

Rust·Node·uv는 빌드 단계에만 있고, 실행 이미지에는 Python·프레임워크·앱과 운영 산출물만 들어갑니다. `APP_DIR` 빌드 인자로 저장소 내 앱을 선택합니다. 자세한 배포·공급망 검사는 [컨테이너 안내](docs/CONTAINERS.md)를 참고하세요.

### 캐시·세션 공유

여러 워커가 ISR 결과와 세션을 공유하려면 SQLite 또는 Redis 백엔드를 사용합니다.

```bash
pynext start --cache-backend sqlite --cache-path .pynext/cache.sqlite3
PYNEXT_REDIS_URL=redis://127.0.0.1:6379/0 pynext start --cache-backend redis
```

Redis를 쓰려면 `pynext-framework[redis]` 설치와 `PYNEXT_REDIS_URL` 환경 변수가 필요합니다. 속도 제한도 `--rate-limit-backend redis`로 워커 간 공유할 수 있습니다.

## 현재 상태

0.1.3은 소규모 서비스와 통제된 파일럿을 대상으로 합니다. 공개 프로덕션 적합성 기준과 개선 계획은 [프로덕션 로드맵](docs/PRODUCTION_ROADMAP.md)에서 계속 다듬고 있습니다. 보안 주의사항은 릴리즈 노트([v0.1.3](docs/RELEASE_NOTES_0.1.3.md))를 참고하세요.

## 기여

기여 방법은 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요. 버그 제보와 기능 제안은 Issues에 남겨주세요.

## 라이선스

PyNext 자체 코드는 [MIT License](LICENSE)로 배포됩니다. `third_party/` 아래의 참고 자료는 각 파일에 표시된 별도 라이선스를 따릅니다.
