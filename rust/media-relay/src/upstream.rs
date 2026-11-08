use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    task::{Context, Poll, ready},
    time::Duration,
};

use axum::{
    body::Bytes,
    http::{HeaderMap, Method, Request, Uri},
};
use http_body_util::Empty;
use hyper::{
    body::{Body, Frame, Incoming, SizeHint},
    rt::{Read, ReadBuf, ReadBufCursor, Write},
};
use hyper_tls::HttpsConnector;
use hyper_util::{
    client::legacy::{
        Client as HyperClient,
        connect::{Connected, Connection, HttpConnector},
    },
    rt::{TokioExecutor, TokioTimer},
};
use reqwest::{
    Client, Response, Url,
    cookie::{CookieStore, Jar},
};
use tokio::time::Sleep;
use tower_service::Service;

pub(super) enum Upstream {
    Original(Client),
    Controlled(Box<Controlled>),
}

impl Upstream {
    pub fn controlled(read_limit: usize, idle: Duration) -> Self {
        let mut http = HttpConnector::new();
        http.enforce_http(false);
        http.set_nodelay(true);
        http.set_connect_timeout(Some(Duration::from_secs(10)));
        let max_read = Arc::new(AtomicUsize::new(0));
        let connector = ReadConnector {
            inner: HttpsConnector::new_with_connector(http),
            limit: read_limit,
            max_read: Arc::clone(&max_read),
        };
        let mut builder = HyperClient::builder(TokioExecutor::new());
        builder
            .pool_timer(TokioTimer::new())
            .pool_idle_timeout(Duration::from_secs(5))
            .pool_max_idle_per_host(20);
        Self::Controlled(Box::new(Controlled {
            client: builder.build(connector),
            cookies: Jar::default(),
            idle,
            max_read,
        }))
    }

    pub fn max_read(&self) -> Option<usize> {
        match self {
            Self::Original(_) => None,
            Self::Controlled(client) => Some(client.max_read.load(Ordering::Relaxed)),
        }
    }

    pub async fn send(
        &self,
        method: Method,
        url: Url,
        headers: HeaderMap,
    ) -> Result<Response, &'static str> {
        match self {
            Self::Original(client) => client
                .request(method, url)
                .headers(headers)
                .send()
                .await
                .map_err(|_| "upstream request failed"),
            Self::Controlled(client) => client.send(method, url, headers).await,
        }
    }
}

pub(super) struct Controlled {
    client: HyperClient<ReadConnector<HttpsConnector<HttpConnector>>, Empty<Bytes>>,
    cookies: Jar,
    idle: Duration,
    max_read: Arc<AtomicUsize>,
}

impl Controlled {
    async fn send(
        &self,
        method: Method,
        url: Url,
        mut headers: HeaderMap,
    ) -> Result<Response, &'static str> {
        if !headers.contains_key("cookie")
            && let Some(cookie) = self.cookies.cookies(&url)
        {
            headers.insert("cookie", cookie);
        }
        headers
            .entry("accept")
            .or_insert_with(|| "*/*".parse().unwrap());
        let mut request = Request::builder()
            .method(method)
            .uri(url.as_str())
            .body(Empty::new())
            .map_err(|_| "invalid upstream request")?;
        *request.headers_mut() = headers;
        let response = tokio::time::timeout(self.idle, self.client.request(request))
            .await
            .map_err(|_| "upstream request timed out")?
            .map_err(|_| "upstream request failed")?;
        self.cookies
            .set_cookies(&mut response.headers().get_all("set-cookie").iter(), &url);
        let (parts, body) = response.into_parts();
        let mut response = axum::http::Response::from_parts(
            parts,
            reqwest::Body::wrap(IdleBody {
                inner: body,
                idle: self.idle,
                sleep: None,
            }),
        );
        // Preserve the response URL without interpreting or rebuilding its signed query.
        use reqwest::ResponseBuilderExt;
        let mut builder = axum::http::Response::builder().url(url);
        response
            .extensions_mut()
            .extend(std::mem::take(builder.extensions_mut().unwrap()));
        Ok(response.into())
    }
}

struct IdleBody {
    inner: Incoming,
    idle: Duration,
    sleep: Option<Pin<Box<Sleep>>>,
}

impl Body for IdleBody {
    type Data = Bytes;
    type Error = io::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, io::Error>>> {
        let idle = self.idle;
        let sleep = self
            .sleep
            .get_or_insert_with(|| Box::pin(tokio::time::sleep(idle)));
        if sleep.as_mut().poll(cx).is_ready() {
            return Poll::Ready(Some(Err(io::ErrorKind::TimedOut.into())));
        }
        let frame = ready!(Pin::new(&mut self.inner).poll_frame(cx));
        self.sleep = None;
        Poll::Ready(frame.map(|result| result.map_err(io::Error::other)))
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

#[derive(Clone)]
struct ReadConnector<C> {
    inner: C,
    limit: usize,
    max_read: Arc<AtomicUsize>,
}

impl<C> Service<Uri> for ReadConnector<C>
where
    C: Service<Uri>,
    C::Future: Send + 'static,
{
    type Response = ReadLimited<C::Response>;
    type Error = C::Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, uri: Uri) -> Self::Future {
        let future = self.inner.call(uri);
        let limit = self.limit;
        let max_read = Arc::clone(&self.max_read);
        Box::pin(async move {
            Ok(ReadLimited {
                inner: future.await?,
                limit,
                local_max: 0,
                max_read,
            })
        })
    }
}

struct ReadLimited<T> {
    inner: T,
    limit: usize,
    local_max: usize,
    max_read: Arc<AtomicUsize>,
}

impl<T: Connection> Connection for ReadLimited<T> {
    fn connected(&self) -> Connected {
        self.inner.connected()
    }
}

impl<T: Read + Unpin> Read for ReadLimited<T> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        mut cursor: ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let limit = if this.limit == 0 {
            cursor.remaining()
        } else {
            cursor.remaining().min(this.limit)
        };
        // The child may initialize/fill only this subslice. Advance the parent only
        // by bytes the child proved initialized; no allocation or body copy occurs.
        let mut part = ReadBuf::uninit(unsafe { &mut cursor.as_mut()[..limit] });
        let result = Pin::new(&mut this.inner).poll_read(cx, part.unfilled());
        let received = part.filled().len();
        unsafe { cursor.advance(received) };
        if received > this.local_max {
            this.local_max = received;
            this.max_read.fetch_max(received, Ordering::Relaxed);
        }
        result
    }
}

impl<T: Write + Unpin> Write for ReadLimited<T> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.get_mut().inner).poll_write(cx, bytes)
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.get_mut().inner).poll_write_vectored(cx, bytes)
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_flush(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hyper_util::rt::TokioIo;

    #[tokio::test]
    async fn actual_read_is_capped_before_bytes_enter_the_http_parser() {
        for (limit, expected) in [(0, 4096), (64, 64), (8192, 4096)] {
            let input = [7_u8; 4096];
            let max_read = Arc::new(AtomicUsize::new(0));
            let mut io = ReadLimited {
                inner: TokioIo::new(input.as_slice()),
                limit,
                local_max: 0,
                max_read: Arc::clone(&max_read),
            };
            let mut output = [0_u8; 8192];
            let mut buffer = ReadBuf::new(&mut output);
            futures_util::future::poll_fn(|cx| Pin::new(&mut io).poll_read(cx, buffer.unfilled()))
                .await
                .unwrap();
            assert_eq!(buffer.filled(), &input[..expected]);
            assert_eq!(max_read.load(Ordering::Relaxed), expected);
        }
    }

    #[tokio::test]
    async fn limited_read_preserves_prefix_and_does_not_wait_to_fill() {
        let input = [7_u8; 3];
        let mut io = ReadLimited {
            inner: TokioIo::new(input.as_slice()),
            limit: 64,
            local_max: 0,
            max_read: Arc::new(AtomicUsize::new(0)),
        };
        let mut output = [9_u8; 8192];
        let mut buffer = ReadBuf::new(&mut output);
        buffer.unfilled().put_slice(b"prefix");
        futures_util::future::poll_fn(|cx| Pin::new(&mut io).poll_read(cx, buffer.unfilled()))
            .await
            .unwrap();
        assert_eq!(buffer.filled(), b"prefix\x07\x07\x07");
    }
}
