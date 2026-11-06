type Props = { initial: number; input?: string };

export function render(props: Props): Node {
  const button = document.createElement("button");
  button.textContent = `Hydrated: ${props.initial}`;
  if (props.input !== undefined) {
    const fragment = document.createDocumentFragment();
    const input = document.createElement("input");
    input.setAttribute("type", "text");
    input.setAttribute("value", props.input);
    fragment.append(button, input);
    return fragment;
  }
  return button;
}

export function hydrate(element: HTMLElement, props: Props) {
  const button = element.querySelector("button");
  if (!button) throw new Error("Expected a server-rendered button");
  let count = props.initial;
  const click = () => { count += 1; button.textContent = `Hydrated: ${count}`; };
  button.addEventListener("click", click);
  return () => button.removeEventListener("click", click);
}

export function mount(element: HTMLElement, props: Props) {
  element.replaceChildren(render(props));
  return hydrate(element, props);
}
