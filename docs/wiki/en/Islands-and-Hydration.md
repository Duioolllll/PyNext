# JavaScript / TypeScript islands and hydration

[Home](Home.md) · [한국어](../ko/Islands-and-Hydration.md)

Islands add optional client interactivity to HTML rendered by Python/Rust. They are bundled with esbuild and use a **PyNext-specific** lifecycle; they do not use React.

## Declare an island

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

Create `islands/counter.js` (or `.ts`):

~~~javascript
export function mount(element, props) {
  element.textContent = String(props.initial);
  return () => { /* dispose listeners or timers if added */ };
}
~~~

Each island module must export `mount(element, props)` and may return cleanup. For **hydration**, also export `render` and `hydrate` and supply a matching server-rendered `fallback`; see [hydrated-counter.ts](../../../examples/basic/islands/hydrated-counter.ts). A DOM mismatch triggers remounting and can lose focus/input state.

## Activation strategies

| Strategy | Behavior |
| --- | --- |
| `load` (default) | Activate after loading |
| `visible` | Activate when IntersectionObserver reports visibility |
| `idle` | Defer until browser idle (with fallback scheduling) |
| `interaction` | With `hydrate=True`, capture up to 16 supported button clicks after listeners register, then replay on successful activation |

Use `island("hydrated-counter", props={"initial": 3}, fallback=h("button", {}, "Hydrated: 3"), hydrate=True, strategy="idle")` for a delayed hydrated island.

**Interaction limitations:** Only supported button click behavior is captured; links, forms and input events are not replayed. Earlier clicks, mismatches, or failed hydration can drop queued events. Replays are synthetic (`isTrusted=false`) and do not satisfy privileged browser user-activation requirements.

## Build/security constraints

Props are public JSON (maximum **16 KiB**) and must not contain credentials. Nested islands, external/npm-package imports and CSS imports are unsupported. One compiled bundle may be at most **2 MiB**, with **32 MiB** total island assets. `/_pynext/` is a reserved URL namespace.

For actual source and behavior tests see [examples/basic/islands](../../../examples/basic/islands) and [browser tests](../../../tests/browser). Client-side router/navigation and HMR are not implemented.
