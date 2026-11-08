use std::{sync::Arc, time::Duration};

use axum::{
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
};
use futures_util::StreamExt;
use reqwest::{Client, Url};
use serde::Deserialize;

use crate::{App, AssetConfig, DeliveryContext, deliver};

struct ForwardTrace {
    logger: Option<crate::logging::AsyncLog>,
    event: serde_json::Value,
    started: std::time::Instant,
    bytes: u64,
    reason: &'static str,
}

impl ForwardTrace {
    fn finish(&mut self, reason: &'static str) {
        self.reason = reason;
    }
}

impl Drop for ForwardTrace {
    fn drop(&mut self) {
        self.event["bytes"] = self.bytes.into();
        self.event["duration_ms"] = (self.started.elapsed().as_secs_f64() * 1000.0).into();
        self.event["reason"] = self.reason.into();
        if let Some(logger) = &self.logger {
            logger.submit(self.event.take(), None, self.reason);
        }
    }
}

pub struct Gateway {
    base: Url,
    client: Client,
}

#[derive(Deserialize)]
struct Descriptor {
    #[serde(default)]
    url: String,
    #[serde(default)]
    headers: std::collections::HashMap<String, String>,
    #[serde(default)]
    mime: String,
    trace_id: String,
    parent_trace: String,
    engine: String,
    selected_engine: String,
    generation: u64,
    reason: String,
    asset: String,
}

impl Gateway {
    pub fn new(base: &str) -> Result<Self, &'static str> {
        let base = Url::parse(base).map_err(|_| "invalid gateway")?;
        if base.scheme() != "http"
            || base.host_str() != Some("127.0.0.1")
            || base.path() != "/"
            || base.query().is_some()
            || base.fragment().is_some()
            || !base.username().is_empty()
            || base.password().is_some()
        {
            return Err("gateway must be a loopback HTTP origin");
        }
        let client = Client::builder()
            .no_proxy()
            .http1_only()
            .tcp_nodelay(true)
            .redirect(reqwest::redirect::Policy::none())
            .referer(false)
            .connect_timeout(Duration::from_secs(3))
            .read_timeout(Duration::from_secs(120))
            .pool_max_idle_per_host(32)
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .no_zstd()
            .build()
            .map_err(|_| "gateway client failed")?;
        Ok(Self { base, client })
    }
}

fn clean(headers: &mut HeaderMap) {
    let nominated: Vec<_> = headers
        .get_all("connection")
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|s| s.split(','))
        .map(|s| s.trim().to_owned())
        .collect();
    for name in nominated {
        headers.remove(name);
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ] {
        headers.remove(name);
    }
    let private: Vec<_> = headers
        .keys()
        .filter(|name| name.as_str().starts_with("x-t4-relay-"))
        .cloned()
        .collect();
    for name in private {
        headers.remove(name);
    }
}

pub async fn forward(State(app): State<Arc<App>>, request: Request) -> Response {
    let started = std::time::Instant::now();
    let gateway = app.gateway.as_ref().expect("gateway router");
    // Check the normalized URL too; the ASGI server percent-decodes path segments.
    let target = format!(
        "{}{}",
        gateway.base.as_str().trim_end_matches('/'),
        request.uri().path_and_query().map_or("/", |v| v.as_str())
    );
    let Ok(target) = Url::parse(&target) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    if private_path(target.path()) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let mut handoff = None;
    if matches!(*request.method(), Method::GET | Method::HEAD)
        && let Some(key) = request.uri().path().strip_prefix("/t4/media/")
        && !key.is_empty()
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.~".contains(&b))
    {
        let url = gateway
            .base
            .join(&format!("/__t4_relay/asset/{key}"))
            .unwrap();
        let metadata = gateway.client.get(url).send().await;
        match metadata {
            Ok(response) if response.status() == StatusCode::OK => {
                // Bound control-plane metadata independently of the media stream.
                let mut bytes = Vec::new();
                let mut stream = response.bytes_stream();
                while let Some(chunk) = stream.next().await {
                    match chunk {
                        Ok(chunk) if bytes.len() + chunk.len() <= 128 * 1024 => {
                            bytes.extend_from_slice(&chunk)
                        }
                        _ => return StatusCode::BAD_GATEWAY.into_response(),
                    }
                }
                let Ok(descriptor) = serde_json::from_slice::<Descriptor>(&bytes) else {
                    return StatusCode::BAD_GATEWAY.into_response();
                };
                if descriptor.engine == "python" {
                    handoff = Some(descriptor);
                } else if descriptor.engine == "rust" {
                    let config = AssetConfig {
                        url: descriptor.url,
                        headers: descriptor.headers,
                        mime: descriptor.mime,
                        hedge: false,
                    };
                    let mut headers = HeaderMap::new();
                    for (name, value) in config.headers {
                        let (Ok(name), Ok(value)) =
                            (name.parse::<axum::http::HeaderName>(), value.parse())
                        else {
                            return StatusCode::BAD_GATEWAY.into_response();
                        };
                        headers.insert(name, value);
                    }
                    let (Ok(url), Ok(mime)) = (Url::parse(&config.url), config.mime.parse()) else {
                        return StatusCode::BAD_GATEWAY.into_response();
                    };
                    if !matches!(url.scheme(), "http" | "https") {
                        return StatusCode::BAD_GATEWAY.into_response();
                    }
                    let asset = Arc::new(crate::Asset {
                        url,
                        headers,
                        mime,
                        hedge: false,
                        hedge_cooldown_until_ms: Default::default(),
                        admission: None,
                    });
                    let trace = descriptor.trace_id.clone();
                    let mut response = deliver(
                        Arc::clone(&app),
                        asset,
                        request.method().clone(),
                        request.headers().clone(),
                        Some(DeliveryContext {
                            trace_id: descriptor.trace_id,
                            parent_trace: descriptor.parent_trace,
                            started,
                            asset: descriptor.asset,
                            selected_engine: descriptor.selected_engine,
                            generation: descriptor.generation,
                            reason: descriptor.reason,
                        }),
                    )
                    .await;
                    response
                        .headers_mut()
                        .insert("x-t4-media-engine", "rust".parse().unwrap());
                    if let Ok(value) = trace.parse() {
                        response.headers_mut().insert("x-t4-trace-id", value);
                    }
                    cors(request.headers(), response.headers_mut());
                    return response;
                } else {
                    return StatusCode::BAD_GATEWAY.into_response();
                }
            }
            // Preserve Python's expired-asset response body and CORS contract.
            Ok(response) if response.status() == StatusCode::GONE => {}
            _ => return StatusCode::BAD_GATEWAY.into_response(),
        }
    }
    let (parts, body) = request.into_parts();
    let mut headers = parts.headers;
    clean(&mut headers);
    let mut trace = handoff.as_ref().map(|context| ForwardTrace {
        logger: app.logger.clone(), started, bytes: 0, reason: "client_disconnect",
        event: serde_json::json!({"event": "relay.forwarded", "engine": "python",
            "trace_id": context.trace_id, "parent_trace": context.parent_trace,
            "asset": context.asset, "selected_engine": context.selected_engine,
            "engine_generation": context.generation, "route_reason": context.reason,
            "method": parts.method.as_str(), "control_ms": started.elapsed().as_secs_f64() * 1000.0}),
    });
    if let Some(context) = handoff {
        for (name, value) in [
            ("x-t4-relay-trace", context.trace_id),
            ("x-t4-relay-selected", context.selected_engine),
            ("x-t4-relay-generation", context.generation.to_string()),
            ("x-t4-relay-reason", context.reason),
            (
                "x-t4-relay-control-ms",
                format!("{:.3}", started.elapsed().as_secs_f64() * 1000.0),
            ),
        ] {
            let Ok(value) = value.parse() else {
                return StatusCode::BAD_GATEWAY.into_response();
            };
            headers.insert(axum::http::HeaderName::from_static(name), value);
        }
    }
    let upstream = gateway
        .client
        .request(parts.method.clone(), target)
        .headers(headers)
        .body(reqwest::Body::wrap_stream(body.into_data_stream()))
        .send()
        .await;
    match upstream {
        Ok(upstream) => {
            let status = upstream.status();
            let mut headers = upstream.headers().clone();
            clean(&mut headers);
            let expected = headers
                .get("content-length")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse::<u64>().ok());
            if let Some(trace) = trace.as_mut() {
                trace.event["upstream_headers_ms"] =
                    (started.elapsed().as_secs_f64() * 1000.0).into();
                trace.event["status"] = status.as_u16().into();
            }
            let body = if parts.method == Method::HEAD || expected == Some(0) {
                if let Some(trace) = trace.as_mut() {
                    trace.finish("complete");
                }
                Body::empty()
            } else if let Some(mut trace) = trace {
                Body::from_stream(async_stream::stream! {
                    let mut chunks = upstream.bytes_stream();
                    while let Some(chunk) = chunks.next().await {
                        match chunk {
                            Ok(bytes) => {
                                if trace.bytes == 0 && !bytes.is_empty() {
                                    trace.event["first_body_ms"] = (started.elapsed().as_secs_f64() * 1000.0).into();
                                }
                                trace.bytes += bytes.len() as u64;
                                if expected == Some(trace.bytes) { trace.reason = "complete"; }
                                yield Ok::<_, std::io::Error>(bytes);
                            }
                            Err(_) => {
                                trace.finish("upstream_error");
                                yield Err(std::io::Error::other("Python media stream failed"));
                                return;
                            }
                        }
                    }
                    trace.finish("complete");
                })
            } else {
                Body::from_stream(upstream.bytes_stream())
            };
            (status, headers, body).into_response()
        }
        Err(_) => {
            if let Some(trace) = trace.as_mut() {
                trace.reason = "upstream_error";
                trace.event["status"] = 502.into();
            }
            (StatusCode::BAD_GATEWAY, "gateway unavailable").into_response()
        }
    }
}

fn private_path(path: &str) -> bool {
    let decoded = percent_encoding::percent_decode_str(path).decode_utf8_lossy();
    let mut segments = Vec::new();
    for part in decoded.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                segments.pop();
            }
            _ => segments.push(part),
        }
    }
    segments.first().is_some_and(|part| *part == "__t4_relay")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn private_path_cannot_be_forwarded_using_encoded_segments() {
        for path in [
            "/__t4_relay/asset/x",
            "/%5f%5ft4_relay/asset/x",
            "/%2F__t4_relay/asset/x",
            "/foo/%2e%2e/__t4_relay/asset/x",
            "//__t4_relay/asset/x",
        ] {
            assert!(private_path(path));
        }
        assert!(!private_path("/t4/media/x"));
    }
}

fn cors(request: &HeaderMap, response: &mut HeaderMap) {
    if request.contains_key("origin") {
        response.insert("access-control-allow-origin", "*".parse().unwrap());
        response.insert(
            "access-control-expose-headers",
            "Content-Length, Content-Range, Accept-Ranges, X-T4-Trace-ID, X-T4-Media-Engine"
                .parse()
                .unwrap(),
        );
    }
}
