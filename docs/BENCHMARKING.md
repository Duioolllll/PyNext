# PyNext 벤치마크 가이드

PyNext의 `benchmarks/bench.py`는 주로 성능 회귀와 오류율을 확인하는 작은 부하 측정 도구입니다. 공식적인 Next.js/FastAPI 성능 순위나 운영 환경 처리량 보증이 아닙니다.

## 세 가지 실행 모드

먼저 `uv sync --locked --extra dev`, `npm ci`, `uv run pynext build --dir examples/basic`를 실행합니다.

```bash
uv run python benchmarks/bench.py --mode axum --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode uvicorn --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode asgi --production --path /api/users --requests 1000 --concurrency 10
```

- **axum**: Rust/Axum 서버에 실제 TCP loopback HTTP 요청을 보냅니다. 출력 `transport=loopback_http`.
- **uvicorn**: 같은 PyNext ASGI 앱을 Uvicorn으로 서비스하고 실제 TCP loopback HTTP 요청을 보냅니다. 출력 `transport=loopback_http`.
- **asgi**: `httpx.ASGITransport`를 통해 네트워크 없이 앱을 직접 호출합니다. 출력 `transport=in_process_asgi`. Axum/Uvicorn의 HTTP 수치와 직접 비교하지 마세요.

세 모드는 모두 클라이언트가 서버와 **같은 프로세스**에 있습니다. HTTP 경로가 같아진다고 해서 CPU 사용량, 이벤트 루프 경쟁, 코어 배치 등 모든 조건이 같아지는 것은 아닙니다. 운영 성능 비교에는 별도 서버·부하 생성 프로세스(가능하면 별도 머신)가 필요합니다.

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
