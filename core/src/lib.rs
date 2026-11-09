//! PyO3 bindings. Mirrors app/backend/interface.py; the thin wrapper is
//! app/backend/rust_backend.py.
//!
//! Every result reaches Python through one delivery thread, so tokio workers
//! never wait for the GIL and streaming never touches it.

mod cache;
mod error;
mod range_server;
mod scheduler;
mod session;
mod sftp_pool;
mod watcher;

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc as std_mpsc, Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use russh::ChannelMsg;
use tokio::io::AsyncWriteExt;
use tokio::runtime::{Handle, Runtime};
use tokio::sync::oneshot;

use cache::{FileKey, BLOCK_SIZE};
use error::{Result, RvError};
use range_server::RangeServer;
use scheduler::Scheduler;
use session::{Hop, Params, Session, State};
use sftp_pool::{Attrs, Transfer};
use watcher::{Event, Watcher};

const STAT_TTL: Duration = Duration::from_secs(1);
const SEARCH_BATCH: Duration = Duration::from_millis(50);
const SCHEDULER_WORKERS: usize = 8;

type Task = Box<dyn FnOnce(Python<'_>) + Send>;
/// Cloning an Arc needs no GIL, unlike cloning a Py.
type Callback = Arc<Py<PyAny>>;

const TERM: &str = "xterm-256color";

/// What the Qt side sends to a running shell.
enum ShellInput {
    Data(Vec<u8>),
    Resize(u32, u32),
    Close,
}


enum Value {
    None,
    Bytes(Bytes),
    Entry(Vec<u8>, Attrs),
    Entries(Vec<(Vec<u8>, Attrs)>),
    Exec(u32, Vec<u8>, Vec<u8>),
}

fn value_to_py(py: Python<'_>, value: Value) -> PyResult<Py<PyAny>> {
    let attrs = |py: Python<'_>, name: &[u8], a: Attrs| -> PyResult<Py<PyAny>> {
        Ok((PyBytes::new(py, name), a.size, a.mtime, a.mode).into_pyobject(py)?.into_any().unbind())
    };
    Ok(match value {
        Value::None => py.None(),
        Value::Bytes(data) => PyBytes::new(py, &data).into_any().unbind(),
        Value::Entry(name, a) => attrs(py, &name, a)?,
        Value::Entries(entries) => {
            let items: Vec<Py<PyAny>> =
                entries.iter().map(|(n, a)| attrs(py, n, *a)).collect::<PyResult<_>>()?;
            PyList::new(py, items)?.into_any().unbind()
        }
        Value::Exec(status, stdout, stderr) => {
            (status, PyBytes::new(py, &stdout), PyBytes::new(py, &stderr)).into_pyobject(py)?.into_any().unbind()
        }
    })
}

fn error_to_py(py: Python<'_>, error: &RvError) -> PyResult<Py<PyAny>> {
    let message = error.to_string();
    let tuple = match error {
        RvError::HostKeyUnknown { host, port, fingerprint, key_line } => {
            PyTuple::new(py, ["hostkey_unknown".to_string(), message, host.clone(), port.to_string(),
                              fingerprint.clone(), key_line.clone()])?
        }
        RvError::HostKeyChanged(_) => PyTuple::new(py, ["hostkey_changed".to_string(), message])?,
        RvError::Auth(_) => PyTuple::new(py, ["auth".to_string(), message])?,
        RvError::NoSuchFile(_) => PyTuple::new(py, ["not_found".to_string(), message])?,
        RvError::PermissionDenied(_) => PyTuple::new(py, ["denied".to_string(), message])?,
        RvError::Lost(_) | RvError::NotConnected => PyTuple::new(py, ["lost".to_string(), message])?,
        RvError::Refused(_) => PyTuple::new(py, ["refused".to_string(), message])?,
        RvError::Cancelled => PyTuple::new(py, ["cancelled".to_string(), message])?,
        _ => PyTuple::new(py, ["io".to_string(), message])?,
    };
    Ok(tuple.into_any().unbind())
}

struct Inner {
    runtime: Handle,
    session: Arc<Session>,
    scheduler: Arc<Scheduler>,
    watcher: Arc<Watcher>,
    range_server: RangeServer,
    delivery: Delivery,
    stats: Arc<StatCache>,
    last_target: Mutex<Option<(String, u16)>>,
    preference: Arc<Mutex<String>>,
    next_exec: AtomicU64,
    execs: Arc<Mutex<HashMap<u64, oneshot::Sender<()>>>>,
    transfers: Mutex<HashMap<u64, Arc<Transfer>>>,
    shells: Arc<Mutex<HashMap<u64, tokio::sync::mpsc::UnboundedSender<ShellInput>>>>,
}

/// Sends work to the delivery thread. None tells it to stop.
#[derive(Clone)]
struct Delivery(std_mpsc::Sender<Option<Task>>);

impl Delivery {
    fn send(&self, task: Task) {
        let _ = self.0.send(Some(task));
    }
}

#[derive(Default)]
struct StatCache {
    generations: Mutex<HashMap<Vec<u8>, u64>>,
    keys: Mutex<HashMap<Vec<u8>, (Instant, Arc<FileKey>)>>,
}

impl StatCache {
    fn bump(&self, path: &[u8]) {
        *self.generations.lock().unwrap().entry(path.to_vec()).or_insert(0) += 1;
        self.keys.lock().unwrap().remove(path);
    }

    async fn key(&self, session: &Session, path: Vec<u8>) -> Result<Arc<FileKey>> {
        if let Some((when, key)) = self.keys.lock().unwrap().get(&path) {
            if when.elapsed() < STAT_TTL {
                return Ok(key.clone());
            }
        }
        let attrs = session.retrying(|conn| {
            let path = path.clone();
            async move { conn.sftp.stat(&path, true).await }
        }).await?;
        let generation = self.generations.lock().unwrap().get(&path).copied().unwrap_or(0);
        let key = Arc::new(FileKey { path: path.clone(), mtime: attrs.mtime, generation, size: attrs.size });
        self.keys.lock().unwrap().insert(path, (Instant::now(), key.clone()));
        Ok(key)
    }
}

async fn read_retrying(session: &Session, path: Vec<u8>, offset: u64, length: u64) -> Result<Bytes> {
    session.retrying(|conn| {
        let path = path.clone();
        async move { conn.sftp.read(&path, offset, length).await }
    }).await
}

#[pyclass]
struct Core {
    inner: Mutex<Option<Arc<Inner>>>,
    runtime: Mutex<Option<Runtime>>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl Core {
    /// An exec or shell channel driven by ShellInput. ``pty`` is (env, columns, rows).
    /// Without ``on_stderr``, stderr goes to ``on_stdout``.
    fn start_channel(&self, command: Option<Vec<u8>>, pty: Option<(Vec<(String, String)>, u32, u32)>,
                     on_stdout: Py<PyAny>, on_stderr: Option<Py<PyAny>>, on_exit: Py<PyAny>) -> PyResult<u64> {
        let inner = self.inner()?;
        let id = inner.next_exec.fetch_add(1, Ordering::Relaxed);
        let (input, mut inbox) = tokio::sync::mpsc::unbounded_channel();
        inner.shells.lock().unwrap().insert(id, input);
        let (session, delivery, shells) = (inner.session.clone(), inner.delivery.clone(), inner.shells.clone());
        let on_stdout: Callback = Arc::new(on_stdout);
        let on_stderr: Callback = on_stderr.map(Arc::new).unwrap_or_else(|| on_stdout.clone());
        inner.runtime.spawn(async move {
            let deliver = |callback: &Callback, data: &[u8]| {
                let callback = callback.clone();
                let data = data.to_vec();
                delivery.send(Box::new(move |py| {
                    if let Err(error) = callback.call1(py, (PyBytes::new(py, &data),)) {
                        error.print(py);
                    }
                }));
            };
            let mut status: Option<u32> = None;
            let outcome: Result<()> = async {
                let conn = session.wait_ready().await?;
                let mut channel = conn.handle.channel_open_session().await?;
                if let Some((env, columns, rows)) = pty {
                    for (name, value) in env {
                        channel.set_env(false, name, value).await?;
                    }
                    channel.request_pty(true, TERM, columns, rows, 0, 0, &[]).await?;
                }
                match &command {
                    Some(command) => channel.exec(true, command.clone()).await?,
                    None => channel.request_shell(true).await?,
                }
                loop {
                    tokio::select! {
                        item = inbox.recv() => match item {
                            Some(ShellInput::Data(data)) => channel.data(&data[..]).await?,
                            Some(ShellInput::Resize(c, r)) => channel.window_change(c, r, 0, 0).await?,
                            Some(ShellInput::Close) | None => {
                                let _ = channel.eof().await;
                                let _ = channel.close().await;
                                break;
                            }
                        },
                        message = channel.wait() => match message {
                            Some(ChannelMsg::Data { data }) => deliver(&on_stdout, &data),
                            Some(ChannelMsg::ExtendedData { data, .. }) => deliver(&on_stderr, &data),
                            Some(ChannelMsg::ExitStatus { exit_status }) => status = Some(exit_status),
                            Some(ChannelMsg::Close) | None => break,
                            _ => {}
                        }
                    }
                }
                Ok(())
            }.await;
            shells.lock().unwrap().remove(&id);
            let message = outcome.err().map(|error| error.to_string());
            delivery.send(Box::new(move |py| {
                if let Err(error) = on_exit.call1(py, (status, message)) {
                    error.print(py);
                }
            }));
        });
        Ok(id)
    }

    fn shell_send(&self, id: u64, item: ShellInput) -> PyResult<()> {
        if let Some(input) = self.inner()?.shells.lock().unwrap().get(&id) {
            let _ = input.send(item);
        }
        Ok(())
    }

    fn inner(&self) -> PyResult<Arc<Inner>> {
        self.inner.lock().unwrap().clone()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("core is closed"))
    }

    /// Run ``work`` on tokio; ``callback(ok, value)`` fires on the delivery thread.
    fn spawn<F>(&self, callback: Py<PyAny>, work: impl FnOnce(Arc<Inner>) -> F) -> PyResult<()>
    where
        F: std::future::Future<Output = Result<Value>> + Send + 'static,
    {
        let inner = self.inner()?;
        let delivery = inner.delivery.clone();
        let future = work(inner.clone());
        inner.runtime.spawn(async move {
            let outcome = future.await;
            delivery.send(Box::new(move |py| {
                let args = match &outcome {
                    Ok(_) => None,
                    Err(error) => Some(error_to_py(py, error)),
                };
                let result = match args {
                    Some(Ok(error)) => callback.call1(py, (false, error)),
                    Some(Err(error)) => Err(error),
                    None => match outcome {
                        Ok(value) => value_to_py(py, value).and_then(|v| callback.call1(py, (true, v))),
                        Err(_) => unreachable!(),
                    },
                };
                if let Err(error) = result {
                    error.print(py);
                }
            }));
        });
        Ok(())
    }

    /// As ``spawn``, for a file transfer. Returns an id for cancel_transfer.
    fn start_transfer<F>(
        &self,
        on_progress: Py<PyAny>,
        callback: Py<PyAny>,
        work: impl FnOnce(Arc<Inner>, Arc<Transfer>) -> F,
    ) -> PyResult<u64>
    where
        F: std::future::Future<Output = Result<()>> + Send + 'static,
    {
        let inner = self.inner()?;
        let id = inner.next_exec.fetch_add(1, Ordering::Relaxed);
        let delivery = inner.delivery.clone();
        let on_progress: Callback = Arc::new(on_progress);
        let transfer = Arc::new(Transfer::new(Box::new(move |done| {
            let callback = on_progress.clone();
            delivery.send(Box::new(move |py| {
                if let Err(error) = callback.call1(py, (done,)) {
                    error.print(py);
                }
            }));
        })));
        inner.transfers.lock().unwrap().insert(id, transfer.clone());
        self.spawn(callback, move |inner| {
            let future = work(inner.clone(), transfer);
            async move {
                let outcome = future.await;
                inner.transfers.lock().unwrap().remove(&id);
                outcome.map(|_| Value::None)
            }
        })?;
        Ok(id)
    }
}

fn start_delivery() -> (Delivery, JoinHandle<()>) {
    let (sender, receiver) = std_mpsc::channel::<Option<Task>>();
    let thread = std::thread::Builder::new()
        .name("rv-delivery".into())
        .spawn(move || {
            while let Ok(Some(task)) = receiver.recv() {
                Python::attach(task);
            }
        })
        .expect("spawn delivery thread");
    (Delivery(sender), thread)
}

fn state_name(state: State) -> &'static str {
    match state {
        State::Disconnected => "disconnected",
        State::Connecting => "connecting",
        State::Connected => "connected",
        State::Reconnecting => "reconnecting",
    }
}

fn bytes_field(dict: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<String>> {
    match dict.get_item(name)? {
        Some(value) if !value.is_none() => Ok(Some(value.extract()?)),
        _ => Ok(None),
    }
}

fn required<'py>(dict: &Bound<'py, PyDict>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    dict.get_item(name)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_string()))
}

fn parse_params(dict: &Bound<'_, PyDict>) -> PyResult<Params> {
    let hops: Vec<(String, u16, String)> = required(dict, "hops")?.extract()?;
    Ok(Params {
        hops: hops.into_iter().map(|(host, port, username)| Hop { host, port, username }).collect(),
        identity_files: required(dict, "identity_files")?.extract()?,
        password: bytes_field(dict, "password")?,
        passphrase: bytes_field(dict, "passphrase")?,
        known_hosts: PathBuf::from(required(dict, "known_hosts")?.extract::<String>()?),
        keepalive_interval: required(dict, "keepalive_interval")?.extract()?,
        reconnect: required(dict, "reconnect")?.extract()?,
    })
}

#[pymethods]
impl Core {
    #[new]
    fn new(cache_bytes: usize, on_state: Py<PyAny>, on_events: Py<PyAny>) -> PyResult<Self> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .thread_name("rv-core")
            .enable_all()
            .build()?;
        let (delivery, thread) = start_delivery();
        let (on_state, on_events): (Callback, Callback) = (Arc::new(on_state), Arc::new(on_events));

        let session = Session::new();
        let stats = Arc::new(StatCache::default());

        let reader = session.clone();
        let read_block: scheduler::ReadBlock = Arc::new(move |path, offset, length| {
            let session = reader.clone();
            Box::pin(async move { read_retrying(&session, path, offset, length).await })
        });

        let _guard = runtime.enter();
        let scheduler = Scheduler::new(cache_bytes, read_block, SCHEDULER_WORKERS);

        let stat_session = session.clone();
        let stat_cache = stats.clone();
        let stat_file: range_server::StatFile = Arc::new(move |path| {
            let session = stat_session.clone();
            let stats = stat_cache.clone();
            Box::pin(async move { stats.key(&session, path).await })
        });
        let range_server = runtime.block_on(RangeServer::start(scheduler.clone(), stat_file))?;

        let events_delivery = delivery.clone();
        let event_session = session.clone();
        let event_stats = stats.clone();
        let deliver: watcher::Deliver = Arc::new(move |events: Vec<Event>| {
            for event in events.iter().filter(|e| !e.is_dir) {
                match event.kind {
                    "modify" | "close_write" => event_stats.bump(&event.path),
                    "resync" => {}
                    _ => {
                        event_stats.bump(&event.path);
                        if let Some(conn) = event_session.current() {
                            conn.sftp.drop_handle(&event.path);
                        }
                    }
                }
            }
            let callback = on_events.clone();
            events_delivery.send(Box::new(move |py| {
                let items: Vec<_> = events
                    .iter()
                    .map(|e| (e.kind, PyBytes::new(py, &e.path), e.is_dir))
                    .collect();
                if let Err(error) = callback.call1(py, (items,)) {
                    error.print(py);
                }
            }));
        });
        let watcher = Watcher::new(session.clone(), deliver);

        let state_delivery = delivery.clone();
        let _ = session.on_state.set(Box::new(move |state, detail| {
            let callback = on_state.clone();
            state_delivery.send(Box::new(move |py| {
                if let Err(error) = callback.call1(py, (state_name(state), detail)) {
                    error.print(py);
                }
            }));
        }));

        let preference = Arc::new(Mutex::new("auto".to_string()));
        let ready_watcher = watcher.clone();
        let ready_stats = stats.clone();
        let ready_preference = preference.clone();
        let _ = session.on_ready.set(Box::new(move |reconnected| {
            ready_stats.keys.lock().unwrap().clear();
            let preference = ready_preference.lock().unwrap().clone();
            tokio::spawn(ready_watcher.clone().start(preference, reconnected));
        }));
        let lost_watcher = watcher.clone();
        let _ = session.on_lost.set(Box::new(move || lost_watcher.stop()));
        drop(_guard);

        let handle = runtime.handle().clone();
        Ok(Core {
            runtime: Mutex::new(Some(runtime)),
            inner: Mutex::new(Some(Arc::new(Inner {
                runtime: handle,
                session,
                scheduler,
                watcher,
                range_server,
                delivery,
                stats,
                last_target: Mutex::new(None),
                preference,
                next_exec: AtomicU64::new(1),
                execs: Arc::new(Mutex::new(HashMap::new())),
                transfers: Mutex::new(HashMap::new()),
                shells: Arc::new(Mutex::new(HashMap::new())),
            }))),
            thread: Mutex::new(Some(thread)),
        })
    }

    fn connect(&self, params: &Bound<'_, PyDict>, watcher: String, callback: Py<PyAny>) -> PyResult<()> {
        let params = parse_params(params)?;
        self.spawn(callback, move |inner| async move {
            inner.watcher.stop();
            let target = params.hops.last().map(|h| (h.host.clone(), h.port));
            {
                // Blocks are keyed by path; another host has other files there.
                let mut last = inner.last_target.lock().unwrap();
                if *last != target {
                    inner.scheduler.cache.lock().unwrap().clear();
                }
                *last = target;
            }
            inner.stats.keys.lock().unwrap().clear();
            *inner.preference.lock().unwrap() = watcher;
            let home = inner.session.connect(params).await?;
            Ok(Value::Bytes(Bytes::from(home)))
        })
    }

    fn disconnect(&self, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, |inner| async move {
            inner.watcher.stop();
            inner.session.close().await;
            Ok(Value::None)
        })
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let inner = self.inner.lock().unwrap().take();
        let runtime = self.runtime.lock().unwrap().take();
        let thread = self.thread.lock().unwrap().take();
        py.detach(move || {
            if let (Some(inner), Some(runtime)) = (inner, runtime) {
                inner.watcher.stop();
                let session = inner.session.clone();
                runtime.block_on(async move {
                    let _ = tokio::time::timeout(Duration::from_secs(3), session.close()).await;
                });
                let _ = inner.delivery.0.send(None);
                runtime.shutdown_timeout(Duration::from_secs(1));
            }
            if let Some(thread) = thread {
                let _ = thread.join();
            }
        });
        Ok(())
    }

    fn listdir(&self, path: Vec<u8>, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            let entries = inner.session.retrying(|conn| {
                let path = path.clone();
                async move { conn.sftp.readdir(&path).await }
            }).await?;
            Ok(Value::Entries(entries.into_iter().map(|e| (e.name, e.attrs)).collect()))
        })
    }

    fn stat(&self, path: Vec<u8>, follow: bool, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            let attrs = inner.session.retrying(|conn| {
                let path = path.clone();
                async move { conn.sftp.stat(&path, follow).await }
            }).await?;
            Ok(Value::Entry(path, attrs))
        })
    }

    fn realpath(&self, path: Vec<u8>, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            let real = inner.session.retrying(|conn| {
                let path = path.clone();
                async move { conn.sftp.realpath(&path).await }
            }).await?;
            Ok(Value::Bytes(Bytes::from(real)))
        })
    }

    fn read(&self, path: Vec<u8>, offset: u64, length: u64, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            Ok(Value::Bytes(read_retrying(&inner.session, path, offset, length).await?))
        })
    }

    fn fetch(&self, path: Vec<u8>, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            inner.stats.keys.lock().unwrap().remove(&path);
            let key = inner.stats.key(&inner.session, path).await?;
            let count = key.size.div_ceil(BLOCK_SIZE);
            let blocks = futures::future::try_join_all(
                (0..count).map(|index| inner.scheduler.get(&key, index))).await?;
            let mut output = Vec::with_capacity(key.size as usize);
            for block in blocks {
                output.extend_from_slice(&block);
            }
            Ok(Value::Bytes(Bytes::from(output)))
        })
    }

    fn download(&self, path: Vec<u8>, destination: String, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            inner.stats.keys.lock().unwrap().remove(&path);
            let key = inner.stats.key(&inner.session, path).await?;
            let owner = inner.scheduler.owner();
            let mut file = tokio::fs::File::create(destination).await?;
            for index in 0..key.size.div_ceil(BLOCK_SIZE) {
                inner.scheduler.prefetch(&owner, &key, index + 1, 16);
                file.write_all(&inner.scheduler.get(&key, index).await?).await?;
            }
            file.flush().await?;
            Ok(Value::None)
        })
    }

    fn download_file(
        &self,
        path: Vec<u8>,
        destination: PathBuf,
        on_progress: Py<PyAny>,
        callback: Py<PyAny>,
    ) -> PyResult<u64> {
        self.start_transfer(on_progress, callback, move |inner, transfer| async move {
            inner.session.retrying(|conn| {
                let (path, destination, transfer) = (path.clone(), destination.clone(), transfer.clone());
                async move { conn.sftp.download_file(&path, &destination, &transfer).await }
            }).await
        })
    }

    fn upload_file(
        &self,
        source: PathBuf,
        path: Vec<u8>,
        mode: u32,
        on_progress: Py<PyAny>,
        callback: Py<PyAny>,
    ) -> PyResult<u64> {
        self.start_transfer(on_progress, callback, move |inner, transfer| async move {
            inner.session.retrying(|conn| {
                let (source, path, transfer) = (source.clone(), path.clone(), transfer.clone());
                async move { conn.sftp.upload_file(&source, &path, mode, &transfer).await }
            }).await?;
            inner.stats.bump(&path);
            Ok(())
        })
    }

    fn cancel_transfer(&self, id: u64) -> PyResult<()> {
        if let Some(transfer) = self.inner()?.transfers.lock().unwrap().get(&id) {
            transfer.cancel();
        }
        Ok(())
    }

    fn mkdir(&self, path: Vec<u8>, mode: u32, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            inner.session.retrying(|conn| {
                let path = path.clone();
                async move { conn.sftp.mkdir(&path, mode).await }
            }).await?;
            Ok(Value::None)
        })
    }

    fn write(&self, path: Vec<u8>, data: &Bound<'_, PyBytes>, callback: Py<PyAny>) -> PyResult<()> {
        let data = Bytes::copy_from_slice(data.as_bytes());
        self.spawn(callback, move |inner| async move {
            let attrs = inner.session.retrying(|conn| {
                let (path, data) = (path.clone(), data.clone());
                async move { conn.sftp.write_file(&path, &data).await }
            }).await?;
            inner.stats.bump(&path);
            Ok(Value::Entry(path, attrs))
        })
    }

    fn open_url(&self, path: Vec<u8>) -> PyResult<String> {
        Ok(self.inner()?.range_server.url_for(&path))
    }

    fn invalidate(&self, path: Vec<u8>) -> PyResult<()> {
        let inner = self.inner()?;
        inner.stats.bump(&path);
        if let Some(conn) = inner.session.current() {
            conn.sftp.drop_handle(&path);
        }
        Ok(())
    }

    fn watch(&self, directories: Vec<Vec<u8>>, hot: Vec<Vec<u8>>) -> PyResult<()> {
        let directories: HashSet<_> = directories.into_iter().collect();
        self.inner()?.watcher.set_watched(directories, hot.into_iter().collect());
        Ok(())
    }

    fn watcher_mode(&self) -> PyResult<&'static str> {
        Ok(self.inner()?.watcher.mode())
    }

    fn established_watches(&self) -> PyResult<Vec<Vec<u8>>> {
        Ok(self.inner()?.watcher.established())
    }

    fn reads_issued(&self) -> PyResult<u64> {
        Ok(self.inner()?.scheduler.reads_issued.load(Ordering::Relaxed))
    }

    fn run(&self, command: Vec<u8>, callback: Py<PyAny>) -> PyResult<()> {
        self.spawn(callback, move |inner| async move {
            let (status, stdout, stderr) = inner.session.run(&command).await?;
            Ok(Value::Exec(status, stdout, stderr))
        })
    }

    /// Stream stdout lines; returns an id for cancel_exec.
    fn exec_lines(&self, command: Vec<u8>, on_lines: Py<PyAny>, on_done: Py<PyAny>) -> PyResult<u64> {
        let inner = self.inner()?;
        let id = inner.next_exec.fetch_add(1, Ordering::Relaxed);
        let (stop, mut stopped) = oneshot::channel();
        inner.execs.lock().unwrap().insert(id, stop);
        let (session, delivery, execs) = (inner.session.clone(), inner.delivery.clone(), inner.execs.clone());
        let on_lines: Callback = Arc::new(on_lines);
        inner.runtime.spawn(async move {
            let flush = |batch: Vec<Vec<u8>>, callback: Callback| {
                delivery.send(Box::new(move |py| {
                    let items: Vec<_> = batch.iter().map(|l| PyBytes::new(py, l)).collect();
                    if let Err(error) = callback.call1(py, (items,)) {
                        error.print(py);
                    }
                }));
            };
            let clone_callback = || on_lines.clone();
            let outcome: Option<String> = match session.open_exec(&command).await {
                Err(error) => Some(error.to_string()),
                Ok(mut channel) => {
                    let (mut pending, mut batch) = (Vec::new(), Vec::new());
                    let mut flushed = Instant::now();
                    let error = loop {
                        tokio::select! {
                            _ = &mut stopped => {
                                let _ = channel.eof().await;
                                let _ = channel.close().await;
                                break Some("cancelled".to_string());
                            }
                            message = channel.wait() => match message {
                                Some(ChannelMsg::Data { data }) => {
                                    pending.extend_from_slice(&data);
                                    while let Some(end) = pending.iter().position(|&b| b == b'\n') {
                                        let mut line: Vec<u8> = pending.drain(..=end).collect();
                                        line.pop();
                                        batch.push(line);
                                    }
                                    if flushed.elapsed() >= SEARCH_BATCH && !batch.is_empty() {
                                        flush(std::mem::take(&mut batch), clone_callback());
                                        flushed = Instant::now();
                                    }
                                }
                                Some(ChannelMsg::Close) | None => break None,
                                _ => {}
                            }
                        }
                    };
                    if !batch.is_empty() {
                        flush(batch, clone_callback());
                    }
                    error
                }
            };
            execs.lock().unwrap().remove(&id);
            delivery.send(Box::new(move |py| {
                if let Err(error) = on_done.call1(py, (outcome,)) {
                    error.print(py);
                }
            }));
        });
        Ok(id)
    }

    fn cancel_exec(&self, id: u64) -> PyResult<()> {
        if let Some(stop) = self.inner()?.execs.lock().unwrap().remove(&id) {
            let _ = stop.send(());
        }
        Ok(())
    }

    /// Login shell on a pty. ``command`` None asks for the plain shell.
    /// Returns an id for shell_write, shell_resize and shell_close.
    /// ``on_exit(status, error)``.
    fn open_shell(&self, command: Option<Vec<u8>>, env: Vec<(String, String)>, columns: u32, rows: u32,
                  on_output: Py<PyAny>, on_exit: Py<PyAny>) -> PyResult<u64> {
        self.start_channel(command, Some((env, columns, rows)), on_output, None, on_exit)
    }

    /// A command without a pty: stdout and stderr apart. Same ids as a shell.
    fn start_process(&self, command: Vec<u8>, on_stdout: Py<PyAny>, on_stderr: Py<PyAny>,
                     on_exit: Py<PyAny>) -> PyResult<u64> {
        self.start_channel(Some(command), None, on_stdout, Some(on_stderr), on_exit)
    }

    fn shell_write(&self, id: u64, data: Vec<u8>) -> PyResult<()> {
        self.shell_send(id, ShellInput::Data(data))
    }

    fn shell_resize(&self, id: u64, columns: u32, rows: u32) -> PyResult<()> {
        self.shell_send(id, ShellInput::Resize(columns, rows))
    }

    fn shell_close(&self, id: u64) -> PyResult<()> {
        self.shell_send(id, ShellInput::Close)
    }
}

#[pymodule]
fn rv_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Core>()?;
    Ok(())
}
