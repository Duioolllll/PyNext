# 벤치마크와 성능 주장

[홈](Home.md) · [English](../en/Benchmarks.md)

저장소의 벤치마크는 **회귀·스모크 측정 도구**이지 Next.js, FastAPI, Django 등과의 보편적 성능 우위를 입증하는 도구가 아닙니다. 상세 기준은 [공식 벤치마크 가이드](../../BENCHMARKING.md)를 참고하세요.

## 준비

~~~bash
uv sync --locked --extra dev
npm ci
uv run pynext build --dir examples/basic
~~~

## 기본 측정

~~~bash
uv run python benchmarks/bench.py --mode axum --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode uvicorn --production --path /api/users --requests 1000 --concurrency 10
uv run python benchmarks/bench.py --mode asgi --production --path /api/users --requests 1000 --concurrency 10
~~~

- `axum`/`uvicorn`은 **실제 TCP loopback HTTP**를 사용합니다. 클라이언트와 서버는 같은 프로세스에 있습니다.
- `asgi`는 네트워크를 사용하지 않는 `httpx.ASGITransport`입니다. HTTP 모드와 처리량을 **직접 비교하면 안 됩니다.**
- 결과는 `transport`, 전체 시도/성공 RPS, 상태·전송 오류, 전체/성공 요청의 p50/p95/p99, 응답 크기, CPU 측정 범위와 생애 최대 RSS를 기록합니다.

## 별도 서버 프로세스 측정

한 터미널에서 `uv run pynext start --dir examples/basic --port 3000` 실행 후 다른 터미널에서:

~~~bash
uv run python benchmarks/bench.py --base-url http://127.0.0.1:3000 --path /api/users --requests 5000 --concurrency 32 --warmup 100
~~~

Linux라면 `--server-pid PID`를 더해 **해당 PID 하나**의 CPU를 측정할 수 있지만 자식 워커는 제외합니다. 출력의 최대 RSS는 외부 서버가 아니라 **부하 생성기**의 메모리 수치입니다.

## 공정한 비교 원칙

CPU 모델·OS·런타임 버전·워커 수·TLS/HTTP 모드·응답 크기·캐시 상태·동시성·요청 수·워밍업·반복 실행 조건을 기록하세요. 동일한 의미의 라우트와 본문을 비교하고 성공 RPS, 오류율, p95/p99도 같이 제시해야 합니다. 오류가 더 많은 상태에서 시도 RPS만 높아도 개선으로 볼 수 없습니다.

CI의 Axum/Uvicorn별 **40개 요청·동시성 4**는 정상 응답을 확인하는 스모크 테스트이며 성능 회귀 임계값이 아닙니다. 별도로 소개된 타 프레임워크와의 측정값은 이 저장소만으로 독립 재현되거나 보증되지 않습니다.
