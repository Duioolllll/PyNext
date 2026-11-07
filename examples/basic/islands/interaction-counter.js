export function render(props) {
  const section = document.createElement("section");
  const button = document.createElement("button");
  button.type = "button";
  const label = document.createElement("span");
  label.setAttribute("data-count", "1");
  label.textContent = `Interaction: ${props.initial}`;
  button.append(label);
  const link = document.createElement("a");
  link.setAttribute("href", "/about");
  link.textContent = "Normal link";
  const form = document.createElement("form");
  form.setAttribute("action", "/about");
  form.setAttribute("method", "get");
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "Normal submit";
  form.append(submit);
  section.append(button, link, form);
  return section;
}

export async function hydrate(element, props) {
  // An optional delay makes it easy to observe clicks arriving during loading.
  if (props.delay_ms) await new Promise((resolve) => setTimeout(resolve, props.delay_ms));
  const button = element.querySelector('button[type="button"]');
  const label = element.querySelector("[data-count]");
  let count = props.initial;
  const click = () => { label.textContent = `Interaction: ${++count}`; };
  button.addEventListener("click", click);
  return () => button.removeEventListener("click", click);
}

export function mount(element, props) {
  element.replaceChildren(render(props));
  return hydrate(element, props);
}
