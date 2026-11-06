// An island owns its element's children. Hydration preserves matching server DOM.
const instances = new WeakMap();
let visibilityObserver = null;

function cancelPending(state) {
  const cancel = state.cancelPending;
  state.cancelPending = null;
  if (cancel) {
    try { cancel(); } catch (error) { console.error("PyNext island scheduling cleanup failed", error); }
  }
}

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
  cancelPending(state);
  if (typeof state.cleanup === "function") {
    try { state.cleanup(); } catch (error) { console.error("PyNext island cleanup failed", error); }
    state.cleanup = null;
  }
}

async function mountIsland(element, state) {
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

function activate(element, state) {
  if (instances.get(element) !== state || state.removed || state.started || !element.isConnected) return;
  state.started = true;
  cancelPending(state);
  void mountIsland(element, state);
}

function scheduleIdle(element, state) {
  let cancel;
  if (typeof window.requestIdleCallback === "function") {
    try {
      const handle = window.requestIdleCallback(() => activate(element, state), { timeout: 2000 });
      cancel = () => { if (typeof window.cancelIdleCallback === "function") window.cancelIdleCallback(handle); };
    } catch (error) {
      cancel = null;
    }
  }
  if (!cancel) {
    const handle = window.setTimeout(() => activate(element, state), 1);
    cancel = () => window.clearTimeout(handle);
  }
  if (state.started || state.removed) cancel();
  else state.cancelPending = cancel;
}

function scheduleIsland(element) {
  if (instances.has(element)) return;
  const state = { removed: false, started: false, cleanup: null, cancelPending: null };
  instances.set(element, state);
  const strategy = element.getAttribute("data-pynext-strategy");
  if (strategy === "load") {
    activate(element, state);
  } else if (strategy === "idle") {
    element.setAttribute("data-pynext-state", "waiting");
    scheduleIdle(element, state);
  } else if (strategy === "visible") {
    element.setAttribute("data-pynext-state", "waiting");
    if (typeof window.IntersectionObserver !== "function") {
      scheduleIdle(element, state);
    } else {
      try {
        if (!visibilityObserver) {
          visibilityObserver = new IntersectionObserver((entries) => {
            for (const entry of entries) {
              if (entry.isIntersecting || entry.intersectionRatio > 0) {
                const current = instances.get(entry.target);
                if (current) activate(entry.target, current);
              }
            }
          });
        }
        state.cancelPending = () => visibilityObserver.unobserve(element);
        visibilityObserver.observe(element);
      } catch (error) {
        state.cancelPending = null;
        scheduleIdle(element, state);
      }
    }
  } else {
    element.setAttribute("data-pynext-error", "invalid-strategy");
    console.error("Invalid PyNext island loading strategy");
  }
}

function eachIsland(node, callback) {
  if (!(node instanceof Element)) return;
  if (node.matches("[data-pynext-island]")) callback(node);
  for (const element of node.querySelectorAll("[data-pynext-island]")) callback(element);
}

function start() {
  for (const element of document.querySelectorAll("[data-pynext-island]")) {
    scheduleIsland(element);
  }
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.removedNodes) eachIsland(node, (element) => {
        if (!element.isConnected) dispose(element);
      });
      for (const node of record.addedNodes) eachIsland(node, (element) => {
        if (element.isConnected) scheduleIsland(element);
      });
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
