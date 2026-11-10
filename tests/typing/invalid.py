# Each labelled line must be rejected by both installed-package checkers.
from pynext import JSONResponse, MiddlewareResult, Request, Response, SecurityConfig, create_app, h, route

bad_response = Response(status="200")  # type-error
bad_config = SecurityConfig(cors_allow_credentials="yes")  # type-error
bad_app = create_app(sync_workers="32")  # type-error
bad_tree = h(42)  # type-error
bad_payload = JSONResponse({"not_json": object()})  # type-error
bad_request = Request("GET", "/", body="text")  # type-error
bad_method = route("TRACE")  # type-error
bad_middleware_request = MiddlewareResult.next(Response("wrong"))  # type-error
bad_middleware_response = MiddlewareResult.response(Request("GET", "/"))  # type-error

@route("GET")
def decorated(request: Request) -> Response:
    return Response(request.path)

bad_argument = decorated("text")  # type-error
