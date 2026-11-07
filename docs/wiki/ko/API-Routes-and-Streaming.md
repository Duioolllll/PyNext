# API 라우트와 응답 스트리밍

[홈](Home.md) · [English](../en/API-Routes-and-Streaming.md)

`app/api/users/route.py`는 `@route("METHOD")`로 꾸민 HTTP 핸들러를 내보냅니다. 파일 기반 라우터를 공유하지만 페이지 레이아웃은 적용되지 않습니다.

## JSON API

~~~python
from pynext import BadRequest, JSONResponse, route

@route("GET")
async def users():
    return JSONResponse({"users": ["Alice", "Bob"]})

@route("POST")
async def create_user(request):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        raise BadRequest("name is required")
    return JSONResponse({"name": body["name"]}, status=201)
~~~

- `request.method`, `request.path`, `request.params`, `request.headers`, `request.body`, `request.query`를 이용할 수 있습니다.
- `request.query`는 값이 목록인 매핑입니다. 예: `request.query.get("q", [""])[0]`. 잘못된 UTF-8 및 쿼리 필드 1,000개 초과 시 접근 시점에 `BadRequest`가 발생합니다.
- `await request.json()`은 잘못된 JSON과 NaN/Infinity 상수를 거부합니다.
- `BadRequest`는 일반화된 본문으로 HTTP **400**, 처리되지 않은 예외는 **500**으로 변환됩니다.
- 핸들러는 딕셔너리가 아니라 `Response`, `JSONResponse`, `StreamingResponse`를 반환해야 합니다.

## 비동기 API 스트리밍

~~~python
from pynext import StreamingResponse, route

@route("GET")
async def stream():
    async def chunks():
        yield '{"phase":"started"}\n'
        yield '{"phase":"finished"}\n'
    return StreamingResponse(chunks(), media_type="application/x-ndjson")
~~~

ASGI와 Axum 모두 비동기 iterable 응답을 스트리밍하며 **청크당 최대 1 MiB**입니다. 네이티브 어댑터는 대기 청크를 제한하고 백프레셔를 처리합니다. 스트리밍 응답에서 `Content-Length`나 `Transfer-Encoding`을 직접 설정하지 마세요. 연결이 끊어지면 iterator 취소/정리를 요청합니다. 헤더 전송 뒤 오류가 발생하면 상태 코드를 변경하는 대신 스트림을 끝냅니다. `HEAD`는 스트림을 소비하지 않습니다.

## HTTP 응답과 안전성

`Response("ok", status=200, media_type="text/plain; charset=utf-8")`는 문자열 또는 바이트 본문을 받습니다. 기본 CSP는 `script-src 'self'; object-src 'none'`이며 응답 헤더와 media type을 검증합니다. 들어오는 HTTP 요청 본문의 최대 크기는 **1 MiB**입니다.

예제: [users API](../../../examples/basic/app/api/users/route.py), [stream API](../../../examples/basic/app/api/stream/route.py). **WebSocket과 스트리밍 페이지 SSR은 아직 지원하지 않습니다.**
