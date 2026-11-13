# PyNext 0.1.3

PR #26 (Next.js 분석 결과: Phase 2 observability & cache + Phase 3)을
머지한 릴리스다. ISR·캐시·미들웨어·빌드·메트릭을 Next.js 동작에
가깝게 다듬고, 동시성·세션·속도 제한의 기본값을 강화했다.

- Phase 2 관측성: 요청 실패를 스팬에 기록하고(`error.type`, 예외 이벤트,
  ERROR 상태; 취소 제외) 라우트 패턴·예외 클래스별로 집계한다
  (`pynext_errors_total`, `snapshot()["errors"]`). 실패 정책 발동은
  별도로 집계한다(`pynext_degraded_total`, `snapshot()["degraded"]`).
  `--metrics-endpoint`(`PYNEXT_METRICS_ENDPOINT=1`, 기본 off)로 예약 경로
  `GET /metrics`에서 Prometheus 텍스트를 제공하며, 헬스 프로브처럼
  미들웨어와 속도 제한을 우회한다. 반복되는 요청 실패는 호출 지점당
  윈도우마다 한 번만 전문으로 기록하고(`PYNEXT_LOG_DEDUP_WINDOW_S`,
  기본 60초) 억제 횟수를 남기며, 클라이언트 연결 끊김·취소는 INFO로
  기록한다. 예외 메시지는 로그에 남기지 않는다. Prometheus 라벨 값은
  이스케이프한다. 느린 `pynext.request` / `pynext.render` 스팬은
  `PYNEXT_TRACE_SPAN_THRESHOLD_MS`를 설정하면 로컬 `.pynext/trace.jsonl`에
  기록한다(기본 off, 50 MiB 로테이션, 라우트 패턴만).
- Phase 2 캐시: ISR 응답에 정책 기반 `Cache-Control` 헤더를 싣는다
  (`public, s-maxage={revalidate}` + SWR 윈도우가 있으면
  `stale-while-revalidate={stale}`), 재생성 실패 시 이전 항목을 유지한다
  (revalidate 3..30초로 고정, stale 윈도우는 최소 3초 유지; 첫 생성
  실패는 500). ISR 수명은 revalidate·stale·태그의 세 축으로 나뉜다.
  선택형 `filesystem` ISR 백엔드를 추가했다(`--cache-backend filesystem`,
  Next.js `FileSystemCache` 방식: `.body` + `.meta` 사이드카 파일,
  프로세스 간 리스, 펜스드 쓰기, 가장 오래된 항목부터 축출).
  `CacheSignal` 방식 ISR 워밍업을 추가했다(`ISRCache.warmup()`,
  `App.warm_isr_cache()`, `pynext start --warm-isr`; 실패는 기록만 하고
  올리지 않는다). 세그먼트 설정 상속(Next.js `reduceAppConfig`)과
  온디맨드 재검증, 시그널 오류(`not_found()`/`forbidden()`/
  `unauthorized()`/`redirect()`)를 지원한다. L1 `PageCache` 축출을
  O(1) 이중 연결 리스트 + 해시맵 LRU로 바꿨다.
- Phase 2 미들웨어: 표준 미들웨어 결과(next / rewrite / redirect /
  response)와 조건부 미들웨어 스코프(`pynext.scoped`, `MiddlewareScope`,
  `RouteHas`)를 지원한다. `websocket_allowed_origins`에 와일드카드
  패턴을 허용한다(Next.js Server Actions `allowedOrigins` 의미).
- Phase 2·3 빌드: 빌드를 단계별로 나누고(Next.js `experimentalBuildMode`
  상당) 증분 빌드를 지원한다. 결정적 빌드 ID(소스 해시 접두사)와 선택적
  배포 ID를 매니페스트에 기록하고 `X-PyNext-Build-Id` /
  `X-PyNext-Deployment-Id` 헤더로 서빙해 스큐를 막는다.
  `pynext info` / `pynext doctor`를 추가했고, 빌드마다 진단 정보를
  `.pynext/diagnostics.json`에 쓴다.
- 동시성·서버 기본값: `pynext start`는 기본적으로 supervisor와 함께
  2개 워커 프로세스로 동작한다(기존 단일 프로세스; `--workers 1`로
  구 동작). `max_concurrent` 기본값은 128, `acquire_timeout_ms` 기본값은
  3000이다. 클라이언트 IP당 실행 슬롯은 기본 16개로 묶는다.
- 속도 제한 기본 on: 기본 60초 윈도우에 클라이언트 IP당 300요청으로
  제한한다(`--rate-limit 0`으로 해제). `/healthz`·`/readyz`는 면제다.
  판정을 내릴 수 없을 때(게이트 포화·기한 초과)는 요청을 허용하고
  계수한다(fail-open, `rate_limit.fail_open`).
- 세션·보안 기본값: 세션 수명은 유휴 타임아웃과 절대 수명(기본 7일)으로
  이중 제한한다. 세션·CSRF 토큰 수명은 기본 12시간(최대 30일)이며,
  목적별 일회용 토큰과 재인증 검사를 지원한다. 세션 페이로드는 키를
  설정하면 AES-256-GCM으로 봉인할 수 있다(`pynext-framework[encryption]`).
  Redis `fail_open` 해지는 중단 중 큐에 쌓았다가 복구 시 처리한다.
  CORS에서 `cors_allowed_origins=["*"]`와 자격 증명 허용의 조합은
  `ConfigError`로 거부한다. 명시적 `allowed_hosts`가 없으면 루프백
  호스트만 허용한다. 응답 헤더의 제어 바이트는 전송 지점에서 버린다.
  요청 스머글링 가드의 첫 헤더 블록 대기는 `header_read_timeout`
  (기본 10초)으로 묶고 기한 초과 시 fail-closed한다.
- 라우팅·정합성: 라우트 발견 시 진입점·레이아웃·소스 경로를 모든
  플랫폼에서 `/` 구분자의 프로젝트 상대 경로로 저장한다. 라우트
  특이성 비교자(`pynext.sortable_routes`, Next.js `sortable-routes.ts`
  번역)를 추가했고, `/readyz`는 공유 백엔드 상태를 반영한다(죽은
  `fail_closed` 백엔드는 503, 죽은 `fail_open` 백엔드는
  `ready (degraded: ...)`).

```bash
pip install pynext-framework==0.1.3
```

보안 주의사항: 0.1.3은 2026-11-11 보안 감사 finding 중 **H-A1(레이아웃
`request` 누수)만 포함**한다. **M-A1(WS 로그아웃 후 소켓 계속 서비스),
M-A4(ASGI 동시성 상한 없음), M-A7(standalone sitecustomize 자동 실행),
M-A9(릴리즈 프로모션 바인딩 없음), H-3(Schema 리스트 `pattern` 미컴파일로
모든 요청 500)은 이 릴리스에 포함되지 않았다.** 해당 finding은 별도
브랜치에서 작업 중이며, 다음 릴리스에 포함될 예정이다. 이 릴리스는
0.1.2와 마찬가지로 릴리즈 판정 "보류" 상태의 감사 대상이며, 공개
프로덕션 적합성을 확보하지 않았다.

구체 변경 내역은 [CHANGELOG](../CHANGELOG.md)의 0.1.3 섹션을 따른다.
적용 범위는 계속 **소규모 웹·통제된 파일럿**이다. 공개 프로덕션 적합성은
확보하지 않았으며 [개선 로드맵](PRODUCTION_ROADMAP.md)을 유지한다.
