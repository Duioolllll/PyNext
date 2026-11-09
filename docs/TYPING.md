# 공개 API 타입

개발 버전은 `pynext.__all__`로 재수출하는 API의 `.pyi`와 PEP 561 `py.typed`를
wheel/sdist에 포함한다. Python 3.13·3.14가 대상이며 mypy·pyright를 검사한다.
실제 런타임 구현과 함께 배포하는 시그니처 파일이므로 별도 stub 패키지는 필요 없다.

```python
from pynext import JSONResponse, Request, route

@route("GET")
async def get(request: Request) -> JSONResponse:
    return JSONResponse({"query": request.query})
```

데코레이터는 함수의 인자·반환형을 보존한다. 요청의 JSON은 재귀적 `JSONValue`,
폼은 `dict[str, list[str | UploadFile]]`, 렌더 트리는 `RenderNode`로 표현한다.
공유 타입은 `pynext.types`에서 가져온다. `Database` 타입을 사용할 때는 선택적
`database`/`postgres` extra를 설치해 SQLAlchemy의 `AsyncSession` 타입을 제공한다.

`request.state`는 앱별 확장 영역이어서 `Any` 값을 허용한다. `resources`는
`startup`/`shutdown` 프로토콜을 보장하며, 개별 리소스는 앱이 알고 있는 타입으로
`typing.cast(Database, request.resources["db"])`처럼 좁힌다. Redis의 주입 가능한
외부 클라이언트와 측정 snapshot 같은 동적 구조도 별도 앱 스키마를 보장하지 않는다.
내부 모듈 전체의 strict 검사가 완료됐다는 의미는 아니다.

```bash
uv sync --locked --extra typing
uv build --wheel
uv run --locked --extra typing python scripts/check_typing.py --wheel dist/*.whl
```

검사는 저장소 밖의 임시 가상환경에 휠을 설치한다. HTTP·페이지·세션·DB·Django·
pub/sub 예제를 두 도구가 통과해야 하고, 잘못된 상태 코드 타입·보안 설정·트리·JSON·
본문·HTTP 메서드·데코레이터 호출은 모두 거부해야 한다. 패키징 smoke도 marker와
핵심 시그니처 파일의 포함을 검사한다.
