use axum::{
    body::{Body, Bytes, HttpBody},
    extract::{
        ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
        FromRequestParts, Request, State,
    },
    http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode, Version},
    response::{IntoResponse, Response as AxumResponse},
    routing::any,
    Router as AxumRouter,
};
use futures_util::SinkExt as _;
use http_body_util::BodyExt as _;
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder as AutoBuilder;
use pynext_cache::{CacheResult, PageCache};
use pynext_router::Router;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use serde_json::Value;
use std::collections::HashMap;
use std::future::Future;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::sync::{mpsc as tokio_mpsc, oneshot, watch, OwnedSemaphorePermit, Semaphore};
use tower_service::Service as TowerService;

/// Prefetch hint header: seconds until the response goes stale, i.e. how
/// long a prefetched copy may be reused (Next.js `staleTimes`, min over
/// segments). Only static pages and fresh ISR responses carry it; a stale
/// or private response carries no hint so the client never retains it.
const PREFETCH_STALE_TIME_HEADER: HeaderName = HeaderName::from_static("x-pynext-stale-time");

#[pyclass(name = "Router")]
struct PyRouter {
    inner: Router,
}

/// A matched route as exposed to Python: the route pattern, each
/// parameter's matched segments, and each parameter's kind
/// (`"dynamic"`, `"catch_all"` or `"optional"`).
type PyRouteMatch = (
    String,
    HashMap<String, Vec<String>>,
    HashMap<String, String>,
);

#[pymethods]
impl PyRouter {
    #[new]
    fn new(routes: Vec<String>) -> PyResult<Self> {
        Ok(Self {
            inner: Router::new(routes).map_err(PyValueError::new_err)?,
        })
    }

    fn ordered_routes(&self) -> Vec<String> {
        self.inner.routes().to_vec()
    }

    fn matches(&self, path: &str) -> Option<PyRouteMatch> {
        self.inner.matches(path).map(|(pattern, params, kinds)| {
            (
                pattern,
                params,
                kinds.into_iter().map(|(k, v)| (k, v.to_string())).collect(),
            )
        })
    }
}

#[pyclass(name = "PageCache")]
struct PyPageCache {
    inner: Arc<PageCache>,
}

#[pymethods]
impl PyPageCache {
    #[new]
    #[pyo3(signature = (max_entries=1024, max_bytes=33_554_432, max_body_bytes=2_097_152))]
    fn new(max_entries: usize, max_bytes: usize, max_body_bytes: usize) -> Self {
        Self {
            inner: Arc::new(PageCache::new(max_entries, max_bytes, max_body_bytes)),
        }
    }

    fn lookup<'py>(
        &self,
        py: Python<'py>,
        key: &str,
    ) -> (&'static str, Option<Bound<'py, PyBytes>>) {
        match self.inner.lookup(key) {
            CacheResult::Fresh(body) => ("fresh", Some(PyBytes::new(py, &body))),
            CacheResult::Stale(body) => ("stale", Some(PyBytes::new(py, &body))),
            CacheResult::Expired => ("expired", None),
            CacheResult::Miss => ("miss", None),
        }
    }

    /// Seconds until the entry for `key` goes stale, or `None` when the
    /// entry is missing, already stale, or expired. This is the prefetch
    /// stale-time hint for a fresh ISR hit served without Python dispatch.
    fn fresh_remaining(&self, key: &str) -> Option<f64> {
        self.inner.fresh_remaining(key).map(|d| d.as_secs_f64())
    }

    fn remove(&self, key: &str) -> bool {
        self.inner.remove(key)
    }

    fn insert(
        &self,
        key: &str,
        body: &[u8],
        fresh_seconds: f64,
        stale_seconds: f64,
    ) -> PyResult<bool> {
        if !fresh_seconds.is_finite()
            || !stale_seconds.is_finite()
            || fresh_seconds <= 0.0
            || stale_seconds < 0.0
            || fresh_seconds > 604_800.0
            || stale_seconds > 604_800.0
        {
            return Err(PyValueError::new_err("Invalid cache durations"));
        }
        Ok(self.inner.insert(
            key.to_owned(),
            Bytes::copy_from_slice(body),
            Duration::from_secs_f64(fresh_seconds),
            Duration::from_secs_f64(stale_seconds),
        ))
    }
}

fn escape(s: &str, output: &mut String) {
    for c in s.chars() {
        match c {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '"' => output.push_str("&quot;"),
            '\'' => output.push_str("&#x27;"),
            _ => output.push(c),
        }
    }
}

fn html_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == ':')
}

fn client_asset_url(value: &str) -> bool {
    value
        .strip_prefix("/_pynext/static/")
        .and_then(|s| s.strip_suffix(".js"))
        .is_some_and(|digest| {
            digest.len() == 64
                && digest
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
}

fn render_node(
    node: &Value,
    output: &mut String,
    depth: usize,
    nonce: Option<&str>,
) -> Result<(), String> {
    if depth > 256 {
        return Err("Render tree exceeds maximum depth".into());
    }
    let obj = node.as_object().ok_or("Node must be an object")?;
    let kind = obj
        .get("kind")
        .and_then(Value::as_str)
        .ok_or("Node has no kind")?;
    match kind {
        "text" => escape(
            obj.get("value")
                .and_then(Value::as_str)
                .ok_or("Text must be a string")?,
            output,
        ),
        "fragment" => {
            for child in obj
                .get("children")
                .and_then(Value::as_array)
                .ok_or("Fragment children must be an array")?
            {
                render_node(child, output, depth + 1, nonce)?;
            }
        }
        "island" => {
            let name = obj
                .get("name")
                .and_then(Value::as_str)
                .ok_or("Island has no name")?;
            if name.len() > 64
                || !name.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            {
                return Err("Invalid island name".into());
            }
            let module = obj
                .get("module")
                .and_then(Value::as_str)
                .ok_or("Island has no module")?;
            if !client_asset_url(module) {
                return Err("Invalid island module URL".into());
            }
            let props = obj
                .get("props")
                .filter(|v| v.is_object())
                .ok_or("Island props must be an object")?;
            let props = serde_json::to_string(props).map_err(|e| e.to_string())?;
            if props.len() > 16_384 {
                return Err("Island props exceed maximum size".into());
            }
            let hydrate = obj
                .get("hydrate")
                .and_then(Value::as_bool)
                .ok_or("Island hydrate must be a boolean")?;
            let strategy = obj
                .get("strategy")
                .and_then(Value::as_str)
                .ok_or("Island has no loading strategy")?;
            if !matches!(strategy, "load" | "visible" | "idle" | "interaction") {
                return Err("Invalid island loading strategy".into());
            }
            if strategy == "interaction" && !hydrate {
                return Err("Interaction strategy requires a hydrated island".into());
            }
            output.push_str("<div data-pynext-island=\"");
            escape(name, output);
            output.push_str("\" data-pynext-module=\"");
            escape(module, output);
            output.push_str("\" data-pynext-mode=\"");
            output.push_str(if hydrate { "hydrate" } else { "mount" });
            output.push_str("\" data-pynext-strategy=\"");
            output.push_str(strategy);
            output.push_str("\" data-pynext-props=\"");
            escape(&props, output);
            if let Some(model) = obj.get("model") {
                if !hydrate
                    || model.get("version").and_then(Value::as_u64) != Some(1)
                    || !model.get("tree").is_some_and(Value::is_object)
                {
                    return Err("Invalid shared island model".into());
                }
                let model = serde_json::to_string(model).map_err(|e| e.to_string())?;
                if model.len() > 16_512 {
                    return Err("Shared island model exceeds maximum size".into());
                }
                output.push_str("\" data-pynext-model=\"");
                escape(&model, output);
            }
            output.push_str("\">");
            for child in obj
                .get("children")
                .and_then(Value::as_array)
                .ok_or("Island children must be an array")?
            {
                render_node(child, output, depth + 1, nonce)?;
            }
            output.push_str("</div>");
        }
        "client_runtime" => {
            let src = obj
                .get("src")
                .and_then(Value::as_str)
                .ok_or("Client runtime has no source")?;
            if !client_asset_url(src) {
                return Err("Invalid client runtime URL".into());
            }
            output.push_str("<script type=\"module\" src=\"");
            escape(src, output);
            output.push('"');
            if let Some(id) = obj.get("dev_id") {
                let id = id.as_str().ok_or("Invalid development server ID")?;
                if id.len() != 32 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return Err("Invalid development server ID".into());
                }
                output.push_str(" data-pynext-dev=\"");
                escape(id, output);
                output.push('"');
            }
            // A per-request nonce lets the CSP header allow this script
            // without opening script-src to every inline script. The nonce
            // is validated in render(); only the token characters produced
            // by the Python helper are accepted.
            if let Some(nonce) = nonce {
                output.push_str(" nonce=\"");
                escape(nonce, output);
                output.push('"');
            }
            output.push_str("></script>");
        }
        "element" => {
            let tag = obj
                .get("tag")
                .and_then(Value::as_str)
                .ok_or("Element has no tag")?;
            if !html_name(tag)
                || matches!(
                    tag.to_ascii_lowercase().as_str(),
                    "script" | "style" | "iframe" | "object" | "embed"
                )
            {
                return Err(format!("Unsupported HTML tag: {tag}"));
            }
            output.push('<');
            output.push_str(tag);
            let attrs = obj
                .get("attrs")
                .and_then(Value::as_object)
                .ok_or("Attributes must be an object")?;
            for (key, value) in attrs {
                let lower = key.to_ascii_lowercase();
                if !html_name(key)
                    || lower.starts_with("on")
                    || lower == "style"
                    || lower == "srcdoc"
                {
                    return Err(format!("Unsupported HTML attribute: {key}"));
                }
                if value.is_null() || value == &Value::Bool(false) {
                    continue;
                }
                output.push(' ');
                output.push_str(key);
                if value == &Value::Bool(true) {
                    continue;
                }
                let val = match value {
                    Value::String(s) => s.clone(),
                    Value::Number(n) => n.to_string(),
                    _ => return Err(format!("Unsupported value for attribute: {key}")),
                };
                if matches!(
                    lower.as_str(),
                    "href" | "src" | "action" | "formaction" | "xlink:href"
                ) {
                    let clean: String = val
                        .chars()
                        .filter(|c| !c.is_ascii_control() && !c.is_ascii_whitespace())
                        .collect::<String>()
                        .to_ascii_lowercase();
                    if clean.starts_with("javascript:")
                        || clean.starts_with("vbscript:")
                        || clean.starts_with("data:")
                    {
                        return Err(format!("Unsafe URL in attribute: {key}"));
                    }
                }
                output.push_str("=\"");
                escape(&val, output);
                output.push('"');
            }
            output.push('>');
            let children = obj
                .get("children")
                .and_then(Value::as_array)
                .ok_or("Element children must be an array")?;
            let void = matches!(
                tag.to_ascii_lowercase().as_str(),
                "area"
                    | "base"
                    | "br"
                    | "col"
                    | "hr"
                    | "img"
                    | "input"
                    | "link"
                    | "meta"
                    | "param"
                    | "source"
                    | "track"
                    | "wbr"
            );
            if void && !children.is_empty() {
                return Err(format!("Void element {tag} cannot have children"));
            }
            for child in children {
                render_node(child, output, depth + 1, nonce)?;
            }
            if !void {
                output.push_str("</");
                output.push_str(tag);
                output.push('>');
            }
        }
        _ => return Err(format!("Unknown node kind: {kind}")),
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (tree_json, nonce=None))]
fn render(py: Python<'_>, tree_json: &str, nonce: Option<&str>) -> PyResult<String> {
    if let Some(nonce) = nonce {
        if nonce.is_empty()
            || !nonce
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'+' | b'/' | b'='))
        {
            return Err(PyValueError::new_err("Invalid CSP nonce"));
        }
    }
    py.detach(|| {
        let node: Value =
            serde_json::from_str(tree_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut result = String::new();
        render_node(&node, &mut result, 0, nonce).map_err(PyValueError::new_err)?;
        Ok(result)
    })
}

struct ServerState {
    callback: Py<PyAny>,
    capacity: Arc<Semaphore>,
    waiting_capacity: Arc<Semaphore>,
    acquire_timeout: Duration,
    /// Adaptive ceiling for the slot wait (C-P1-5); `None` keeps the
    /// fixed `acquire_timeout`.
    acquire_timeout_max: Option<Duration>,
    /// EWMA of slot hold times, feeding the adaptive timeout.
    hold_stats: Arc<HoldTimeStats>,
    body_capacity: Arc<Semaphore>,
    body_timeout: Duration,
    // Fast-path budget (S-P1-11 / M-1): static assets and Fresh ISR pages
    // bypass the execution semaphore, so without their own cap a flood on
    // those paths is bounded only by the connection limits (1024/128).
    // A permit is held from dispatch until the response body has been
    // fully consumed by hyper (see `capped_body`); when the budget is
    // exhausted the request is shed with 503 + Retry-After instead of
    // falling through to the Python bridge, which would turn a cheap
    // static flood into expensive Python work.
    fast_path_capacity: Arc<Semaphore>,
    // Connection budget (A4): a semaphore for the total number of live TCP
    // connections, plus a per-IP count. Both are checked in the accept loop
    // before any request is read.
    connection_capacity: Arc<Semaphore>,
    per_ip_connections: Arc<Mutex<HashMap<IpAddr, usize>>>,
    max_connections_per_ip: usize,
    // Trusted proxies (S-P1-18, M-8): the same list Python's ProxyTrust
    // resolves X-Forwarded-For / Forwarded against. A peer in this list
    // is exempt from the per-IP connection cap in the accept loop (see
    // there for why); an empty list is the fail-closed default.
    trusted_proxies: Vec<TrustedProxyNetwork>,
    // Bounds slow request headers and, because hyper applies it to every
    // header read on the connection, idle keep-alive time between requests.
    // Zero disables the timeout.
    header_read_timeout: Duration,
    // Minimum average body receive rate in bytes/second (0 disables).
    body_min_rate: usize,
    // S-P1-14: total lifetime cap for one streaming response, from the
    // first chunk to the last (0 disables). The per-chunk 30s timeout
    // alone lets a client hold an execution slot forever by sending
    // one byte every 29 seconds; the lifetime cap bounds that.
    streaming_max_lifetime: Duration,
    // S-P1-15: per-IP cap on concurrent body slots. The global
    // body_capacity semaphore still applies; this stops one IP from
    // rotating through all 16 body slots and 503ing legitimate uploads.
    body_per_ip: Arc<Mutex<HashMap<IpAddr, usize>>>,
    max_body_per_ip: usize,
    // S-P1-16: WebSocket idle timeout, maximum session lifetime, and
    // the interval at which the server pings an idle session (0
    // disables each). A ping unanswered for WS_PONG_TIMEOUT closes
    // the session; accepted TCP connections also get OS keepalive
    // probes (see the accept loop).
    ws_idle_timeout: Duration,
    ws_max_lifetime: Duration,
    ws_ping_interval: Duration,
    static_pages: HashMap<String, Bytes>,
    /// Prefetch stale-time hint (seconds) per static page path, computed
    /// in Python as min(SSG stale time, layout stale times). The Axum
    /// fast path serves static pages without Python dispatch, so it
    /// needs the per-path values up front.
    prefetch_stale_times: HashMap<String, u64>,
    public_assets: HashMap<String, PublicAsset>,
    client_assets: HashMap<String, Bytes>,
    asset_etags: HashMap<String, HeaderValue>,
    isr_cache: Arc<PageCache>,
    /// Per-route ISR `Cache-Control` values (`pattern -> header`), computed
    /// in Python from each ISR policy. The native server matches a fresh
    /// L1 hit against the route trie and serves the policy header; routes
    /// without a policy fall back to `public, max-age=0, must-revalidate`.
    isr_cache_control: HashMap<String, HeaderValue>,
    middleware_enabled: bool,
    shared_cache_enabled: bool,
    /// A1 security headers configured in Python (`SecurityConfig`), applied
    /// to every response the Rust layer builds itself (static assets,
    /// cached ISR pages, plain errors). Bridge responses already carry them
    /// from Python dispatch and pass through untouched.
    security_headers: Vec<(Vec<u8>, Vec<u8>)>,
    /// Per-path response headers configured in Python (`App(headers=...)`),
    /// applied to the same Rust-built responses. Each rule carries its own
    /// one-pattern router, so every matching rule applies.
    path_headers: Vec<PathHeaderRule>,
    /// Host allowlist (S-P1-19, M-9): compiled `allowed_hosts` patterns,
    /// checked for every request at the top of `dispatch`.
    allowed_hosts: Vec<HostPattern>,
    /// Build-identity headers configured in Python (`X-PyNext-Build-Id`,
    /// `X-PyNext-Deployment-Id`), applied to the static assets and HTML
    /// the Rust layer serves itself, so a client can detect skew against
    /// a newer deploy. Bridge responses already carry them from Python
    /// dispatch and pass through untouched.
    build_headers: Vec<(Vec<u8>, Vec<u8>)>,
    websocket_router: Router,
    websocket_paths: std::collections::HashSet<String>,
    websocket_protocols: HashMap<String, Vec<String>>,
    websocket_max_message: usize,
    ws_callback: Option<Py<PyAny>>,
    ws_origin_callback: Option<Py<PyAny>>,
    // Header-stage admission, shared by HTTP and WebSocket handshakes.
    // Checks use a separate bounded budget from body intake and handlers.
    ws_rate_limit_callback: Option<Py<PyAny>>,
    http_rate_limit_callback: Option<Py<PyAny>>,
    rate_limit_capacity: Arc<Semaphore>,
    // H-2: rate-limit decisions that could not be produced (decision
    // gate full or the 2s deadline expired) and failed open instead of
    // 503. Read through `NativeServer::rate_limit_fail_open_count`.
    rate_limit_fail_open: Arc<AtomicU64>,
    // Per-IP execution quota (H-5): how many execution slots one client
    // IP may hold at once, and the live per-IP counts. The global pool
    // is `capacity`; without a per-IP cap one IP could take every slot
    // with slow requests and 503 all other users.
    per_ip_executions: Arc<Mutex<HashMap<IpAddr, usize>>>,
    max_concurrent_per_ip: usize,
    ws_origin_capacity: Arc<Semaphore>,
    ws_auth_callback: Option<Py<PyAny>>,
    ws_auth_capacity: Arc<Semaphore>,
}

/// One held per-IP execution slot (H-5); releases the IP's count on drop,
/// so the quota is freed even when the request ends early (body error,
/// timeout, client disconnect) and for streaming responses, whose guard
/// travels with the stream until it ends.
struct IpExecutionGuard {
    counts: Arc<Mutex<HashMap<IpAddr, usize>>>,
    ip: IpAddr,
}

impl Drop for IpExecutionGuard {
    fn drop(&mut self) {
        let mut counts = self.counts.lock().unwrap_or_else(|err| err.into_inner());
        if let Some(count) = counts.get_mut(&self.ip) {
            *count = count.saturating_sub(1);
            if *count == 0 {
                counts.remove(&self.ip);
            }
        }
    }
}

impl ServerState {
    /// Try to take one execution slot for `ip` (H-5). Returns `None`
    /// when the IP already holds `max_concurrent_per_ip` slots; the
    /// caller answers 503 without touching the global pool or its wait
    /// queue, so one IP cannot starve the others' queue access either.
    fn try_acquire_ip_execution(&self, ip: IpAddr) -> Option<IpExecutionGuard> {
        let mut counts = self
            .per_ip_executions
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let count = counts.entry(ip).or_insert(0);
        if *count >= self.max_concurrent_per_ip {
            return None;
        }
        *count += 1;
        Some(IpExecutionGuard {
            counts: Arc::clone(&self.per_ip_executions),
            ip,
        })
    }
}

#[pyclass(name = "_RequestCancellation")]
struct RequestCancellation {
    cancelled: Arc<AtomicBool>,
}

#[pymethods]
impl RequestCancellation {
    fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }
}

struct CancelOnDrop(Arc<AtomicBool>);

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

enum BridgeBody {
    Buffered(Vec<u8>),
    Streaming(Py<PyAny>),
}

fn close_stream(stream: Arc<Py<PyAny>>) {
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            let _ = stream.bind(py).call_method0("close");
        });
    });
}

fn streaming_body(
    stream: Py<PyAny>,
    permit: Option<OwnedSemaphorePermit>,
    ip_guard: Option<IpExecutionGuard>,
    hold_stats: Arc<HoldTimeStats>,
    acquired_at: Instant,
    max_lifetime: Duration,
) -> Body {
    let stream = Arc::new(stream);
    let (sender, receiver) = tokio_mpsc::channel::<Result<Bytes, std::io::Error>>(1);
    let failed = Arc::new(AtomicBool::new(false));
    let lifetime_exceeded = Arc::new(AtomicBool::new(false));
    let producer_failed = Arc::clone(&failed);
    let producer_expired = Arc::clone(&lifetime_exceeded);
    let started = tokio::time::Instant::now();
    tokio::spawn(async move {
        let _permit = permit;
        // The per-IP slot stays held until the stream ends, like the
        // global execution slot (H-5).
        let _ip_guard = ip_guard;
        loop {
            // S-P1-14: total lifetime cap, checked before every chunk.
            // A stream that keeps trickling inside the per-chunk 30s
            // timeout is cut off here, freeing its execution slot.
            if !max_lifetime.is_zero() && started.elapsed() >= max_lifetime {
                producer_expired.store(true, Ordering::Release);
                break;
            }
            // Bound the reserve wait by the remaining lifetime too, so
            // a stalled stream cannot overshoot the cap by a full 30s.
            let reserve_timeout = if max_lifetime.is_zero() {
                Duration::from_secs(30)
            } else {
                Duration::from_secs(30).min(max_lifetime.saturating_sub(started.elapsed()))
            };
            let slot = match tokio::time::timeout(reserve_timeout, sender.reserve()).await {
                Ok(Ok(slot)) => slot,
                Ok(Err(_)) => break,
                Err(_) => {
                    producer_failed.store(true, Ordering::Release);
                    break;
                }
            };
            let next_stream = Arc::clone(&stream);
            let next = tokio::task::spawn_blocking(move || {
                Python::attach(|py| {
                    next_stream
                        .bind(py)
                        .call_method0("next_chunk")?
                        .extract::<Option<Vec<u8>>>()
                })
            });
            tokio::select! {
                _ = sender.closed() => break,
                result = next => match result {
                    Ok(Ok(Some(chunk))) => { slot.send(Ok(Bytes::from(chunk))); }
                    Ok(Ok(None)) => break,
                    _ => {
                        slot.send(Err(std::io::Error::other("PyNext response stream failed")));
                        break;
                    }
                }
            }
        }
        // The slot is held until the stream ends: record the full hold
        // so the adaptive timeout (C-P1-5) sees streaming lifetimes too.
        hold_stats.record(acquired_at.elapsed());
        close_stream(stream);
    });
    let body = futures_util::stream::unfold(
        (receiver, failed, lifetime_exceeded),
        |(mut receiver, failed, lifetime_exceeded)| async move {
            match receiver.recv().await {
                Some(item) => Some((item, (receiver, failed, lifetime_exceeded))),
                None if lifetime_exceeded.swap(false, Ordering::AcqRel) => Some((
                    Err(std::io::Error::other(
                        "PyNext response stream lifetime exceeded",
                    )),
                    (receiver, failed, lifetime_exceeded),
                )),
                None if failed.swap(false, Ordering::AcqRel) => Some((
                    Err(std::io::Error::other("PyNext response stream stalled")),
                    (receiver, failed, lifetime_exceeded),
                )),
                None => None,
            }
        },
    );
    Body::from_stream(body)
}

const MAX_REQUEST_BODY: usize = 1_048_576;

struct PublicAsset {
    body: Bytes,
    content_type: HeaderValue,
}

fn trim_ows(value: &[u8]) -> &[u8] {
    let start = value
        .iter()
        .position(|b| !matches!(b, b' ' | b'\t'))
        .unwrap_or(value.len());
    let end = value
        .iter()
        .rposition(|b| !matches!(b, b' ' | b'\t'))
        .map_or(start, |i| i + 1);
    &value[start..end]
}

fn tags_match(headers: &HeaderMap, name: HeaderName, etag: &[u8], weak: bool) -> bool {
    for value in headers.get_all(name).iter() {
        let input = value.as_bytes();
        let mut quoted = false;
        let mut start = 0;
        let mut parts = Vec::new();
        for (index, byte) in input.iter().enumerate() {
            match byte {
                b'"' => quoted = !quoted,
                b',' if !quoted => {
                    parts.push(trim_ows(&input[start..index]));
                    start = index + 1;
                }
                _ => {}
            }
        }
        if quoted {
            continue;
        }
        parts.push(trim_ows(&input[start..]));
        for part in parts {
            let part = if weak {
                part.strip_prefix(b"W/").unwrap_or(part)
            } else {
                part
            };
            if part == b"*" || part == etag {
                return true;
            }
        }
    }
    false
}

enum ByteRange {
    Ignore,
    Unsatisfiable,
    Partial(usize, usize),
}

fn bounded_number(value: &[u8], ceiling: usize) -> Option<usize> {
    if value.is_empty() || !value.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let maximum = ceiling.saturating_add(1);
    Some(value.iter().fold(0usize, |number, digit| {
        number
            .saturating_mul(10)
            .saturating_add((digit - b'0') as usize)
            .min(maximum)
    }))
}

fn single_range(value: &[u8], length: usize) -> ByteRange {
    let Some(equals) = value.iter().position(|b| *b == b'=') else {
        return ByteRange::Ignore;
    };
    let unit = trim_ows(&value[..equals]);
    let fields = trim_ows(&value[equals + 1..]);
    if !unit.eq_ignore_ascii_case(b"bytes") || fields.contains(&b',') {
        return ByteRange::Ignore;
    }
    let Some(dash) = fields.iter().position(|b| *b == b'-') else {
        return ByteRange::Ignore;
    };
    let (first, last) = (&fields[..dash], &fields[dash + 1..]);
    if last.contains(&b'-') || (first.is_empty() && last.is_empty()) {
        return ByteRange::Ignore;
    }
    let start = if first.is_empty() {
        None
    } else {
        bounded_number(first, length)
    };
    let end = if last.is_empty() {
        None
    } else {
        bounded_number(last, length)
    };
    if (!first.is_empty() && start.is_none()) || (!last.is_empty() && end.is_none()) {
        return ByteRange::Ignore;
    }
    if let Some(start) = start {
        if end.is_some_and(|end| end < start) {
            return ByteRange::Ignore;
        }
        if start >= length {
            return ByteRange::Unsatisfiable;
        }
        return ByteRange::Partial(start, end.unwrap_or(length - 1).min(length - 1));
    }
    let suffix = end.expect("a suffix was validated above");
    if suffix == 0 || length == 0 {
        return ByteRange::Unsatisfiable;
    }
    ByteRange::Partial(length.saturating_sub(suffix), length - 1)
}

struct AssetPolicy<'a> {
    content_type: &'a HeaderValue,
    etag: &'a HeaderValue,
    cache_control: &'static str,
    vary: bool,
    /// Prefetch stale-time hint (seconds) for static pages; `None` for
    /// assets, which are never prefetched as pages.
    stale_time: Option<u64>,
}

/// Add the Python-configured A1 security headers to a response the Rust
/// layer builds itself (static assets, cached ISR pages, plain errors).
/// Bridge responses already carry them from Python dispatch and pass
/// through untouched. Invalid entries are skipped; Python validates the
/// values before handing them over.
fn apply_security_headers(
    mut builder: axum::http::response::Builder,
    security_headers: &[(Vec<u8>, Vec<u8>)],
) -> axum::http::response::Builder {
    for (name, value) in security_headers {
        if let (Ok(name), Ok(value)) =
            (HeaderName::from_bytes(name), HeaderValue::from_bytes(value))
        {
            builder = builder.header(name, value);
        }
    }
    builder
}

/// One per-path response-header rule, compiled in Python
/// (`App(headers=...)`) and handed over as `(source, headers)` pairs.
/// Each rule carries its own one-pattern `Router`, so — unlike route
/// dispatch, which takes the single best match — every matching rule
/// applies.
struct PathHeaderRule {
    router: Router,
    headers: Vec<(HeaderName, HeaderValue)>,
}

/// Merged per-path headers for `path`: every matching rule applies, and
/// when two rules set the same header the later rule wins — the same
/// merge as `http.PathHeaders.headers_for`.
fn path_headers_for(path: &str, rules: &[PathHeaderRule]) -> Vec<(HeaderName, HeaderValue)> {
    let mut merged: Vec<(HeaderName, HeaderValue)> = Vec::new();
    for rule in rules {
        if rule.router.matches(path).is_some() {
            for (name, value) in &rule.headers {
                if let Some(slot) = merged.iter_mut().find(|(n, _)| n == name) {
                    slot.1 = value.clone();
                } else {
                    merged.push((name.clone(), value.clone()));
                }
            }
        }
    }
    merged
}

/// Apply merged per-path headers to a response the Rust layer built.
/// A header already present wins (asset or framework default), except
/// `content-security-policy`, which replaces the default policy the
/// security headers added — mirroring `http.apply_path_headers`.
/// Responses that cross the Python bridge already carry their path
/// headers from dispatch and never reach this function.
fn apply_path_headers(
    response: AxumResponse<Body>,
    path: &str,
    rules: &[PathHeaderRule],
) -> AxumResponse<Body> {
    apply_merged_path_headers(response, &path_headers_for(path, rules))
}

/// Apply already-merged per-path headers (see [`path_headers_for`]) to a
/// response. Split out so a response built after the bridge — where the
/// rules were moved into the blocking task — can still carry the headers
/// merged for its path beforehand.
fn apply_merged_path_headers(
    mut response: AxumResponse<Body>,
    merged: &[(HeaderName, HeaderValue)],
) -> AxumResponse<Body> {
    for (name, value) in merged {
        // CSP always replaces the default policy; any other header is
        // added only when the response does not already carry it.
        if name == header::CONTENT_SECURITY_POLICY || !response.headers().contains_key(name) {
            response.headers_mut().insert(name, value.clone());
        }
    }
    response
}

/// Apply per-path headers to a Rust-built error response for `raw_path`,
/// decoding it first; an undecodable path matches no rule, so the
/// response passes through unchanged.
fn apply_path_headers_for_raw_path(
    response: AxumResponse<Body>,
    raw_path: &str,
    rules: &[PathHeaderRule],
) -> AxumResponse<Body> {
    match static_path(raw_path) {
        Some(path) => apply_path_headers(response, &path, rules),
        None => response,
    }
}

/// A fast-path response body that holds its concurrency permit until the
/// body has been fully consumed (or the connection is dropped): the permit
/// lives in the stream state, so it is released when hyper polls the stream
/// to its end or drops it — not when `dispatch` returns. Holding the permit
/// only across `dispatch` would be useless, because the fast-path section
/// has no await point and hyper streams the body after `dispatch` returns;
/// under socket backpressure hyper stops polling the body, so a slow reader
/// keeps the permit (and the budget) occupied, which is exactly what the
/// cap must bound.
fn capped_body(content: Bytes, permit: Option<OwnedSemaphorePermit>) -> Body {
    let Some(permit) = permit else {
        return Body::from(content);
    };
    let stream =
        futures_util::stream::unfold((Some(content), Some(permit)), |(data, permit)| async move {
            let data = data?;
            Some((Ok::<_, std::convert::Infallible>(data), (None, permit)))
        });
    Body::from_stream(stream)
}

/// Add the Python-configured build-identity headers to a response the
/// Rust layer builds itself (static assets, cached ISR pages). Invalid
/// entries are skipped; Python validates the values before handing them
/// over. Like `apply_security_headers`, this never overwrites a header
/// the builder already carries.
fn apply_build_headers(
    mut builder: axum::http::response::Builder,
    build_headers: &[(Vec<u8>, Vec<u8>)],
) -> axum::http::response::Builder {
    for (name, value) in build_headers {
        if let (Ok(name), Ok(value)) =
            (HeaderName::from_bytes(name), HeaderValue::from_bytes(value))
        {
            builder = builder.header(name, value);
        }
    }
    builder
}

fn static_response(
    parts: &axum::http::request::Parts,
    content: &Bytes,
    policy: AssetPolicy<'_>,
    security_headers: &[(Vec<u8>, Vec<u8>)],
    build_headers: &[(Vec<u8>, Vec<u8>)],
    fast_path_permit: Option<OwnedSemaphorePermit>,
) -> AxumResponse<Body> {
    let length = content.len();
    let mut status = StatusCode::OK;
    let mut range = None;
    let mut content_range = None;
    if parts.headers.contains_key(header::IF_MATCH)
        && !tags_match(
            &parts.headers,
            header::IF_MATCH,
            policy.etag.as_bytes(),
            false,
        )
    {
        status = StatusCode::PRECONDITION_FAILED;
    } else if parts.headers.contains_key(header::IF_NONE_MATCH)
        && tags_match(
            &parts.headers,
            header::IF_NONE_MATCH,
            policy.etag.as_bytes(),
            true,
        )
    {
        status = StatusCode::NOT_MODIFIED;
    } else if parts.method == axum::http::Method::GET {
        let ranges: Vec<_> = parts.headers.get_all(header::RANGE).iter().collect();
        let validators: Vec<_> = parts.headers.get_all(header::IF_RANGE).iter().collect();
        if ranges.len() == 1
            && (validators.is_empty()
                || (validators.len() == 1
                    && trim_ows(validators[0].as_bytes()) == policy.etag.as_bytes()))
        {
            match single_range(ranges[0].as_bytes(), length) {
                ByteRange::Ignore => {}
                ByteRange::Unsatisfiable => {
                    status = StatusCode::RANGE_NOT_SATISFIABLE;
                    content_range = Some(format!("bytes */{length}"));
                }
                ByteRange::Partial(start, end) => {
                    status = StatusCode::PARTIAL_CONTENT;
                    range = Some((start, end));
                    content_range = Some(format!("bytes {start}-{end}/{length}"));
                }
            }
        }
    }
    let response_length = match range {
        Some((start, end)) => end - start + 1,
        None if status == StatusCode::OK => length,
        None => 0,
    };
    let body = if parts.method == axum::http::Method::HEAD
        || !matches!(status, StatusCode::OK | StatusCode::PARTIAL_CONTENT)
    {
        Body::empty()
    } else if let Some((start, end)) = range {
        capped_body(content.slice(start..end + 1), fast_path_permit)
    } else {
        capped_body(content.clone(), fast_path_permit)
    };
    let mut builder = AxumResponse::builder()
        .status(status)
        .header(header::CONTENT_TYPE, policy.content_type.clone())
        .header(header::ETAG, policy.etag.clone())
        .header(header::CACHE_CONTROL, policy.cache_control)
        .header(header::ACCEPT_RANGES, "bytes");
    builder = apply_security_headers(builder, security_headers);
    builder = apply_build_headers(builder, build_headers);
    if status != StatusCode::NOT_MODIFIED {
        builder = builder.header(header::CONTENT_LENGTH, response_length.to_string());
    }
    if let Some(content_range) = content_range {
        builder = builder.header(header::CONTENT_RANGE, content_range);
    }
    if policy.vary {
        builder = builder.header(header::VARY, "Cookie, Authorization");
    }
    if let Some(stale_time) = policy.stale_time {
        builder = builder.header(PREFETCH_STALE_TIME_HEADER, stale_time.to_string());
    }
    builder.body(body).expect("static response is valid")
}

// Follow the Python adapter's strict single-pass path decoding. Invalid paths
// fall back to that adapter, which returns the normal 400 response.
fn static_path(raw: &str) -> Option<String> {
    let input = raw.as_bytes();
    let mut output = Vec::with_capacity(input.len());
    let mut i = 0;
    while i < input.len() {
        if input[i] == b'%' {
            let high = (*input.get(i + 1)? as char).to_digit(16)?;
            let low = (*input.get(i + 2)? as char).to_digit(16)?;
            let value = ((high << 4) | low) as u8;
            if matches!(value, b'/' | b'\\' | 0) {
                return None;
            }
            output.push(value);
            i += 3;
        } else {
            output.push(input[i]);
            i += 1;
        }
    }
    let path = String::from_utf8(output).ok()?;
    if !path.starts_with('/')
        || path.contains("//")
        || path.contains('\\')
        || path.chars().any(|c| (c as u32) < 32 || c == '\u{7f}')
        || path.split('/').any(|part| part == "." || part == "..")
    {
        return None;
    }
    Some(path)
}

fn plain_response(
    status: StatusCode,
    body: &'static str,
    security_headers: &[(Vec<u8>, Vec<u8>)],
) -> AxumResponse<Body> {
    apply_security_headers(
        AxumResponse::builder()
            .status(status)
            .header("content-type", "text/plain; charset=utf-8"),
        security_headers,
    )
    .body(Body::from(body))
    .expect("static response is valid")
}

/// `Retry-After` for a request that waited the full slot-acquire timeout
/// without getting a slot: whole seconds, at least 1. The queue was full
/// for the whole configured wait, so telling the client to retry sooner
/// would most likely hit the same wall.
fn retry_after_for_timeout(timeout: Duration) -> u64 {
    (timeout.as_millis() as u64).div_ceil(1000).max(1)
}

/// Exponentially weighted moving average of execution-slot hold times
/// (C-P1-5). One sample per completed request: the time from slot
/// acquisition to slot release — handler execution for buffered
/// responses, the whole stream lifetime for streaming ones.
struct HoldTimeStats {
    /// EWMA in milliseconds; `None` until the first request completes.
    ewma_ms: Mutex<Option<f64>>,
}

impl HoldTimeStats {
    fn new() -> Self {
        Self {
            ewma_ms: Mutex::new(None),
        }
    }

    fn record(&self, hold: Duration) {
        let sample = hold.as_secs_f64() * 1000.0;
        let mut guard = self
            .ewma_ms
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *guard = Some(match *guard {
            Some(ewma) => 0.8 * ewma + 0.2 * sample,
            None => sample,
        });
    }

    fn ewma_ms(&self) -> Option<f64> {
        *self
            .ewma_ms
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

/// Effective slot-acquire timeout for one waiter (C-P1-5).
///
/// Without a ceiling (`None`) the timeout stays fixed at `base` — the
/// historical behaviour. With a ceiling, the wait adapts to the
/// observed slot-hold time: twice the EWMA, so a waiter survives
/// roughly one full slot turnover, clamped to `[base, max]` so the
/// wait stays bounded (each waiter can hold a fully buffered body).
/// Until the first request completes there is no EWMA and the base
/// applies.
fn effective_acquire_timeout(
    base: Duration,
    max: Option<Duration>,
    ewma_ms: Option<f64>,
) -> Duration {
    let Some(max) = max else {
        return base;
    };
    let Some(ewma_ms) = ewma_ms else {
        return base;
    };
    Duration::from_secs_f64(ewma_ms / 1000.0 * 2.0).clamp(base, max)
}

fn busy_response(
    retry_after_secs: u64,
    security_headers: &[(Vec<u8>, Vec<u8>)],
) -> AxumResponse<Body> {
    apply_security_headers(
        AxumResponse::builder()
            .status(StatusCode::SERVICE_UNAVAILABLE)
            .header("content-type", "text/plain; charset=utf-8")
            .header("retry-after", retry_after_secs.to_string()),
        security_headers,
    )
    .body(Body::from("Server Busy"))
    .expect("busy response is valid")
}

/// Control characters (CTL) in a header name or value (S-P1-7).
///
/// The same rule on both sides, matching aiohttp's `_safe_header` /
/// `_write_str_raise_on_nlcr` (RFC 9110 §5.5, RFC 9112 §4): a header
/// value may not contain `0x00-0x08`, `0x0A-0x1F` or `0x7F` (HTAB
/// `0x09` is a legal field-value byte); a header name may not contain
/// any byte below `0x20` or `0x7F` at all. A CTL in a header value is
/// the classic response-splitting / request-smuggling primitive
/// (CVE-2023-37276, CVE-2026-34519/34520), so the request parser
/// rejects the request with 400 (see [`dispatch`]) and the response
/// writer rejects the response (see [`bridge_response_header`]).
fn header_value_contains_ctl(value: &[u8]) -> bool {
    value
        .iter()
        .any(|b| matches!(b, 0x00..=0x08 | 0x0A..=0x1F | 0x7F))
}

fn header_name_contains_ctl(name: &[u8]) -> bool {
    name.iter().any(|b| *b < 0x20 || *b == 0x7F)
}

fn headers_contain_ctl(headers: &HeaderMap) -> bool {
    headers.iter().any(|(name, value)| {
        header_name_contains_ctl(name.as_str().as_bytes())
            || header_value_contains_ctl(value.as_bytes())
    })
}

/// Validate one response header the Python bridge hands back, before it
/// is written to the wire (S-P1-7, writer side). Returns `None` when
/// the name/value cannot go on the wire — in particular when either
/// carries a control character, under the same rule as
/// [`headers_contain_ctl`]. The caller turns `None` into a 500: the app
/// set a header the wire format cannot carry, so the response is
/// rejected whole instead of letting hyper fail the write mid-response.
fn bridge_response_header(key: &[u8], value: &[u8]) -> Option<(HeaderName, HeaderValue)> {
    if header_name_contains_ctl(key) || header_value_contains_ctl(value) {
        return None;
    }
    Some((
        HeaderName::from_bytes(key).ok()?,
        HeaderValue::from_bytes(value).ok()?,
    ))
}

/// Conflicting `Content-Length` headers must fail as a clean 400 in dispatch
/// instead of reaching hyper as a protocol error that just drops the
/// connection. Identical duplicates are one value and pass; anything
/// unparsable or disagreeing is a conflict.
///
/// This check is a backstop, not the deciding layer in the cases the tests
/// cover: for an ordinary request hyper's parser turns conflicting
/// `Content-Length` values into an automatic 400 before dispatch ever runs.
/// Keep the check anyway: it is the layer that catches conflicting values
/// if hyper's parsing ever lets them through.
fn conflicting_content_length(headers: &HeaderMap) -> bool {
    let mut values = std::collections::HashSet::new();
    for value in headers.get_all(header::CONTENT_LENGTH).iter() {
        let Ok(text) = value.to_str() else {
            return true;
        };
        for part in text.split(',') {
            let Ok(length) = part.trim().parse::<u64>() else {
                return true;
            };
            values.insert(length);
        }
    }
    values.len() > 1
}

/// Host header validation (S-P1-19, M-9; Django `ALLOWED_HOSTS`
/// semantics). Python uses the Host for same-origin decisions and for
/// building absolute URLs (password-reset mail links and the like), so
/// an unvalidated Host is an injection vector. The check runs at the
/// top of `dispatch`, before the WebSocket branch, the fast path and
/// the Python bridge, so it covers every transport the native server
/// serves. A missing or disallowed Host is a 400; the allowlist is
/// configured through `allowed_hosts` (`pynext.config`), defaulting
/// to loopback only.
enum HostPattern {
    /// `*`: any host.
    Any,
    /// An exact hostname or IP literal, lowercased.
    Exact(String),
    /// `.example.com`: the domain itself and every subdomain.
    DomainAndSubdomains(String),
    /// `*.example.com`: subdomains only, not the bare domain.
    Subdomains(String),
}

/// The default allowlist, matching `pynext.config.DEFAULT_ALLOWED_HOSTS`.
fn default_host_patterns() -> Vec<HostPattern> {
    vec![
        HostPattern::Exact("localhost".to_string()),
        HostPattern::Exact("127.0.0.1".to_string()),
        HostPattern::Exact("::1".to_string()),
    ]
}

/// Compile one `allowed_hosts` entry. Entries are bare hosts — no
/// scheme, no port (the request Host's port is stripped at match time),
/// no path or query — mirroring the Python validator in
/// `pynext.config._validate_allowed_host`, so a direct `NativeServer`
/// caller cannot smuggle in a pattern Python would have rejected.
fn parse_host_pattern(entry: &str) -> Result<HostPattern, String> {
    let invalid = || format!("invalid allowed host {entry:?}");
    let lowered = entry.to_ascii_lowercase();
    if lowered == "*" {
        return Ok(HostPattern::Any);
    }
    if lowered.is_empty()
        || lowered.contains("://")
        || lowered
            .chars()
            .any(|c| c.is_whitespace() || matches!(c, '/' | '?' | '#' | '@'))
    {
        return Err(invalid());
    }
    let (suffix, body) = if let Some(rest) = lowered.strip_prefix("*.") {
        (true, rest)
    } else if let Some(rest) = lowered.strip_prefix('.') {
        (true, rest)
    } else {
        (false, lowered.as_str())
    };
    if body.contains('*') {
        return Err(invalid());
    }
    let (bracketed, body) = if body.starts_with('[') && body.ends_with(']') {
        (true, &body[1..body.len() - 1])
    } else {
        (false, body)
    };
    if body.is_empty() || body.len() > 253 {
        return Err(invalid());
    }
    // An IP literal (v4 or v6) is exact only: `.127.0.0.1` names no
    // subdomain tree. Anything with a colon that is not a valid IP is
    // a port (or malformed) and is rejected. A bracketed name that is
    // not an IP literal is rejected too.
    if bracketed && body.parse::<std::net::IpAddr>().is_err() {
        return Err(invalid());
    }
    if body.parse::<std::net::IpAddr>().is_ok() {
        if suffix {
            return Err(invalid());
        }
        return Ok(HostPattern::Exact(body.to_string()));
    }
    if body.contains(':') {
        return Err(invalid());
    }
    if !body.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && label
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
            && !label.starts_with('-')
            && !label.ends_with('-')
    }) {
        return Err(invalid());
    }
    let suffix_text = format!(".{body}");
    if suffix {
        if lowered.starts_with("*.") {
            Ok(HostPattern::Subdomains(suffix_text))
        } else {
            Ok(HostPattern::DomainAndSubdomains(suffix_text))
        }
    } else {
        Ok(HostPattern::Exact(body.to_string()))
    }
}

fn parse_host_patterns(entries: Vec<String>) -> Result<Vec<HostPattern>, String> {
    entries
        .iter()
        .map(|entry| parse_host_pattern(entry))
        .collect()
}

/// Strip a port from a Host header value and return the bare host,
/// lowercased, or `None` when the value is not a usable host. A
/// trailing dot is stripped (`example.com.` is `example.com`).
/// Bracketed IPv6 (`[::1]:3000`) and bare IPv6 (`::1`, which carries
/// no port) are handled; a single `host:port` pair loses the port.
fn bare_host(value: &str) -> Option<String> {
    let value = value.trim();
    if value.is_empty()
        || value
            .chars()
            .any(|c| c.is_whitespace() || c.is_control() || matches!(c, '/' | '?' | '#' | '@'))
    {
        return None;
    }
    let mut host = if let Some(rest) = value.strip_prefix('[') {
        let (inside, after) = rest.split_once(']')?;
        if !after.is_empty() && !after.starts_with(':') {
            return None;
        }
        inside.to_string()
    } else if value.matches(':').count() > 1 {
        // A bare IPv6 literal: no port can be attached to this form.
        value.to_string()
    } else if let Some((before, port)) = value.split_once(':') {
        if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        before.to_string()
    } else {
        value.to_string()
    };
    if host.ends_with('.') {
        host.pop();
    }
    if host.is_empty() {
        return None;
    }
    Some(host.to_ascii_lowercase())
}

fn host_matches(host: &str, pattern: &HostPattern) -> bool {
    match pattern {
        HostPattern::Any => true,
        HostPattern::Exact(exact) => host == exact,
        HostPattern::DomainAndSubdomains(suffix) => {
            host == &suffix[1..] || host.ends_with(suffix.as_str())
        }
        HostPattern::Subdomains(suffix) => host.ends_with(suffix.as_str()),
    }
}

/// The request Host must be present and on the allowlist. Missing is
/// 400 (HTTP/1.1 requests without Host are already rejected by hyper
/// before dispatch; this is the layer that catches HTTP/1.0 and any
/// other form hyper lets through). h2c carries no Host header: the
/// `:authority` pseudo-header (exposed as the request URI authority)
/// stands in for it, so it is the fallback source when the header is
/// absent.
fn host_header_allowed(
    headers: &HeaderMap,
    authority: Option<&str>,
    patterns: &[HostPattern],
) -> bool {
    let text = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .or(authority);
    let Some(text) = text else {
        return false;
    };
    let Some(host) = bare_host(text) else {
        return false;
    };
    patterns.iter().any(|pattern| host_matches(&host, pattern))
}
/// Marker header the first-request sniff (`SmugglingGuardIo`) inserts when
/// a request carries both `Content-Length` and `Transfer-Encoding`
/// (S-P1-31, Sanic CVE-2026-85078). hyper strips `Content-Length` from the
/// header map as soon as `Transfer-Encoding` is present (it removes earlier
/// CL headers and skips later ones), so by the time dispatch runs the
/// combination is invisible — the marker is the only trace of it. The
/// name is in Python's `INTERNAL_HEADERS`, so a client-sent copy is
/// stripped at both entry points before any handler sees it; on the native
/// path the sniff is the only producer, and a client that sends the marker
/// itself only gets its own request rejected.
const SMUGGLING_MARKER_NAME: &str = "x-pynext-smuggling";
/// Value the sniff inserts for a `Content-Length` + `Transfer-Encoding`
/// combination.
const SMUGGLING_MARKER_CL_TE: &str = "cl-te";

fn has_smuggling_marker(headers: &HeaderMap) -> bool {
    headers
        .get(SMUGGLING_MARKER_NAME)
        .is_some_and(|v| v.as_bytes() == SMUGGLING_MARKER_CL_TE.as_bytes())
}

/// A `Transfer-Encoding` whose final coding is not `chunked` is a 400
/// (RFC 9112 section 6.1: the message body length cannot be determined
/// reliably otherwise). hyper already rejects this at parse time, so this
/// is a backstop mirroring hyper's own rule — only the last header line's
/// last comma-separated coding decides — in case hyper's parsing ever lets
/// one through. `Transfer-Encoding` on HTTP/1.0 is likewise invalid.
fn invalid_transfer_encoding(headers: &HeaderMap, version: Version) -> bool {
    if !headers.contains_key(header::TRANSFER_ENCODING) {
        return false;
    }
    if version == Version::HTTP_10 {
        return true;
    }
    let Some(last) = headers
        .get_all(header::TRANSFER_ENCODING)
        .iter()
        .next_back()
    else {
        return false;
    };
    let Ok(text) = last.to_str() else {
        return true;
    };
    !text
        .rsplit(',')
        .next()
        .is_some_and(|coding| coding.trim().eq_ignore_ascii_case("chunked"))
}

/// Request-smuggling framing (S-P1-31): ambiguous framing is a 400 with
/// `Connection: close`, never a request the handler runs. Covers the
/// sniff marker (`Content-Length` + `Transfer-Encoding`), conflicting
/// `Content-Length` values, and a non-`chunked` `Transfer-Encoding`.
fn smuggling_framing(headers: &HeaderMap, version: Version) -> bool {
    has_smuggling_marker(headers)
        || conflicting_content_length(headers)
        || invalid_transfer_encoding(headers, version)
}

enum BodyReadError {
    TooLarge,
    BadRequest,
    // The body arrived slower than `body_min_rate` on average after the
    // grace window: a slow client holding a body slot, not a malformed
    // request.
    TooSlow,
}

/// A body that is still arriving after this grace window must average at
/// least `body_min_rate` bytes/second, re-checked every 250 ms while the
/// read is stalled. The check only runs while the body is incomplete, so a
/// body that finished arriving is never rejected for being slow after the
/// fact.
const BODY_MIN_RATE_GRACE: Duration = Duration::from_secs(2);
const BODY_MIN_RATE_CHECK_INTERVAL: Duration = Duration::from_millis(250);

/// Read a request body with our own byte count instead of classifying the
/// error `to_bytes` returns: that classification downcasts through
/// `error.source()`, so it breaks if axum/http-body-util ever changes how
/// the limit error is wrapped. A body that grows past the limit is 413 no
/// matter how the underlying error is shaped.
async fn read_limited_body(
    mut body: Body,
    limit: usize,
    min_rate: usize,
) -> Result<Bytes, BodyReadError> {
    let mut buf = Vec::new();
    let started = tokio::time::Instant::now();
    loop {
        let frame = tokio::select! {
            frame = body.frame() => frame,
            _ = tokio::time::sleep(BODY_MIN_RATE_CHECK_INTERVAL) => {
                // Stalled mid-body: enforce the minimum rate now instead of
                // waiting for the whole-body timeout to free the slot.
                if min_rate > 0
                    && started.elapsed() >= BODY_MIN_RATE_GRACE
                    && (buf.len() as f64) < min_rate as f64 * started.elapsed().as_secs_f64()
                {
                    return Err(BodyReadError::TooSlow);
                }
                continue;
            }
        };
        let Some(frame) = frame else { break };
        let frame = frame.map_err(|_| BodyReadError::BadRequest)?;
        // Chunk trailers (S-P1-31, Sanic CVE-2026-85078): hyper delivers
        // any trailer fields after the final 0-chunk as a trailers frame.
        // A trailer must never be silently accepted — in the CVE the
        // trailer bytes were left in the keep-alive buffer and re-parsed
        // as a smuggled request — so any trailer frame is a 400. The
        // caller closes the connection (`close_with_unread_body`), so the
        // bytes can never be reused as another request. A plain
        // `0\r\n\r\n` terminator carries no trailer frame, so ordinary
        // chunked requests are unaffected.
        if frame.is_trailers() {
            return Err(BodyReadError::BadRequest);
        }
        if let Ok(data) = frame.into_data() {
            if buf.len() + data.len() > limit {
                return Err(BodyReadError::TooLarge);
            }
            buf.extend_from_slice(&data);
            // S-P1-15: cumulative rate check on every frame, not only
            // while stalled. The stall branch below never fires for a
            // client that sends 1 byte every 250 ms, because each byte
            // arrives just before the 250 ms check would run; checking
            // here closes that trickle bypass. The grace window still
            // protects short bodies and slow starts.
            if min_rate > 0
                && started.elapsed() >= BODY_MIN_RATE_GRACE
                && (buf.len() as f64) < min_rate as f64 * started.elapsed().as_secs_f64()
            {
                return Err(BodyReadError::TooSlow);
            }
        }
    }
    Ok(Bytes::from(buf))
}

// HTTP/1 connections with an unread body must not be reused for another request.
fn close_with_unread_body(
    mut response: AxumResponse<Body>,
    version: Version,
) -> AxumResponse<Body> {
    if matches!(version, Version::HTTP_10 | Version::HTTP_11) {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
    }
    response
}

enum WsOutgoing {
    Accept,
    Text(String),
    Bytes(Vec<u8>),
    Close(u16, String),
    Done,
}

enum WsPush {
    Text(String),
    Bytes(Vec<u8>),
    None,
}

/// Run one short Python call on a blocking thread and wait for it. Used for
/// the session queue operations, which must not run on a Tokio worker thread
/// (they take the GIL and, for `_take_outgoing`, wait on the Python loop).
async fn ws_call(session: &Arc<Py<PyAny>>, method: &'static str, arg: WsPush) -> bool {
    let session = Arc::clone(session);
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            let bound = session.bind(py);
            match arg {
                WsPush::Text(text) => bound.call_method1(method, (text,)),
                WsPush::Bytes(data) => bound.call_method1(method, (data,)),
                WsPush::None => bound.call_method0(method),
            }
            .map(|value| value.extract::<bool>().unwrap_or(true))
            .unwrap_or(false)
        })
    })
    .await
    .unwrap_or(false)
}

async fn ws_is_done(session: &Arc<Py<PyAny>>) -> bool {
    let session = Arc::clone(session);
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            session
                .bind(py)
                .call_method0("_is_done")
                .and_then(|value| value.extract::<bool>())
                .unwrap_or(false)
        })
    })
    .await
    .unwrap_or(false)
}

async fn ws_cancel(session: &Arc<Py<PyAny>>) {
    ws_call(session, "_cancel", WsPush::None).await;
}

/// Take the next outgoing WebSocket item from the Python session, waiting at
/// most `timeout` for the handler to produce one. Runs on a blocking thread
/// because it waits on the Python event loop from outside it.
async fn ws_take_outgoing(session: &Arc<Py<PyAny>>, timeout: Duration) -> Option<WsOutgoing> {
    let session = Arc::clone(session);
    let millis = timeout.as_millis() as u64;
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            let item = session.bind(py).call_method1("_take_outgoing", (millis,))?;
            let item = item.extract::<Option<(
                String,
                Option<String>,
                Option<Vec<u8>>,
                Option<u16>,
                Option<String>,
            )>>()?;
            Ok::<_, PyErr>(
                item.map(|(kind, text, data, code, reason)| match kind.as_str() {
                    "text" => WsOutgoing::Text(text.unwrap_or_default()),
                    "bytes" => WsOutgoing::Bytes(data.unwrap_or_default()),
                    "close" => WsOutgoing::Close(code.unwrap_or(1000), reason.unwrap_or_default()),
                    "done" => WsOutgoing::Done,
                    _ => WsOutgoing::Accept,
                }),
            )
        })
        .unwrap_or(None)
    })
    .await
    .ok()
    .flatten()
}

fn ws_close_frame(code: u16, reason: &str) -> Message {
    Message::Close(Some(CloseFrame {
        code,
        reason: reason.into(),
    }))
}

/// Grace period for a pong after the server pings an idle WebSocket
/// session (S-P1-16): a peer that never answers is half-open, so the
/// session is closed instead of held forever.
const WS_PONG_TIMEOUT: Duration = Duration::from_secs(10);

/// Drive one upgraded WebSocket connection: forward frames to the Python
/// handler's session inbox and forward the handler's outbox items as frames.
/// The Python handler owns the application logic; this task only translates
/// between the socket and the session queues. It ends when the peer closes,
/// the handler finishes, or either side fails.
async fn run_ws_session(
    state: Arc<ServerState>,
    path: String,
    query: Vec<u8>,
    headers: Vec<(Vec<u8>, Vec<u8>)>,
    protocol: Option<String>,
    mut socket: WebSocket,
) {
    let Some(ws_callback) =
        Python::attach(|py| state.ws_callback.as_ref().map(|cb| cb.clone_ref(py)))
    else {
        let _ = socket
            .send(ws_close_frame(1011, "websocket setup failed"))
            .await;
        return;
    };
    let session = tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            let session = ws_callback
                .bind(py)
                .call1((path, query, headers, protocol))?;
            Ok::<_, PyErr>(session.unbind())
        })
    })
    .await;
    let session = match session {
        Ok(Ok(session)) => Arc::new(session),
        _ => {
            let _ = socket
                .send(ws_close_frame(1011, "websocket setup failed"))
                .await;
            return;
        }
    };
    let mut sent_close = false;
    // S-P1-16: idle timeout, maximum lifetime, and ping/pong liveness.
    // `last_activity` moves on any socket frame (including ping/pong)
    // and on any frame sent to the peer; the 5s outgoing wait returning
    // empty does not count as activity.
    let started = tokio::time::Instant::now();
    let mut last_activity = tokio::time::Instant::now();
    let mut awaiting_pong_since: Option<tokio::time::Instant> = None;
    // Hold one outgoing wait across loop iterations. Recreating the wait
    // inside `select!` would drop it whenever a socket message wins, but
    // the dropped wait's blocking task keeps waiting on the outbox and can
    // steal the next item. The wait has a bounded timeout as a safety net;
    // the Python side also enqueues a "done" item when the handler ends,
    // so a finished handler is noticed promptly.
    let mut outgoing_wait = Box::pin(ws_take_outgoing(&session, Duration::from_secs(5)));
    loop {
        // S-P1-16: earliest of the idle deadline, the lifetime
        // deadline, the next ping, and the pong grace expiry.
        // Recomputed every iteration because activity moves the
        // idle/ping deadlines.
        let idle_deadline = if state.ws_idle_timeout.is_zero() {
            None
        } else {
            Some(last_activity + state.ws_idle_timeout)
        };
        let max_deadline = if state.ws_max_lifetime.is_zero() {
            None
        } else {
            Some(started + state.ws_max_lifetime)
        };
        let ping_deadline = if state.ws_ping_interval.is_zero() {
            None
        } else {
            Some(last_activity + state.ws_ping_interval)
        };
        let pong_deadline = awaiting_pong_since.map(|since| since + WS_PONG_TIMEOUT);
        let timer_deadline = [idle_deadline, max_deadline, ping_deadline, pong_deadline]
            .into_iter()
            .flatten()
            .min();
        tokio::select! {
            message = socket.recv() => {
                match message {
                    Some(Ok(Message::Text(text))) => {
                        last_activity = tokio::time::Instant::now();
                        awaiting_pong_since = None;
                        if text.len() > state.websocket_max_message {
                            let _ = socket.send(ws_close_frame(1009, "message too large")).await;
                            sent_close = true;
                            ws_call(&session, "_push_disconnect", WsPush::None).await;
                            break;
                        }
                        if !ws_call(&session, "_push_text", WsPush::Text(text.to_string())).await {
                            let _ = socket.send(ws_close_frame(1013, "receiver overloaded")).await;
                            sent_close = true;
                            break;
                        }
                    }
                    Some(Ok(Message::Binary(data))) => {
                        last_activity = tokio::time::Instant::now();
                        awaiting_pong_since = None;
                        if data.len() > state.websocket_max_message {
                            let _ = socket.send(ws_close_frame(1009, "message too large")).await;
                            sent_close = true;
                            ws_call(&session, "_push_disconnect", WsPush::None).await;
                            break;
                        }
                        if !ws_call(&session, "_push_bytes", WsPush::Bytes(data.to_vec())).await {
                            let _ = socket.send(ws_close_frame(1013, "receiver overloaded")).await;
                            sent_close = true;
                            break;
                        }
                    }
                    Some(Ok(Message::Close(_frame))) => {
                        // Tungstenite queues an automatic close reply when
                        // a close frame arrives; SinkExt::close flushes it.
                        // Sending a second close frame here would fail and
                        // skip that flush, so the peer would see EOF.
                        let _ = socket.close().await;
                        sent_close = true;
                        ws_call(&session, "_push_disconnect", WsPush::None).await;
                        break;
                    }
                    Some(Ok(Message::Ping(data))) => {
                        last_activity = tokio::time::Instant::now();
                        awaiting_pong_since = None;
                        let _ = socket.send(Message::Pong(data)).await;
                    }
                    Some(Ok(Message::Pong(_))) => {
                        last_activity = tokio::time::Instant::now();
                        awaiting_pong_since = None;
                    }
                    Some(Err(_)) | None => {
                        // Tungstenite rejects an over-limit message at the
                        // protocol level before it is delivered, so the
                        // length check above never sees it: answer with
                        // 1009 here. For other errors the peer is usually
                        // gone and this send is best-effort.
                        let _ = socket.send(ws_close_frame(1009, "message too large")).await;
                        sent_close = true;
                        ws_call(&session, "_push_disconnect", WsPush::None).await;
                        break;
                    }
                }
            }
            outgoing = &mut outgoing_wait => {
                match outgoing {
                    Some(WsOutgoing::Accept) => {}
                    Some(WsOutgoing::Text(text)) => {
                        let sent = socket.send(Message::Text(text.into())).await;
                        if sent.is_err() {
                            break;
                        }
                        last_activity = tokio::time::Instant::now();
                    }
                    Some(WsOutgoing::Bytes(data)) => {
                        if socket.send(Message::Binary(data.into())).await.is_err() {
                            break;
                        }
                        last_activity = tokio::time::Instant::now();
                    }
                    Some(WsOutgoing::Close(code, reason)) => {
                        let _ = socket.send(ws_close_frame(code, &reason)).await;
                        sent_close = true;
                        break;
                    }
                    Some(WsOutgoing::Done) => {
                        break;
                    }
                    None => {
                        if ws_is_done(&session).await {
                            break;
                        }
                    }
                }
                outgoing_wait = Box::pin(ws_take_outgoing(&session, Duration::from_secs(5)));
            }
            // S-P1-16: idle timeout, maximum lifetime, ping/pong.
            _ = async {
                match timer_deadline {
                    Some(deadline) => tokio::time::sleep_until(deadline).await,
                    None => std::future::pending().await,
                }
            } => {
                let now = tokio::time::Instant::now();
                if let Some(deadline) = max_deadline {
                    if now >= deadline {
                        let _ = socket
                            .send(ws_close_frame(1000, "session lifetime exceeded"))
                            .await;
                        sent_close = true;
                        ws_call(&session, "_push_disconnect", WsPush::None).await;
                        break;
                    }
                }
                if let Some(since) = awaiting_pong_since {
                    if now >= since + WS_PONG_TIMEOUT {
                        let _ = socket
                            .send(ws_close_frame(1011, "ping timeout"))
                            .await;
                        sent_close = true;
                        ws_call(&session, "_push_disconnect", WsPush::None).await;
                        break;
                    }
                }
                if let Some(deadline) = idle_deadline {
                    if now >= deadline {
                        let _ = socket
                            .send(ws_close_frame(1000, "idle timeout"))
                            .await;
                        sent_close = true;
                        ws_call(&session, "_push_disconnect", WsPush::None).await;
                        break;
                    }
                }
                if let Some(deadline) = ping_deadline {
                    if now >= deadline && awaiting_pong_since.is_none() {
                        if socket.send(Message::Ping(Bytes::new())).await.is_err() {
                            break;
                        }
                        awaiting_pong_since = Some(now);
                    }
                }
            }
        }
    }
    ws_cancel(&session).await;
    if !sent_close {
        let _ = socket.send(ws_close_frame(1000, "closing")).await;
    }
}

async fn rate_limit_denial(
    state: &Arc<ServerState>,
    parts: &axum::http::request::Parts,
    path: String,
    is_websocket: bool,
) -> Option<AxumResponse<Body>> {
    let callback = if is_websocket {
        &state.ws_rate_limit_callback
    } else {
        &state.http_rate_limit_callback
    };
    callback.as_ref()?;
    // `path` moves into the callback task below, so merge its headers now.
    let merged_path_headers = path_headers_for(&path, &state.path_headers);
    let unavailable = || {
        let mut response = plain_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "Service Unavailable",
            &state.security_headers,
        );
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
        response
    };
    let Ok(permit) = Arc::clone(&state.rate_limit_capacity).try_acquire_owned() else {
        // H-2: no decision capacity is left. Failing closed here turns
        // the protection into a rejection device under load, so fail
        // open and count it instead of 503ing a request the limiter
        // would have allowed.
        state.rate_limit_fail_open.fetch_add(1, Ordering::Relaxed);
        return None;
    };
    let headers: Vec<(Vec<u8>, Vec<u8>)> = parts
        .headers
        .iter()
        .map(|(name, value)| (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec()))
        .collect();
    let peer = parts
        .extensions
        .get::<PeerAddr>()
        .map(|addr| addr.0.ip().to_string());
    let rate_state = Arc::clone(state);
    let decision = tokio::time::timeout(
        Duration::from_secs(2),
        tokio::task::spawn_blocking(move || {
            // A timed-out blocking task retains its permit until it exits.
            let _permit = permit;
            Python::attach(|py| -> PyResult<(bool, Option<u64>)> {
                let raw_headers = PyList::empty(py);
                for (name, value) in &headers {
                    raw_headers.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
                }
                let callback = if is_websocket {
                    &rate_state.ws_rate_limit_callback
                } else {
                    &rate_state.http_rate_limit_callback
                };
                callback
                    .as_ref()
                    .expect("callback checked above")
                    .bind(py)
                    .call1((path, raw_headers, peer))?
                    .extract()
            })
        }),
    )
    .await;
    match decision {
        Ok(Ok(Ok((true, _)))) => None,
        Ok(Ok(Ok((false, retry_after)))) => {
            let mut response = plain_response(
                StatusCode::TOO_MANY_REQUESTS,
                "Too Many Requests",
                &state.security_headers,
            );
            if let Ok(value) = HeaderValue::from_str(&retry_after.unwrap_or(1).max(1).to_string()) {
                response.headers_mut().insert(header::RETRY_AFTER, value);
            }
            Some(apply_merged_path_headers(response, &merged_path_headers))
        }
        // H-2: the 2s decision deadline expired (Err) or the blocking
        // task failed to spawn (Ok(Err)), so no decision exists. Fail
        // open and count it. A callback that raised — including a
        // limiter backend reporting itself unavailable, whose own
        // failure policy decides — or a malformed decision still fails
        // closed below.
        Ok(Err(_)) | Err(_) => {
            state.rate_limit_fail_open.fetch_add(1, Ordering::Relaxed);
            None
        }
        _ => Some(apply_merged_path_headers(
            unavailable(),
            &merged_path_headers,
        )),
    }
}

async fn dispatch(State(state): State<Arc<ServerState>>, request: Request) -> AxumResponse<Body> {
    let (parts, body) = request.into_parts();
    // Host validation (S-P1-19, M-9) runs first, before the WebSocket
    // branch, the fast path and the Python bridge: no request reaches
    // any handler with a missing or disallowed Host.
    if !host_header_allowed(
        &parts.headers,
        parts.uri.authority().map(|authority| authority.as_str()),
        &state.allowed_hosts,
    ) {
        return close_with_unread_body(
            apply_path_headers_for_raw_path(
                plain_response(
                    StatusCode::BAD_REQUEST,
                    "Bad Request",
                    &state.security_headers,
                ),
                parts.uri.path(),
                &state.path_headers,
            ),
            parts.version,
        );
    }
    // S-P1-7 (parser side): a control character in a request header
    // name or value is a clean 400, mirroring the conflicting
    // Content-Length check below. hyper's parser already rejects most
    // CTL on the wire; this is the explicit layer that also covers any
    // header reaching dispatch through another path, and keeps the
    // parser rule identical to the writer rule (see
    // `bridge_response_header`) and to aiohttp's `_safe_header`.
    if headers_contain_ctl(&parts.headers) {
        return close_with_unread_body(
            apply_path_headers_for_raw_path(
                plain_response(
                    StatusCode::BAD_REQUEST,
                    "Bad Request",
                    &state.security_headers,
                ),
                parts.uri.path(),
                &state.path_headers,
            ),
            parts.version,
        );
    }
    // Request-smuggling framing (S-P1-31) runs before anything else,
    // including the WebSocket upgrade below: an ambiguously framed
    // request is a 400 with `Connection: close`, never an upgrade or a
    // handler run. The body has not been read yet, so the connection
    // must not be reused.
    if smuggling_framing(&parts.headers, parts.version) {
        return close_with_unread_body(
            apply_path_headers_for_raw_path(
                plain_response(
                    StatusCode::BAD_REQUEST,
                    "Bad Request",
                    &state.security_headers,
                ),
                parts.uri.path(),
                &state.path_headers,
            ),
            parts.version,
        );
    }
    // A WebSocket upgrade is decided here, before any body handling: the
    // upgrade handshake must not compete for a Python execution slot, and a
    // request that is not a WebSocket route must not reach the HTTP bridge
    // (which would answer a handshake with an unrelated HTTP response).
    let upgrade_headers = parts.method == axum::http::Method::GET
        && parts.headers.get(header::CONNECTION).is_some_and(|v| {
            v.to_str()
                .unwrap_or_default()
                .split(',')
                .any(|token| token.trim().eq_ignore_ascii_case("upgrade"))
        })
        && parts
            .headers
            .get(header::UPGRADE)
            .is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"websocket"));
    if upgrade_headers {
        let Some(path) = static_path(parts.uri.path()) else {
            return plain_response(StatusCode::NOT_FOUND, "Not Found", &state.security_headers);
        };
        let Some((pattern, _, _)) = state.websocket_router.matches(&path) else {
            return plain_response(StatusCode::NOT_FOUND, "Not Found", &state.security_headers);
        };
        if !state.websocket_paths.contains(&pattern) {
            return plain_response(StatusCode::NOT_FOUND, "Not Found", &state.security_headers);
        }
        if let Some(response) = rate_limit_denial(&state, &parts, path.clone(), true).await {
            return response;
        }
        // Origin policy must run before the HTTP 101 response. Limit the
        // Python checks separately so stalled callbacks cannot queue without
        // bound or consume the ordinary HTTP execution budget.
        let Ok(permit) = Arc::clone(&state.ws_origin_capacity).try_acquire_owned() else {
            return plain_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "Service Unavailable",
                &state.security_headers,
            );
        };
        if state.ws_origin_callback.is_none() {
            return plain_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "Service Unavailable",
                &state.security_headers,
            );
        }
        let origin_headers: Vec<(Vec<u8>, Vec<u8>)> = parts
            .headers
            .iter()
            .map(|(name, value)| (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec()))
            .collect();
        let peer = parts
            .extensions
            .get::<PeerAddr>()
            .map(|addr| addr.0.ip().to_string());
        let origin_state = Arc::clone(&state);
        let decision = tokio::time::timeout(
            Duration::from_secs(2),
            tokio::task::spawn_blocking(move || {
                // The permit stays with the task even if the timeout expires.
                let _permit = permit;
                Python::attach(|py| -> PyResult<bool> {
                    let headers = PyList::empty(py);
                    for (name, value) in &origin_headers {
                        headers.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
                    }
                    origin_state
                        .ws_origin_callback
                        .as_ref()
                        .expect("callback checked above")
                        .bind(py)
                        .call1((headers, peer))?
                        .extract()
                })
            }),
        )
        .await;
        match decision {
            Ok(Ok(Ok(true))) => {}
            Ok(Ok(Ok(false))) => {
                return plain_response(StatusCode::FORBIDDEN, "Forbidden", &state.security_headers);
            }
            _ => {
                return plain_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Service Unavailable",
                    &state.security_headers,
                );
            }
        }
        // Authorization (global middleware chain + the handler's
        // @require_auth/@require_permission policy) must run before the
        // HTTP 101 response, like the Origin policy above. The Python
        // callback returns true (allow), false (deny -> 403), or None
        // (pre-flight error -> 503, fail closed). The handshake must
        // never complete before this decision.
        let Ok(auth_permit) = Arc::clone(&state.ws_auth_capacity).try_acquire_owned() else {
            return plain_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "Service Unavailable",
                &state.security_headers,
            );
        };
        if state.ws_auth_callback.is_none() {
            return plain_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "Service Unavailable",
                &state.security_headers,
            );
        }
        let auth_path = path.clone();
        let auth_query = parts.uri.query().unwrap_or("").as_bytes().to_vec();
        let auth_headers: Vec<(Vec<u8>, Vec<u8>)> = parts
            .headers
            .iter()
            .map(|(name, value)| (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec()))
            .collect();
        let auth_peer = parts
            .extensions
            .get::<PeerAddr>()
            .map(|addr| addr.0.ip().to_string());
        let auth_state = Arc::clone(&state);
        let auth_decision = tokio::time::timeout(
            Duration::from_secs(5),
            tokio::task::spawn_blocking(move || {
                // The permit stays with the task even if the timeout expires.
                let _permit = auth_permit;
                Python::attach(|py| -> PyResult<Option<bool>> {
                    let headers = PyList::empty(py);
                    for (name, value) in &auth_headers {
                        headers.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
                    }
                    auth_state
                        .ws_auth_callback
                        .as_ref()
                        .expect("callback checked above")
                        .bind(py)
                        .call1((auth_path, PyBytes::new(py, &auth_query), headers, auth_peer))?
                        .extract()
                })
            }),
        )
        .await;
        match auth_decision {
            Ok(Ok(Ok(Some(true)))) => {}
            Ok(Ok(Ok(Some(false)))) => {
                return plain_response(StatusCode::FORBIDDEN, "Forbidden", &state.security_headers);
            }
            _ => {
                return plain_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Service Unavailable",
                    &state.security_headers,
                );
            }
        }
        let mut parts = parts;
        let upgrade = match WebSocketUpgrade::from_request_parts(&mut parts, &state).await {
            Ok(upgrade) => upgrade,
            Err(rejection) => {
                // A failed upgrade is still an HTTP response: give it
                // the same A1 security headers every other Rust-built
                // response carries instead of axum's bare rejection.
                let mut response = rejection.into_response();
                for (name, value) in &state.security_headers {
                    if let (Ok(name), Ok(value)) =
                        (HeaderName::from_bytes(name), HeaderValue::from_bytes(value))
                    {
                        if !response.headers().contains_key(&name) {
                            response.headers_mut().insert(name, value);
                        }
                    }
                }
                return response;
            }
        };
        // Bound the protocol-level message size as well as the application
        // limit enforced in run_ws_session; tungstenite otherwise allows
        // up to 64 MiB per message.
        let requested = upgrade.requested_protocols().next().is_some();
        let protocols = state
            .websocket_protocols
            .get(&pattern)
            .cloned()
            .unwrap_or_default();
        let has_protocols = !protocols.is_empty();
        let upgrade = upgrade.protocols(protocols);
        let protocol = upgrade
            .selected_protocol()
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        if has_protocols && requested && protocol.is_none() {
            return plain_response(
                StatusCode::BAD_REQUEST,
                "Unsupported WebSocket subprotocol",
                &state.security_headers,
            );
        }
        let upgrade = upgrade.max_message_size(state.websocket_max_message);
        let query = parts.uri.query().unwrap_or("").as_bytes().to_vec();
        let headers: Vec<(Vec<u8>, Vec<u8>)> = parts
            .headers
            .iter()
            .map(|(k, v)| (k.as_str().as_bytes().to_vec(), v.as_bytes().to_vec()))
            .collect();
        let state = Arc::clone(&state);
        return upgrade.on_upgrade(move |socket| async move {
            run_ws_session(state, path, query, headers, protocol, socket).await;
        });
    }
    // Decide admission from headers before polling the body. In particular,
    // an over-limit Expect: 100-continue request must never be told to upload.
    let decoded_path = static_path(parts.uri.path());
    // Health probes are reserved framework paths (H-3): they never take
    // an execution slot (see the slot acquisition below).
    let is_health = matches!(decoded_path.as_deref(), Some("/healthz" | "/readyz"));
    if state.http_rate_limit_callback.is_some() {
        let Some(path) = decoded_path.as_ref() else {
            return close_with_unread_body(
                plain_response(
                    StatusCode::BAD_REQUEST,
                    "Bad Request",
                    &state.security_headers,
                ),
                parts.version,
            );
        };
        if path != "/healthz" && path != "/readyz" {
            if let Some(mut response) = rate_limit_denial(&state, &parts, path.clone(), false).await
            {
                if parts.method == axum::http::Method::HEAD {
                    *response.body_mut() = Body::empty();
                }
                return if body.is_end_stream() {
                    response
                } else {
                    close_with_unread_body(response, parts.version)
                };
            }
        }
    }
    // Empty requests do not compete with clients currently uploading bodies.
    let body = if body.is_end_stream() {
        Bytes::new()
    } else {
        // S-P1-15: per-IP cap on body slots, checked before the global
        // semaphore so one IP rotating through uploads cannot take all
        // 16 slots and 503 legitimate uploads. IPv4-mapped IPv6 counts
        // under its IPv4 form, matching the accept loop. The guard
        // lives until the body read finishes, then releases the slot.
        let peer_ip = parts
            .extensions
            .get::<PeerAddr>()
            .map(|peer| match peer.0.ip() {
                IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
                    Some(v4) => IpAddr::V4(v4),
                    None => peer.0.ip(),
                },
                ip => ip,
            });
        let _body_ip_guard = match peer_ip {
            Some(ip) => {
                let admitted = {
                    let mut counts = state
                        .body_per_ip
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    let count = counts.entry(ip).or_insert(0);
                    if *count >= state.max_body_per_ip {
                        false
                    } else {
                        *count += 1;
                        true
                    }
                };
                if !admitted {
                    return close_with_unread_body(
                        apply_path_headers_for_raw_path(
                            busy_response(1, &state.security_headers),
                            parts.uri.path(),
                            &state.path_headers,
                        ),
                        parts.version,
                    );
                }
                Some(PerIpBodyGuard {
                    counts: Arc::clone(&state.body_per_ip),
                    ip,
                })
            }
            None => None,
        };
        let _body_permit = match state.body_capacity.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                return close_with_unread_body(
                    apply_path_headers_for_raw_path(
                        busy_response(1, &state.security_headers),
                        parts.uri.path(),
                        &state.path_headers,
                    ),
                    parts.version,
                );
            }
        };
        match tokio::time::timeout(
            state.body_timeout,
            read_limited_body(body, MAX_REQUEST_BODY, state.body_min_rate),
        )
        .await
        {
            Ok(Ok(bytes)) => bytes,
            Ok(Err(error)) => {
                let (status, message, retry_after) = match error {
                    BodyReadError::TooLarge => {
                        (StatusCode::PAYLOAD_TOO_LARGE, "Payload Too Large", false)
                    }
                    BodyReadError::BadRequest => (StatusCode::BAD_REQUEST, "Bad Request", false),
                    // A body below the minimum receive rate is a slow
                    // client, not a malformed request: 408 like the
                    // whole-body timeout, with Retry-After so a client
                    // that can go faster retries instead of hammering.
                    BodyReadError::TooSlow => {
                        (StatusCode::REQUEST_TIMEOUT, "Request Timeout", true)
                    }
                };
                let mut response = apply_path_headers_for_raw_path(
                    plain_response(status, message, &state.security_headers),
                    parts.uri.path(),
                    &state.path_headers,
                );
                if retry_after {
                    response
                        .headers_mut()
                        .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
                }
                return close_with_unread_body(response, parts.version);
            }
            Err(_) => {
                let mut response = apply_path_headers_for_raw_path(
                    plain_response(
                        StatusCode::REQUEST_TIMEOUT,
                        "Request Timeout",
                        &state.security_headers,
                    ),
                    parts.uri.path(),
                    &state.path_headers,
                );
                response
                    .headers_mut()
                    .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
                return close_with_unread_body(response, parts.version);
            }
        }
    };
    if !state.middleware_enabled
        // Origin-bearing responses, including static ranges and 304s,
        // use the same Python CORS policy as ASGI.
        && !parts.headers.contains_key(header::ORIGIN)
        && (parts.method == axum::http::Method::GET || parts.method == axum::http::Method::HEAD)
        // Health probes are reserved framework paths (B6): a public
        // asset or static page with the same name must not shadow them,
        // so they always fall through to the Python bridge.
        && !matches!(decoded_path.as_deref(), Some("/healthz" | "/readyz"))
    {
        if let Some(path) = decoded_path {
            if let Some(content) = state.client_assets.get(&path) {
                // S-P1-11 (M-1): the fast path bypasses the execution
                // semaphore, so it has its own budget. The permit is
                // only taken once a fast-path hit is confirmed — a
                // dynamic request must never be shed by this cap — and
                // exhaustion sheds with 503 + Retry-After instead of
                // falling through to the Python bridge.
                let Ok(fast_permit) = state.fast_path_capacity.clone().try_acquire_owned() else {
                    return apply_path_headers_for_raw_path(
                        busy_response(1, &state.security_headers),
                        parts.uri.path(),
                        &state.path_headers,
                    );
                };
                return apply_path_headers(
                    static_response(
                        &parts,
                        content,
                        AssetPolicy {
                            content_type: &HeaderValue::from_static(
                                "text/javascript; charset=utf-8",
                            ),
                            etag: &state.asset_etags[&path],
                            cache_control: "public, max-age=31536000, immutable",
                            vary: false,
                            stale_time: None,
                        },
                        &state.security_headers,
                        &state.build_headers,
                        Some(fast_permit),
                    ),
                    &path,
                    &state.path_headers,
                );
            }
            if let Some(asset) = state.public_assets.get(&path) {
                let Ok(fast_permit) = state.fast_path_capacity.clone().try_acquire_owned() else {
                    return apply_path_headers_for_raw_path(
                        busy_response(1, &state.security_headers),
                        parts.uri.path(),
                        &state.path_headers,
                    );
                };
                return apply_path_headers(
                    static_response(
                        &parts,
                        &asset.body,
                        AssetPolicy {
                            content_type: &asset.content_type,
                            etag: &state.asset_etags[&path],
                            cache_control: "public, max-age=0, must-revalidate",
                            vary: false,
                            stale_time: None,
                        },
                        &state.security_headers,
                        &state.build_headers,
                        Some(fast_permit),
                    ),
                    &path,
                    &state.path_headers,
                );
            }
            if let Some(content) = state.static_pages.get(&path) {
                let Ok(fast_permit) = state.fast_path_capacity.clone().try_acquire_owned() else {
                    return apply_path_headers_for_raw_path(
                        busy_response(1, &state.security_headers),
                        parts.uri.path(),
                        &state.path_headers,
                    );
                };
                return apply_path_headers(
                    static_response(
                        &parts,
                        content,
                        AssetPolicy {
                            content_type: &HeaderValue::from_static("text/html; charset=utf-8"),
                            etag: &state.asset_etags[&path],
                            cache_control: "public, max-age=0, must-revalidate",
                            vary: true,
                            stale_time: state.prefetch_stale_times.get(&path).copied(),
                        },
                        &state.security_headers,
                        &state.build_headers,
                        Some(fast_permit),
                    ),
                    &path,
                    &state.path_headers,
                );
            }
            if !state.shared_cache_enabled
                && !parts.headers.contains_key("authorization")
                && !parts.headers.contains_key("cookie")
            {
                if let CacheResult::Fresh(content) = state.isr_cache.lookup(&path) {
                    let Ok(fast_permit) = state.fast_path_capacity.clone().try_acquire_owned()
                    else {
                        return apply_path_headers_for_raw_path(
                            busy_response(1, &state.security_headers),
                            parts.uri.path(),
                            &state.path_headers,
                        );
                    };
                    let length = content.len();
                    let body = if parts.method == axum::http::Method::HEAD {
                        Body::empty()
                    } else {
                        capped_body(content, Some(fast_permit))
                    };
                    // Serve the route's policy-based SWR header (Next.js
                    // `getCacheControlHeader`); routes without an ISR
                    // policy keep the previous no-cache default.
                    let cache_control = state
                        .websocket_router
                        .matches(&path)
                        .and_then(|(pattern, _, _)| state.isr_cache_control.get(&pattern))
                        .cloned()
                        .unwrap_or_else(|| {
                            HeaderValue::from_static("public, max-age=0, must-revalidate")
                        });
                    // The fresh window still left on this entry is the
                    // prefetch stale-time hint; a sub-second remainder
                    // carries no hint rather than a "0" that reads stale.
                    let mut builder = AxumResponse::builder()
                        .status(StatusCode::OK)
                        .header("content-type", "text/html; charset=utf-8")
                        .header("content-length", length.to_string())
                        .header(header::CACHE_CONTROL, cache_control)
                        .header("vary", "Cookie, Authorization");
                    if let Some(remaining) = state.isr_cache.fresh_remaining(&path) {
                        let seconds = remaining.as_secs();
                        if seconds >= 1 {
                            builder =
                                builder.header(PREFETCH_STALE_TIME_HEADER, seconds.to_string());
                        }
                    }
                    return apply_path_headers(
                        apply_build_headers(
                            apply_security_headers(builder, &state.security_headers),
                            &state.build_headers,
                        )
                        .body(body)
                        .expect("cached response is valid"),
                        &path,
                        &state.path_headers,
                    );
                }
            }
        }
    }
    // Overload policy (roadmap C7): bounded wait, then reject. A burst
    // that clears within the acquire timeout degrades gracefully, and only
    // sustained overload gets a 503 with Retry-After. A request that finds
    // a free slot takes it immediately without consuming queue capacity;
    // only requests that must wait take a waiting permit, so `max_waiting`
    // bounds exactly the number of waiters (0: a slot must be free at
    // once). Waiting requests hold a Tokio task, not a Python worker
    // thread. Bound this queue too: each waiter can already hold a fully
    // buffered request body, so an unbounded wait would trade 503s for
    // memory exhaustion and head-of-line latency, and probabilistic early
    // shedding would drop requests that would have succeeded within the
    // timeout. The queue size is configurable via `max_waiting`; the
    // default stays `min(max_concurrent, 64)`.
    // H-3: health probes never take an execution slot. A probe flood
    // could otherwise fill the queue and 503 real requests, and a probe
    // queued behind 64 slow requests could 503 the probe itself.
    // Readiness is still decided by the Python bridge below.
    // H-5: one IP may hold at most max_concurrent_per_ip execution
    // slots. The quota is taken before the global pool, so a single IP
    // cannot fill the wait queue either; the guard releases the IP's
    // count when the request finishes (streaming included).
    let ip_guard = if is_health {
        None
    } else {
        match parts.extensions.get::<PeerAddr>().map(|peer| peer.0.ip()) {
            Some(ip) => match state.try_acquire_ip_execution(ip) {
                Some(guard) => Some(guard),
                None => {
                    return apply_path_headers_for_raw_path(
                        busy_response(1, &state.security_headers),
                        parts.uri.path(),
                        &state.path_headers,
                    )
                }
            },
            // No peer address (for example a Unix listener): nothing to
            // quota against.
            None => None,
        }
    };
    let permit = if is_health {
        None
    } else {
        Some(match state.capacity.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                let waiting = match state.waiting_capacity.clone().try_acquire_owned() {
                    Ok(permit) => permit,
                    // No waiting slot: the earliest a slot could free is
                    // unknown, so the advisory retry delay stays 1 second.
                    Err(_) => {
                        return apply_path_headers_for_raw_path(
                            busy_response(1, &state.security_headers),
                            parts.uri.path(),
                            &state.path_headers,
                        )
                    }
                };
                // Adaptive wait (C-P1-5): the effective timeout follows the
                // observed slot-hold time, so slow handlers stretch the
                // wait instead of converting latency into 503s. Fairness is
                // already FIFO: tokio's Semaphore grants permits in the
                // order they were requested, so a waiter never starves
                // behind later arrivals.
                let effective_timeout = effective_acquire_timeout(
                    state.acquire_timeout,
                    state.acquire_timeout_max,
                    state.hold_stats.ewma_ms(),
                );
                match tokio::time::timeout(
                    effective_timeout,
                    state.capacity.clone().acquire_owned(),
                )
                .await
                {
                    Ok(Ok(permit)) => {
                        drop(waiting);
                        permit
                    }
                    _ => {
                        return apply_path_headers_for_raw_path(
                            busy_response(
                                retry_after_for_timeout(effective_timeout),
                                &state.security_headers,
                            ),
                            parts.uri.path(),
                            &state.path_headers,
                        )
                    }
                }
            }
        })
    };
    // Slot-hold clock for the adaptive timeout (C-P1-5): started when
    // the permit is acquired, recorded when it is released (buffered
    // responses below, streaming ones in streaming_body, bridge errors
    // in the error arm).
    let acquired_at = Instant::now();
    let hold_stats = Arc::clone(&state.hold_stats);
    let method = parts.method.to_string();
    let path = parts.uri.path().to_string();
    let query = parts.uri.query().unwrap_or("").as_bytes().to_vec();
    let headers: Vec<(Vec<u8>, Vec<u8>)> = parts
        .headers
        .iter()
        .map(|(k, v)| (k.as_str().as_bytes().to_vec(), v.as_bytes().to_vec()))
        .collect();
    let cancellation = Arc::new(AtomicBool::new(false));
    let _cancel_on_drop = CancelOnDrop(Arc::clone(&cancellation));
    let client_ip = parts
        .extensions
        .get::<PeerAddr>()
        .map(|peer| peer.0.ip().to_string());
    let security_headers = state.security_headers.clone();
    // The bridge task moves `state`, so capture the streaming lifetime
    // cap beforehand (S-P1-14).
    let streaming_max_lifetime = state.streaming_max_lifetime;
    // The bridge task moves `state`, so merge this request's path headers
    // beforehand for the plain 500s built below; bridge responses already
    // carry their path headers from Python dispatch.
    let path_headers_for_request = static_path(&path)
        .map(|decoded| path_headers_for(&decoded, &state.path_headers))
        .unwrap_or_default();
    let result = tokio::task::spawn_blocking(move || {
        let result = Python::attach(|py| {
            let cb = state.callback.bind(py);
            let token = Py::new(
                py,
                RequestCancellation {
                    cancelled: cancellation,
                },
            )?;
            let result = cb.call1((
                method,
                path,
                PyBytes::new(py, &query),
                headers,
                PyBytes::new(py, &body),
                token,
                client_ip,
            ))?;
            let (status, headers, body) =
                result.extract::<(u16, Vec<(Vec<u8>, Vec<u8>)>, Py<PyAny>)>()?;
            let body = if body.bind(py).is_instance_of::<PyBytes>() {
                BridgeBody::Buffered(body.bind(py).extract::<Vec<u8>>()?)
            } else {
                BridgeBody::Streaming(body)
            };
            Ok::<_, PyErr>((status, headers, body))
        });
        (result, permit, ip_guard)
    })
    .await;
    match result {
        Ok((Ok((status, headers, body)), permit, ip_guard)) => {
            let Ok(status) = StatusCode::from_u16(status) else {
                if let BridgeBody::Streaming(stream) = body {
                    close_stream(Arc::new(stream));
                }
                return apply_merged_path_headers(
                    plain_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Internal Server Error",
                        &security_headers,
                    ),
                    &path_headers_for_request,
                );
            };
            let mut response = AxumResponse::new(Body::empty());
            *response.status_mut() = status;
            for (key, value) in headers {
                // S-P1-7 (writer side): a CTL in a response header name
                // or value rejects the whole response with 500 — the app
                // set a header the wire format cannot carry. A handler
                // can smuggle one past `Response`'s constructor by
                // appending to `response.headers` afterwards, so this
                // check is the last gate before serialization, mirroring
                // the ASGI-side strip in `app._send_response`.
                let Some((key, value)) = bridge_response_header(&key, &value) else {
                    if let BridgeBody::Streaming(stream) = body {
                        close_stream(Arc::new(stream));
                    }
                    return apply_merged_path_headers(
                        plain_response(
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "Internal Server Error",
                            &security_headers,
                        ),
                        &path_headers_for_request,
                    );
                };
                response.headers_mut().append(key, value);
            }
            *response.body_mut() = match body {
                BridgeBody::Buffered(body) => {
                    hold_stats.record(acquired_at.elapsed());
                    drop(permit);
                    drop(ip_guard);
                    Body::from(body)
                }
                BridgeBody::Streaming(stream) => streaming_body(
                    stream,
                    permit,
                    ip_guard,
                    hold_stats,
                    acquired_at,
                    streaming_max_lifetime,
                ),
            };
            response
        }
        _ => {
            hold_stats.record(acquired_at.elapsed());
            apply_merged_path_headers(
                plain_response(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Internal Server Error",
                    &security_headers,
                ),
                &path_headers_for_request,
            )
        }
    }
}

/// RAII guard for one entry in the per-IP body-slot count (S-P1-15):
/// created when a body intake is admitted, it decrements the count when
/// the body read ends (success, rejection, or timeout) so a slot is
/// never leaked.
struct PerIpBodyGuard {
    counts: Arc<Mutex<HashMap<IpAddr, usize>>>,
    ip: IpAddr,
}

impl Drop for PerIpBodyGuard {
    fn drop(&mut self) {
        let mut counts = self
            .counts
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(count) = counts.get_mut(&self.ip) {
            *count = count.saturating_sub(1);
            if *count == 0 {
                counts.remove(&self.ip);
            }
        }
    }
}

/// RAII guard for one entry in the per-IP connection count: created when a
/// connection is admitted, it decrements the count when the connection task
/// ends (normal close, error, or server shutdown) so a slot is never leaked.
struct PerIpConnectionGuard {
    counts: Arc<Mutex<HashMap<IpAddr, usize>>>,
    ip: IpAddr,
}

impl Drop for PerIpConnectionGuard {
    fn drop(&mut self) {
        let mut counts = self
            .counts
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(count) = counts.get_mut(&self.ip) {
            *count = count.saturating_sub(1);
            if *count == 0 {
                counts.remove(&self.ip);
            }
        }
    }
}

/// Last-activity clock for one connection (S-P1-13 / M-3): h2c has no
/// header-read timeout, so idle h2c connections would otherwise sit in
/// the connection budget forever. Time is millis since the connection
/// started, stored atomically so the socket wrapper and the watchdog task
/// can share it without a lock.
struct IdleClock {
    base: tokio::time::Instant,
    last_millis: AtomicU64,
}

impl IdleClock {
    fn new() -> Self {
        Self {
            base: tokio::time::Instant::now(),
            last_millis: AtomicU64::new(0),
        }
    }

    fn touch(&self) {
        self.last_millis
            .store(self.base.elapsed().as_millis() as u64, Ordering::Relaxed);
    }

    fn idle(&self) -> Duration {
        let elapsed_millis = self.base.elapsed().as_millis() as u64;
        Duration::from_millis(
            elapsed_millis.saturating_sub(self.last_millis.load(Ordering::Relaxed)),
        )
    }
}

/// A socket wrapper that records every successful read/write on the
/// connection's [`IdleClock`]. Hyper never reports per-connection
/// activity, so the idle watchdog in the accept loop needs this hook.
struct IdleWatchStream<S> {
    inner: S,
    clock: Arc<IdleClock>,
}

impl<S: AsyncRead + Unpin> AsyncRead for IdleWatchStream<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let filled_before = buf.filled().len();
        let result = Pin::new(&mut self.inner).poll_read(cx, buf);
        if let Poll::Ready(Ok(())) = &result {
            if buf.filled().len() > filled_before {
                self.clock.touch();
            }
        }
        result
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for IdleWatchStream<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        let result = Pin::new(&mut self.inner).poll_write(cx, buf);
        if let Poll::Ready(Ok(written)) = &result {
            if *written > 0 {
                self.clock.touch();
            }
        }
        result
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

/// One trusted-proxy network (S-P1-18, M-8), parsed from the same
/// `"IP"` / `"IP/prefix"` strings Python's `ProxyTrust` accepts, so the
/// Rust accept loop and the Python forwarded-header resolution share one
/// trust list (the Python side passes `app.proxy_trust` through
/// `start_native`). No `ipnet` dependency: CIDR matching is a masked
/// comparison on the address bits, in `std` only.
struct TrustedProxyNetwork {
    network: IpAddr,
    prefix_len: u8,
}

impl TrustedProxyNetwork {
    fn parse(value: &str) -> Result<Self, String> {
        let invalid =
            || format!("Invalid trusted proxy {value:?}: must be an IP address or CIDR network");
        let (addr_text, prefix_text) = match value.split_once('/') {
            Some((addr, prefix)) => (addr, Some(prefix)),
            None => (value, None),
        };
        let addr: IpAddr = addr_text.parse().map_err(|_| invalid())?;
        let addr = normalize_client_ip(addr);
        let max_prefix = match addr {
            IpAddr::V4(_) => 32,
            IpAddr::V6(_) => 128,
        };
        let prefix_len = match prefix_text {
            Some(text) => text.parse::<u8>().map_err(|_| invalid())?,
            None => max_prefix,
        };
        if prefix_len > max_prefix {
            return Err(invalid());
        }
        Ok(Self {
            network: mask_network(addr, prefix_len),
            prefix_len,
        })
    }

    fn contains(&self, ip: IpAddr) -> bool {
        let ip = normalize_client_ip(ip);
        match (self.network, ip) {
            (IpAddr::V4(network), IpAddr::V4(addr)) => {
                mask_network(IpAddr::V4(addr), self.prefix_len) == IpAddr::V4(network)
            }
            (IpAddr::V6(network), IpAddr::V6(addr)) => {
                mask_network(IpAddr::V6(addr), self.prefix_len) == IpAddr::V6(network)
            }
            _ => false,
        }
    }
}

/// IPv4-mapped IPv6 (`::ffff:192.0.2.1`) counts as its IPv4 form, matching
/// `pynext.proxy._normalize_ip`, so the same peer is not two different
/// keys (and a mapped trusted proxy is recognised as trusted).
fn normalize_client_ip(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) => IpAddr::V4(v4),
            None => ip,
        },
        ip => ip,
    }
}

fn mask_network(ip: IpAddr, prefix_len: u8) -> IpAddr {
    match ip {
        IpAddr::V4(addr) => {
            let mask = if prefix_len == 0 {
                0u32
            } else {
                u32::MAX << (32 - prefix_len)
            };
            IpAddr::V4(std::net::Ipv4Addr::from(u32::from(addr) & mask))
        }
        IpAddr::V6(addr) => {
            let mask = if prefix_len == 0 {
                0u128
            } else {
                u128::MAX << (128 - prefix_len)
            };
            IpAddr::V6(std::net::Ipv6Addr::from(u128::from(addr) & mask))
        }
    }
}

/// The peer address of the TCP connection a request arrived on, carried in
/// request extensions so dispatch can hand the client IP to the Python
/// bridge. The Python rate limiter keys buckets on it (spec §11); without
/// this the bridge would have no trustworthy per-client key at all.
#[derive(Clone)]
struct PeerAddr(SocketAddr);

/// Bridge from hyper's connection service to the axum router: the router is a
/// tower service, so each connection polls it ready and then calls it with the
/// request body converted to axum's `Body`.
/// Bytes of the first request's header block the smuggling sniff holds
/// back for inspection. A header block larger than this is passed through
/// uninspected and left to hyper's own limits; a `Content-Length` +
/// `Transfer-Encoding` combination hiding past 128 KiB of headers is not
/// inspected, but hyper still closes the connection after such a request
/// (`keep_alive = false` when both are present), so it cannot poison a
/// reused connection.
const SMUGGLING_SNIFF_LIMIT: usize = 128 * 1024;

/// Find the end of the first request's header block: returns
/// `(end, insert_at)` where `end` is the index just past the blank-line
/// terminator and `insert_at` is where a marker header line goes (just
/// before the final blank line). `None` while the block is incomplete.
fn first_header_block_end(buf: &[u8]) -> Option<(usize, usize)> {
    // Search CRLF CRLF first: it cannot contain a bare LF LF.
    if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
        return Some((pos + 4, pos + 2));
    }
    if let Some(pos) = buf.windows(2).position(|w| w == b"\n\n") {
        return Some((pos + 2, pos + 1));
    }
    None
}

/// Scan the first request's header block for a `Content-Length` +
/// `Transfer-Encoding` combination and, when found, insert the smuggling
/// marker header just before the final blank line. Returns true when the
/// marker was inserted. The request line (first line) is skipped, header
/// names compare case-insensitively, and an h2c prior-knowledge preface
/// (`PRI * HTTP/2.0`) is left untouched — it has no headers to scan and
/// hyper-util's auto builder still needs the exact preface bytes.
fn mark_smuggling_request(buf: &mut Vec<u8>, end: usize, insert_at: usize) -> bool {
    let head = &buf[..end];
    if head.starts_with(b"PRI * HTTP/2.0") {
        return false;
    }
    let mut saw_cl = false;
    let mut saw_te = false;
    for (index, line) in head.split(|&b| b == b'\n').enumerate() {
        if index == 0 {
            continue; // request line
        }
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        let Some(colon) = line.iter().position(|&b| b == b':') else {
            continue;
        };
        let name = &line[..colon];
        if name.eq_ignore_ascii_case(b"content-length") {
            saw_cl = true;
        } else if name.eq_ignore_ascii_case(b"transfer-encoding") {
            saw_te = true;
        }
        if saw_cl && saw_te {
            break;
        }
    }
    if !(saw_cl && saw_te) {
        return false;
    }
    let marker = format!("{SMUGGLING_MARKER_NAME}: {SMUGGLING_MARKER_CL_TE}\r\n");
    buf.splice(insert_at..insert_at, marker.into_bytes());
    true
}

/// Byte-stream wrapper that sniffs the first request on a connection for
/// request-smuggling framing (S-P1-31) before hyper parses it.
///
/// hyper decides framing while parsing and strips `Content-Length` as
/// soon as `Transfer-Encoding` is present, so dispatch can never see a
/// `Content-Length` + `Transfer-Encoding` combination — the only place to
/// catch it is the raw bytes. This wrapper holds back the first request's
/// header block until its blank-line terminator arrives, scans it, and
/// inserts the `x-pynext-smuggling` marker when both headers are present;
/// dispatch then rejects with 400 and closes the connection. Everything
/// else passes through byte-for-byte, including the h2c preface and any
/// bytes after the header block.
///
/// Only the first request per connection is inspected: finding later
/// requests would require re-implementing body framing (lengths and
/// chunked boundaries) on this layer. That is the documented residual —
/// hyper already closes the connection after any request that carries
/// both headers, so a smuggled request cannot poison a reused
/// connection; the first-request sniff additionally turns the
/// still-executed handler run into a 400.
///
/// The hold-back is bounded by the same `header_read_timeout` hyper
/// enforces once it sees bytes: the guard sits in front of hyper, so
/// without its own deadline a client that never completes its first
/// header block would starve hyper-util's auto-builder protocol sniff
/// (`read_version` waits unboundedly for 24 bytes) and hyper's header
/// read timeout would never get a chance to fire — the connection (and
/// its accept-loop slot) would be held forever. When the deadline
/// expires the guard fails closed with a timeout error, which aborts
/// the connection; the partial block is never delivered uninspected, so
/// a slow sender cannot bypass the smuggling scan. A zero timeout
/// disables the bound, matching `header_read_timeout = 0`.
struct SmugglingGuardIo<S> {
    inner: S,
    /// Bytes read from `inner` but not yet delivered to hyper.
    pending: Vec<u8>,
    /// The first header block has been inspected (or inspection was
    /// abandoned at EOF / over the size cap).
    inspected: bool,
    /// Fires when the first header block has not completed within
    /// `header_read_timeout` of the connection being accepted.
    /// `None` when the timeout is disabled (zero).
    deadline: Option<Pin<Box<tokio::time::Sleep>>>,
}

impl<S> SmugglingGuardIo<S> {
    fn new(inner: S, header_read_timeout: Duration) -> Self {
        Self {
            inner,
            pending: Vec::new(),
            inspected: false,
            deadline: if header_read_timeout.is_zero() {
                None
            } else {
                Some(Box::pin(tokio::time::sleep_until(
                    tokio::time::Instant::now() + header_read_timeout,
                )))
            },
        }
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> AsyncRead for SmugglingGuardIo<S> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        if !this.inspected {
            // Hold back bytes until the first header block is complete
            // enough to scan; a partial header block must never leak to
            // hyper uninspected.
            loop {
                if this.pending.len() >= SMUGGLING_SNIFF_LIMIT {
                    this.inspected = true;
                    break;
                }
                if let Some((end, insert_at)) = first_header_block_end(&this.pending) {
                    mark_smuggling_request(&mut this.pending, end, insert_at);
                    this.inspected = true;
                    break;
                }
                // Bound the hold-back (see the struct docs): a first
                // header block that never completes must abort the
                // connection instead of starving hyper's protocol sniff
                // forever. Polling the sleep registers this task's waker,
                // so the connection is re-polled at the deadline even if
                // no more bytes arrive.
                if let Some(deadline) = this.deadline.as_mut() {
                    if deadline.as_mut().poll(cx).is_ready() {
                        return Poll::Ready(Err(std::io::Error::new(
                            std::io::ErrorKind::TimedOut,
                            "first header block did not complete within header_read_timeout",
                        )));
                    }
                }
                let mut chunk = [0u8; 8192];
                let mut chunk_buf = ReadBuf::new(&mut chunk);
                match Pin::new(&mut this.inner).poll_read(cx, &mut chunk_buf) {
                    Poll::Ready(Ok(())) => {
                        let filled = chunk_buf.filled();
                        if filled.is_empty() {
                            // EOF before a complete header block: deliver
                            // what we have; there is nothing to smuggle.
                            this.inspected = true;
                            break;
                        }
                        this.pending.extend_from_slice(filled);
                    }
                    Poll::Ready(Err(err)) => return Poll::Ready(Err(err)),
                    Poll::Pending => return Poll::Pending,
                }
            }
        }
        if !this.pending.is_empty() {
            let n = buf.remaining().min(this.pending.len());
            buf.put_slice(&this.pending[..n]);
            this.pending.drain(..n);
            return Poll::Ready(Ok(()));
        }
        Pin::new(&mut this.inner).poll_read(cx, buf)
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> AsyncWrite for SmugglingGuardIo<S> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.get_mut().inner).poll_write(cx, buf)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_flush(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}

struct HyperService {
    router: AxumRouter,
    peer_addr: Option<SocketAddr>,
}

impl hyper::service::Service<hyper::Request<hyper::body::Incoming>> for HyperService {
    type Response = AxumResponse<Body>;
    type Error = std::convert::Infallible;
    type Future = std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Self::Response, Self::Error>> + Send>,
    >;

    fn call(&self, req: hyper::Request<hyper::body::Incoming>) -> Self::Future {
        let mut router = self.router.clone();
        let peer_addr = self.peer_addr;
        Box::pin(async move {
            std::future::poll_fn(|cx| {
                <AxumRouter as TowerService<Request>>::poll_ready(&mut router, cx)
            })
            .await?;
            let mut req = req.map(Body::new);
            if let Some(addr) = peer_addr {
                req.extensions_mut().insert(PeerAddr(addr));
            }
            router.call(req).await
        })
    }
}

// Slow header transmission (slow-loris on the request line/headers) is
// bounded by hyper's per-connection header read timeout, so one idle
// connection cannot hold a slot in the accept loop forever. The timeout is
// configurable per server (see `header_read_timeout` in ServerState); 0
// disables it.

/// Optional SO_REUSEPORT binding for callers on Unix. The supervisor uses
/// a shared listening socket instead, including on Windows.
#[cfg(unix)]
fn bind_reuse_port(host: &str, port: u16) -> std::io::Result<std::net::TcpListener> {
    use std::net::ToSocketAddrs;
    let addr = (host, port).to_socket_addrs()?.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "could not resolve host")
    })?;
    let socket = socket2::Socket::new(
        socket2::Domain::for_address(addr),
        socket2::Type::STREAM,
        None,
    )?;
    socket.set_reuse_port(true)?;
    socket.bind(&addr.into())?;
    socket.listen(1024)?;
    Ok(std::net::TcpListener::from(socket))
}

#[cfg(not(unix))]
fn bind_reuse_port(_host: &str, _port: u16) -> std::io::Result<std::net::TcpListener> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "reuse_port (multi-worker mode) is only supported on Unix",
    ))
}

#[cfg(unix)]
fn take_python_listener(py: Python<'_>, listener: Py<PyAny>) -> PyResult<std::net::TcpListener> {
    use std::os::fd::FromRawFd;
    let socket_type = py.import("socket")?.getattr("socket")?;
    let listener = listener.bind(py);
    if !listener.is_instance(&socket_type)? {
        return Err(PyValueError::new_err("listener must be a socket.socket"));
    }
    let fd = listener.call_method0("detach")?.extract::<i32>()?;
    if fd < 0 {
        return Err(PyValueError::new_err("listener socket is closed"));
    }
    // Python's detach transfers sole ownership of this duplicated descriptor.
    Ok(unsafe { std::net::TcpListener::from_raw_fd(fd) })
}

#[cfg(windows)]
fn take_python_listener(py: Python<'_>, listener: Py<PyAny>) -> PyResult<std::net::TcpListener> {
    use std::os::windows::io::{FromRawSocket, RawSocket};
    let socket_type = py.import("socket")?.getattr("socket")?;
    let listener = listener.bind(py);
    if !listener.is_instance(&socket_type)? {
        return Err(PyValueError::new_err("listener must be a socket.socket"));
    }
    let socket = listener.call_method0("detach")?.extract::<RawSocket>()?;
    // Python's detach transfers sole ownership of this duplicated socket.
    Ok(unsafe { std::net::TcpListener::from_raw_socket(socket) })
}

#[cfg(not(any(unix, windows)))]
fn take_python_listener(_py: Python<'_>, _listener: Py<PyAny>) -> PyResult<std::net::TcpListener> {
    Err(PyValueError::new_err(
        "shared listeners are unavailable on this platform",
    ))
}

#[pyclass]
struct NativeServer {
    // stop() may pass the shutdown budget that is left when it is
    // called; None keeps the construction-time drain bound.
    shutdown: Option<oneshot::Sender<Option<Duration>>>,
    thread: Option<JoinHandle<()>>,
    #[pyo3(get)]
    address: String,
    // H-2: shared with ServerState so the fail-open count stays readable
    // after the state moves into the server thread.
    rate_limit_fail_open: Arc<AtomicU64>,
}

#[pymethods]
impl NativeServer {
    #[new]
    #[pyo3(signature = (callback, host, port, static_pages, public_assets, client_assets, isr_cache, asset_etags, max_concurrent=64, acquire_timeout_ms=1000, max_waiting=None, body_capacity=16, body_timeout_ms=10000, websocket_paths=None, websocket_callback=None, reuse_port=false, route_patterns=None, middleware_enabled=false, shared_cache_enabled=false, websocket_protocols=None, websocket_max_message=65536, listener=None, max_connections=1024, max_connections_per_ip=128, header_read_timeout_ms=10000, body_min_rate_bytes_per_sec=1024, security_headers=None, build_headers=None, drain_timeout_secs=30.0, websocket_origin_callback=None, websocket_rate_limit_callback=None, websocket_auth_callback=None, http_rate_limit_callback=None, path_headers=None, max_concurrent_per_ip=16, acquire_timeout_max_ms=None, fast_path_max_concurrent=None, streaming_max_lifetime_secs=300, body_capacity_per_ip=4, ws_idle_timeout_secs=300, ws_max_lifetime_secs=3600, ws_ping_interval_secs=60, trusted_proxies=None, allowed_hosts=None, isr_cache_control=None, prefetch_stale_times=None))]
    #[allow(clippy::too_many_arguments)]
    #[allow(clippy::type_complexity)]
    fn new(
        py: Python<'_>,
        callback: Py<PyAny>,
        host: &str,
        port: u16,
        static_pages: HashMap<String, Vec<u8>>,
        public_assets: HashMap<String, (Vec<u8>, String)>,
        client_assets: HashMap<String, Vec<u8>>,
        isr_cache: PyRef<'_, PyPageCache>,
        asset_etags: HashMap<String, String>,
        max_concurrent: usize,
        acquire_timeout_ms: u64,
        max_waiting: Option<usize>,
        body_capacity: usize,
        body_timeout_ms: u64,
        websocket_paths: Option<Vec<String>>,
        websocket_callback: Option<Py<PyAny>>,
        reuse_port: bool,
        route_patterns: Option<Vec<String>>,
        middleware_enabled: bool,
        shared_cache_enabled: bool,
        websocket_protocols: Option<HashMap<String, Vec<String>>>,
        websocket_max_message: usize,
        listener: Option<Py<PyAny>>,
        max_connections: usize,
        max_connections_per_ip: usize,
        header_read_timeout_ms: u64,
        body_min_rate_bytes_per_sec: usize,
        security_headers: Option<Vec<(Vec<u8>, Vec<u8>)>>,
        build_headers: Option<Vec<(Vec<u8>, Vec<u8>)>>,
        drain_timeout_secs: f64,
        websocket_origin_callback: Option<Py<PyAny>>,
        websocket_rate_limit_callback: Option<Py<PyAny>>,
        websocket_auth_callback: Option<Py<PyAny>>,
        http_rate_limit_callback: Option<Py<PyAny>>,
        path_headers: Option<Vec<(String, Vec<(Vec<u8>, Vec<u8>)>)>>,
        max_concurrent_per_ip: usize,
        acquire_timeout_max_ms: Option<u64>,
        fast_path_max_concurrent: Option<usize>,
        streaming_max_lifetime_secs: u64,
        body_capacity_per_ip: usize,
        ws_idle_timeout_secs: u64,
        ws_max_lifetime_secs: u64,
        ws_ping_interval_secs: u64,
        trusted_proxies: Option<Vec<String>>,
        allowed_hosts: Option<Vec<String>>,
        isr_cache_control: Option<HashMap<String, String>>,
        prefetch_stale_times: Option<HashMap<String, u64>>,
    ) -> PyResult<Self> {
        // The drain bound is a shutdown budget, not a request timeout:
        // negative or NaN values would silently become "no drain at
        // all", so clamp them to zero instead.
        let drain_timeout = Duration::from_secs_f64(if drain_timeout_secs.is_finite() {
            drain_timeout_secs.max(0.0)
        } else {
            0.0
        });
        let websocket_paths = websocket_paths.unwrap_or_default();
        if websocket_max_message == 0 || websocket_max_message > 16 * 1024 * 1024 {
            return Err(PyValueError::new_err(
                "websocket_max_message is outside the supported range",
            ));
        }
        // Route against the complete application trie first. A static HTTP
        // route must still shadow a dynamic WebSocket route.
        let websocket_router =
            Router::new(route_patterns.unwrap_or_else(|| websocket_paths.clone()))
                .map_err(PyValueError::new_err)?;
        if max_concurrent == 0 || max_concurrent > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "max_concurrent is outside the supported range",
            ));
        }
        if let Some(waiting) = max_waiting {
            if waiting > Semaphore::MAX_PERMITS {
                return Err(PyValueError::new_err(
                    "max_waiting is outside the supported range",
                ));
            }
        }
        if body_capacity == 0 || body_capacity > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "body_capacity is outside the supported range",
            ));
        }
        if max_connections == 0 || max_connections > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "max_connections is outside the supported range",
            ));
        }
        if max_connections_per_ip == 0 || max_connections_per_ip > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "max_connections_per_ip is outside the supported range",
            ));
        }
        if max_concurrent_per_ip == 0 || max_concurrent_per_ip > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "max_concurrent_per_ip is outside the supported range",
            ));
        }
        // S-P1-11: the fast-path budget defaults to 4x the execution
        // slots — generous for cheap static responses, but still a cap
        // instead of the bare connection limit. An explicit 0 would shed
        // every fast-path response, so it is rejected like the other
        // zero capacities.
        let fast_path_max_concurrent = fast_path_max_concurrent
            .unwrap_or_else(|| max_concurrent.saturating_mul(4).min(Semaphore::MAX_PERMITS));
        if fast_path_max_concurrent == 0 || fast_path_max_concurrent > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "fast_path_max_concurrent is outside the supported range",
            ));
        }
        if body_capacity_per_ip == 0 || body_capacity_per_ip > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "body_capacity_per_ip is outside the supported range",
            ));
        }
        // Trusted proxies (S-P1-18): invalid entries fail at startup, like
        // Python's parse_trusted_proxies, instead of silently trusting
        // nobody — or everybody.
        let trusted_proxies = trusted_proxies
            .unwrap_or_default()
            .into_iter()
            .map(|value| TrustedProxyNetwork::parse(&value).map_err(PyValueError::new_err))
            .collect::<PyResult<Vec<_>>>()?;
        if static_pages
            .keys()
            .chain(public_assets.keys())
            .chain(client_assets.keys())
            .any(|path| !asset_etags.contains_key(path))
        {
            return Err(PyValueError::new_err("Missing static asset ETag"));
        }
        let asset_etags = asset_etags
            .into_iter()
            .map(|(path, etag)| {
                let bytes = etag.as_bytes();
                if bytes.len() != 66
                    || bytes[0] != b'"'
                    || bytes[65] != b'"'
                    || !bytes[1..65]
                        .iter()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
                {
                    return Err(PyValueError::new_err("Invalid static asset ETag"));
                }
                let value = HeaderValue::from_bytes(bytes)
                    .map_err(|_| PyValueError::new_err("Invalid static asset ETag"))?;
                Ok((path, value))
            })
            .collect::<PyResult<HashMap<_, _>>>()?;
        // Per-path headers arrive already validated from Python; reject
        // them here too so a direct NativeServer caller cannot smuggle in
        // a bad source or header value past the Python compiler.
        let path_headers = path_headers
            .unwrap_or_default()
            .into_iter()
            .map(|(source, headers)| {
                let router = Router::new(vec![source]).map_err(PyValueError::new_err)?;
                let headers = headers
                    .into_iter()
                    .map(|(name, value)| {
                        Ok((
                            HeaderName::from_bytes(&name)
                                .map_err(|_| PyValueError::new_err("Invalid path header name"))?,
                            HeaderValue::from_bytes(&value)
                                .map_err(|_| PyValueError::new_err("Invalid path header value"))?,
                        ))
                    })
                    .collect::<PyResult<Vec<_>>>()?;
                Ok(PathHeaderRule { router, headers })
            })
            .collect::<PyResult<Vec<_>>>()?;
        // Host allowlist (S-P1-19, M-9): an explicit list is compiled
        // as given; None or an empty list means the loopback default,
        // matching `pynext.config.effective_allowed_hosts`.
        let allowed_hosts = match allowed_hosts {
            Some(entries) if !entries.is_empty() => {
                parse_host_patterns(entries).map_err(PyValueError::new_err)?
            }
            _ => default_host_patterns(),
        };
        let isr_cache = isr_cache.inner.clone();
        let isr_cache_control = isr_cache_control
            .unwrap_or_default()
            .into_iter()
            .map(|(pattern, value)| {
                let value = HeaderValue::from_str(&value)
                    .map_err(|_| PyValueError::new_err("Invalid ISR cache-control header"))?;
                Ok((pattern, value))
            })
            .collect::<PyResult<HashMap<_, _>>>()?;
        let public_assets = public_assets
            .into_iter()
            .map(|(path, (body, content_type))| {
                let content_type = HeaderValue::from_bytes(content_type.as_bytes())
                    .map_err(|_| PyValueError::new_err("Invalid public asset content type"))?;
                Ok((
                    path,
                    PublicAsset {
                        body: Bytes::from(body),
                        content_type,
                    },
                ))
            })
            .collect::<PyResult<HashMap<_, _>>>()?;
        let listener = if let Some(listener) = listener {
            take_python_listener(py, listener)?
        } else if reuse_port {
            bind_reuse_port(host, port)
                .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?
        } else {
            std::net::TcpListener::bind((host, port))
                .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?
        };
        listener
            .set_nonblocking(true)
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
        let address = listener
            .local_addr()
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?
            .to_string();
        let (shutdown, rx) = oneshot::channel::<Option<Duration>>();
        // H-2: created here so both the server thread's state and this
        // handle share one fail-open counter.
        let rate_limit_fail_open = Arc::new(AtomicU64::new(0));
        let rate_limit_fail_open_state = Arc::clone(&rate_limit_fail_open);
        // The spawned thread reports its startup result back: previously a
        // panic in `.expect` here only killed the thread while `new()` had
        // already returned Ok with an address, leaving a dead server behind.
        let (startup_tx, startup_rx) = mpsc::channel::<Result<(), String>>();
        let thread = std::thread::spawn(move || {
            let runtime = match tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
            {
                Ok(runtime) => runtime,
                Err(e) => {
                    let _ = startup_tx.send(Err(format!("Tokio runtime failed: {e}")));
                    return;
                }
            };
            runtime.block_on(async move {
                let listener = match tokio::net::TcpListener::from_std(listener) {
                    Ok(listener) => listener,
                    Err(e) => {
                        let _ = startup_tx.send(Err(format!("Configured listener failed: {e}")));
                        return;
                    }
                };
                let _ = startup_tx.send(Ok(()));
                let state = Arc::new(ServerState {
                    callback,
                    capacity: Arc::new(Semaphore::new(max_concurrent)),
                    // Configurable (roadmap C7); None keeps the old
                    // default of min(max_concurrent, 64). 0 means an
                    // execution slot must be free immediately, else 503.
                    waiting_capacity: Arc::new(Semaphore::new(
                        max_waiting.unwrap_or_else(|| max_concurrent.min(64)),
                    )),
                    acquire_timeout: Duration::from_millis(acquire_timeout_ms),
                    acquire_timeout_max: acquire_timeout_max_ms.map(Duration::from_millis),
                    hold_stats: Arc::new(HoldTimeStats::new()),
                    // Body intake gets fewer slots than Python execution on
                    // purpose: a flood of slow uploads can then only take the
                    // body slots, leaving execution slots free for requests
                    // whose bodies are already in. The cost is that a burst of
                    // legitimate concurrent uploads 503s sooner; raise
                    // body_capacity if uploads, not execution, are the
                    // bottleneck.
                    body_capacity: Arc::new(Semaphore::new(body_capacity)),
                    body_timeout: Duration::from_millis(body_timeout_ms),
                    fast_path_capacity: Arc::new(Semaphore::new(fast_path_max_concurrent)),
                    connection_capacity: Arc::new(Semaphore::new(max_connections)),
                    per_ip_connections: Arc::new(Mutex::new(HashMap::new())),
                    max_connections_per_ip,
                    trusted_proxies,
                    header_read_timeout: Duration::from_millis(header_read_timeout_ms),
                    body_min_rate: body_min_rate_bytes_per_sec,
                    // S-P1-14/15/16 (0 disables the streaming lifetime,
                    // the WS idle timeout, the WS lifetime, and WS pings).
                    streaming_max_lifetime: Duration::from_secs(streaming_max_lifetime_secs),
                    body_per_ip: Arc::new(Mutex::new(HashMap::new())),
                    max_body_per_ip: body_capacity_per_ip,
                    ws_idle_timeout: Duration::from_secs(ws_idle_timeout_secs),
                    ws_max_lifetime: Duration::from_secs(ws_max_lifetime_secs),
                    ws_ping_interval: Duration::from_secs(ws_ping_interval_secs),
                    static_pages: static_pages
                        .into_iter()
                        .map(|(path, body)| (path, Bytes::from(body)))
                        .collect(),
                    prefetch_stale_times: prefetch_stale_times.unwrap_or_default(),
                    public_assets,
                    client_assets: client_assets
                        .into_iter()
                        .map(|(path, body)| (path, Bytes::from(body)))
                        .collect(),
                    asset_etags,
                    isr_cache,
                    isr_cache_control,
                    middleware_enabled,
                    shared_cache_enabled,
                    security_headers: security_headers.unwrap_or_default(),
                    path_headers,
                    allowed_hosts,
                    build_headers: build_headers.unwrap_or_default(),
                    websocket_router,
                    websocket_paths: websocket_paths.into_iter().collect(),
                    websocket_protocols: websocket_protocols.unwrap_or_default(),
                    websocket_max_message,
                    ws_callback: websocket_callback,
                    ws_origin_callback: websocket_origin_callback,
                    ws_rate_limit_callback: websocket_rate_limit_callback,
                    http_rate_limit_callback,
                    rate_limit_capacity: Arc::new(Semaphore::new(64)),
                    rate_limit_fail_open: rate_limit_fail_open_state,
                    per_ip_executions: Arc::new(Mutex::new(HashMap::new())),
                    max_concurrent_per_ip,
                    ws_origin_capacity: Arc::new(Semaphore::new(64)),
                    ws_auth_callback: websocket_auth_callback,
                    ws_auth_capacity: Arc::new(Semaphore::new(64)),
                });
                // The accept loop below enforces the connection budget from
                // this handle; the router takes its own clone via with_state.
                let limits = Arc::clone(&state);
                let app = AxumRouter::new().fallback(any(dispatch)).with_state(state);
                // stop() may pass the shutdown budget that is left when
                // it is called; None keeps the construction-time bound
                // (used when the server is dropped without stop()).
                let mut shutdown = std::pin::pin!(async { rx.await.ok().flatten() });
                let mut connections = tokio::task::JoinSet::new();
                let (connection_shutdown, _) = watch::channel(false);
                let drain_override = loop {
                    tokio::select! {
                        override_value = &mut shutdown => break override_value,
                        accepted = listener.accept() => {
                            let (stream, peer_addr) = match accepted {
                                Ok(pair) => pair,
                                Err(_) => {
                                    tokio::time::sleep(Duration::from_millis(10)).await;
                                    continue;
                                }
                            };
                            // S-P1-16: TCP keepalive probes catch
                            // half-open peers (idle WebSocket sessions
                            // especially) that no application timeout can
                            // see until it tries to write. Best-effort:
                            // a platform that cannot set it keeps the
                            // application-level ping/pong instead.
                            {
                                let keepalive = socket2::TcpKeepalive::new()
                                    .with_time(Duration::from_secs(60))
                                    .with_interval(Duration::from_secs(10));
                                let _ = socket2::SockRef::from(&stream)
                                    .set_tcp_keepalive(&keepalive);
                            }
                            // Connection budget (A4): the total cap and the
                            // per-IP cap are enforced here, before any
                            // request is read, so a flood of idle or slow
                            // connections cannot starve the accept loop or
                            // the Python bridge. An over-limit connection is
                            // dropped by closing the socket: the client sees
                            // EOF, not a response, because no HTTP request
                            // exists to answer yet. Both permits are held
                            // for the lifetime of the connection task.
                            // IPv4-mapped IPv6 addresses count under their
                            // IPv4 form, matching the Python rate limiter
                            // (pynext.proxy normalizes the same way).
                            let peer_ip = normalize_client_ip(peer_addr.ip());
                            let total_permit = match limits
                                .connection_capacity
                                .clone()
                                .try_acquire_owned()
                            {
                                Ok(permit) => permit,
                                Err(_) => continue,
                            };
                            // Per-IP cap, trust-aware (S-P1-18, M-8): the
                            // accept loop runs before any request is read,
                            // so no X-Forwarded-For / Forwarded header
                            // exists yet to resolve a client IP from — the
                            // peer is the only identity available here.
                            // Keying the cap on the peer is correct for a
                            // direct client, but behind a trusted proxy
                            // every user would share the proxy's single
                            // 128-connection budget. A peer in
                            // `trusted_proxies` (the same list Python's
                            // ProxyTrust resolves forwarded headers
                            // against) is therefore exempt from the per-IP
                            // cap: the total connection cap still bounds
                            // it, and per-client limits behind the proxy
                            // are enforced at request time, where headers
                            // exist — the Python rate limiter keys buckets
                            // on the trust-resolved client IP, and an
                            // untrusted peer's forwarded headers are
                            // ignored there. Exempting an unlisted peer
                            // would let any client mint unlimited
                            // connections, so only a listed peer is exempt.
                            let per_ip_guard = if limits
                                .trusted_proxies
                                .iter()
                                .any(|network| network.contains(peer_ip))
                            {
                                None
                            } else {
                                let per_ip_admitted = {
                                    let mut counts = limits
                                        .per_ip_connections
                                        .lock()
                                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                                    let count = counts.entry(peer_ip).or_insert(0);
                                    if *count >= limits.max_connections_per_ip {
                                        false
                                    } else {
                                        *count += 1;
                                        true
                                    }
                                };
                                if !per_ip_admitted {
                                    drop(total_permit);
                                    continue;
                                }
                                Some(PerIpConnectionGuard {
                                    counts: Arc::clone(&limits.per_ip_connections),
                                    ip: peer_ip,
                                })
                            };
                            let header_read_timeout = limits.header_read_timeout;
                            let service = HyperService {
                                router: app.clone(),
                                peer_addr: Some(peer_addr),
                            };
                            // Request-smuggling sniff (S-P1-31): wrap the
                            // stream so the first request's header block is
                            // scanned for a `Content-Length` +
                            // `Transfer-Encoding` combination before hyper
                            // parses it. The wrap happens here, outside the
                            // h2c/auto-builder setup below, and only changes
                            // the byte stream's first header block.
                            let stream = SmugglingGuardIo::new(stream, header_read_timeout);
                            let mut stop_connection = connection_shutdown.subscribe();
                            // Auto-detect the protocol per connection: a
                            // client that opens with the HTTP/2
                            // prior-knowledge preface (`PRI * HTTP/2.0`) is
                            // served h2c, everything else is HTTP/1.1. The
                            // old Http1Builder-only loop turned the h2
                            // preface into a VersionH2 error and dropped the
                            // connection. The header read timeout applies to
                            // the HTTP/1.1 side via `.http1()`; h2c gets the
                            // same bound from the idle watchdog below
                            // (S-P1-13), since hyper has no h2c idle
                            // timeout of its own.
                            connections.spawn(async move {
                                let _total_permit = total_permit;
                                let _per_ip_guard = per_ip_guard;
                                // S-P1-13 (M-3): tell h2c apart from
                                // HTTP/1.1 by peeking at the prior-knowledge
                                // preface. Hyper auto-detects the protocol
                                // itself; the peek only decides whether the
                                // idle watchdog applies, so a slow or
                                // partial preface merely fails open to "no
                                // watchdog". The peek consumes nothing, and
                                // a client that has not sent anything yet
                                // only delays connection setup by the peek
                                // timeout.
                                let is_h2c = {
                                    let mut preface = [0u8; 24];
                                    match tokio::time::timeout(
                                        Duration::from_millis(100),
                                        // The stream is already wrapped in
                                        // SmugglingGuardIo (S-P1-31), which
                                        // has no peek of its own: peek the
                                        // inner TcpStream directly. The guard
                                        // has not read anything yet (hyper
                                        // reads first), so its pending buffer
                                        // is empty and the peek sees the same
                                        // bytes hyper will.
                                        stream.inner.peek(&mut preface),
                                    )
                                    .await
                                    {
                                        Ok(Ok(n)) => {
                                            n >= 24
                                                && preface
                                                    == *b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
                                        }
                                        _ => false,
                                    }
                                };
                                let idle_clock = Arc::new(IdleClock::new());
                                let watched = IdleWatchStream {
                                    inner: stream,
                                    clock: Arc::clone(&idle_clock),
                                };
                                let mut builder = AutoBuilder::new(TokioExecutor::new());
                                {
                                    let mut http1 = builder.http1();
                                    http1.timer(TokioTimer::new());
                                    if !header_read_timeout.is_zero() {
                                        http1.header_read_timeout(header_read_timeout);
                                    }
                                }
                                // Upgrades (WebSocket) need the upgradable
                                // connection: plain serve_connection leaves
                                // the upgrade future failing with "upgrade
                                // expected but low level API in use".
                                let connection = builder
                                    .serve_connection_with_upgrades(TokioIo::new(watched), service);
                                tokio::pin!(connection);
                                tokio::select! {
                                    _ = &mut connection => {}
                                    _ = stop_connection.changed() => {
                                        connection.as_mut().graceful_shutdown();
                                        let _ = connection.await;
                                    }
                                    // Idle timeout for h2c only: HTTP/1.1
                                    // already gets the header-read timeout
                                    // from hyper, and applying this watchdog
                                    // there too would kill long handlers that
                                    // stay silent for a while. Activity is
                                    // any socket read/write, so a live stream
                                    // or a peer answering frames keeps the
                                    // connection; only a connection idle for
                                    // the whole timeout is dropped, which
                                    // releases its connection-budget permits.
                                    _ = async {
                                        if !is_h2c || header_read_timeout.is_zero() {
                                            return futures_util::future::pending::<()>().await;
                                        }
                                        loop {
                                            let idle = idle_clock.idle();
                                            if idle >= header_read_timeout {
                                                return;
                                            }
                                            tokio::time::sleep(header_read_timeout - idle).await;
                                        }
                                    } => {}
                                }
                            });
                        }
                        _ = connections.join_next(), if !connections.is_empty() => {}
                    }
                };
                let _ = connection_shutdown.send(true);
                // Drain in-flight connections, bounded so `stop()` cannot
                // hang on a stuck connection. The bound is the shutdown
                // budget that is left when stop() is called (serve()
                // passes what remains of its shared budget); a bare
                // stop() or a drop falls back to the construction-time
                // bound, which only covers connections whose close
                // handshake is still in flight after serve() already
                // waited for Python handlers.
                let drain_timeout = drain_override.unwrap_or(drain_timeout);
                let _ = tokio::time::timeout(drain_timeout, async {
                    while connections.join_next().await.is_some() {}
                })
                .await;
            });
        });
        match startup_rx.recv() {
            Ok(Ok(())) => {}
            Ok(Err(message)) => {
                return Err(pyo3::exceptions::PyOSError::new_err(message));
            }
            Err(_) => {
                return Err(pyo3::exceptions::PyOSError::new_err(
                    "server thread failed to start",
                ));
            }
        }
        Ok(Self {
            shutdown: Some(shutdown),
            thread: Some(thread),
            address,
            rate_limit_fail_open,
        })
    }

    /// Stop the server, draining connections for at most ``timeout``
    /// seconds when given; without one the drain bound the server was
    /// constructed with applies. ``serve()`` passes the shutdown
    /// budget that is left, so the whole shutdown stays within one
    /// shared timeout instead of each stage taking a fresh budget.
    #[pyo3(signature = (timeout=None))]
    fn stop(&mut self, py: Python<'_>, timeout: Option<f64>) {
        if let Some(shutdown) = self.shutdown.take() {
            // Clamped like the construction-time value, so a negative
            // or NaN budget becomes "no drain at all" instead of
            // hanging the shutdown.
            let drain = timeout.map(|secs| {
                Duration::from_secs_f64(if secs.is_finite() { secs.max(0.0) } else { 0.0 })
            });
            let _ = shutdown.send(drain);
        }
        if let Some(thread) = self.thread.take() {
            py.detach(move || {
                let _ = thread.join();
            });
        }
    }

    /// H-2: how many rate-limit decisions failed open (no decision
    /// could be produced) since this server started.
    fn rate_limit_fail_open_count(&self) -> u64 {
        self.rate_limit_fail_open.load(Ordering::Relaxed)
    }
}

impl Drop for NativeServer {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(None);
        }
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<PyRouter>()?;
    m.add_class::<PyPageCache>()?;
    m.add_class::<NativeServer>()?;
    m.add_class::<RequestCancellation>()?;
    m.add_function(wrap_pyfunction!(render, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use serde_json::json;

    // Host allowlist (S-P1-19, M-9): Django ALLOWED_HOSTS semantics —
    // exact, `.domain` (domain + subdomains), `*.domain` (subdomains
    // only), `*` (any), with the request port stripped before matching.
    #[test]
    fn host_patterns_match_django_semantics() {
        let patterns = parse_host_patterns(vec![
            "example.com".to_string(),
            ".example.org".to_string(),
            "*.example.net".to_string(),
        ])
        .unwrap();
        let allowed = |host: &str| {
            let bare = bare_host(host).unwrap();
            patterns.iter().any(|p| host_matches(&bare, p))
        };
        assert!(allowed("example.com"));
        assert!(allowed("example.com:3000"));
        assert!(allowed("EXAMPLE.COM"));
        assert!(allowed("example.com."));
        assert!(!allowed("sub.example.com"));
        assert!(!allowed("evil.com"));
        assert!(allowed("example.org"));
        assert!(allowed("www.example.org"));
        assert!(allowed("deep.sub.example.org"));
        assert!(allowed("www.example.net"));
        assert!(!allowed("example.net"));
        assert!(!allowed("example.com.evil.com"));
    }

    #[test]
    fn host_patterns_reject_bad_entries() {
        for bad in [
            "",
            "https://example.com",
            "example.com:3000",
            "example.com/path",
            "exa mple.com",
            "foo.*.example.com",
            ".127.0.0.1",
            "*.127.0.0.1",
            "[example.com]",
            "example..com",
            "-example.com",
            "example.com-",
        ] {
            assert!(parse_host_pattern(bad).is_err(), "{bad}");
        }
        assert!(parse_host_pattern("*").is_ok());
        assert!(parse_host_pattern("[::1]").is_ok());
        assert!(parse_host_pattern("::1").is_ok());
        assert!(matches!(
            parse_host_pattern("EXAMPLE.COM").unwrap(),
            HostPattern::Exact(ref h) if h == "example.com"
        ));
    }

    #[test]
    fn host_header_requires_a_present_allowed_host() {
        let patterns = default_host_patterns();
        let mut headers = HeaderMap::new();
        assert!(!host_header_allowed(&headers, None, &patterns));
        // h2c carries :authority instead of a Host header: the URI
        // authority is the fallback source.
        assert!(host_header_allowed(
            &headers,
            Some("127.0.0.1:3000"),
            &patterns
        ));
        assert!(!host_header_allowed(&headers, Some("evil.com"), &patterns));
        headers.insert(header::HOST, HeaderValue::from_static("127.0.0.1:3000"));
        assert!(host_header_allowed(&headers, None, &patterns));
        headers.insert(header::HOST, HeaderValue::from_static("[::1]:3000"));
        assert!(host_header_allowed(&headers, None, &patterns));
        headers.insert(header::HOST, HeaderValue::from_static("evil.com"));
        assert!(!host_header_allowed(&headers, None, &patterns));
        headers.insert(header::HOST, HeaderValue::from_static(""));
        assert!(!host_header_allowed(&headers, None, &patterns));
    }

    // Property-based coverage for the HTML serializer (finding #48, spec §13).
    // These tests call the pure-Rust render_node directly; the same renderer
    // is also exercised from Python by tests/test_property_router_render.py.
    proptest! {
        #![proptest_config(ProptestConfig::with_cases(64))]
        #[test]
        fn rendered_text_never_leaks_raw_specials(text in proptest::arbitrary::any::<String>()) {
            let node = json!({"kind": "text", "value": text});
            let mut output = String::new();
            render_node(&node, &mut output, 0, None).unwrap();
            prop_assert!(!output.contains('<') && !output.contains('>'));
        }
        #[test]
        fn void_elements_never_render_a_closing_tag(
            tag in prop::sample::select(vec![
                "area", "base", "br", "col", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr",
            ])
        ) {
            let node = json!({"kind": "element", "tag": tag, "attrs": {}, "children": []});
            let mut output = String::new();
            render_node(&node, &mut output, 0, None).unwrap();
            prop_assert_eq!(output, format!("<{tag}>"));
            let with_child = json!({"kind": "element", "tag": tag, "attrs": {},
                                    "children": [{"kind": "text", "value": "x"}]});
            let mut output = String::new();
            prop_assert!(render_node(&with_child, &mut output, 0, None).is_err());
        }
        #[test]
        fn arbitrary_attribute_values_do_not_panic(value in proptest::arbitrary::any::<String>()) {
            let node = json!({"kind": "element", "tag": "a",
                              "attrs": {"title": value}, "children": []});
            let mut output = String::new();
            let _ = render_node(&node, &mut output, 0, None);
        }
        #[test]
        fn malformed_nodes_fail_cleanly(value in proptest::arbitrary::any::<String>()) {
            // Arbitrary JSON that is not a render tree must fail with an error
            // string, never panic.
            let node: Value = serde_json::from_str(&value)
                .unwrap_or(Value::String(value));
            let mut output = String::new();
            let _ = render_node(&node, &mut output, 0, None);
        }
    }

    // Adaptive admission timeout (C-P1-5): the effective wait follows the
    // observed slot-hold time, capped by the configured ceiling.
    #[test]
    fn adaptive_timeout_stays_fixed_without_ceiling_or_samples() {
        let base = Duration::from_millis(1000);
        assert_eq!(effective_acquire_timeout(base, None, Some(5000.0)), base);
        assert_eq!(
            effective_acquire_timeout(base, Some(Duration::from_millis(5000)), None),
            base
        );
    }

    #[test]
    fn adaptive_timeout_tracks_hold_time_ewma() {
        let base = Duration::from_millis(1000);
        let max = Duration::from_millis(5000);
        // Mean hold 500 ms -> 2x = 1000 ms, the base: no stretch.
        assert_eq!(
            effective_acquire_timeout(base, Some(max), Some(500.0)),
            base
        );
        // Mean hold 1500 ms -> 2x = 3000 ms.
        assert_eq!(
            effective_acquire_timeout(base, Some(max), Some(1500.0)),
            Duration::from_millis(3000)
        );
        // Mean hold 10 s -> 2x = 20 s, capped at the 5000 ms ceiling.
        assert_eq!(
            effective_acquire_timeout(base, Some(max), Some(10000.0)),
            max
        );
    }

    #[test]
    fn hold_time_ewma_learns_from_samples() {
        let stats = HoldTimeStats::new();
        assert_eq!(stats.ewma_ms(), None);
        stats.record(Duration::from_millis(1000));
        assert_eq!(stats.ewma_ms(), Some(1000.0));
        stats.record(Duration::from_millis(2000));
        let ewma = stats.ewma_ms().unwrap();
        assert!((ewma - 1200.0).abs() < 1e-6, "ewma was {ewma}");
    }

    // S-P1-30: the native bridge builds response headers with
    // `HeaderValue::from_bytes` / `HeaderName::from_bytes` and turns a
    // failure into a 500 instead of writing the bytes, so a CR, LF or NUL
    // in a Python-supplied header can never split the response on the
    // wire. Pin that `http`-crate rejection here: if a future upgrade
    // ever accepted these bytes, the bridge's 500 fallback would silently
    // stop defending the response.
    #[test]
    fn bridge_header_bytes_reject_crlf() {
        assert!(HeaderValue::from_bytes(b"ok").is_ok());
        assert!(HeaderName::from_bytes(b"x-custom").is_ok());
        for bad_value in [
            &b"a\r\nInjected: 1"[..],
            &b"a\rb"[..],
            &b"a\nb"[..],
            &b"a\x00b"[..],
        ] {
            assert!(
                HeaderValue::from_bytes(bad_value).is_err(),
                "{bad_value:?} must be rejected"
            );
        }
        assert!(HeaderName::from_bytes(b"x-bad\r\nname").is_err());
    }
    // Trusted-proxy networks (S-P1-18, M-8): the accept loop's trust
    // decision, mirroring pynext.proxy.parse_trusted_proxies.
    #[test]
    fn trusted_proxy_network_matches_host_and_cidr() {
        let host = TrustedProxyNetwork::parse("127.0.0.1").unwrap();
        assert!(host.contains("127.0.0.1".parse().unwrap()));
        assert!(!host.contains("127.0.0.2".parse().unwrap()));
        let cidr = TrustedProxyNetwork::parse("10.0.0.0/8").unwrap();
        assert!(cidr.contains("10.1.2.3".parse().unwrap()));
        assert!(cidr.contains("10.255.255.255".parse().unwrap()));
        assert!(!cidr.contains("11.0.0.1".parse().unwrap()));
        assert!(!cidr.contains("192.0.2.9".parse().unwrap()));
        // A /32 is a single host; a /0 trusts the whole family.
        assert!(TrustedProxyNetwork::parse("192.0.2.9/32")
            .unwrap()
            .contains("192.0.2.9".parse().unwrap()));
        assert!(TrustedProxyNetwork::parse("0.0.0.0/0")
            .unwrap()
            .contains("203.0.113.7".parse().unwrap()));
    }

    #[test]
    fn trusted_proxy_network_handles_ipv6_and_mapped_ipv4() {
        let v6 = TrustedProxyNetwork::parse("2001:db8::/32").unwrap();
        assert!(v6.contains("2001:db8::1".parse().unwrap()));
        assert!(!v6.contains("2001:db9::1".parse().unwrap()));
        // IPv4-mapped IPv6 normalizes to IPv4 on both sides, matching
        // pynext.proxy: a mapped trusted proxy and a mapped peer match.
        let mapped = TrustedProxyNetwork::parse("::ffff:10.0.0.0/8").unwrap();
        assert!(mapped.contains("10.1.2.3".parse().unwrap()));
        assert!(mapped.contains("::ffff:10.1.2.3".parse().unwrap()));
        let v4 = TrustedProxyNetwork::parse("10.0.0.0/8").unwrap();
        assert!(v4.contains("::ffff:10.1.2.3".parse().unwrap()));
        assert!(!v4.contains("::ffff:11.0.0.1".parse().unwrap()));
        // Families do not cross: an IPv4 network never matches IPv6.
        assert!(!v4.contains("2001:db8::1".parse().unwrap()));
    }

    #[test]
    fn trusted_proxy_network_rejects_bad_entries() {
        for bad in [
            "not-an-ip",
            "10.0.0.0/33",
            "2001:db8::/129",
            "10.0.0.0/",
            "/8",
            "",
            "10.0.0.0/8/1",
        ] {
            assert!(TrustedProxyNetwork::parse(bad).is_err(), "{bad}");
        }
    }
    // S-P1-7: header CTL validation, parser and writer sides share one rule.
    #[test]
    fn header_value_ctl_set_matches_aiohttp_safe_header() {
        // Every CTL byte aiohttp rejects in a header value.
        for byte in (0x00..=0x08).chain(0x0A..=0x1F).chain([0x7F]) {
            assert!(
                header_value_contains_ctl(&[b'a', byte, b'b']),
                "byte 0x{byte:02x} must be a CTL in a header value"
            );
        }
        // HTAB, SP, printable ASCII and obs-text are legal field-value bytes.
        assert!(!header_value_contains_ctl(b"a\tb c~d"));
        assert!(!header_value_contains_ctl(&[0x80, 0xFF]));
        assert!(!header_value_contains_ctl(b""));
    }

    #[test]
    fn header_name_ctl_rejects_any_byte_below_space_or_del() {
        for byte in (0x00..=0x1F).chain([0x7F]) {
            assert!(
                header_name_contains_ctl(&[b'x', byte]),
                "byte 0x{byte:02x} must be a CTL in a header name"
            );
        }
        assert!(!header_name_contains_ctl(b"x-header"));
    }

    #[test]
    fn headers_contain_ctl_covers_names_and_values() {
        let mut headers = HeaderMap::new();
        headers.insert("x-ok", HeaderValue::from_static("fine"));
        assert!(!headers_contain_ctl(&headers));
        // HeaderValue cannot hold a CTL, so exercise the value path via a
        // raw map built from bytes that `HeaderValue::from_bytes` accepts
        // only when clean; the CTL case is covered through
        // `bridge_response_header` below.
        headers.insert("x-tab", HeaderValue::from_static("a\tb"));
        assert!(!headers_contain_ctl(&headers));
    }

    #[test]
    fn bridge_response_header_rejects_ctl_and_accepts_clean() {
        assert!(bridge_response_header(b"x-ok", b"fine").is_some());
        assert!(bridge_response_header(b"x-tab", b"a\tb").is_some());
        assert!(bridge_response_header(b"x-evil", b"a\x01b").is_none());
        assert!(bridge_response_header(b"x-evil", b"a\nb").is_none());
        assert!(bridge_response_header(b"x-evil", b"a\rb").is_none());
        assert!(bridge_response_header(b"x-evil", b"a\x00b").is_none());
        assert!(bridge_response_header(b"x-evil", b"a\x7fb").is_none());
        assert!(bridge_response_header(b"x-\x01vil", b"a").is_none());
        assert!(bridge_response_header(b"bad name", b"a").is_none());
    }
    // Request-smuggling sniff (S-P1-31): pure-function coverage for the
    // first-request header scan. The live behaviour (400 + connection
    // close) is covered from Python by tests/test_request_smuggling.py.
    fn sniffed(raw: &str) -> (Vec<u8>, bool) {
        let mut buf = raw.as_bytes().to_vec();
        let marked = match first_header_block_end(&buf) {
            Some((end, insert_at)) => mark_smuggling_request(&mut buf, end, insert_at),
            None => false,
        };
        (buf, marked)
    }

    #[test]
    fn sniff_marks_content_length_with_transfer_encoding() {
        let (buf, marked) = sniffed(
            "POST /api/input HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\nhello",
        );
        assert!(marked);
        assert!(String::from_utf8_lossy(&buf).contains("x-pynext-smuggling: cl-te"));
        assert!(buf.ends_with(b"\r\n\r\nhello"));
    }

    #[test]
    fn sniff_marks_transfer_encoding_before_content_length() {
        let (buf, marked) = sniffed(
            "POST /api/input HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n",
        );
        assert!(marked);
        assert!(String::from_utf8_lossy(&buf).contains("x-pynext-smuggling: cl-te"));
    }

    #[test]
    fn sniff_leaves_clean_requests_untouched() {
        for raw in [
            "POST /api/input HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            "POST /api/input HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\nhello",
            "GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
        ] {
            let (buf, marked) = sniffed(raw);
            assert!(!marked, "{raw}");
            assert_eq!(buf, raw.as_bytes(), "{raw}");
        }
    }

    #[test]
    fn sniff_leaves_h2c_preface_untouched() {
        let (buf, marked) = sniffed("PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n");
        assert!(!marked);
        assert_eq!(buf, b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n");
    }

    #[test]
    fn sniff_handles_lf_only_header_block() {
        let (buf, marked) = sniffed(
            "POST /api/input HTTP/1.1\nHost: localhost\nContent-Length: 5\nTransfer-Encoding: chunked\n\nhello",
        );
        assert!(marked);
        assert!(String::from_utf8_lossy(&buf).contains("x-pynext-smuggling: cl-te"));
    }

    #[test]
    fn sniff_incomplete_header_block_is_not_marked() {
        let mut buf =
            b"POST /api/input HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n".to_vec();
        assert_eq!(first_header_block_end(&buf), None);
        assert!(!mark_smuggling_request(&mut buf, 0, 0));
    }

    #[test]
    fn smuggling_framing_rejects_marker_and_bad_transfer_encoding() {
        let mut headers = HeaderMap::new();
        headers.insert("x-pynext-smuggling", HeaderValue::from_static("cl-te"));
        assert!(smuggling_framing(&headers, Version::HTTP_11));
        let mut headers = HeaderMap::new();
        headers.insert(header::TRANSFER_ENCODING, HeaderValue::from_static("gzip"));
        assert!(smuggling_framing(&headers, Version::HTTP_11));
        let mut headers = HeaderMap::new();
        headers.insert(
            header::TRANSFER_ENCODING,
            HeaderValue::from_static("gzip, chunked"),
        );
        assert!(!smuggling_framing(&headers, Version::HTTP_11));
        let mut headers = HeaderMap::new();
        headers.insert(
            header::TRANSFER_ENCODING,
            HeaderValue::from_static("chunked"),
        );
        assert!(smuggling_framing(&headers, Version::HTTP_10));
    }
}
