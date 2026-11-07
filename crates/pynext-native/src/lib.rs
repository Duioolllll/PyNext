use axum::{
    body::{Body, Bytes, HttpBody},
    extract::{Request, State},
    http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode, Version},
    response::Response as AxumResponse,
    routing::any,
    Router as AxumRouter,
};
use http_body_util::BodyExt as _;
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder as AutoBuilder;
use pynext_cache::{CacheResult, PageCache};
use pynext_router::Router;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;
use tokio::sync::{mpsc as tokio_mpsc, oneshot, OwnedSemaphorePermit, Semaphore};
use tower_service::Service as TowerService;

#[pyclass(name = "Router")]
struct PyRouter {
    inner: Router,
}

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

    fn matches(&self, path: &str) -> Option<(String, HashMap<String, Vec<String>>)> {
        self.inner.matches(path)
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

fn render_node(node: &Value, output: &mut String, depth: usize) -> Result<(), String> {
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
                render_node(child, output, depth + 1)?;
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
            if !matches!(strategy, "load" | "visible" | "idle") {
                return Err("Invalid island loading strategy".into());
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
            output.push_str("\">");
            for child in obj
                .get("children")
                .and_then(Value::as_array)
                .ok_or("Island children must be an array")?
            {
                render_node(child, output, depth + 1)?;
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
            output.push_str("\"></script>");
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
                    | "embed"
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
                render_node(child, output, depth + 1)?;
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
fn render(py: Python<'_>, tree_json: &str) -> PyResult<String> {
    py.allow_threads(|| {
        let node: Value =
            serde_json::from_str(tree_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut result = String::new();
        render_node(&node, &mut result, 0).map_err(PyValueError::new_err)?;
        Ok(result)
    })
}

struct ServerState {
    callback: Py<PyAny>,
    capacity: Arc<Semaphore>,
    waiting_capacity: Arc<Semaphore>,
    acquire_timeout: Duration,
    body_capacity: Arc<Semaphore>,
    body_timeout: Duration,
    static_pages: HashMap<String, Bytes>,
    public_assets: HashMap<String, PublicAsset>,
    client_assets: HashMap<String, Bytes>,
    asset_etags: HashMap<String, HeaderValue>,
    isr_cache: Arc<PageCache>,
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
        Python::with_gil(|py| {
            let _ = stream.bind(py).call_method0("close");
        });
    });
}

fn streaming_body(stream: Py<PyAny>, permit: OwnedSemaphorePermit) -> Body {
    let stream = Arc::new(stream);
    let (sender, receiver) = tokio_mpsc::channel::<Result<Bytes, std::io::Error>>(1);
    let failed = Arc::new(AtomicBool::new(false));
    let producer_failed = Arc::clone(&failed);
    tokio::spawn(async move {
        let _permit = permit;
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
                Python::with_gil(|py| {
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
    nosniff: bool,
    vary: bool,
}

fn static_response(
    parts: &axum::http::request::Parts,
    content: &Bytes,
    policy: AssetPolicy<'_>,
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
        Body::from(content.slice(start..end + 1))
    } else {
        Body::from(content.clone())
    };
    let mut builder = AxumResponse::builder()
        .status(status)
        .header(header::CONTENT_TYPE, policy.content_type.clone())
        .header(header::ETAG, policy.etag.clone())
        .header(header::CACHE_CONTROL, policy.cache_control)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(
            "content-security-policy",
            "script-src 'self'; object-src 'none'",
        );
    if status != StatusCode::NOT_MODIFIED {
        builder = builder.header(header::CONTENT_LENGTH, response_length.to_string());
    }
    if let Some(content_range) = content_range {
        builder = builder.header(header::CONTENT_RANGE, content_range);
    }
    if policy.nosniff {
        builder = builder.header("x-content-type-options", "nosniff");
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

fn plain_response(status: StatusCode, body: &'static str) -> AxumResponse<Body> {
    AxumResponse::builder()
        .status(status)
        .header("content-type", "text/plain; charset=utf-8")
        .header(
            "content-security-policy",
            "script-src 'self'; object-src 'none'",
        )
        .body(Body::from(body))
        .expect("static response is valid")
}

fn busy_response() -> AxumResponse<Body> {
    AxumResponse::builder()
        .status(StatusCode::SERVICE_UNAVAILABLE)
        .header("content-type", "text/plain; charset=utf-8")
        .header(
            "content-security-policy",
            "script-src 'self'; object-src 'none'",
        )
        .header("retry-after", "1")
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
}

/// Read a request body with our own byte count instead of classifying the
/// error `to_bytes` returns: that classification downcasts through
/// `error.source()`, so it breaks if axum/http-body-util ever changes how
/// the limit error is wrapped. A body that grows past the limit is 413 no
/// matter how the underlying error is shaped.
async fn read_limited_body(mut body: Body, limit: usize) -> Result<Bytes, BodyReadError> {
    let mut buf = Vec::new();
    while let Some(frame) = body.frame().await {
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

async fn dispatch(State(state): State<Arc<ServerState>>, request: Request) -> AxumResponse<Body> {
    let (parts, body) = request.into_parts();
    if conflicting_content_length(&parts.headers) {
        return close_with_unread_body(
            plain_response(StatusCode::BAD_REQUEST, "Bad Request"),
            parts.version,
        );
    }
    // Empty requests do not compete with clients currently uploading bodies.
    let body = if body.is_end_stream() {
        Bytes::new()
    } else {
        let _body_permit = match state.body_capacity.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                return close_with_unread_body(busy_response(), parts.version);
            }
        };
        match tokio::time::timeout(
            state.body_timeout,
            read_limited_body(body, MAX_REQUEST_BODY),
        )
        .await
        {
            Ok(Ok(bytes)) => bytes,
            Ok(Err(error)) => {
                let (status, message) = match error {
                    BodyReadError::TooLarge => (StatusCode::PAYLOAD_TOO_LARGE, "Payload Too Large"),
                    BodyReadError::BadRequest => (StatusCode::BAD_REQUEST, "Bad Request"),
                };
                return close_with_unread_body(plain_response(status, message), parts.version);
            }
            Err(_) => {
                let mut response = plain_response(StatusCode::REQUEST_TIMEOUT, "Request Timeout");
                response
                    .headers_mut()
                    .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
                return close_with_unread_body(response, parts.version);
            }
        }
    };
    if parts.method == axum::http::Method::GET || parts.method == axum::http::Method::HEAD {
        if let Some(path) = static_path(parts.uri.path()) {
            if let Some(content) = state.client_assets.get(&path) {
                return static_response(
                    &parts,
                    content,
                    AssetPolicy {
                        content_type: &HeaderValue::from_static("text/javascript; charset=utf-8"),
                        etag: &state.asset_etags[&path],
                        cache_control: "public, max-age=31536000, immutable",
                        nosniff: true,
                        vary: false,
                    },
                );
            }
            if let Some(asset) = state.public_assets.get(&path) {
                return static_response(
                    &parts,
                    &asset.body,
                    AssetPolicy {
                        content_type: &asset.content_type,
                        etag: &state.asset_etags[&path],
                        cache_control: "public, max-age=0, must-revalidate",
                        nosniff: true,
                        vary: false,
                    },
                );
            }
            if let Some(content) = state.static_pages.get(&path) {
                return static_response(
                    &parts,
                    content,
                    AssetPolicy {
                        content_type: &HeaderValue::from_static("text/html; charset=utf-8"),
                        etag: &state.asset_etags[&path],
                        cache_control: "public, max-age=0, must-revalidate",
                        nosniff: false,
                        vary: true,
                    },
                );
            }
            if !parts.headers.contains_key("authorization") && !parts.headers.contains_key("cookie")
            {
                if let CacheResult::Fresh(content) = state.isr_cache.lookup(&path) {
                    let length = content.len();
                    let body = if parts.method == axum::http::Method::HEAD {
                        Body::empty()
                    } else {
                        Body::from(content)
                    };
                    return AxumResponse::builder()
                        .status(StatusCode::OK)
                        .header("content-type", "text/html; charset=utf-8")
                        .header("content-length", length.to_string())
                        .header("cache-control", "public, max-age=0, must-revalidate")
                        .header(
                            "content-security-policy",
                            "script-src 'self'; object-src 'none'",
                        )
                        .header("vary", "Cookie, Authorization")
                        .body(body)
                        .expect("cached response is valid");
                }
            }
        }
    }
    // Static/cache hits never compete with Python handler execution.
    // Wait briefly for a slot instead of rejecting the moment the server is
    // busy: a burst that clears within the timeout degrades gracefully, and
    // only sustained overload gets a 503 with Retry-After. Waiting requests
    // hold a Tokio task, not a Python worker thread. Bound this queue too:
    // each waiter can already hold a fully buffered request body.
    let waiting = match state.waiting_capacity.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => return busy_response(),
    };
    let permit = match tokio::time::timeout(
        state.acquire_timeout,
        state.capacity.clone().acquire_owned(),
    )
    .await
    {
        Ok(Ok(permit)) => permit,
        _ => return busy_response(),
    };
    drop(waiting);
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
    let result = tokio::task::spawn_blocking(move || {
        let result = Python::with_gil(|py| {
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
        (result, permit)
    })
    .await;
    match result {
        Ok((Ok((status, headers, body)), permit)) => {
            let Ok(status) = StatusCode::from_u16(status) else {
                if let BridgeBody::Streaming(stream) = body {
                    close_stream(Arc::new(stream));
                }
                return plain_response(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
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
                    return plain_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Internal Server Error",
                    );
                };
                response.headers_mut().append(key, value);
            }
            *response.body_mut() = match body {
                BridgeBody::Buffered(body) => {
                    drop(permit);
                    Body::from(body)
                }
                BridgeBody::Streaming(stream) => streaming_body(stream, permit),
            };
            response
        }
        _ => plain_response(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"),
    }
}

/// Bridge from hyper's connection service to the axum router: the router is a
/// tower service, so each connection polls it ready and then calls it with the
/// request body converted to axum's `Body`.
struct HyperService {
    router: AxumRouter,
}

impl hyper::service::Service<hyper::Request<hyper::body::Incoming>> for HyperService {
    type Response = AxumResponse<Body>;
    type Error = std::convert::Infallible;
    type Future = std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Self::Response, Self::Error>> + Send>,
    >;

    fn call(&self, req: hyper::Request<hyper::body::Incoming>) -> Self::Future {
        let mut router = self.router.clone();
        Box::pin(async move {
            std::future::poll_fn(|cx| {
                <AxumRouter as TowerService<Request>>::poll_ready(&mut router, cx)
            })
            .await?;
            let req = req.map(Body::new);
            router.call(req).await
        })
    }
}

// Slow header transmission (slow-loris on the request line/headers) is bounded
// by hyper's per-connection header read timeout, so one idle connection cannot
// hold a slot in the accept loop forever. This is separate from the body read
// timeout, which only starts once headers are complete.
const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(10);

#[pyclass]
struct NativeServer {
    shutdown: Option<oneshot::Sender<()>>,
    thread: Option<JoinHandle<()>>,
    #[pyo3(get)]
    address: String,
}

#[pymethods]
impl NativeServer {
    #[new]
    #[pyo3(signature = (callback, host, port, static_pages, public_assets, client_assets, isr_cache, asset_etags, max_concurrent=64, acquire_timeout_ms=1000, body_capacity=16, body_timeout_ms=10000))]
    #[allow(clippy::too_many_arguments)]
    fn new(
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
        body_capacity: usize,
        body_timeout_ms: u64,
    ) -> PyResult<Self> {
        if max_concurrent == 0 || max_concurrent > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "max_concurrent is outside the supported range",
            ));
        }
        if body_capacity == 0 || body_capacity > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(
                "body_capacity is outside the supported range",
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
        let listener = std::net::TcpListener::bind((host, port))
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
        listener
            .set_nonblocking(true)
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
        let address = listener
            .local_addr()
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?
            .to_string();
        let (shutdown, rx) = oneshot::channel();
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
                    waiting_capacity: Arc::new(Semaphore::new(max_concurrent.min(64))),
                    acquire_timeout: Duration::from_millis(acquire_timeout_ms),
                    // Body intake gets fewer slots than Python execution on
                    // purpose: a flood of slow uploads can then only take the
                    // body slots, leaving execution slots free for requests
                    // whose bodies are already in. The cost is that a burst of
                    // legitimate concurrent uploads 503s sooner; raise
                    // body_capacity if uploads, not execution, are the
                    // bottleneck.
                    body_capacity: Arc::new(Semaphore::new(body_capacity)),
                    body_timeout: Duration::from_millis(body_timeout_ms),
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
                });
                let app = AxumRouter::new().fallback(any(dispatch)).with_state(state);
                let mut shutdown = std::pin::pin!(async {
                    let _ = rx.await;
                });
                let mut connections = tokio::task::JoinSet::new();
                loop {
                    tokio::select! {
                        _ = &mut shutdown => break,
                        accepted = listener.accept() => {
                            let (stream, _) = match accepted {
                                Ok(pair) => pair,
                                Err(_) => {
                                    tokio::time::sleep(Duration::from_millis(10)).await;
                                    continue;
                                }
                            };
                            let service = HyperService {
                                router: app.clone(),
                            };
                            // Auto-detect the protocol per connection: a
                            // client that opens with the HTTP/2
                            // prior-knowledge preface (`PRI * HTTP/2.0`) is
                            // served h2c, everything else is HTTP/1.1. The
                            // old Http1Builder-only loop turned the h2
                            // preface into a VersionH2 error and dropped the
                            // connection. The 10s header read timeout still
                            // applies to the HTTP/1.1 side via `.http1()`.
                            connections.spawn(async move {
                                let mut builder = AutoBuilder::new(TokioExecutor::new());
                                builder
                                    .http1()
                                    .timer(TokioTimer::new())
                                    .header_read_timeout(HEADER_READ_TIMEOUT);
                                let _ = builder
                                    .serve_connection(TokioIo::new(stream), service)
                                    .await;
                            });
                        }
                        _ = connections.join_next(), if !connections.is_empty() => {}
                    }
                }
                // Drain in-flight connections, bounded so `stop()` cannot
                // hang on a stuck connection.
                let _ = tokio::time::timeout(Duration::from_secs(30), async {
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
        })
    }

    fn stop(&mut self, py: Python<'_>) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(thread) = self.thread.take() {
            py.allow_threads(move || {
                let _ = thread.join();
            });
        }
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
    m.add_class::<PyRouter>()?;
    m.add_class::<PyPageCache>()?;
    m.add_class::<NativeServer>()?;
    m.add_class::<RequestCancellation>()?;
    m.add_function(wrap_pyfunction!(render, m)?)?;
    Ok(())
}
