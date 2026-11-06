// An island owns its element's children. Hydration preserves matching server DOM.
const instances = new WeakMap();

function matchesServerDOM(element, expected) {
  if (!(expected instanceof Node) || expected.isConnected) {
    throw new Error("Island render must return a detached DOM node");
  }
  const container = document.createElement("div");
  container.append(expected);
  const actualChildren = Array.from(element.childNodes);
  const expectedChildren = Array.from(container.childNodes);
  return actualChildren.length === expectedChildren.length &&
    actualChildren.every((node, index) => node.isEqualNode(expectedChildren[index]));
}

function dispose(element) {
  const state = instances.get(element);
  if (!state) return;
  instances.delete(element);
  state.removed = true;
  if (typeof state.cleanup === "function") {
    try { state.cleanup(); } catch (error) { console.error("PyNext island cleanup failed", error); }
    state.cleanup = null;
  }
}

async function mountIsland(element) {
  if (instances.has(element)) return;
  const state = { removed: false, cleanup: null };
  instances.set(element, state);
  try {
    const moduleUrl = element.getAttribute("data-pynext-module");
    if (!/^\/_pynext\/static\/[a-f0-9]{64}\.js$/.test(moduleUrl || "")) {
      throw new Error("Invalid island module URL");
    }
    const props = JSON.parse(element.getAttribute("data-pynext-props") || "{}");
    const module = await import(moduleUrl);
    if (typeof module.mount !== "function") throw new Error("Island must export mount");
    if (!element.isConnected || state.removed) return;
    const mode = element.getAttribute("data-pynext-mode");
    let cleanup, status;
    if (mode === "hydrate") {
      if (typeof module.render !== "function" || typeof module.hydrate !== "function") {
        throw new Error("Hydrated island must export render and hydrate");
      }
      const expected = await module.render(props);
      if (!element.isConnected || state.removed) return;
      if (matchesServerDOM(element, expected)) {
        cleanup = await module.hydrate(element, props);
        status = "hydrated";
      } else {
        element.setAttribute("data-pynext-mismatch", "true");
        console.warn("PyNext island DOM mismatch; mounting a fresh island", element);
        cleanup = await module.mount(element, props);
        status = "mounted";
      }
    } else if (mode === "mount") {
      cleanup = await module.mount(element, props);
      status = "mounted";
    } else {
      throw new Error("Invalid island mode");
    }
    if (!state.removed && element.isConnected) element.setAttribute("data-pynext-state", status);
    if (typeof cleanup === "function") {
      if (state.removed || !element.isConnected) cleanup();
      else state.cleanup = cleanup;
    }
  } catch (error) {
    element.setAttribute("data-pynext-error", "activation-failed");
    console.error("PyNext island failed to activate", error);
  }
}

function eachIsland(node, callback) {
  if (!(node instanceof Element)) return;
  if (node.matches("[data-pynext-island]")) callback(node);
  for (const element of node.querySelectorAll("[data-pynext-island]")) callback(element);
}

function start() {
  for (const element of document.querySelectorAll("[data-pynext-island]")) {
    void mountIsland(element);
  }
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.removedNodes) eachIsland(node, (element) => {
        if (!element.isConnected) dispose(element);
      });
      for (const node of record.addedNodes) eachIsland(node, (element) => {
        if (element.isConnected) void mountIsland(element);
      });
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
