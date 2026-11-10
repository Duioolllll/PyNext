# 탈출구 명시화 — SQL 파라미터 분리 + HTML 이스케이프 (S-P1-10)

Django 교훈 3의 PyNext 적용: 기본 경로는 구조적으로 안전하게, 위험한
경로는 이름에서 위험이 드러나게. 이 문서는 모든 탈출구 사용처의
목록이자, 코드 리뷰 체크리스트의 "탈출구 사용처" 항목이다.

## 규칙

1. **SQL은 파라미터 분리 전달이 기본이다.** 값을 SQL 문자열에 끼워
   넣지 않는다 — f-string, `%` 포매팅, `+` 연결 모두 금지. 값은
   `?` (DB-API) / `:name` (SQLAlchemy) 플레이스홀더로 전달한다.
2. **Raw SQL이 필요하면 `raw_sql()`이라는 명시적 이름을 쓴다.**
   DB-API 경로: `pynext.sql.raw_sql()` (→ `RawSQL`, `.execute(conn)`).
   SQLAlchemy 경로: `pynext.database.raw_sql()` (→ `text()` + named
   `bindparams`). 다른 이름으로 raw SQL을 실행하지 않는다.
3. **HTML 이스케이프는 기본 ON이다.** `h()` 트리의 일반 문자열 자식은
   전부 `"text"` 노드가 되어 렌더러가 이스케이프한다. opt-out은
   `mark_safe()` 하나뿐이며, 그 결과는 `{"kind": "raw"}` 노드가 된다.
4. **속성값은 항상 이스케이프된다.** `mark_safe()`로 감싸도 속성값은
   raw가 되지 않는다 — 속성 인젝션에는 안전한 raw 형태가 없다.

## SQL 사용처 목록 (감사 결과, 기준 46e1123 + 본 항목)

| 위치 | SQL 생성 방식 | 판정 |
|---|---|---|
| `python/pynext/cache.py` — ISR/공유 캐시 (`SQLiteCacheBackend`) | 모든 값 조회·삽입·삭제가 `?` 플레이스홀더 + 별도 튜플 전달. `IN (...)`도 `?`를 개수만큼 생성해 값을 분리 전달 (`_tag_status`) | 안전 (기본 경로) |
| `python/pynext/cache.py::_ensure_column` | DDL이라 식별자를 바인딩할 수 없어, 유일한 보간 지점. `raw_sql()` + `quote_identifier()` (식별자 `[A-Za-z_][A-Za-z0-9_]*`만 허용) + `validate_ddl_definition()` (타입 + `NOT NULL` + 리터럴 `DEFAULT`만 허용)으로 제한. 호출 인자는 프레임워크 상수 3건뿐 | 탈출구 — 명시화됨 |
| `python/pynext/sessions.py` — `SQLiteSessionStore` | 모든 문이 `?` 플레이스홀더 + 분리 전달 (`SELECT/INSERT/UPDATE/DELETE … WHERE id=?`). 보간 없음 | 안전 (감사만 함, 본 항목에서 미변경) |
| `python/pynext/database.py` — `Database` | 프레임워크 자체 SQL은 상수 `text("SELECT 1")` (연결 확인용)뿐. 앱 SQL은 세션을 통해 SQLAlchemy로 실행하며, raw가 필요하면 `database.raw_sql(sql, {"name": value})`를 쓴다 | 탈출구 — `raw_sql()` 제공됨 |
| `python/pynext/django.py` — `DjangoResource` | Django ORM에 위임, 프레임워크 측 raw SQL 없음 | 안전 |
| `python/pynext/redis_cache.py`, `fetch_cache.py` | SQL 미사용 (Redis/메모리) | 해당 없음 |

새 SQL 생성 지점을 추가할 때는 이 표에 행을 추가하고, 값이 있으면
반드시 플레이스홀더로 전달할 것. `raw_sql()`을 쓰면 리뷰에서 이
문서의 체크리스트 항목이 된다.

## HTML 이스케이프 사용처 목록

| 위치 | 동작 | 판정 |
|---|---|---|
| Rust 네이티브 렌더러 (`crates/pynext-native/src/lib.rs::render_node`) | `"text"` 노드와 모든 속성값·아일랜드 props를 `&<>"'` 이스케이프. `script/style/iframe/object/embed` 태그, `on*`·`style`·`srcdoc` 속성, `javascript:`/`vbscript:`/`data:` URL 거부 | 기본 ON (잠김) |
| `h()` / `fragment()` (`python/pynext/components.py`) | 일반 문자열 자식 → `{"kind": "text"}` (이스케이프됨). `mark_safe()`로 감싼 문자열만 → `{"kind": "raw"}` | 기본 ON, opt-out 1개 |
| ISR 캐시 본문 생성 경로 (`App._render_page_inner` → `_native.render` → `SQLiteCache`) | 캐시에 저장되는 것은 이미 이스케이프된 렌더링 결과 바이트다. 캐시 적중 시 본문을 그대로 서빙하며, 이스케이프를 풀거나 다시 적용하지 않는다 — 이중 이스케이프도, 이스케이프 해제도 없다 | 기본 ON (렌더러와 동일) |
| 공유 모델 (`_resolve_shared_model`) | `text` 노드만 값으로 받고, `raw` 노드는 거부한다 (`ValueError`). 모델 경유 raw HTML 불가 | 차단됨 |
| 개발 에러 페이지 (`App` dev traceback) | `html.escape()`로 이스케이프 | 기본 ON |

### `mark_safe()` 후속 작업 (네이티브 트랙)

`"raw"` 노드는 Python 트리 단계에서만 존재한다. 네이티브 렌더러는
아직 `kind == "raw"`를 모르며, 알 수 없는 노드 종류는 렌더링
전체 오류로 **fail-closed** 처리한다 (절대 unescaped로 흘리지
않는다). `mark_safe()`가 실제로 raw HTML을 내보내려면 네이티브
렌더러에 `"raw"` 분기(값 그대로 출력)를 추가해야 하며, 그 전까지
`mark_safe()` 사용처는 렌더링 오류가 된다. 이 후속 작업을 할 때는
반드시 `raw` 노드를 받는 지점이 `mark_safe()` 경유뿐인지, 그리고
이 문서의 체크리스트를 다시 확인할 것.

## 리뷰 체크리스트 — "탈출구 사용처"

PR 리뷰 시 아래를 `grep`으로 확인한다:

```sh
# SQL 탈출구 — 이 목록 외의 raw SQL 실행이 있으면 안 된다
grep -rn "raw_sql\|RawSQL" python/ tests/
grep -rn "execute(f\"\|execute(f'\|executescript(f\"" python/pynext/  # 0건이어야 한다
# HTML 탈출구 — mark_safe 사용처를 전부 읽는다
grep -rn "mark_safe\|SafeString\|\"raw\"\|'raw'" python/ tests/
```

- [ ] `raw_sql()` 호출마다 값이 `params`로 분리 전달되는가?
      (f-string/`%`/연결로 값을 끼워 넣은 호출은 거부)
- [ ] `quote_identifier()` / `validate_ddl_definition()` 우회 없이,
      식별자·DDL 조각이 검증 함수를 거치는가?
- [ ] `mark_safe()` 호출마다 인자가 신뢰할 수 있는 HTML인가?
      (사용자 입력, 쿼리 문자열, DB 내용, 파일 내용을 그대로
      `mark_safe()`에 넣으면 안 된다. 신뢰할 수 없는 값은 먼저
      이스케이프하거나, 허용 태그만 남기는 sanitize를 거칠 것)
- [ ] `mark_safe()`를 속성값에 쓰지 않았는가? (속성은 항상 이스케이프)
- [ ] 새 SQL 생성 지점이 위 SQL 표에 추가됐는가?
- [ ] 새 HTML 생성 지점이 위 HTML 표에 추가됐는가?
- [ ] 테스트: 이스케이프 기본값 테스트 + 파라미터 분리 테스트가
      `tests/test_security_escape_hatches.py`에 있는가?

## 참고

- 근거: `~/workspace/concurrency-analysis/ROADMAP.md` §1.2 S-P1-10,
  `~/workspace/concurrency-analysis/django-security.md` 교훈 3.
- 관련 테스트: `tests/test_security_escape_hatches.py`,
  `tests/test_app.py::test_…` (렌더러 이스케이프), `tests/test_cache.py`.
