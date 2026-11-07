// An island owns its element's children. Hydration preserves matching server DOM.
const instances = new WeakMap();
let visibilityObserver = null;
const MAX_INTERACTION_EVENTS = 16;

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

function interactionButton(element, target) {
  if (!(target instanceof Element) || !element.contains(target)) return null;
  // Navigation, form submission, labels and editable regions retain their
  // browser behavior instead of being captured and replayed.
  if (target.closest("a[href], label, [contenteditable]:not([contenteditable='false'])")) return null;
  const button = target.closest("button");
  if (!button || !element.contains(button) || button.type !== "button" || button.matches(":disabled")) return null;
  return button;
}

function releaseInteraction(element, state, replay = false) {
  const interaction = state.interaction;
  state.interaction = null;
  if (!interaction) return;
  element.removeEventListener("click", interaction.listener, true);
  if (!replay) return;
  for (const event of interaction.events) {
    if (!element.isConnected || interactionButton(element, event.target) !== event.button) continue;
    event.target.dispatchEvent(new MouseEvent("click", event.options));
  }
}

function dispose(element) {
  const state = instances.get(element);
  if (!state) return;
  instances.delete(element);
  state.removed = true;
  cancelPending(state);
  releaseInteraction(element, state);
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
    releaseInteraction(element, state, status === "hydrated" && !state.removed && element.isConnected);
  } catch (error) {
    releaseInteraction(element, state);
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

function scheduleInteraction(element, state) {
  if (element.getAttribute("data-pynext-mode") !== "hydrate") {
    element.setAttribute("data-pynext-error", "invalid-strategy");
    console.error("Interaction strategy requires a hydrated island");
    return;
  }
  const interaction = { events: [], overflow: false, listener: null };
  interaction.listener = (event) => {
    if (!event.isTrusted || event.defaultPrevented || event.button !== 0 || state.removed) return;
    const button = interactionButton(element, event.target);
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (interaction.events.length < MAX_INTERACTION_EVENTS) {
      interaction.events.push({
        target: event.target,
        button,
        options: {
          bubbles: true, cancelable: true, composed: true,
          button: event.button, buttons: event.buttons, detail: event.detail,
          clientX: event.clientX, clientY: event.clientY,
          screenX: event.screenX, screenY: event.screenY,
          ctrlKey: event.ctrlKey, altKey: event.altKey,
          shiftKey: event.shiftKey, metaKey: event.metaKey,
        },
      });
    } else if (!interaction.overflow) {
      interaction.overflow = true;
      console.warn("PyNext island interaction buffer is full; additional clicks are dropped");
    }
    element.setAttribute("data-pynext-state", "activating");
    activate(element, state);
  };
  state.interaction = interaction;
  element.addEventListener("click", interaction.listener, true);
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
  const state = { removed: false, started: false, cleanup: null, cancelPending: null, interaction: null };
  instances.set(element, state);
  const strategy = element.getAttribute("data-pynext-strategy");
  if (strategy === "load") {
    activate(element, state);
  } else if (strategy === "idle") {
    element.setAttribute("data-pynext-state", "waiting");
    scheduleIdle(element, state);
  } else if (strategy === "interaction") {
    element.setAttribute("data-pynext-state", "waiting");
    scheduleInteraction(element, state);
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
