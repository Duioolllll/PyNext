# JS/TS Islands와 Hydration

[홈](Home.md) · [English](../en/Islands-and-Hydration.md)

Islands는 Python/Rust로 렌더링한 HTML에 선택적으로 상호작용을 추가하는 기술입니다. esbuild로 번들링하며 **PyNext 독자 라이프사이클**을 사용합니다. React와 호환되지는 않습니다.

## Island 선언

~~~python
from pynext import h, island, page

@page()
def Page():
    return h("main", {}, island(
        "counter",
        props={"initial": 1},
        fallback=h("p", {}, "Enable JavaScript to use the counter."),
    ))
~~~

`islands/counter.js`(또는 `.ts`)를 생성하세요.

~~~javascript
export function mount(element, props) {
  element.textContent = String(props.initial);
  return () => { /* 필요시 이벤트 리스너/타이머 해제 */ };
}
~~~

모듈에는 `mount(element, props)`가 필수이고 정리 함수를 반환할 수 있습니다. 기존 서버 DOM을 재사용하는 **hydration**을 쓰려면 `render`와 `hydrate`도 내보내고 서버 렌더링 `fallback`을 제공해야 합니다. [hydrated-counter.ts](../../../examples/basic/islands/hydrated-counter.ts)를 참고하세요. DOM이 일치하지 않으면 다시 mount하며 입력값이나 포커스가 사라질 수 있습니다.

## 활성화 전략

| 전략 | 의미 |
| --- | --- |
| `load` (기본) | 페이지 로드 후 활성화 |
| `visible` | IntersectionObserver가 표시를 감지하면 활성화 |
| `idle` | 브라우저 유휴 시점에 활성화(대체 스케줄링 있음) |
| `interaction` | `hydrate=True`에서 리스너 등록 후 지원되는 버튼 클릭 최대 16개를 큐에 보관했다가 활성화 후 재생 |

예시: `island("hydrated-counter", props={"initial": 3}, fallback=h("button", {}, "Hydrated: 3"), hydrate=True, strategy="idle")`.

**상호작용 제한:** 지원되는 버튼 클릭만 캡처합니다. 링크·폼·입력 이벤트는 재생하지 않습니다. 초기 클릭, hydration 불일치, 활성화 실패 시 이벤트가 유실될 수 있습니다. 재생 이벤트는 합성 이벤트(`isTrusted=false`)이며 브라우저 사용자 활성화 권한을 보장하지 않습니다.

## 빌드 제약 및 보안

Props는 브라우저에 공개되는 JSON이며 **16 KiB** 한도입니다. 비밀 값을 넣지 마세요. 중첩 islands, 외부/npm 패키지 import, CSS import는 미지원입니다. 단일 번들은 **2 MiB**, 전체 island 자산은 **32 MiB** 이내여야 합니다. `/_pynext/` 경로는 예약되어 있습니다.

실제 구현은 [examples/basic/islands](../../../examples/basic/islands), 브라우저 검증은 [tests/browser](../../../tests/browser)를 참고하세요. 클라이언트 라우팅과 island HMR(바뀐 island 모듈을 전체 새로고침 없이 교체)은 구현됐습니다. HMR 중 island 상태는 보존되지 않습니다.
