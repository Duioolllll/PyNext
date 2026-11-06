import { label } from "./lib/label";

export function mount(element: HTMLElement, props: { initial: number; note?: string }) {
  let count = props.initial;
  const button = document.createElement("button");
  button.textContent = label(count);
  const onClick = () => { count += 1; button.textContent = label(count); };
  button.addEventListener("click", onClick);
  element.replaceChildren(button);
  return () => button.removeEventListener("click", onClick);
}
