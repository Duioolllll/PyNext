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

// Shared island models are bounded data, not executable browser code.
// Construct a detached DOM from the same model and props the server used.
function renderSharedModel(model, props) {
  if (!model || model.version !== 1 || !model.tree || Object.keys(model).length !== 2) {
    throw new Error("Invalid shared island model");
  }
  let count = 0;
  const validName = /^[A-Za-z][A-Za-z0-9_:-]{0,127}$/;
  const propName = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
  const blockedTags = new Set(["script", "style", "iframe", "object", "embed"]);
  const urlAttrs = new Set(["href", "src", "action", "formaction", "xlink:href"]);
  const voidTags = new Set(["area", "base", "br", "col", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
  const htmlText = (value) => value.replace(/\r\n?/g, "\n").replace(/\x00/g, "\ufffd");

  function render(node, depth) {
    count += 1;
    if (count > 512 || depth > 32 || !node || typeof node !== "object" || Array.isArray(node)) {
      throw new Error("Invalid shared model node");
    }
    const keys = Object.keys(node);
    if (node.kind === "prop" && keys.length === 2 && propName.test(node.name || "") &&
        Object.prototype.hasOwnProperty.call(props, node.name) && typeof props[node.name] === "string") {
      return document.createTextNode(htmlText(props[node.name]));
    }
    if (node.kind === "text" && keys.length === 2 && typeof node.value === "string") {
      return document.createTextNode(htmlText(node.value));
    }
    if (node.kind === "fragment" && keys.length === 2 && Array.isArray(node.children)) {
      const fragment = document.createDocumentFragment();
      for (const child of node.children) fragment.append(render(child, depth + 1));
      return fragment;
    }
    if (node.kind !== "element" || keys.length !== 4 || typeof node.tag !== "string" ||
        !validName.test(node.tag) || blockedTags.has(node.tag.toLowerCase()) ||
        !node.attrs || typeof node.attrs !== "object" || Array.isArray(node.attrs) ||
        !Array.isArray(node.children) || (voidTags.has(node.tag.toLowerCase()) && node.children.length)) {
      throw new Error("Invalid shared model element");
    }
    const element = document.createElement(node.tag);
    for (const [name, value] of Object.entries(node.attrs)) {
      const lower = name.toLowerCase();
      if (!validName.test(name) || lower.startsWith("on") || lower.startsWith("data-pynext-") ||
          lower === "style" || lower === "srcdoc" ||
          !(value === null || typeof value === "boolean" || typeof value === "string")) {
        throw new Error("Invalid shared model attribute");
      }
      if (value === null || value === false) continue;
      if (urlAttrs.has(lower) && typeof value === "string" &&
          /^(javascript|vbscript|data):/i.test(value.replace(/[\x00-\x20\x7f]/g, ""))) {
        throw new Error("Unsafe URL in shared model");
      }
      element.setAttribute(name, value === true ? "" : htmlText(value));
    }
    for (const child of node.children) element.append(render(child, depth + 1));
    return element;
  }
  const result = render(model.tree, 0);
  result.normalize();
  return result;
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
      const sharedModel = element.getAttribute("data-pynext-model");
      const expected = sharedModel === null ? await module.render(props)
        : renderSharedModel(JSON.parse(sharedModel), props);
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

// ---- Client-side navigation and prefetch (finding #51) ----
// Partial navigation: same-origin link clicks fetch the target document and
// swap the body instead of reloading. Any failure falls back to a full
// document navigation, so a page never breaks.
const PREFETCH_MAX_ENTRIES = 20;
const PREFETCH_MAX_BYTES = 1024 * 1024;
const PREFETCH_MAX_INFLIGHT = 4;
const prefetchCache = new Map();
const prefetchInflight = new Set();
const observedLinks = new WeakSet();
let linkObserver = null;
let navigationToken = 0;
let activeNavigation = null;

function navigationUrl(anchor) {
  if (!anchor || anchor.tagName !== "A") return null;
  const href = anchor.getAttribute("href");
  if (!href || href.startsWith("#")) return null;
  let url;
  try {
    url = new URL(href, document.baseURI);
  } catch (error) {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.origin !== window.location.origin) return null;
  return url;
}

function isNavigable(anchor) {
  const url = navigationUrl(anchor);
  if (!url) return null;
  if (anchor.hasAttribute("download")) return null;
  const target = anchor.getAttribute("target");
  if (target && target !== "_self") return null;
  // A same-page hash link is the browser's job (anchor scrolling).
  if (url.pathname === window.location.pathname && url.search === window.location.search) return null;
  return url;
}

function cachedPage(url) {
  const cached = prefetchCache.get(url.href);
  if (!cached) return null;
  if (cached.expires <= Date.now()) {
    prefetchCache.delete(url.href);
    return null;
  }
  return cached.html;
}

async function fetchPage(url, { useCache = true, signal } = {}) {
  const cached = useCache ? cachedPage(url) : null;
  if (cached !== null) return { html: cached, maxAge: 0 };
  const response = await fetch(url.href, { headers: { Accept: "text/html" }, signal });
  if (!response.ok) throw new Error(`Navigation fetch failed: ${response.status}`);
  const finalUrl = new URL(response.url);
  if (finalUrl.origin !== url.origin || finalUrl.pathname !== url.pathname || finalUrl.search !== url.search) {
    throw new Error("Navigation redirect requires a full page load");
  }
  const type = response.headers.get("content-type") || "";
  if (!type.toLowerCase().includes("text/html")) throw new Error("Navigation fetch did not return HTML");
  const csp = response.headers.get("content-security-policy");
  // Must match the server's default policy in content_security_policy()
  // (python/pynext/http.py); a different policy means the swapped content
  // would run under the wrong CSP, so fall back to a full page load.
  // Keep this in sync when the default policy changes.
  if (csp && csp !== "script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'") {
    throw new Error("Navigation CSP requires a full page load");
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Navigation response has no readable body");
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > PREFETCH_MAX_BYTES) throw new Error("Navigation page too large");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  const html = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  const directives = (response.headers.get("cache-control") || "").toLowerCase().split(",").map((v) => v.trim());
  const vary = (response.headers.get("vary") || "").toLowerCase().split(",").map((v) => v.trim());
  const age = directives.map((v) => /^max-age=(\d+)$/.exec(v)).find(Boolean);
  const safe = directives.includes("public") && !directives.some((v) => /^(private|no-store|no-cache)(=|$)/.test(v)) &&
    !vary.some((v) => ["*", "cookie", "authorization"].includes(v));
  // Prefetch hint (X-PyNext-Stale-Time): seconds until the response goes
  // stale. Static pages and fresh ISR responses carry it; it supersedes
  // Cache-Control max-age, which is 0 for those responses because shared
  // caches must revalidate. A malformed value is ignored. Retention still
  // caps at 60 seconds, as before.
  const staleHeader = response.headers.get("x-pynext-stale-time");
  const staleTime = staleHeader !== null && /^\d+$/.test(staleHeader.trim())
    ? Number(staleHeader.trim()) : null;
  const maxAge = safe
    ? (staleTime !== null ? Math.min(staleTime, 60) : (age ? Math.min(Number(age[1]), 60) : 0))
    : 0;
  return { html, maxAge };
}

function swapDocument(html, url) {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  const head = (doc) => Array.from(doc.head.children).filter((node) => node.tagName !== "TITLE")
    .map((node) => node.outerHTML).join("\n");
  const attrs = (node) => Array.from(node.attributes).map((a) => `${a.name}=${a.value}`).sort().join("\n");
  if (head(parsed) !== head(document) || attrs(parsed.documentElement) !== attrs(document.documentElement) ||
      attrs(parsed.body) !== attrs(document.body)) {
    throw new Error("Navigation document metadata changed");
  }
  document.body.replaceChildren(...parsed.body.childNodes);
  if (parsed.title) document.title = parsed.title;
  if (url.hash) {
    const target = document.getElementById(decodeURIComponent(url.hash.slice(1)));
    if (target) {
      target.scrollIntoView();
      return;
    }
  }
  window.scrollTo(0, 0);
}

async function navigate(url, { push = true, onFailure = "full" } = {}) {
  const token = ++navigationToken;
  activeNavigation?.abort();
  const controller = new AbortController();
  activeNavigation = controller;
  let html;
  try {
    const runtime = document.querySelector('script[src^="/_pynext/static/"]');
    if (runtime?.nonce) throw new Error("Nonce CSP requires a full page load");
    ({ html } = await fetchPage(url, { signal: controller.signal }));
  } catch (error) {
    // A superseded failure must never redirect or reload the newer page,
    // even when a transport ignores abort and rejects later.
    if (token !== navigationToken || controller.signal.aborted) return false;
    if (onFailure === "reload") window.location.reload();
    else window.location.href = url.href;
    return false;
  }
  if (token !== navigationToken) return false;
  try {
    swapDocument(html, url);
  } catch (error) {
    if (token !== navigationToken || controller.signal.aborted) return false;
    if (onFailure === "reload") window.location.reload();
    else window.location.href = url.href;
    return false;
  }
  if (push) {
    try {
      window.history.pushState(null, "", url.href);
    } catch (error) {
      // The swapped content stays even if history cannot be updated.
    }
  }
  scanLinks();
  if (activeNavigation === controller) activeNavigation = null;
  return true;
}

function prefetch(url) {
  const key = url.href;
  if (cachedPage(url) !== null || prefetchInflight.has(key) || prefetchInflight.size >= PREFETCH_MAX_INFLIGHT) {
    return Promise.resolve();
  }
  prefetchInflight.add(key);
  return fetchPage(url, { useCache: false }).then(({ html, maxAge }) => {
    if (!maxAge) return;
    if (prefetchCache.size >= PREFETCH_MAX_ENTRIES) {
      prefetchCache.delete(prefetchCache.keys().next().value);
    }
    // Keep the stale time with the entry: `stale` is the seconds until
    // this copy goes stale (the server's hint when it sent one, else the
    // Cache-Control max-age that set the expiry). It always agrees with
    // `expires`, so a reader can tell whether a prefetched copy is stale
    // without re-reading the response headers.
    prefetchCache.set(key, { html, expires: Date.now() + maxAge * 1000, stale: maxAge });
  }).catch(() => {
    // Prefetch is best-effort; the click still navigates.
  }).finally(() => prefetchInflight.delete(key));
}

function observeLink(anchor) {
  if (observedLinks.has(anchor) || !isNavigable(anchor)) return;
  observedLinks.add(anchor);
  if (typeof window.IntersectionObserver !== "function") return;
  try {
    if (!linkObserver) {
      linkObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting && entry.intersectionRatio <= 0) continue;
          const link = entry.target;
          linkObserver.unobserve(link);
          const url = navigationUrl(link);
          if (url) prefetch(url);
        }
      });
    }
    linkObserver.observe(anchor);
  } catch (error) {
    // Prefetch is best-effort.
  }
}

function scanLinks() {
  for (const anchor of document.querySelectorAll("a[href]")) observeLink(anchor);
}

function startNavigation() {
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
    const url = anchor ? isNavigable(anchor) : null;
    if (!url) return;
    event.preventDefault();
    void navigate(url);
  });
  for (const type of ["mouseover", "focusin"]) {
    document.addEventListener(type, (event) => {
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      const url = anchor ? isNavigable(anchor) : null;
      if (url) prefetch(url);
    });
  }
  window.addEventListener("popstate", () => {
    void navigate(new URL(window.location.href), { push: false, onFailure: "reload" });
  });
  scanLinks();
  window.__pynextNav = {
    navigate: (href) => {
      let url;
      try {
        url = new URL(href, window.location.href);
      } catch (error) {
        return Promise.resolve(false);
      }
      if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) {
        return Promise.resolve(false);
      }
      return navigate(url);
    },
    prefetch: (href) => {
      try {
        const url = new URL(href, window.location.href);
        if (url.origin === window.location.origin && ["http:", "https:"].includes(url.protocol)) return prefetch(url);
      } catch (error) {
        // Ignore an unparseable debug prefetch.
      }
    },
    isPrefetched: (href) => {
      try {
        return cachedPage(new URL(href, window.location.href)) !== null;
      } catch (error) {
        return false;
      }
    },
  };
}

function startDevelopmentUpdates() {
  const script = document.querySelector('script[data-pynext-dev]');
  const expectedId = script && script.getAttribute("data-pynext-dev");
  if (!/^[a-f0-9]{32}$/.test(expectedId || "")) return;

  async function poll() {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3000);
    try {
      const response = await fetch("/_pynext/dev/client", {
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
      });
      if (!response.ok) return;
      const state = await response.json();
      if (!state || !/^[a-f0-9]{32}$/.test(state.id || "") ||
          !state.bundles || typeof state.bundles !== "object" || Array.isArray(state.bundles)) return;
      if (state.id !== expectedId) {
        window.location.reload();
        return;
      }
      for (const element of document.querySelectorAll("[data-pynext-island]")) {
        const name = element.getAttribute("data-pynext-island");
        const digest = state.bundles[name];
        if (typeof digest !== "string" || !/^[a-f0-9]{64}$/.test(digest)) continue;
        const url = `/_pynext/static/${digest}.js`;
        if (element.getAttribute("data-pynext-module") === url) continue;
        const previous = instances.get(element);
        const wasStarted = !!(previous && previous.started);
        dispose(element);
        element.setAttribute("data-pynext-module", url);
        element.removeAttribute("data-pynext-state");
        element.removeAttribute("data-pynext-error");
        element.removeAttribute("data-pynext-mismatch");
        scheduleIsland(element);
        const current = instances.get(element);
        if (wasStarted && current) activate(element, current);
      }
    } catch (error) {
      // The development server may be restarting or have a broken edit.
    } finally {
      window.clearTimeout(timeout);
      window.setTimeout(poll, 1000);
    }
  }
  window.setTimeout(poll, 1000);
}

function start() {
  for (const element of document.querySelectorAll("[data-pynext-island]")) {
    scheduleIsland(element);
  }
  startNavigation();
  startDevelopmentUpdates();
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
