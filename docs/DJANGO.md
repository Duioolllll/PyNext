# Django ORM·폼 연동

`DjangoResource`는 Django의 ORM과 폼 검증을 PyNext 핸들러에서 사용하기 위한 선택적 리소스다. Django HTTP 앱, 미들웨어, ASGI 서브앱 마운트와 Django 템플릿은 지원하지 않는다. **개발 중인 미출시 기능이며 공개된 0.1.0 패키지에는 없다.**

## 설치와 초기화

이 기능이 포함된 저장소 체크아웃에서 `pip install '.[django]'` 또는 `uv sync --extra django`를 실행한다. 소스 빌드에는 Rust가 필요하다. Django 5.2.8 이상 5.2 계열을 사용한다.

SQLite는 Django의 기본 드라이버를 사용한다. Django ORM에서 PostgreSQL을 사용하려면 `pip install '.[django-postgres]'` 또는 `uv sync --extra django-postgres`로 psycopg 드라이버를 함께 설치하고 Django의 `DATABASES` 설정을 지정한다. 이는 SQLAlchemy 연동의 `postgres`/asyncpg extra와 별도다.

Django 설정 모듈과 앱은 Python에서 import할 수 있어야 한다. 예를 들어 별도의 Django 프로젝트를 설치 가능한 Python 패키지로 만들고, 그 `settings.py`의 `INSTALLED_APPS`에 모델 앱을 등록한다. PyNext는 프로젝트 루트를 전역 `sys.path`에 추가하지 않는다. 자격 증명은 환경 변수나 비밀 저장소로 전달한다.

```python
# resources.py
from pynext import DjangoResource

RESOURCES = {"django": DjangoResource("mysite.settings", max_pending=32)}
```

리소스 선언을 읽을 때 Django 앱 레지스트리를 초기화하므로 라우트 모듈에서 모델을 import할 수 있다. 실제 작업 스레드는 앱 lifespan에서 시작·정리한다. ASGI 서버는 lifespan을 활성화해야 하며, 직접 실행하는 코드는 `await app.startup()`/`await app.shutdown()`을 호출해야 한다. 스키마는 Django의 `manage.py makemigrations`와 `manage.py migrate`로 **별도 배포 단계에서 한 번** 관리한다. 웹 워커가 자동으로 마이그레이션하지 않는다.

Django 설정과 앱 레지스트리는 프로세스 전역이다. 한 프로세스에서 서로 다른 설정 모듈을 사용하는 앱은 지원하지 않는다. 모델·설정·`INSTALLED_APPS`를 변경하면 개발 서버의 단순 모듈 새로고침 대신 **프로세스를 완전히 재시작**한다. 초기화 실패 후 Django 전역 상태가 부분 설정됐을 때도 새 프로세스에서 다시 시작한다.

## ORM과 폼 사용

Django 설정에 `catalog` 앱과 `Item(name=...)` 모델이 등록됐다고 가정한다. 모델 import와 PyNext 라우팅은 아래처럼 작성할 수 있다.

```python
# app/items/route.py
from django import forms
from django.db import transaction
from catalog.models import Item
from pynext import DjangoBusyError, JSONResponse, Response, route

class ItemForm(forms.Form):
    name = forms.CharField(max_length=100)

@route("POST")
async def create(request):
    # 실제 앱에서는 PyNext 인증·권한·CSRF와 입력 크기 정책을 적용한다.
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "Expected a JSON object"}, status=400)

    def validate_and_save():
        form = ItemForm(data)
        if not form.is_valid():
            return {"ok": False, "errors": form.errors.get_json_data()}
        with transaction.atomic():
            item = Item.objects.create(name=form.cleaned_data["name"])
            return {"ok": True, "id": item.pk, "name": item.name}

    try:
        result = await request.resources["django"].run(validate_and_save)
    except DjangoBusyError:
        return Response("Database work queue is full", status=503,
                        headers=[(b"retry-after", b"1")])
    return JSONResponse(result, status=201 if result["ok"] else 400)
```

`run()`은 동기 함수를 전용 스레드에서 실행한다. 동기 ORM이나 DB 조회가 들어가는 폼 검증을 이벤트 루프에서 직접 호출하지 않는다. 각 호출은 완료 시 **해당 스레드의 Django DB 연결을 닫도록 요청한다**. Django는 SQLite 메모리 DB의 데이터를 유지하기 위해 연결을 닫지 않을 수 있다. 메모리 DB는 다른 스레드·프로세스나 별도 마이그레이션 프로세스와 공유되지 않으므로 일반 앱에는 파일 기반 SQLite를 사용한다. Django 자체의 비동기 ORM을 직접 사용하는 경우의 연결 수명주기는 이 리소스가 관리하지 않는다.

트랜잭션은 한 `run()` 호출 안에서 `transaction.atomic()`으로 끝낸다. 여러 호출에 걸친 트랜잭션은 제공하지 않는다. `QuerySet`·모델·반복자나 비동기 함수를 반환하지 말고, 콜백 안에서 평가한 문자열·숫자·dict·list 같은 데이터를 반환한다. 최상위의 지연 객체는 오류로 거부하지만, 중첩 객체까지 모두 검사하지는 않는다. **반환값 검사는 콜백 실행 후** 이루어지므로, 거부 전에 이미 커밋된 쓰기는 되돌리지 않는다.

## 동시성·종료와 제약

- 리소스마다 전용 작업 스레드 1개가 요청을 순차 실행하며 `max_pending`(기본 32)은 실행 중인 작업을 포함한다. 가득 차면 `DjangoBusyError`가 발생하므로 앱에서 503 등으로 처리한다. 워커마다 별도의 큐와 DB 연결이 생긴다. 같은 리소스의 `run()`을 작업 스레드 안에서 다시 호출하면 교착을 막기 위해 오류를 반환한다.
- 요청이 취소돼도 이미 실행 중인 동기 함수는 중단할 수 없다. 작업이 끝날 때까지 큐 용량이 유지되고 종료 시에도 완료를 기다린다. 외부 호출·DB 타임아웃과 쓰기 멱등성을 앱에서 정한다.
- DB 연결 정리를 매 작업 종료 때 요청하며, 기동 시 데이터베이스 연결 상태나 마이그레이션 완료 여부를 검사하지 않는다. readiness가 지속적인 DB 건강 상태를 보장하지 않는다.
- Django 설정 초기화는 앱 모듈 로드 중에 일어난다. 신뢰하는 앱 코드만 빌드·실행하고, 외부 공개 운영에 필요한 보안 검증은 별도로 수행한다.
