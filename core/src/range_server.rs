//! Loopback HTTP server mapping byte ranges onto cached file blocks.

use std::convert::Infallible;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use bytes::Bytes;
use futures::stream;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Empty, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::header::{ACCEPT_RANGES, CACHE_CONTROL, CONTENT_LENGTH, CONTENT_RANGE, CONTENT_TYPE, RANGE};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use percent_encoding::{percent_decode, percent_encode, AsciiSet, NON_ALPHANUMERIC};
use tokio::net::TcpListener;

use crate::cache::{FileKey, BLOCK_SIZE};
use crate::error::Result;
use crate::scheduler::{Owner, Scheduler};

const READ_AHEAD_BLOCKS: u64 = 8;
const PATH_SAFE: &AsciiSet = &NON_ALPHANUMERIC.remove(b'/').remove(b'-').remove(b'_').remove(b'.').remove(b'~');

pub type StatFile =
    Arc<dyn Fn(Vec<u8>) -> Pin<Box<dyn Future<Output = Result<Arc<FileKey>>> + Send>> + Send + Sync>;
type Body = BoxBody<Bytes, std::io::Error>;

pub struct RangeServer {
    pub port: u16,
    token: String,
}

/// Inclusive range. Ok(None) means serve everything; Err means 416.
pub fn parse_range(header: Option<&str>, size: u64) -> std::result::Result<Option<(u64, u64)>, ()> {
    let Some(spec) = header.and_then(|h| h.trim().strip_prefix("bytes=")) else { return Ok(None) };
    if spec.contains(',') {
        return Ok(None); // multi-range: RFC 9110 lets us ignore it
    }
    let Some((first, last)) = spec.split_once('-') else { return Ok(None) };
    if first.is_empty() {
        let Ok(length) = last.parse::<u64>() else { return Ok(None) };
        if length == 0 || size == 0 {
            return Err(());
        }
        return Ok(Some((size.saturating_sub(length), size - 1)));
    }
    let Ok(start) = first.parse::<u64>() else { return Ok(None) };
    let end = if last.is_empty() {
        size.saturating_sub(1)
    } else {
        match last.parse::<u64>() {
            Ok(value) => value.min(size.saturating_sub(1)),
            Err(_) => return Ok(None),
        }
    };
    if start >= size || start > end {
        return Err(());
    }
    Ok(Some((start, end)))
}

fn content_type(path: &[u8]) -> &'static str {
    let name = String::from_utf8_lossy(path).to_lowercase();
    match name.rsplit('.').next().unwrap_or("") {
        "mp4" | "m4v" => "video/mp4",
        "mov" => "video/quicktime",
        "mkv" => "video/x-matroska",
        "webm" => "video/webm",
        "avi" => "video/x-msvideo",
        "ts" => "video/mp2t",
        _ => "application/octet-stream",
    }
}

fn same_token(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn empty() -> Body {
    Empty::<Bytes>::new().map_err(|never: Infallible| match never {}).boxed()
}

fn status(code: StatusCode) -> Response<Body> {
    let mut response = Response::new(empty());
    *response.status_mut() = code;
    response.headers_mut().insert(CONTENT_LENGTH, 0.into());
    response
}

struct Transfer {
    scheduler: Arc<Scheduler>,
    key: Arc<FileKey>,
    offset: u64,
    end: u64,
    // Dropped with the body when the player hangs up, which cancels read-ahead.
    owner: Owner,
}

async fn next_chunk(mut t: Transfer) -> Option<(std::io::Result<Frame<Bytes>>, Transfer)> {
    if t.offset > t.end {
        return None;
    }
    let index = t.offset / BLOCK_SIZE;
    t.scheduler.prefetch(&t.owner, &t.key, index + 1, READ_AHEAD_BLOCKS);
    let block = match t.scheduler.get(&t.key, index).await {
        Ok(block) => block,
        Err(error) => return Some((Err(std::io::Error::other(error.to_string())), t)),
    };
    let base = index * BLOCK_SIZE;
    let low = (t.offset - base) as usize;
    let high = (block.len() as u64).min(t.end + 1 - base) as usize;
    if low >= high {
        let error = std::io::Error::other("file shrank during read");
        t.offset = t.end + 1;
        return Some((Err(error), t));
    }
    t.offset = base + high as u64;
    Some((Ok(Frame::data(block.slice(low..high))), t))
}

async fn serve(request: Request<Incoming>, token: Arc<String>, scheduler: Arc<Scheduler>,
               stat_file: StatFile) -> std::result::Result<Response<Body>, Infallible> {
    let method = request.method().clone();
    if method != Method::GET && method != Method::HEAD {
        return Ok(status(StatusCode::METHOD_NOT_ALLOWED));
    }
    let target = request.uri().path().trim_start_matches('/');
    let (given, encoded) = target.split_once('/').unwrap_or((target, ""));
    if !same_token(given.as_bytes(), token.as_bytes()) {
        return Ok(status(StatusCode::FORBIDDEN));
    }
    let mut path = vec![b'/'];
    path.extend(percent_decode(encoded.as_bytes()));

    let Ok(key) = stat_file(path.clone()).await else {
        return Ok(status(StatusCode::NOT_FOUND));
    };
    let size = key.size;
    let header = request.headers().get(RANGE).and_then(|v| v.to_str().ok());
    let range = match parse_range(header, size) {
        Ok(range) => range,
        Err(()) => {
            let mut response = status(StatusCode::RANGE_NOT_SATISFIABLE);
            response.headers_mut().insert(CONTENT_RANGE, format!("bytes */{size}").parse().unwrap());
            return Ok(response);
        }
    };
    let (start, end) = range.unwrap_or((0, size.saturating_sub(1)));
    let length = if size == 0 { 0 } else { end - start + 1 };

    let body = if method == Method::GET && length > 0 {
        let owner = scheduler.owner();
        let transfer = Transfer { scheduler: scheduler.clone(), key, offset: start, end, owner };
        StreamBody::new(stream::unfold(transfer, next_chunk)).boxed()
    } else {
        empty()
    };
    let mut response = Response::new(body);
    if range.is_some() {
        *response.status_mut() = StatusCode::PARTIAL_CONTENT;
        response.headers_mut().insert(CONTENT_RANGE, format!("bytes {start}-{end}/{size}").parse().unwrap());
    }
    let headers = response.headers_mut();
    headers.insert(CONTENT_TYPE, content_type(&path).parse().unwrap());
    headers.insert(CONTENT_LENGTH, length.into());
    headers.insert(ACCEPT_RANGES, "bytes".parse().unwrap());
    headers.insert(CACHE_CONTROL, "no-store".parse().unwrap());
    Ok(response)
}

impl RangeServer {
    pub async fn start(scheduler: Arc<Scheduler>, stat_file: StatFile) -> std::io::Result<RangeServer> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
        let port = listener.local_addr()?.port();
        let mut seed = [0u8; 24];
        getrandom::fill(&mut seed).map_err(|e| std::io::Error::other(e.to_string()))?;
        let token: String = seed.iter().map(|b| format!("{b:02x}")).collect();
        let shared = Arc::new(token.clone());
        tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                let _ = socket.set_nodelay(true);
                let token = shared.clone();
                let scheduler = scheduler.clone();
                let stat_file = stat_file.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |request| {
                        serve(request, token.clone(), scheduler.clone(), stat_file.clone())
                    });
                    let _ = http1::Builder::new().serve_connection(TokioIo::new(socket), service).await;
                });
            }
        });
        Ok(RangeServer { port, token })
    }

    pub fn url_for(&self, path: &[u8]) -> String {
        format!("http://127.0.0.1:{}/{}{}", self.port, self.token, percent_encode(path, PATH_SAFE))
    }
}
