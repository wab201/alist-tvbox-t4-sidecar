use std::{
    collections::HashMap,
    env, fs,
    io::{self, Write},
    net::SocketAddr,
    process::ExitCode,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use axum::{
    Json, Router,
    body::Body,
    extract::{Path, State},
    http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    serve::ListenerExt,
};
use futures_util::StreamExt;
use reqwest::{Client, Url};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

mod gateway;
mod hedge;
mod logging;
mod observe;
mod startup;
mod upstream;
use hedge::{HedgePolicy, Opened};
use observe::{Observation, observed};
use startup::{STARTUP_QUANTUM, Starting, StartupFairness};
use upstream::Upstream;

fn configure_media_socket(stream: &mut TcpStream) {
    // Match the ASGI server: do not delay short headers/first chunks behind Nagle.
    if stream.set_nodelay(true).is_err() {
        let _ = writeln!(
            io::stderr().lock(),
            "{{\"event\":\"relay.socket_config_failed\"}}"
        );
    }
}

// Assets come from a local manifest or the private Python control plane, never client URLs.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AssetConfig {
    url: String,
    #[serde(default)]
    headers: HashMap<String, String>,
    #[serde(default = "default_mime")]
    mime: String,
    #[serde(default)]
    hedge: bool,
}

fn default_mime() -> String {
    "application/octet-stream".to_owned()
}

struct Asset {
    url: Url,
    headers: HeaderMap,
    mime: HeaderValue,
    hedge: bool,
    hedge_cooldown_until_ms: AtomicU64,
    admission: Option<Arc<Semaphore>>,
}

#[derive(Default)]
struct Counters {
    active: AtomicU64,
    requests: AtomicU64,
    completed: AtomicU64,
    cancelled: AtomicU64,
    errors: AtomicU64,
    bytes: AtomicU64,
    hedge_started: AtomicU64,
    hedge_wins: AtomicU64,
    hedge_skipped: AtomicU64,
    hedge_cooldown_skipped: AtomicU64,
    hedge_budget_skipped: AtomicU64,
    hedge_rejected: AtomicU64,
    hedge_active: AtomicU64,
    hedge_peak: AtomicU64,
    log_events: AtomicU64,
    log_encode_ns: AtomicU64,
    log_write_ns: AtomicU64,
    log_max_write_ns: AtomicU64,
    log_written: AtomicU64,
    log_write_errors: AtomicU64,
    log_pending: AtomicU64,
    log_dropped_complete: AtomicU64,
    log_dropped_error: AtomicU64,
    log_dropped_cancelled: AtomicU64,
    log_disconnected: AtomicU64,
}

struct App {
    assets: HashMap<String, Arc<Asset>>,
    gateway: Option<gateway::Gateway>,
    client: Upstream,
    counters: Arc<Counters>,
    trace: bool,
    observe_startup: bool,
    observe_log: bool,
    logger: Option<logging::AsyncLog>,
    startup_limit: usize,
    yield_after_first_body: bool,
    delivery_budget: usize,
    startup: Option<Arc<StartupFairness>>,
    hedge: HedgePolicy,
    started: Instant,
}

fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
}

fn retain_authorization(previous: &Url, next: &Url) -> bool {
    same_origin(previous, next)
        || previous.host_str() == next.host_str()
            && previous.scheme() == "http"
            && next.scheme() == "https"
            && previous.port_or_known_default() == Some(80)
            && next.port_or_known_default() == Some(443)
}

impl App {
    async fn upstream(
        &self,
        method: Method,
        mut url: Url,
        mut headers: HeaderMap,
    ) -> Result<reqwest::Response, &'static str> {
        for redirects in 0..=20 {
            let response = self
                .client
                .send(method.clone(), url.clone(), headers.clone())
                .await?;
            if !matches!(response.status().as_u16(), 301 | 302 | 303 | 307 | 308) {
                return Ok(response);
            }
            let Some(location) = response.headers().get("location") else {
                return Ok(response);
            };
            if redirects == 20 {
                return Err("upstream redirect limit");
            }
            let next = url
                .join(location.to_str().map_err(|_| "invalid redirect")?)
                .map_err(|_| "invalid redirect")?;
            if !matches!(next.scheme(), "http" | "https") {
                return Err("unsupported redirect");
            }
            // Match HTTPX: reselect Cookie from the jar on *every* redirect.
            headers.remove("cookie");
            if !same_origin(&url, &next) {
                headers.remove("host");
                if !retain_authorization(&url, &next) {
                    headers.remove("authorization");
                }
            }
            drop(response);
            url = next;
        }
        unreachable!()
    }
}

struct Transfer {
    counters: Arc<Counters>,
    id: u64,
    client_trace: Option<String>,
    started: Instant,
    bytes: u64,
    status: u16,
    reason: &'static str,
    upstream_headers_ms: Option<u128>,
    first_body_ms: Option<u128>,
    handler_us: Option<u64>,
    upstream_request_us: Option<u64>,
    upstream_headers_us: Option<u64>,
    first_body_us: Option<u64>,
    response_ready_us: Option<u64>,
    body_poll_us: Option<u64>,
    first_body_size: Option<usize>,
    chunks: u64,
    max_chunk_size: usize,
    upstream_chunks: u64,
    max_upstream_chunk_size: usize,
    hedged: bool,
    hedge_skipped: bool,
    hedge_cooldown: bool,
    hedge_budget: bool,
    hedge_winner: u8,
    startup: Option<Starting>,
    open_observation: Option<Arc<Observation>>,
    body_observation: Option<Arc<Observation>>,
    observe_log: bool,
    logger: Option<logging::AsyncLog>,
    admission: Option<OwnedSemaphorePermit>,
    admission_wait_us: u64,
    trace_id: String,
    parent_trace: String,
    method: String,
    range: bool,
    control_ms: Option<u128>,
}

fn unix_us() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_micros() as u64
}

impl Transfer {
    fn new(counters: Arc<Counters>, client_trace: Option<String>) -> Self {
        counters.active.fetch_add(1, Ordering::Relaxed);
        let id = counters.requests.fetch_add(1, Ordering::Relaxed) + 1;
        let handler_us = client_trace.as_ref().map(|_| unix_us());
        Self {
            counters,
            id,
            client_trace,
            started: Instant::now(),
            bytes: 0,
            status: 0,
            reason: "client_disconnect",
            upstream_headers_ms: None,
            first_body_ms: None,
            handler_us,
            upstream_request_us: None,
            upstream_headers_us: None,
            first_body_us: None,
            response_ready_us: None,
            body_poll_us: None,
            first_body_size: None,
            chunks: 0,
            max_chunk_size: 0,
            upstream_chunks: 0,
            max_upstream_chunk_size: 0,
            hedged: false,
            hedge_skipped: false,
            hedge_cooldown: false,
            hedge_budget: false,
            hedge_winner: 0,
            startup: None,
            open_observation: None,
            body_observation: None,
            observe_log: false,
            logger: None,
            admission: None,
            admission_wait_us: 0,
            trace_id: String::new(),
            parent_trace: String::new(),
            method: String::new(),
            range: false,
            control_ms: None,
        }
    }

    fn mark_upstream_headers(&mut self, opened: &Opened) {
        self.upstream_headers_ms = Some(opened.headers_at.duration_since(self.started).as_millis());
        if self.client_trace.is_some() {
            self.upstream_headers_us = Some(opened.headers_us);
        }
    }

    fn mark_first_body(&mut self) {
        if self.first_body_ms.is_none() {
            self.admission = None;
            self.startup = None;
            self.first_body_ms = Some(self.started.elapsed().as_millis());
            if self.client_trace.is_some() {
                self.first_body_us = Some(unix_us());
            }
        }
    }

    fn progress(&mut self, bytes: usize) {
        self.bytes += bytes as u64;
        if self.client_trace.is_some() {
            self.chunks += 1;
            self.first_body_size.get_or_insert(bytes);
            self.max_chunk_size = self.max_chunk_size.max(bytes);
        }
    }

    fn read_chunk(&mut self, bytes: usize) {
        if self.client_trace.is_some() {
            self.upstream_chunks += 1;
            self.max_upstream_chunk_size = self.max_upstream_chunk_size.max(bytes);
        }
    }

    fn finish(&mut self, reason: &'static str) {
        self.admission = None;
        self.startup = None;
        self.reason = reason;
    }
}

impl Drop for Transfer {
    fn drop(&mut self) {
        self.admission = None;
        self.counters.active.fetch_sub(1, Ordering::Relaxed);
        self.counters.bytes.fetch_add(self.bytes, Ordering::Relaxed);
        let counter = match self.reason {
            "complete" => &self.counters.completed,
            "upstream_error" => &self.counters.errors,
            _ => &self.counters.cancelled,
        };
        counter.fetch_add(1, Ordering::Relaxed);
        // Only metadata: never print source URLs, response bodies or headers.
        let log_started = self.observe_log.then(Instant::now);
        let mut event = json!({
            "event": "relay.closed",
            "request": self.id,
            "status": self.status,
            "bytes": self.bytes,
            "duration_ms": self.started.elapsed().as_millis() as u64,
            "reason": self.reason,
            "hedged": self.hedged,
            "hedge_skipped": self.hedge_skipped,
            "hedge_cooldown": self.hedge_cooldown,
            "hedge_budget": self.hedge_budget,
            "hedge_winner": self.hedge_winner,
            "engine": "rust",
            "trace_id": self.trace_id,
            "parent_trace": self.parent_trace,
            "method": self.method,
            "range": self.range,
            "first_body_ms": self.first_body_ms,
            "upstream_headers_ms": self.upstream_headers_ms,
            "control_ms": self.control_ms,
        });
        if let Some(trace) = self.client_trace.as_ref() {
            event["client_trace"] = Value::String(trace.clone());
            event["upstream_headers_ms"] = self
                .upstream_headers_ms
                .map(|value| Value::from(value as u64))
                .unwrap_or(Value::Null);
            for (name, value) in [
                ("handler_us", self.handler_us),
                ("upstream_request_us", self.upstream_request_us),
                ("upstream_headers_us", self.upstream_headers_us),
                ("first_body_us", self.first_body_us),
                ("response_ready_us", self.response_ready_us),
                ("body_poll_us", self.body_poll_us),
            ] {
                event[name] = value.map(Value::from).unwrap_or(Value::Null);
            }
            event["first_body_ms"] = self
                .first_body_ms
                .map(|value| Value::from(value as u64))
                .unwrap_or(Value::Null);
            event["chunks"] = Value::from(self.chunks);
            event["first_body_size"] = self.first_body_size.map(Value::from).unwrap_or(Value::Null);
            event["max_chunk_size"] = Value::from(self.max_chunk_size);
            event["upstream_chunks"] = Value::from(self.upstream_chunks);
            event["max_upstream_chunk_size"] = Value::from(self.max_upstream_chunk_size);
            event["admission_wait_us"] = Value::from(self.admission_wait_us);
            if let Some(stats) = self.open_observation.as_ref() {
                event["open_observation"] = stats.snapshot();
            }
            if let Some(stats) = self.body_observation.as_ref() {
                event["body_observation"] = stats.snapshot();
            }
        }
        let encoded = log_started.map(|started| started.elapsed().as_nanos() as u64);
        if let Some(logger) = &self.logger {
            logger.submit(event, encoded, self.reason);
        } else {
            logging::write_event(&event, encoded, &self.counters);
        }
    }
}

async fn health(State(app): State<Arc<App>>) -> Json<Value> {
    let c = &app.counters;
    let runtime = tokio::runtime::Handle::current().metrics();
    Json(json!({
        "engine": "rust",
        "experimental": true,
        "active": c.active.load(Ordering::Relaxed),
        "requests": c.requests.load(Ordering::Relaxed),
        "completed": c.completed.load(Ordering::Relaxed),
        "cancelled": c.cancelled.load(Ordering::Relaxed),
        "errors": c.errors.load(Ordering::Relaxed),
        "bytes": c.bytes.load(Ordering::Relaxed),
        "hedge_started": c.hedge_started.load(Ordering::Relaxed),
        "hedge_wins": c.hedge_wins.load(Ordering::Relaxed),
        "hedge_skipped": c.hedge_skipped.load(Ordering::Relaxed),
        "hedge_cooldown_skipped": c.hedge_cooldown_skipped.load(Ordering::Relaxed),
        "hedge_budget_skipped": c.hedge_budget_skipped.load(Ordering::Relaxed),
        "hedge_rejected": c.hedge_rejected.load(Ordering::Relaxed),
        "hedge_eligible": app.hedge.eligible.load(Ordering::Relaxed),
        "hedge_credits": app.hedge.credits.load(Ordering::Relaxed),
        "hedge_active": c.hedge_active.load(Ordering::Relaxed),
        "hedge_peak": c.hedge_peak.load(Ordering::Relaxed),
        "max_upstream_read": app.client.max_read(),
        "startup_limit": app.startup_limit,
        "starting": app.startup.as_ref().map_or(0, |policy| policy.waiting()),
        "startup_yields": app.startup.as_ref().map_or(0, |policy| policy.yields()),
        "log_observation": {
            "events": c.log_events.load(Ordering::Relaxed),
            "event_build_ns": c.log_encode_ns.load(Ordering::Relaxed),
            "format_lock_write_flush_ns": c.log_write_ns.load(Ordering::Relaxed),
            "max_format_lock_write_flush_ns": c.log_max_write_ns.load(Ordering::Relaxed),
        },
        "logging": {
            "async": app.logger.is_some(),
            "capacity": if app.logger.is_some() { logging::QUEUE_CAPACITY } else { 0 },
            "pending": c.log_pending.load(Ordering::Relaxed),
            "written": c.log_written.load(Ordering::Relaxed),
            "write_errors": c.log_write_errors.load(Ordering::Relaxed),
            "dropped_complete": c.log_dropped_complete.load(Ordering::Relaxed),
            "dropped_upstream_error": c.log_dropped_error.load(Ordering::Relaxed),
            "dropped_cancelled": c.log_dropped_cancelled.load(Ordering::Relaxed),
            "disconnected": c.log_disconnected.load(Ordering::Relaxed),
        },
        "runtime_observation": app.observe_startup.then(|| json!({
            "alive_tasks": runtime.num_alive_tasks(),
            "global_queue_depth": runtime.global_queue_depth(),
            "workers": (0..runtime.num_workers()).map(|index| json!({
                "busy_ns": runtime.worker_total_busy_duration(index).as_nanos() as u64,
                "parks": runtime.worker_park_count(index),
            })).collect::<Vec<_>>(),
        })),
    }))
}

async fn media(
    State(app): State<Arc<App>>,
    Path(key): Path<String>,
    method: Method,
    request_headers: HeaderMap,
) -> Response {
    let Some(asset) = app.assets.get(&key) else {
        return (StatusCode::NOT_FOUND, "asset not registered").into_response();
    };
    let asset = Arc::clone(asset);
    deliver(app, asset, method, request_headers, None).await
}

async fn reject_media_method(request: axum::extract::Request) -> Response {
    // Drain a small rejected body so HTTP/1 peers receive 405 rather than a reset.
    let drained = tokio::time::timeout(
        Duration::from_secs(2),
        axum::body::to_bytes(request.into_body(), 8192),
    )
    .await;
    match drained {
        Ok(Ok(_)) => (StatusCode::METHOD_NOT_ALLOWED, [("allow", "GET, HEAD")]).into_response(),
        _ => (StatusCode::BAD_REQUEST, [("connection", "close")]).into_response(),
    }
}

async fn deliver(
    app: Arc<App>,
    asset: Arc<Asset>,
    method: Method,
    request_headers: HeaderMap,
    context: Option<(String, String, Instant)>,
) -> Response {
    let client_trace = if app.trace {
        request_headers
            .get("x-t4-relay-client-trace")
            .and_then(|value| value.to_str().ok())
            .filter(|value| {
                !value.is_empty()
                    && value.len() <= 96
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"-_.:".contains(&byte))
            })
            .map(str::to_owned)
    } else {
        None
    };
    let mut transfer = Transfer::new(Arc::clone(&app.counters), client_trace);
    transfer.method = method.to_string();
    transfer.range = request_headers.contains_key("range");
    if let Some((trace_id, parent_trace, started)) = context {
        transfer.trace_id = trace_id;
        transfer.parent_trace = parent_trace;
        transfer.control_ms = Some(started.elapsed().as_millis());
        transfer.started = started;
    }
    transfer.observe_log = app.observe_log;
    transfer.logger = app.logger.clone();
    if app.observe_startup
        && transfer.client_trace.is_some()
        && request_headers
            .get("x-t4-relay-observe")
            .is_some_and(|v| v == "1")
    {
        transfer.open_observation = Some(Observation::new());
        transfer.body_observation = Some(Observation::new());
    }
    transfer.startup = app.startup.as_ref().map(|policy| policy.enter());
    let bypass_admission = app.trace
        && transfer.client_trace.is_some()
        && request_headers
            .get("x-t4-relay-admission-bypass")
            .is_some_and(|v| v == "1");
    if method == Method::GET
        && !bypass_admission
        && let Some(gate) = asset.admission.as_ref()
    {
        let started = Instant::now();
        transfer.admission = Some(
            Arc::clone(gate)
                .acquire_owned()
                .await
                .expect("gate is never closed"),
        );
        transfer.admission_wait_us = started.elapsed().as_micros() as u64;
    }
    let mut headers = asset.headers.clone();
    headers.insert("accept-encoding", HeaderValue::from_static("identity"));
    for name in ["range", "if-range"] {
        if let Some(value) = request_headers.get(name) {
            headers.insert(HeaderName::from_static(name), value.clone());
        }
    }
    if let Some(trace) = transfer.client_trace.as_ref() {
        headers.insert(
            "x-t4-relay-client-trace",
            HeaderValue::from_str(trace).expect("validated trace header"),
        );
        transfer.upstream_request_us = Some(unix_us());
    }
    let open_observation = transfer.open_observation.clone();
    let upstream = observed(
        app.open_media(method.clone(), &asset, headers, &mut transfer),
        open_observation.as_ref(),
    )
    .await;
    let opened = match upstream {
        Ok(response) => response,
        Err(_) => {
            transfer.status = 502;
            transfer.reason = "upstream_error";
            return (StatusCode::BAD_GATEWAY, "media upstream failed").into_response();
        }
    };
    transfer.mark_upstream_headers(&opened);
    transfer.status = opened.response.status().as_u16();
    let upstream = &opened.response;
    let mut response_headers = HeaderMap::new();
    for name in [
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
        "content-encoding",
        "retry-after",
    ] {
        if let Some(value) = upstream.headers().get(name) {
            response_headers.insert(HeaderName::from_static(name), value.clone());
        }
    }
    response_headers
        .entry("content-type")
        .or_insert_with(|| asset.mime.clone());
    response_headers.insert("cache-control", HeaderValue::from_static("no-store"));
    response_headers.insert("x-accel-buffering", HeaderValue::from_static("no"));
    let status = upstream.status();
    let expected = upstream
        .headers()
        .get("content-length")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok());
    if transfer.client_trace.is_some() {
        transfer.response_ready_us = Some(unix_us());
    }
    let body = if method == Method::HEAD || expected == Some(0) {
        transfer.finish("complete");
        drop(opened);
        drop(transfer);
        Body::empty()
    } else {
        // No producer queue or whole-object cache: downstream polling is backpressure.
        let stream = async_stream::stream! {
            if transfer.client_trace.is_some() {
                transfer.body_poll_us = Some(unix_us());
            }
            let mut first = true;
            let opened = if opened.body_race.is_some() {
                match app.resume_body(opened, &asset, &mut transfer).await {
                    Ok(opened) => {
                        transfer.mark_upstream_headers(&opened);
                        opened
                    }
                    Err(_) => {
                        transfer.finish("upstream_error");
                        yield Err(io::Error::other("media upstream first body failed"));
                        return;
                    }
                }
            } else {
                opened
            };
            let Opened { response: upstream, prefix, .. } = opened;
            let mut chunks = futures_util::stream::iter(prefix.map(Ok))
                .chain(upstream.bytes_stream());
            let mut turn_bytes = 0;
            let mut startup_turn_bytes = 0;
            loop {
                let observation = if first { transfer.body_observation.clone() } else { None };
                let Some(chunk) = observed(chunks.next(), observation.as_ref()).await else { break; };
                match chunk {
                    Ok(mut bytes) => {
                        transfer.read_chunk(bytes.len());
                        while !bytes.is_empty() {
                            let startup_contended = app.startup.as_ref().is_some_and(
                                |policy| policy.has_other_startup(transfer.startup.is_some()),
                            );
                            let frame = if app.delivery_budget > 0 || startup_contended {
                                bytes.split_to(bytes.len().min(STARTUP_QUANTUM))
                            } else {
                                bytes.split_to(bytes.len())
                            };
                            // Keep the Drop guard alive across downstream polls.
                            transfer.mark_first_body();
                            transfer.progress(frame.len());
                            turn_bytes += frame.len();
                            startup_turn_bytes += frame.len();
                            // Hyper can stop at Content-Length without another EOF poll.
                            if expected == Some(transfer.bytes) {
                                transfer.finish("complete");
                            }
                            yield Ok::<_, io::Error>(frame);
                            if first {
                                first = false;
                                if app.yield_after_first_body {
                                    tokio::task::yield_now().await;
                                }
                            }
                            let startup_yield = app.startup.as_ref().is_some_and(
                                |policy| policy.should_yield(&mut startup_turn_bytes),
                            );
                            if startup_yield {
                                tokio::task::yield_now().await;
                            } else if app.delivery_budget > 0 && turn_bytes >= app.delivery_budget {
                                turn_bytes = 0;
                                tokio::task::yield_now().await;
                            }
                        }
                    }
                    Err(_) => {
                        transfer.finish("upstream_error");
                        yield Err(io::Error::other("media upstream body failed"));
                        return;
                    }
                }
            }
            transfer.finish("complete");
        };
        Body::from_stream::<_>(stream)
    };
    (status, response_headers, body).into_response()
}

fn load_assets(bytes: &[u8]) -> Result<HashMap<String, Asset>, &'static str> {
    let config: HashMap<String, AssetConfig> =
        serde_json::from_slice(bytes).map_err(|_| "invalid asset manifest")?;
    config
        .into_iter()
        .map(|(key, config)| {
            if key.is_empty()
                || !key
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_.~".contains(&c))
            {
                return Err("invalid asset key");
            }
            let url = Url::parse(&config.url).map_err(|_| "invalid asset URL")?;
            if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
                return Err("unsupported asset URL");
            }
            let mut headers = HeaderMap::new();
            for (name, value) in config.headers {
                let name =
                    HeaderName::from_bytes(name.as_bytes()).map_err(|_| "invalid asset header")?;
                let value = HeaderValue::from_str(&value).map_err(|_| "invalid asset header")?;
                headers.insert(name, value);
            }
            let mime = HeaderValue::from_str(&config.mime).map_err(|_| "invalid asset MIME")?;
            Ok((
                key,
                Asset {
                    url,
                    headers,
                    mime,
                    hedge: config.hedge,
                    hedge_cooldown_until_ms: AtomicU64::new(0),
                    admission: None,
                },
            ))
        })
        .collect()
}

async fn run() -> Result<(), &'static str> {
    let mut args = env::args().skip(1);
    let mut listen: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let mut manifest = None;
    let mut gateway_url = None;
    let mut idle_ms = 25_000;
    let mut trace = false;
    let mut observe_startup = false;
    let mut observe_log = false;
    let mut async_log = false;
    let mut startup_limit = 0;
    let mut yield_after_first_body = false;
    let mut delivery_budget = 0;
    let mut upstream_read = None;
    let mut startup_fairness = false;
    let mut hedge = HedgePolicy::default();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--listen" => {
                listen = args
                    .next()
                    .and_then(|value| value.parse().ok())
                    .ok_or("invalid listen address")?
            }
            "--assets" => manifest = args.next(),
            "--gateway" => gateway_url = args.next(),
            "--read-timeout-ms" => {
                idle_ms = args
                    .next()
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| *value > 0)
                    .ok_or("invalid read timeout")?
            }
            "--trace" => trace = true,
            "--observe-log" => observe_log = true,
            "--async-log" => async_log = true,
            "--startup-limit" => {
                startup_limit = args
                    .next()
                    .and_then(|value| value.parse::<usize>().ok())
                    .filter(|value| (1..=16).contains(value))
                    .ok_or("invalid startup limit")?;
            }
            "--observe-startup" => {
                observe_startup = true;
                trace = true;
            }
            "--startup-fairness" => startup_fairness = true,
            "--upstream-read-kib" => {
                upstream_read = Some(
                    args.next()
                        .and_then(|value| value.parse::<usize>().ok())
                        .filter(|value| *value == 0 || (8..=1024).contains(value))
                        .ok_or("invalid upstream read limit")?
                        * 1024,
                );
            }
            "--yield-after-first-body" => yield_after_first_body = true,
            "--delivery-budget-kib" => {
                delivery_budget = args
                    .next()
                    .and_then(|value| value.parse::<usize>().ok())
                    .filter(|value| (64..=1024).contains(value))
                    .ok_or("invalid delivery budget")?
                    * 1024;
            }
            "--hedge-delay-ms" => {
                hedge.delay = Some(Duration::from_millis(
                    args.next()
                        .and_then(|value| value.parse::<u64>().ok())
                        .filter(|value| *value > 0)
                        .ok_or("invalid hedge delay")?,
                ));
            }
            "--hedge-limit" => {
                hedge.limit = args
                    .next()
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| (1..=16).contains(value))
                    .ok_or("invalid hedge limit")?;
            }
            _ => return Err("unknown argument"),
        }
    }
    if !listen.ip().is_loopback() && gateway_url.is_none() {
        return Err("experimental relay must listen on loopback");
    }
    if manifest.is_some() == gateway_url.is_some() {
        return Err("provide exactly one of --assets and --gateway");
    }
    let gateway = gateway_url
        .map(|url| gateway::Gateway::new(&url))
        .transpose()?;
    if gateway.is_some()
        && (trace
            || observe_startup
            || observe_log
            || startup_limit > 0
            || yield_after_first_body
            || delivery_budget > 0
            || upstream_read.is_some()
            || startup_fairness
            || hedge.delay.is_some())
    {
        return Err("diagnostic options cannot be used in gateway mode");
    }
    let mut assets = match manifest {
        Some(path) => load_assets(&fs::read(path).map_err(|_| "could not read asset manifest")?)?,
        None => HashMap::new(),
    };
    if startup_limit > 0 {
        let mut gates = HashMap::new();
        for asset in assets.values_mut() {
            // Different upstream origins must never block each other's startup.
            let gate = gates
                .entry(asset.url.origin().ascii_serialization())
                .or_insert_with(|| Arc::new(Semaphore::new(startup_limit)));
            asset.admission = Some(Arc::clone(gate));
        }
    }
    let client = match upstream_read {
        Some(limit) => Upstream::controlled(limit, Duration::from_millis(idle_ms)),
        None => Upstream::Original(
            Client::builder()
                .no_proxy()
                .http1_only()
                .tcp_nodelay(true)
                .cookie_store(true)
                .redirect(reqwest::redirect::Policy::none())
                .referer(false)
                .connect_timeout(Duration::from_secs(10))
                .read_timeout(Duration::from_millis(idle_ms))
                .pool_max_idle_per_host(20)
                .pool_idle_timeout(Duration::from_secs(5))
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .no_zstd()
                .build()
                .map_err(|_| "could not initialize HTTP client")?,
        ),
    };
    let counters = Arc::new(Counters::default());
    let (logger, log_drain) = if async_log {
        let (logger, drain) = logging::AsyncLog::start(Arc::clone(&counters))
            .map_err(|_| "could not start log writer")?;
        (Some(logger), Some(drain))
    } else {
        (None, None)
    };
    let app = Arc::new(App {
        assets: assets
            .into_iter()
            .map(|(key, asset)| (key, Arc::new(asset)))
            .collect(),
        gateway,
        client,
        counters,
        trace,
        observe_startup,
        observe_log,
        logger,
        startup_limit,
        yield_after_first_body,
        delivery_budget,
        startup: startup_fairness.then(|| Arc::new(StartupFairness::default())),
        hedge,
        started: Instant::now(),
    });
    let router = if app.gateway.is_some() {
        Router::new()
            .route("/t4/api/diagnostics/media-engine", get(health))
            .fallback(gateway::forward)
    } else {
        Router::new().route("/health", get(health)).route(
            "/t4/media/{key}",
            get(media).head(media).fallback(reject_media_method),
        )
    }
    .with_state(app);
    let listener = TcpListener::bind(listen)
        .await
        .map_err(|_| "could not bind relay listener")?;
    let address = listener.local_addr().map_err(|_| "no listener address")?;
    println!(
        "{}",
        json!({"event": "ready", "listen": address.to_string()})
    );
    let result = axum::serve(listener.tap_io(configure_media_socket), router)
        .with_graceful_shutdown(shutdown())
        .await
        .map_err(|_| "relay server failed");
    if let Some(drain) = log_drain {
        let drained = tokio::task::spawn_blocking(move || drain.wait(Duration::from_secs(2)))
            .await
            .unwrap_or(false);
        if !drained {
            return Err("log drain timed out");
        }
    }
    result
}

async fn shutdown() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        if let Ok(mut terminate) = signal(SignalKind::terminate()) {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {},
                _ = terminate.recv() => {},
            }
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn admission_releases_at_first_body_error_and_cancellation() {
        let gate = Arc::new(Semaphore::new(1));
        for reason in ["first", "error", "cancel"] {
            let mut transfer = Transfer::new(Arc::new(Counters::default()), None);
            transfer.admission = Some(Arc::clone(&gate).try_acquire_owned().unwrap());
            assert_eq!(gate.available_permits(), 0);
            match reason {
                "first" => transfer.mark_first_body(),
                "error" => transfer.finish("upstream_error"),
                _ => {}
            }
            if reason != "cancel" {
                assert_eq!(gate.available_permits(), 1);
            }
            drop(transfer);
            assert_eq!(gate.available_permits(), 1);
        }
    }

    #[test]
    fn validates_manifest_without_echoing_secrets() {
        for bytes in [
            br#"{"key":{"url":"file:///private/value"}}"#.as_slice(),
            br#"{"a/b":{"url":"http://localhost/file"}}"#,
            br#"{"key":{"url":"http://localhost/file","headers":{"Cookie":"secret\r\nx: y"}}}"#,
            br#"{"key":{"url":"http://localhost/file","kind":"hls"}}"#,
        ] {
            assert!(load_assets(bytes).is_err());
        }
        let assets = load_assets(
            br#"{"opaque-key.bin":{"url":"http://127.0.0.1/file?signature=private","headers":{"Referer":"https://fixture.example"}}}"#,
        )
        .unwrap();
        assert_eq!(
            assets["opaque-key.bin"].headers["referer"],
            "https://fixture.example"
        );
    }

    #[test]
    fn counters_classify_completion_cancel_and_error() {
        let counters = Arc::new(Counters::default());
        {
            let mut transfer = Transfer::new(Arc::clone(&counters), None);
            transfer.reason = "complete";
            transfer.bytes = 8;
        }
        drop(Transfer::new(Arc::clone(&counters), None));
        {
            let mut transfer = Transfer::new(Arc::clone(&counters), None);
            transfer.reason = "upstream_error";
        }
        assert_eq!(counters.active.load(Ordering::Relaxed), 0);
        assert_eq!(counters.requests.load(Ordering::Relaxed), 3);
        assert_eq!(counters.completed.load(Ordering::Relaxed), 1);
        assert_eq!(counters.cancelled.load(Ordering::Relaxed), 1);
        assert_eq!(counters.errors.load(Ordering::Relaxed), 1);
        assert_eq!(counters.bytes.load(Ordering::Relaxed), 8);
    }

    #[test]
    fn authorization_origin_matches_httpx_get_head_redirect_contract() {
        let original = Url::parse("http://fixture.example/file").unwrap();
        for (next, keep) in [
            ("http://fixture.example/other", true),
            ("http://fixture.example:80/other", true),
            ("https://fixture.example/other", true),
            ("https://fixture.example:443/other", true),
            ("http://fixture.example:81/other", false),
            ("https://fixture.example:444/other", false),
            ("http://other.example/file", false),
        ] {
            assert_eq!(
                retain_authorization(&original, &Url::parse(next).unwrap()),
                keep
            );
        }
        assert!(!retain_authorization(
            &Url::parse("https://fixture.example/file").unwrap(),
            &original,
        ));
    }

    #[tokio::test]
    async fn accepted_media_socket_has_nodelay_enabled() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let mut listener = listener.tap_io(configure_media_socket);
        let ((accepted, _), connected) = tokio::join!(
            axum::serve::Listener::accept(&mut listener),
            TcpStream::connect(address),
        );
        let _client = connected.unwrap();
        assert!(accepted.nodelay().unwrap());
    }
}
