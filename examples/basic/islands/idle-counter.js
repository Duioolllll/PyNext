export function mount(element, props) {
  let count = props.initial;
  const button = document.createElement("button");
  button.textContent = `Idle: ${count}`;
  const click = () => { count += 1; button.textContent = `Idle: ${count}`; };
  button.addEventListener("click", click);
  element.replaceChildren(button);
  return () => button.removeEventListener("click", click);
}
