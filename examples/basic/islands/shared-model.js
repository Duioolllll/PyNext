export function render(props) {
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("data-count", "1");
  button.setAttribute("title", 'Hello & "world"');
  button.textContent = `Shared: ${props.name}`;
  return button;
}

export function hydrate(element, props) {
  const button = element.querySelector("button");
  let count = 0;
  const click = () => { button.textContent = `Shared: ${props.name} (${++count})`; };
  button.addEventListener("click", click);
  return () => button.removeEventListener("click", click);
}

export function mount(element, props) {
  element.replaceChildren(render(props));
  return hydrate(element, props);
}
