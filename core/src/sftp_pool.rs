//! SFTP v3 client on a russh channel.
//!
//! Hand-written because names and handles are opaque bytes: russh-sftp decodes
//! both as lossy UTF-8, which makes non-UTF-8 files unreachable and corrupts
//! binary handles. Requests go through a writer task, so a cancelled read can
//! never leave a half-written packet on the stream.

use std::collections::HashMap;
use std::num::NonZeroUsize;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use bytes::{Buf, BufMut, Bytes, BytesMut};
use futures::future::try_join_all;
use lru::LruCache;
use russh::client::Msg;
use russh::Channel;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot, OnceCell};

use crate::error::{Result, RvError};

const FXP_INIT: u8 = 1;
const FXP_VERSION: u8 = 2;
const FXP_OPEN: u8 = 3;
const FXP_CLOSE: u8 = 4;
const FXP_READ: u8 = 5;
const FXP_WRITE: u8 = 6;
const FXP_LSTAT: u8 = 7;
const FXP_FSTAT: u8 = 8;
const FXP_FSETSTAT: u8 = 10;
const FXP_OPENDIR: u8 = 11;
const FXP_READDIR: u8 = 12;
const FXP_REMOVE: u8 = 13;
const FXP_MKDIR: u8 = 14;
const FXP_REALPATH: u8 = 16;
const FXP_STAT: u8 = 17;
const FXP_EXTENDED: u8 = 200;
const FXP_STATUS: u8 = 101;
const FXP_HANDLE: u8 = 102;
const FXP_DATA: u8 = 103;
const FXP_NAME: u8 = 104;
const FXP_ATTRS: u8 = 105;
const FXP_EXTENDED_REPLY: u8 = 201;

const FX_EOF: u32 = 1;
const FX_NO_SUCH_FILE: u32 = 2;
const FX_PERMISSION_DENIED: u32 = 3;
const FX_OK: u32 = 0;
const FX_OP_UNSUPPORTED: u32 = 8;

const ATTR_SIZE: u32 = 0x1;
const ATTR_UIDGID: u32 = 0x2;
const ATTR_PERMISSIONS: u32 = 0x4;
const ATTR_ACMODTIME: u32 = 0x8;
const ATTR_EXTENDED: u32 = 0x8000_0000;

const FXF_READ: u32 = 0x01;
const FXF_WRITE: u32 = 0x02;
const FXF_CREAT: u32 = 0x08;
const FXF_TRUNC: u32 = 0x10;
const FXF_EXCL: u32 = 0x20;
const REQUEST_SIZE: u64 = 64 * 1024;
/// A whole chunk is in flight at once on transfers.
const TRANSFER_CHUNK: usize = 4 * 1024 * 1024;
const MODE_TYPE_MASK: u32 = 0o170000;
const MODE_DIR: u32 = 0o040000;
const MAX_PACKET: usize = 8 * 1024 * 1024;
const MAX_OPEN_HANDLES: usize = 32;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Attrs {
    pub size: u64,
    pub mode: u32,
    pub mtime: u32,
    pub uid: u32,
    pub gid: u32,
}

pub struct DirEntry {
    pub name: Vec<u8>,
    pub attrs: Attrs,
}

/// One file on its way: cancelled by flag, so temp files can still be removed.
pub struct Transfer {
    stop: AtomicBool,
    report: Box<dyn Fn(u64) + Send + Sync>,
}

impl Transfer {
    pub fn new(report: Box<dyn Fn(u64) + Send + Sync>) -> Self {
        Transfer { stop: AtomicBool::new(false), report }
    }

    pub fn cancel(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }

    fn step(&self, done: u64) -> Result<()> {
        if self.stop.load(Ordering::Relaxed) {
            return Err(RvError::Cancelled);
        }
        (self.report)(done);
        Ok(())
    }
}

/// What goes into a file being replaced.
#[derive(Clone, Copy)]
enum Content<'a> {
    Bytes(&'a [u8]),
    File(&'a Path, &'a Transfer),
}

struct Reply {
    kind: u8,
    body: Bytes,
}

type Waiters = Arc<Mutex<HashMap<u32, oneshot::Sender<Reply>>>>;
type HandleCell = Arc<OnceCell<Bytes>>;

pub struct Sftp {
    next_id: AtomicU32,
    writer: mpsc::UnboundedSender<Vec<u8>>,
    waiters: Waiters,
    closed: Arc<AtomicBool>,
    handles: Mutex<LruCache<Vec<u8>, HandleCell>>,
}

fn put_bytes(buffer: &mut Vec<u8>, data: &[u8]) {
    buffer.put_u32(data.len() as u32);
    buffer.extend_from_slice(data);
}

fn take_bytes(body: &mut Bytes) -> Result<Bytes> {
    if body.remaining() < 4 {
        return Err(RvError::Io("short sftp packet".into()));
    }
    let length = body.get_u32() as usize;
    if body.remaining() < length {
        return Err(RvError::Io("short sftp string".into()));
    }
    Ok(body.split_to(length))
}

fn take_u32(body: &mut Bytes) -> Result<u32> {
    if body.remaining() < 4 {
        return Err(RvError::Io("short sftp packet".into()));
    }
    Ok(body.get_u32())
}

fn take_attrs(body: &mut Bytes) -> Result<Attrs> {
    let flags = take_u32(body)?;
    let mut attrs = Attrs::default();
    if flags & ATTR_SIZE != 0 {
        if body.remaining() < 8 {
            return Err(RvError::Io("short sftp attrs".into()));
        }
        attrs.size = body.get_u64();
    }
    if flags & ATTR_UIDGID != 0 {
        attrs.uid = take_u32(body)?;
        attrs.gid = take_u32(body)?;
    }
    if flags & ATTR_PERMISSIONS != 0 {
        attrs.mode = take_u32(body)?;
    }
    if flags & ATTR_ACMODTIME != 0 {
        take_u32(body)?;
        attrs.mtime = take_u32(body)?;
    }
    if flags & ATTR_EXTENDED != 0 {
        for _ in 0..take_u32(body)? {
            take_bytes(body)?;
            take_bytes(body)?;
        }
    }
    Ok(attrs)
}

fn status_error(body: &mut Bytes, path: &[u8]) -> RvError {
    let code = take_u32(body).unwrap_or(4);
    let message = take_bytes(body).map(|m| String::from_utf8_lossy(&m).into_owned()).unwrap_or_default();
    let name = String::from_utf8_lossy(path).into_owned();
    match code {
        FX_NO_SUCH_FILE => RvError::NoSuchFile(name),
        FX_PERMISSION_DENIED => RvError::PermissionDenied(name),
        _ => RvError::Sftp(code, message),
    }
}

impl Sftp {
    pub async fn start(channel: Channel<Msg>) -> Result<Arc<Sftp>> {
        channel.request_subsystem(true, "sftp").await?;
        let (mut reader, mut write_half) = tokio::io::split(channel.into_stream());

        let init = [0, 0, 0, 5, FXP_INIT, 0, 0, 0, 3];
        write_half.write_all(&init).await?;
        let length = reader.read_u32().await? as usize;
        if !(1..=MAX_PACKET).contains(&length) {
            return Err(RvError::Io("bad sftp version packet".into()));
        }
        let mut version = vec![0; length];
        reader.read_exact(&mut version).await?;
        if version[0] != FXP_VERSION {
            return Err(RvError::Io("server did not start sftp".into()));
        }

        let (writer, mut outgoing) = mpsc::unbounded_channel::<Vec<u8>>();
        tokio::spawn(async move {
            while let Some(packet) = outgoing.recv().await {
                if write_half.write_all(&packet).await.is_err() {
                    break;
                }
            }
            let _ = write_half.shutdown().await;
        });

        let waiters: Waiters = Arc::new(Mutex::new(HashMap::new()));
        let incoming = waiters.clone();
        let closed = Arc::new(AtomicBool::new(false));
        let reader_closed = closed.clone();
        tokio::spawn(async move {
            // Dropping the map on exit fails every waiter, which reads as Lost.
            let _ = read_replies(&mut reader, &incoming).await;
            reader_closed.store(true, Ordering::SeqCst);
            incoming.lock().unwrap().clear();
        });

        Ok(Arc::new(Sftp {
            next_id: AtomicU32::new(1),
            writer,
            waiters,
            closed,
            handles: Mutex::new(LruCache::new(NonZeroUsize::new(MAX_OPEN_HANDLES).unwrap())),
        }))
    }

    async fn request(&self, kind: u8, payload: &[u8]) -> Result<Reply> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = oneshot::channel();
        self.waiters.lock().unwrap().insert(id, sender);
        // After the insert: a reader that has already gone would never answer.
        if self.closed.load(Ordering::SeqCst) {
            self.waiters.lock().unwrap().remove(&id);
            return Err(RvError::Lost("sftp channel closed".into()));
        }
        let mut packet = Vec::with_capacity(payload.len() + 9);
        packet.put_u32(payload.len() as u32 + 5);
        packet.put_u8(kind);
        packet.put_u32(id);
        packet.extend_from_slice(payload);
        if self.writer.send(packet).is_err() {
            self.waiters.lock().unwrap().remove(&id);
            return Err(RvError::Lost("sftp channel closed".into()));
        }
        // On cancellation the reply finds nobody waiting and is dropped.
        receiver.await.map_err(|_| RvError::Lost("sftp channel closed".into()))
    }

    async fn path_request(&self, kind: u8, path: &[u8]) -> Result<Reply> {
        let mut payload = Vec::with_capacity(path.len() + 4);
        put_bytes(&mut payload, path);
        self.request(kind, &payload).await
    }

    pub async fn stat(&self, path: &[u8], follow: bool) -> Result<Attrs> {
        let mut reply = self.path_request(if follow { FXP_STAT } else { FXP_LSTAT }, path).await?;
        match reply.kind {
            FXP_ATTRS => take_attrs(&mut reply.body),
            _ => Err(status_error(&mut reply.body, path)),
        }
    }

    pub async fn realpath(&self, path: &[u8]) -> Result<Vec<u8>> {
        let mut reply = self.path_request(FXP_REALPATH, path).await?;
        if reply.kind != FXP_NAME {
            return Err(status_error(&mut reply.body, path));
        }
        take_u32(&mut reply.body)?;
        Ok(take_bytes(&mut reply.body)?.to_vec())
    }

    pub async fn readdir(&self, path: &[u8]) -> Result<Vec<DirEntry>> {
        let mut reply = self.path_request(FXP_OPENDIR, path).await?;
        if reply.kind != FXP_HANDLE {
            return Err(status_error(&mut reply.body, path));
        }
        let handle = take_bytes(&mut reply.body)?;
        let mut payload = Vec::with_capacity(handle.len() + 4);
        put_bytes(&mut payload, &handle);
        let mut entries = Vec::new();
        let outcome = loop {
            let mut reply = match self.request(FXP_READDIR, &payload).await {
                Ok(reply) => reply,
                Err(error) => break Err(error),
            };
            if reply.kind != FXP_NAME {
                let code = reply.body.clone().get_u32();
                break if code == FX_EOF { Ok(()) } else { Err(status_error(&mut reply.body, path)) };
            }
            let count = take_u32(&mut reply.body)?;
            entries.reserve(count as usize);
            for _ in 0..count {
                let name = take_bytes(&mut reply.body)?;
                take_bytes(&mut reply.body)?; // longname
                let attrs = take_attrs(&mut reply.body)?;
                if name.as_ref() != b"." && name.as_ref() != b".." {
                    entries.push(DirEntry { name: name.to_vec(), attrs });
                }
            }
        };
        self.close_handle(&handle);
        outcome.map(|_| entries)
    }

    fn close_handle(&self, handle: &[u8]) {
        let mut payload = Vec::with_capacity(handle.len() + 4);
        put_bytes(&mut payload, handle);
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let mut packet = Vec::with_capacity(payload.len() + 9);
        packet.put_u32(payload.len() as u32 + 5);
        packet.put_u8(FXP_CLOSE);
        packet.put_u32(id);
        packet.extend_from_slice(&payload);
        let _ = self.writer.send(packet); // nobody needs the status
    }

    async fn handle(&self, path: &[u8]) -> Result<Bytes> {
        let (cell, evicted) = {
            let mut handles = self.handles.lock().unwrap();
            if let Some(cell) = handles.get(path) {
                (cell.clone(), None)
            } else {
                let cell: HandleCell = Arc::new(OnceCell::new());
                let evicted = handles.push(path.to_vec(), cell.clone());
                (cell, evicted)
            }
        };
        if let Some((_, old)) = evicted {
            if let Some(handle) = old.get() {
                self.close_handle(handle);
            }
        }
        // The cell makes this single flight: concurrent block reads share one open.
        let result = cell
            .get_or_try_init(|| async {
                self.open_for_read(path).await
            })
            .await;
        match result {
            Ok(handle) => Ok(handle.clone()),
            Err(error) => {
                let mut handles = self.handles.lock().unwrap();
                if handles.peek(path).is_some_and(|c| Arc::ptr_eq(c, &cell)) {
                    handles.pop(path);
                }
                Err(error)
            }
        }
    }

    /// The path may now name another inode; a kept handle would read the old one.
    pub fn drop_handle(&self, path: &[u8]) {
        let cell = self.handles.lock().unwrap().pop(path);
        if let Some(handle) = cell.as_ref().and_then(|c| c.get()) {
            self.close_handle(handle);
        }
    }

    async fn status_request(&self, kind: u8, payload: &[u8], path: &[u8]) -> Result<()> {
        let mut reply = self.request(kind, payload).await?;
        if reply.kind == FXP_STATUS && reply.body.clone().get_u32() == FX_OK {
            return Ok(());
        }
        Err(status_error(&mut reply.body, path))
    }

    async fn open_for_read(&self, path: &[u8]) -> Result<Bytes> {
        let mut payload = Vec::with_capacity(path.len() + 16);
        put_bytes(&mut payload, path);
        payload.put_u32(FXF_READ);
        payload.put_u32(0);
        let mut reply = self.request(FXP_OPEN, &payload).await?;
        if reply.kind != FXP_HANDLE {
            return Err(status_error(&mut reply.body, path));
        }
        take_bytes(&mut reply.body)
    }

    async fn open_for_write(&self, path: &[u8], flags: u32, mode: Option<u32>) -> Result<Bytes> {
        let mut payload = Vec::with_capacity(path.len() + 16);
        put_bytes(&mut payload, path);
        payload.put_u32(flags);
        match mode {
            Some(mode) => {
                payload.put_u32(ATTR_PERMISSIONS);
                payload.put_u32(mode);
            }
            None => payload.put_u32(0),
        }
        let mut reply = self.request(FXP_OPEN, &payload).await?;
        if reply.kind != FXP_HANDLE {
            return Err(status_error(&mut reply.body, path));
        }
        take_bytes(&mut reply.body)
    }

    async fn write_all(&self, handle: &[u8], data: &[u8], base: u64) -> Result<()> {
        let writes = data.chunks(REQUEST_SIZE as usize).enumerate().map(|(index, chunk)| async move {
            let mut payload = Vec::with_capacity(handle.len() + chunk.len() + 16);
            put_bytes(&mut payload, handle);
            payload.put_u64(base + index as u64 * REQUEST_SIZE);
            put_bytes(&mut payload, chunk);
            self.status_request(FXP_WRITE, &payload, b"").await
        });
        try_join_all(writes).await.map(|_| ())
    }

    async fn close_and_wait(&self, handle: &[u8]) -> Result<()> {
        let mut payload = Vec::with_capacity(handle.len() + 4);
        put_bytes(&mut payload, handle);
        self.status_request(FXP_CLOSE, &payload, b"").await
    }

    async fn extended(&self, name: &[u8], arguments: &[&[u8]], path: &[u8]) -> Result<()> {
        let mut payload = Vec::new();
        put_bytes(&mut payload, name);
        for argument in arguments {
            put_bytes(&mut payload, argument);
        }
        self.status_request(FXP_EXTENDED, &payload, path).await
    }

    async fn remove_quietly(&self, path: &[u8]) {
        let mut payload = Vec::with_capacity(path.len() + 4);
        put_bytes(&mut payload, path);
        let _ = self.status_request(FXP_REMOVE, &payload, path).await;
    }

    async fn send(&self, handle: &[u8], content: Content<'_>) -> Result<()> {
        let (source, transfer) = match content {
            Content::Bytes(data) => return self.write_all(handle, data, 0).await,
            Content::File(source, transfer) => (source, transfer),
        };
        let mut file = tokio::fs::File::open(source).await?;
        let mut buffer = vec![0; TRANSFER_CHUNK];
        let mut sent = 0;
        transfer.step(sent)?;
        loop {
            let mut filled = 0;
            while filled < buffer.len() {
                match file.read(&mut buffer[filled..]).await? {
                    0 => break,
                    count => filled += count,
                }
            }
            if filled == 0 {
                return Ok(());
            }
            self.write_all(handle, &buffer[..filled], sent).await?;
            sent += filled as u64;
            transfer.step(sent)?;
        }
    }

    pub async fn mkdir(&self, path: &[u8], mode: u32) -> Result<()> {
        let mut payload = Vec::with_capacity(path.len() + 12);
        put_bytes(&mut payload, path);
        payload.put_u32(ATTR_PERMISSIONS);
        payload.put_u32(mode);
        match self.status_request(FXP_MKDIR, &payload, path).await {
            // SFTP v3 has no "already exists" status, so look.
            Err(error @ (RvError::Sftp(..) | RvError::PermissionDenied(_))) => {
                match self.stat(path, true).await {
                    Ok(attrs) if attrs.mode & MODE_TYPE_MASK == MODE_DIR => Ok(()),
                    _ => Err(error),
                }
            }
            other => other,
        }
    }

    /// Own handle, not the shared one: that may still point at an old inode.
    pub async fn download_file(&self, path: &[u8], destination: &Path, transfer: &Transfer) -> Result<()> {
        let handle = self.open_for_read(path).await?;
        let outcome = self.receive(&handle, destination, transfer).await;
        self.close_handle(&handle);
        outcome
    }

    async fn receive(&self, handle: &[u8], destination: &Path, transfer: &Transfer) -> Result<()> {
        let mut file = tokio::fs::File::create(destination).await?;
        let mut received = 0;
        transfer.step(received)?;
        loop {
            let data = self.read_handle(handle, received, TRANSFER_CHUNK as u64).await?;
            if data.is_empty() {
                file.flush().await?;
                return Ok(());
            }
            file.write_all(&data).await?;
            received += data.len() as u64;
            transfer.step(received)?;
        }
    }

    pub async fn upload_file(&self, source: &Path, path: &[u8], mode: u32, transfer: &Transfer) -> Result<()> {
        self.replace(path, Content::File(source, transfer), mode).await.map(|_| ())
    }

    pub async fn write_file(&self, path: &[u8], data: &[u8]) -> Result<Attrs> {
        self.replace(path, Content::Bytes(data), 0o644).await
    }

    /// Replaces the file through a temp file and an atomic rename. Mirrors
    /// Session._replace in the Python backend.
    async fn replace(&self, path: &[u8], content: Content<'_>, new_mode: u32) -> Result<Attrs> {
        let target = self.realpath(path).await?; // keeps a symlink a symlink
        let current = match self.stat(&target, true).await {
            Ok(attrs) => Some(attrs),
            Err(RvError::NoSuchFile(_)) => None,
            Err(error) => return Err(error),
        };
        if !self.write_atomically(&target, content, current, new_mode).await? {
            let handle = self.open_for_write(&target, FXF_WRITE | FXF_CREAT | FXF_TRUNC, None).await?;
            let written = self.send(&handle, content).await;
            let closed = self.close_and_wait(&handle).await;
            written.and(closed)?;
        }
        self.drop_handle(path);
        self.drop_handle(&target);
        self.stat(&target, true).await
    }

    /// Ok(false) if a rename couldn't keep the file as it was.
    async fn write_atomically(
        &self,
        target: &[u8],
        content: Content<'_>,
        current: Option<Attrs>,
        new_mode: u32,
    ) -> Result<bool> {
        let split = target.iter().rposition(|&b| b == b'/').map_or(0, |i| i + 1);
        let mut token = [0u8; 4];
        getrandom::fill(&mut token).map_err(|e| RvError::Io(e.to_string()))?;
        // Not built from the name: a long one would leave no room for a suffix.
        let mut temp = target[..split].to_vec();
        temp.extend_from_slice(b".rv-");
        temp.extend(token.iter().flat_map(|b| format!("{b:02x}").into_bytes()));
        temp.extend_from_slice(b".tmp");

        let flags = FXF_WRITE | FXF_CREAT | FXF_EXCL;
        let handle = match self.open_for_write(&temp, flags, Some(0o600)).await {
            Ok(handle) => handle,
            Err(RvError::PermissionDenied(_)) => return Ok(false),
            Err(error) => return Err(error),
        };
        let outcome = self.fill_temp(&handle, content, current, new_mode).await;
        let closed = self.close_and_wait(&handle).await;
        let filled = match outcome.and_then(|same_owner| closed.map(|_| same_owner)) {
            Ok(filled) => filled,
            Err(error) => {
                self.remove_quietly(&temp).await;
                return Err(error);
            }
        };
        if !filled {
            self.remove_quietly(&temp).await;
            return Ok(false);
        }
        match self.extended(b"posix-rename@openssh.com", &[&temp, target], target).await {
            Ok(()) => Ok(true),
            Err(RvError::Sftp(FX_OP_UNSUPPORTED, _)) => {
                self.remove_quietly(&temp).await;
                Ok(false)
            }
            Err(error) => {
                self.remove_quietly(&temp).await;
                Err(error)
            }
        }
    }

    /// Ok(false) if the temp file's owner differs from the target's.
    async fn fill_temp(
        &self,
        handle: &[u8],
        content: Content<'_>,
        current: Option<Attrs>,
        new_mode: u32,
    ) -> Result<bool> {
        let mut payload = Vec::with_capacity(handle.len() + 12);
        put_bytes(&mut payload, handle);
        let mut reply = self.request(FXP_FSTAT, &payload).await?;
        if reply.kind != FXP_ATTRS {
            return Err(status_error(&mut reply.body, b""));
        }
        let mine = take_attrs(&mut reply.body)?;
        if current.is_some_and(|c| (c.uid, c.gid) != (mine.uid, mine.gid)) {
            return Ok(false);
        }
        self.send(handle, content).await?;
        payload.put_u32(ATTR_PERMISSIONS);
        payload.put_u32(current.map_or(new_mode, |c| c.mode & 0o7777));
        self.status_request(FXP_FSETSTAT, &payload, b"").await?;
        match self.extended(b"fsync@openssh.com", &[handle], b"").await {
            Ok(()) | Err(RvError::Sftp(FX_OP_UNSUPPORTED, _)) => Ok(true),
            Err(error) => Err(error),
        }
    }

    async fn read_chunk(&self, handle: &[u8], offset: u64, length: u32) -> Result<Option<Bytes>> {
        let mut payload = Vec::with_capacity(handle.len() + 16);
        put_bytes(&mut payload, handle);
        payload.put_u64(offset);
        payload.put_u32(length);
        let mut reply = self.request(FXP_READ, &payload).await?;
        match reply.kind {
            FXP_DATA => Ok(Some(take_bytes(&mut reply.body)?)),
            _ if reply.body.clone().get_u32() == FX_EOF => Ok(None),
            _ => Err(status_error(&mut reply.body, b"")),
        }
    }

    /// Pipelined read; shorter than ``length`` only at end of file.
    pub async fn read(&self, path: &[u8], offset: u64, length: u64) -> Result<Bytes> {
        let handle = self.handle(path).await?;
        self.read_handle(&handle, offset, length).await
    }

    async fn read_handle(&self, handle: &[u8], offset: u64, length: u64) -> Result<Bytes> {
        let mut position = offset;
        let end = offset + length;
        let mut output: Option<BytesMut> = None;
        loop {
            let mut requests = Vec::new();
            let mut cursor = position;
            while cursor < end {
                let size = REQUEST_SIZE.min(end - cursor);
                requests.push(self.read_chunk(handle, cursor, size as u32));
                cursor += size;
            }
            let chunks = try_join_all(requests).await?;
            let single = chunks.len() == 1 && output.is_none();
            let mut stop = false;
            let mut sizes = position;
            let mut last = None;
            for chunk in chunks {
                let wanted = REQUEST_SIZE.min(end - sizes);
                match chunk {
                    None => {
                        stop = true;
                        break;
                    }
                    Some(data) => {
                        let short = (data.len() as u64) < wanted;
                        position += data.len() as u64;
                        sizes += wanted;
                        if single {
                            last = Some(data);
                        } else {
                            output
                                .get_or_insert_with(|| BytesMut::with_capacity(length as usize))
                                .extend_from_slice(&data);
                        }
                        if short {
                            break; // re-read from here; EOF ends the loop next pass
                        }
                    }
                }
            }
            if stop || position >= end {
                return Ok(match (last, output) {
                    (Some(data), None) => data,
                    (_, Some(buffer)) => buffer.freeze(),
                    _ => Bytes::new(),
                });
            }
            if let Some(data) = last {
                output.get_or_insert_with(|| BytesMut::with_capacity(length as usize)).extend_from_slice(&data);
            }
        }
    }
}

async fn read_replies<R: AsyncReadExt + Unpin>(reader: &mut R, waiters: &Waiters) -> Result<()> {
    loop {
        let length = reader.read_u32().await? as usize;
        if !(5..=MAX_PACKET).contains(&length) {
            return Err(RvError::Io("bad sftp packet length".into()));
        }
        let mut packet = BytesMut::zeroed(length);
        reader.read_exact(&mut packet).await?;
        let mut packet = packet.freeze();
        let kind = packet.get_u8();
        let id = packet.get_u32();
        if matches!(kind, FXP_STATUS | FXP_HANDLE | FXP_DATA | FXP_NAME | FXP_ATTRS | FXP_EXTENDED_REPLY) {
            if let Some(waiter) = waiters.lock().unwrap().remove(&id) {
                let _ = waiter.send(Reply { kind, body: packet });
            }
        }
    }
}
