# 컴포넌트와 레이아웃

[홈](Home.md) · [English](../en/Components-and-Layouts.md)

PyNext의 서버 UI는 Python 헬퍼로 트리를 구성합니다. Python 코드 자체가 브라우저로 전송되지는 않습니다.

## 요소와 Fragment

~~~python
from pynext import fragment, h, page

@page()
def Page():
    return h(
        "main", {"class": "content"},
        h("h1", {}, "문서"),
        fragment(
            h("p", {}, "HTML 이스케이프 처리되는 텍스트"),
            h("a", {"href": "/about"}, "소개"),
        ),
    )
~~~

- `h(tag, attrs=None, *children)`: HTML 요소 노드 생성.
- `fragment(*children)`: 별도 HTML 태그 없이 여러 노드 결합.
- 자식은 렌더 노드·문자열·정수·실수이며, `None`은 건너뜁니다.
- 텍스트는 Rust 렌더러에서 HTML 이스케이프됩니다. **임의 raw HTML 삽입 API는 없습니다.**
- 유효하지 않은 노드/속성, 중첩 islands, 지나치게 깊은 트리는 거부합니다.

## 공유 레이아웃

`app/layout.py`:

~~~python
from pynext import h, layout

@layout
def Layout(children):
    return h(
        "html", {"lang": "ko"},
        h("head", {}, h("title", {}, "My PyNext Site")),
        h("body", {}, h("nav", {}, "환영합니다"), children),
    )
~~~

상위 디렉터리의 레이아웃이 하위 페이지를 순서대로 감쌉니다. 루트 HTML 레이아웃이 있으면 doctype이 포함된 문서를 반환합니다. API 라우트에는 레이아웃이 적용되지 않습니다.

## 렌더링 규칙

페이지에는 `@page()`, `@page(render="ssg")`, `@page(render="isr", revalidate=30, stale=120)`를 사용하고, 레이아웃은 `@layout`를 사용합니다. `Page`는 렌더 노드 딕셔너리를 반환해야 하며 단순 텍스트나 임의 HTML 문자열을 직접 반환할 수 없습니다.

페이지에 Island가 있으면 빌드된 JS 모듈 경로와 런타임을 프레임워크가 연결합니다. Props는 브라우저에 공개되는 데이터이므로 비밀 값을 넣지 마세요.

관련 문서: [렌더링](Rendering.md), [Islands](Islands-and-Hydration.md), [실제 레이아웃 예시](../../../examples/basic/app/layout.py).
