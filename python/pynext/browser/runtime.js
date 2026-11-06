// An island owns its element's children. Its module exports mount(element, props).
const instances = new WeakMap();

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
    const cleanup = await module.mount(element, props);
    if (typeof cleanup === "function") {
      if (state.removed || !element.isConnected) cleanup();
      else state.cleanup = cleanup;
    }
  } catch (error) {
    element.setAttribute("data-pynext-error", "mount-failed");
    console.error("PyNext island failed to mount", error);
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
