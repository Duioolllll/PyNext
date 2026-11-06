export function render(props) {
  const button = document.createElement("button");
  button.textContent = `Visible: ${props.initial}`;
  return button;
}

export function hydrate(element, props) {
  const button = element.querySelector("button");
  if (!button) throw new Error("Expected a server-rendered button");
  let count = props.initial;
  const click = () => { count += 1; button.textContent = `Visible: ${count}`; };
  button.addEventListener("click", click);
  return () => button.removeEventListener("click", click);
}

export function mount(element, props) {
  element.replaceChildren(render(props));
  return hydrate(element, props);
}
