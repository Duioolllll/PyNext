use axum::{
    body::{to_bytes, Body, Bytes, HttpBody},
    extract::{Request, State},
    http::{header, HeaderName, HeaderValue, StatusCode, Version},
    response::Response as AxumResponse,
    routing::any,
    Router as AxumRouter,
};
use pynext_cache::{CacheResult, PageCache};
use pynext_router::Router;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use serde_json::Value;
use std::collections::HashMap;
use std::error::Error as _;
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;
use tokio::sync::{oneshot, Semaphore};

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
    body_capacity: Arc<Semaphore>,
    static_pages: HashMap<String, Bytes>,
    public_assets: HashMap<String, PublicAsset>,
    client_assets: HashMap<String, Bytes>,
    isr_cache: Arc<PageCache>,
}

const MAX_REQUEST_BODY: usize = 1_048_576;
const BODY_READ_TIMEOUT: Duration = Duration::from_secs(10);

struct PublicAsset {
    body: Bytes,
    content_type: HeaderValue,
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
    // Empty requests do not compete with clients currently uploading bodies.
    let body = if body.is_end_stream() {
        Bytes::new()
    } else {
        let _body_permit = match state.body_capacity.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                return close_with_unread_body(
                    plain_response(StatusCode::SERVICE_UNAVAILABLE, "Server Busy"),
                    parts.version,
                );
            }
        };
        match tokio::time::timeout(BODY_READ_TIMEOUT, to_bytes(body, MAX_REQUEST_BODY)).await {
            Ok(Ok(bytes)) => bytes,
            Ok(Err(error)) => {
                let status = if error
                    .source()
                    .is_some_and(|source| source.is::<http_body_util::LengthLimitError>())
                {
                    StatusCode::PAYLOAD_TOO_LARGE
                } else {
                    StatusCode::BAD_REQUEST
                };
                let message = if status == StatusCode::PAYLOAD_TOO_LARGE {
                    "Payload Too Large"
                } else {
                    "Bad Request"
                };
                return close_with_unread_body(plain_response(status, message), parts.version);
            }
            Err(_) => {
                return close_with_unread_body(
                    plain_response(StatusCode::REQUEST_TIMEOUT, "Request Timeout"),
                    parts.version,
                );
            }
        }
    };
    if parts.method == axum::http::Method::GET || parts.method == axum::http::Method::HEAD {
        if let Some(path) = static_path(parts.uri.path()) {
            if let Some(content) = state.client_assets.get(&path) {
                let body = if parts.method == axum::http::Method::HEAD {
                    Body::empty()
                } else {
                    Body::from(content.clone())
                };
                return AxumResponse::builder()
                    .status(StatusCode::OK)
                    .header("content-type", "text/javascript; charset=utf-8")
                    .header("content-length", content.len().to_string())
                    .header("cache-control", "public, max-age=31536000, immutable")
                    .header("x-content-type-options", "nosniff")
                    .header(
                        "content-security-policy",
                        "script-src 'self'; object-src 'none'",
                    )
                    .body(body)
                    .expect("client asset response is valid");
            }
            if let Some(asset) = state.public_assets.get(&path) {
                let body = if parts.method == axum::http::Method::HEAD {
                    Body::empty()
                } else {
                    Body::from(asset.body.clone())
                };
                return AxumResponse::builder()
                    .status(StatusCode::OK)
                    .header("content-type", asset.content_type.clone())
                    .header("content-length", asset.body.len().to_string())
                    .header("cache-control", "public, max-age=0, must-revalidate")
                    .header("x-content-type-options", "nosniff")
                    .header(
                        "content-security-policy",
                        "script-src 'self'; object-src 'none'",
                    )
                    .body(body)
                    .expect("public asset response is valid");
            }
            if let Some(content) = state.static_pages.get(&path) {
                let length = content.len();
                let body = if parts.method == axum::http::Method::HEAD {
                    Body::empty()
                } else {
                    Body::from(content.clone())
                };
                return AxumResponse::builder()
                    .status(StatusCode::OK)
                    .header("content-type", "text/html; charset=utf-8")
                    .header("content-length", length.to_string())
                    .header(
                        "content-security-policy",
                        "script-src 'self'; object-src 'none'",
                    )
                    .header("vary", "Cookie, Authorization")
                    .body(body)
                    .expect("static response is valid");
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
    let permit = match state.capacity.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => return plain_response(StatusCode::SERVICE_UNAVAILABLE, "Server Busy"),
    };
    let method = parts.method.to_string();
    let path = parts.uri.path().to_string();
    let query = parts.uri.query().unwrap_or("").as_bytes().to_vec();
    let headers: Vec<(Vec<u8>, Vec<u8>)> = parts
        .headers
        .iter()
        .map(|(k, v)| (k.as_str().as_bytes().to_vec(), v.as_bytes().to_vec()))
        .collect();
    let result = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        Python::with_gil(|py| {
            let cb = state.callback.bind(py);
            let result = cb.call1((
                method,
                path,
                PyBytes::new(py, &query),
                headers,
                PyBytes::new(py, &body),
            ))?;
            result.extract::<(u16, Vec<(Vec<u8>, Vec<u8>)>, Vec<u8>)>()
        })
    })
    .await;
    match result {
        Ok(Ok((status, headers, body))) => {
            let Ok(status) = StatusCode::from_u16(status) else {
                return plain_response(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
            };
            let mut response = AxumResponse::new(Body::from(body));
            *response.status_mut() = status;
            for (key, value) in headers {
                let (Ok(key), Ok(value)) = (
                    HeaderName::from_bytes(&key),
                    HeaderValue::from_bytes(&value),
                ) else {
                    return plain_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Internal Server Error",
                    );
                };
                response.headers_mut().append(key, value);
            }
            response
        }
        _ => plain_response(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"),
    }
}

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
    fn new(
        callback: Py<PyAny>,
        host: &str,
        port: u16,
        static_pages: HashMap<String, Vec<u8>>,
        public_assets: HashMap<String, (Vec<u8>, String)>,
        client_assets: HashMap<String, Vec<u8>>,
        isr_cache: PyRef<'_, PyPageCache>,
    ) -> PyResult<Self> {
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
        let thread = std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .expect("Tokio runtime");
            runtime.block_on(async move {
                let listener =
                    tokio::net::TcpListener::from_std(listener).expect("configured listener");
                let state = Arc::new(ServerState {
                    callback,
                    capacity: Arc::new(Semaphore::new(64)),
                    body_capacity: Arc::new(Semaphore::new(64)),
                    static_pages: static_pages
                        .into_iter()
                        .map(|(path, body)| (path, Bytes::from(body)))
                        .collect(),
                    public_assets,
                    client_assets: client_assets
                        .into_iter()
                        .map(|(path, body)| (path, Bytes::from(body)))
                        .collect(),
                    isr_cache,
                });
                let app = AxumRouter::new().fallback(any(dispatch)).with_state(state);
                if let Err(e) = axum::serve(listener, app)
                    .with_graceful_shutdown(async {
                        let _ = rx.await;
                    })
                    .await
                {
                    eprintln!("PyNext server failed: {e}");
                }
            });
        });
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
    m.add_function(wrap_pyfunction!(render, m)?)?;
    Ok(())
}
