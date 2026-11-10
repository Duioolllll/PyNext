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
    // Bounds slow request headers and, because hyper applies it to every
    // header read on the connection, idle keep-alive time between requests.
    // Zero disables the timeout.
    header_read_timeout: Duration,
    // Minimum average body receive rate in bytes/second (0 disables).
    body_min_rate: usize,
    static_pages: HashMap<String, Bytes>,
    public_assets: HashMap<String, PublicAsset>,
    client_assets: HashMap<String, Bytes>,
    asset_etags: HashMap<String, HeaderValue>,
    isr_cache: Arc<PageCache>,
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
) -> Body {
    let stream = Arc::new(stream);
    let (sender, receiver) = tokio_mpsc::channel::<Result<Bytes, std::io::Error>>(1);
    let failed = Arc::new(AtomicBool::new(false));
    let producer_failed = Arc::clone(&failed);
    tokio::spawn(async move {
        let _permit = permit;
        // The per-IP slot stays held until the stream ends, like the
        // global execution slot (H-5).
        let _ip_guard = ip_guard;
        loop {
            let slot = match tokio::time::timeout(Duration::from_secs(30), sender.reserve()).await {
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
    let body =
        futures_util::stream::unfold((receiver, failed), |(mut receiver, failed)| async move {
            match receiver.recv().await {
                Some(item) => Some((item, (receiver, failed))),
                None if failed.swap(false, Ordering::AcqRel) => Some((
                    Err(std::io::Error::other("PyNext response stream stalled")),
                    (receiver, failed),
                )),
                None => None,
            }
        });
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

fn static_response(
    parts: &axum::http::request::Parts,
    content: &Bytes,
    policy: AssetPolicy<'_>,
    security_headers: &[(Vec<u8>, Vec<u8>)],
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
    if status != StatusCode::NOT_MODIFIED {
        builder = builder.header(header::CONTENT_LENGTH, response_length.to_string());
    }
    if let Some(content_range) = content_range {
        builder = builder.header(header::CONTENT_RANGE, content_range);
    }
    if policy.vary {
        builder = builder.header(header::VARY, "Cookie, Authorization");
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

/// Conflicting `Content-Length` headers must fail as a clean 400 in dispatch
/// instead of reaching hyper as a protocol error that just drops the
/// connection. Identical duplicates are one value and pass; anything
/// unparsable or disagreeing is a conflict.
///
/// This check is a backstop, not the deciding layer in the cases the tests
/// cover: for an ordinary request hyper's parser turns conflicting
/// `Content-Length` values into an automatic 400 before dispatch ever runs,
/// and when `Transfer-Encoding: chunked` is present hyper drops
/// `Content-Length` entirely (it removes earlier CL headers and skips later
/// ones), so the request is framed as chunked and the conflicting values
/// never reach dispatch — see
/// `test_transfer_encoding_wins_over_conflicting_content_length`. Keep the
/// check anyway: it is the layer that catches conflicting values if hyper's
/// parsing ever lets them through.
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
        if let Ok(data) = frame.into_data() {
            if buf.len() + data.len() > limit {
                return Err(BodyReadError::TooLarge);
            }
            buf.extend_from_slice(&data);
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
    // Hold one outgoing wait across loop iterations. Recreating the wait
    // inside `select!` would drop it whenever a socket message wins, but
    // the dropped wait's blocking task keeps waiting on the outbox and can
    // steal the next item. The wait has a bounded timeout as a safety net;
    // the Python side also enqueues a "done" item when the handler ends,
    // so a finished handler is noticed promptly.
    let mut outgoing_wait = Box::pin(ws_take_outgoing(&session, Duration::from_secs(5)));
    loop {
        tokio::select! {
            message = socket.recv() => {
                match message {
                    Some(Ok(Message::Text(text))) => {
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
                        let _ = socket.send(Message::Pong(data)).await;
                    }
                    Some(Ok(Message::Pong(_))) => {}
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
                    }
                    Some(WsOutgoing::Bytes(data)) => {
                        if socket.send(Message::Binary(data.into())).await.is_err() {
                            break;
                        }
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
    if conflicting_content_length(&parts.headers) {
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
                        },
                        &state.security_headers,
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
                        },
                        &state.security_headers,
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
                        },
                        &state.security_headers,
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
                    return apply_path_headers(
                        apply_security_headers(
                            AxumResponse::builder()
                                .status(StatusCode::OK)
                                .header("content-type", "text/html; charset=utf-8")
                                .header("content-length", length.to_string())
                                .header("cache-control", "public, max-age=0, must-revalidate")
                                .header("vary", "Cookie, Authorization"),
                            &state.security_headers,
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
                let (Ok(key), Ok(value)) = (
                    HeaderName::from_bytes(&key),
                    HeaderValue::from_bytes(&value),
                ) else {
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
                BridgeBody::Streaming(stream) => {
                    streaming_body(stream, permit, ip_guard, hold_stats, acquired_at)
                }
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

/// The peer address of the TCP connection a request arrived on, carried in
/// request extensions so dispatch can hand the client IP to the Python
/// bridge. The Python rate limiter keys buckets on it (spec §11); without
/// this the bridge would have no trustworthy per-client key at all.
#[derive(Clone)]
struct PeerAddr(SocketAddr);

/// Bridge from hyper's connection service to the axum router: the router is a
/// tower service, so each connection polls it ready and then calls it with the
/// request body converted to axum's `Body`.
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
    shutdown: Option<oneshot::Sender<()>>,
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
    #[pyo3(signature = (callback, host, port, static_pages, public_assets, client_assets, isr_cache, asset_etags, max_concurrent=64, acquire_timeout_ms=1000, max_waiting=None, body_capacity=16, body_timeout_ms=10000, websocket_paths=None, websocket_callback=None, reuse_port=false, route_patterns=None, middleware_enabled=false, shared_cache_enabled=false, websocket_protocols=None, websocket_max_message=65536, listener=None, max_connections=1024, max_connections_per_ip=128, header_read_timeout_ms=10000, body_min_rate_bytes_per_sec=1024, security_headers=None, drain_timeout_secs=30.0, websocket_origin_callback=None, websocket_rate_limit_callback=None, websocket_auth_callback=None, http_rate_limit_callback=None, path_headers=None, max_concurrent_per_ip=16, acquire_timeout_max_ms=None, fast_path_max_concurrent=None))]
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
        drain_timeout_secs: f64,
        websocket_origin_callback: Option<Py<PyAny>>,
        websocket_rate_limit_callback: Option<Py<PyAny>>,
        websocket_auth_callback: Option<Py<PyAny>>,
        http_rate_limit_callback: Option<Py<PyAny>>,
        path_headers: Option<Vec<(String, Vec<(Vec<u8>, Vec<u8>)>)>>,
        max_concurrent_per_ip: usize,
        acquire_timeout_max_ms: Option<u64>,
        fast_path_max_concurrent: Option<usize>,
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
        let isr_cache = isr_cache.inner.clone();
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
        let (shutdown, rx) = oneshot::channel();
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
                    header_read_timeout: Duration::from_millis(header_read_timeout_ms),
                    body_min_rate: body_min_rate_bytes_per_sec,
                    static_pages: static_pages
                        .into_iter()
                        .map(|(path, body)| (path, Bytes::from(body)))
                        .collect(),
                    public_assets,
                    client_assets: client_assets
                        .into_iter()
                        .map(|(path, body)| (path, Bytes::from(body)))
                        .collect(),
                    asset_etags,
                    isr_cache,
                    middleware_enabled,
                    shared_cache_enabled,
                    security_headers: security_headers.unwrap_or_default(),
                    path_headers,
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
                let mut shutdown = std::pin::pin!(async {
                    let _ = rx.await;
                });
                let mut connections = tokio::task::JoinSet::new();
                let (connection_shutdown, _) = watch::channel(false);
                loop {
                    tokio::select! {
                        _ = &mut shutdown => break,
                        accepted = listener.accept() => {
                            let (stream, peer_addr) = match accepted {
                                Ok(pair) => pair,
                                Err(_) => {
                                    tokio::time::sleep(Duration::from_millis(10)).await;
                                    continue;
                                }
                            };
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
                            // IPv4 form, matching the Python rate limiter.
                            let peer_ip = match peer_addr.ip() {
                                IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
                                    Some(v4) => IpAddr::V4(v4),
                                    None => peer_addr.ip(),
                                },
                                ip => ip,
                            };
                            let total_permit = match limits
                                .connection_capacity
                                .clone()
                                .try_acquire_owned()
                            {
                                Ok(permit) => permit,
                                Err(_) => continue,
                            };
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
                            let per_ip_guard = PerIpConnectionGuard {
                                counts: Arc::clone(&limits.per_ip_connections),
                                ip: peer_ip,
                            };
                            let header_read_timeout = limits.header_read_timeout;
                            let service = HyperService {
                                router: app.clone(),
                                peer_addr: Some(peer_addr),
                            };
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
                                        stream.peek(&mut preface),
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
                }
                let _ = connection_shutdown.send(true);
                // Drain in-flight connections, bounded so `stop()` cannot
                // hang on a stuck connection. The bound is the Python
                // shutdown timeout (B6): serve() already waited that long
                // for Python handlers, so this only covers connections
                // whose close handshake is still in flight.
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

    fn stop(&mut self, py: Python<'_>) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
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
            let _ = shutdown.send(());
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
}
