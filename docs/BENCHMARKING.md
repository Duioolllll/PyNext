# PyNext 벤치마크 가이드

PyNext의 `benchmarks/bench.py`는 주로 성능 회귀와 오류율을 확인하는 작은 부하 측정 도구입니다. 공식적인 Next.js/FastAPI 성능 순위나 운영 환경 처리량 보증이 아닙니다.

## 네 가지 실행 모드

먼저 `uv sync --locked --extra dev`, `npm ci`, `uv run pynext build --dir examples/basic`를 실행합니다.

```bash
uv run python benchmarks/bench.py --mode axum --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode uvicorn --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode asgi --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode starlette --path /api/users --requests 1000 --concurrency 10
```

- **axum**: Rust/Axum 서버에 실제 TCP loopback HTTP 요청을 보냅니다. 출력 `transport=loopback_http`.
- **uvicorn**: 같은 PyNext ASGI 앱을 Uvicorn으로 서비스하고 실제 TCP loopback HTTP 요청을 보냅니다. 출력 `transport=loopback_http`.
- **asgi**: `httpx.ASGITransport`를 통해 네트워크 없이 앱을 직접 호출합니다. 출력 `transport=in_process_asgi`. Axum/Uvicorn의 HTTP 수치와 직접 비교하지 마세요.
- **starlette**: 동일 기능을 구현한 Starlette 참조 앱(`benchmarks/starlette_app.py`, `/`·`/about`·`/blog/{slug}`·`/api/users`)을 Uvicorn으로 서비스하고 실제 TCP loopback HTTP 요청을 보냅니다. 출력 `transport=loopback_http`. PyNext 앱을 빌드하지 않으므로 `--production`은 이 그룹에 적용되지 않습니다.

개별 `--mode` 실행에서는 클라이언트가 서버와 같은 프로세스에 있습니다. `--compare`는 각 서버를 별도 프로세스로 실행합니다. 운영 성능 비교에는 별도 머신을 포함한 더 엄격한 측정이 필요합니다.

## `--compare`: 동일 조건 비교

`--compare`는 loopback HTTP 그룹 세 가지(axum, uvicorn, starlette)를 동일한
경로·요청 수·동시성·워밍업으로 차례로 실행해 하나의 결합 결과를 출력합니다.
인프로세스 `asgi` 모드는 네트워크를 거치지 않으므로 비교에서 제외합니다.

```bash
uv run python benchmarks/bench.py --compare --production --path /api/users --requests 1000 --concurrency 10 --warmup 10
```

각 그룹의 `process_peak_rss_bytes`는 별도 서버 프로세스의 RSS를 약 10ms 간격으로
샘플링한 최댓값입니다. 짧은 피크는 놓칠 수 있으며 자식 프로세스는 포함하지
않습니다. `process_cpu_seconds`는 부하 생성기 프로세스의 측정 구간 CPU 시간이고,
Linux에서는 서버 PID의 CPU 시간도 별도로 기록합니다.

### 기록된 비교 (2026-11-08, 1회 측정)

조건: `--compare --production --path /api/users --requests 1000
--concurrency 10 --warmup 10`, Python 3.13.18, Uvicorn 0.54.0, Starlette
1.10.0, httpx 0.28.1, Linux x86_64, loopback HTTP, 부하 생성기와 같은
프로세스. 전체 결과는 `benchmarks/results-2026-11-08.json`에 있습니다.

| 그룹 | 성공 요청 | 오류 | 성공 처리량(초당) | p50/p95/p99(ms) | CPU(초) | 응답 크기 |
|---|---:|---:|---:|---|---:|---:|
| axum (PyNext 독립 실행) | 1000 | 0 | 281.21 | 17.2 / 100.9 / 183.0 | 2.491 | 25 B |
| uvicorn (PyNext ASGI) | 1000 | 0 | 215.95 | 43.6 / 62.8 / 87.6 | 2.037 | 25 B |
| starlette (참조 앱) | 1000 | 0 | 227.06 | 42.7 / 51.4 / 58.7 | 1.840 | 25 B |

1회 측정이라 배율이나 순위를 단정하지 마세요. 특히 axum의 p95/p99 꼬리는
같은 조건에서 다시 측정하면 달라질 수 있습니다. Next.js 비교군은 이
스크립트에 포함되지 않았습니다(이전 Next.js 비교는 별도 측정입니다).

## 외부 HTTP 서버로 측정

PyNext 서버를 별도 터미널에서 시작한 뒤:

```bash
uv run pynext start --dir examples/basic --host 127.0.0.1 --port 3000
```

다른 터미널에서 동일한 요청 경로와 부하 설정으로 실행합니다.

```bash
uv run python benchmarks/bench.py --base-url http://127.0.0.1:3000 --path /api/users --requests 5000 --concurrency 32 --warmup 100
```

이때 출력은 `mode=external`, `transport=external_http`, `process_cpu_scope=load_generator_only`입니다. Linux에서 서버 PID를 알고 있으면 `--server-pid PID`를 추가하여 **해당 PID 하나**의 CPU 시간을 별도로 수집할 수 있습니다. 자식 워커·별도 프로세스나 GPU 작업은 포함하지 않습니다. `process_peak_rss_bytes`는 부하 생성기 프로세스의 생애 최대 RSS이며 서버 RSS가 아닙니다.

Uvicorn과 비교하려면 `uvicorn --app-dir examples/basic asgi:app`으로 별도 서버를 실행하고 동일한 `--base-url` 측정 방법을 사용하세요. ASGI 예제는 개발 모드이므로 프로덕션 SSG를 비교할 때는 `create_app(project_path, production=True)`를 사용한 별도 ASGI 진입점을 준비해야 합니다. 모든 구현에서 동일한 앱 경로·응답 본문·서버 설정을 유지하세요.

## 결과 해석 및 비교 기준

`attempted_requests_per_second`는 성공과 오류를 포함한 전체 시도 처리량입니다. 성능 비교에는 `successful_requests_per_second`, `error_rate`, `http_status_counts`, `latency_ms` 및 `successful_latency_ms`의 p50/p95/p99, `response_bytes`를 함께 사용하세요. 오류가 늘어난 상태에서 시도 처리량만 높은 결과는 개선으로 볼 수 없습니다.

최소한 CPU 모델과 코어 수, 운영체제, Python/Rust/Uvicorn/HTTP 클라이언트 버전, 서버 워커 수, 요청 경로와 응답 크기, 캐시 상태, TLS/프로토콜, 워밍업 횟수, 동시성, 전체 요청 수를 기록하세요. 서로 다른 경로(예: 정적 캐시 히트와 동적 API)를 비교하려면 작업 부하를 분리하고 결과를 구분해야 합니다.

CI의 40요청·동시성 4 테스트는 Axum/Uvicorn이 올바르게 응답하는지 확인하는 **스모크 테스트**일 뿐 성능 회귀 임계값으로 사용하지 않습니다. 프레임워크 간 처리량 우위는 별도 검증 결과로만 주장하세요.
