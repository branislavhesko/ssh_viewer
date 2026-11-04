//! One SSH connection: authentication, host keys, reconnect, exec channels.

use std::borrow::Cow;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::time::Duration;

use russh::client::{self, Handle, Msg};
use russh::keys::agent::client::AgentClient;
use russh::keys::{self, HashAlg, PrivateKeyWithHashAlg, PublicKey, PublicKeyOrCertificate};
use russh::{cipher, compression, Channel, ChannelMsg, Preferred};
use tokio::sync::{mpsc, watch};

use crate::error::{Result, RvError};
use crate::sftp_pool::Sftp;

const RECONNECT_WAIT: Duration = Duration::from_secs(30);
const CIPHERS: &[cipher::Name] = &[
    cipher::AES_128_GCM,
    cipher::CHACHA20_POLY1305,
    cipher::AES_256_GCM,
    cipher::AES_128_CTR,
    cipher::AES_256_CTR,
];

#[derive(Clone, Debug)]
pub struct Hop {
    pub host: String,
    pub port: u16,
    pub username: String,
}

/// Fully resolved: ~/.ssh/config is read on the Python side, as russh can't.
#[derive(Clone, Debug)]
pub struct Params {
    pub hops: Vec<Hop>, // ProxyJump chain, then the target last
    pub identity_files: Vec<String>,
    pub password: Option<String>,
    pub passphrase: Option<String>,
    pub known_hosts: PathBuf,
    pub keepalive_interval: u64,
    pub reconnect: bool,
}

pub struct Client {
    host: String,
    port: u16,
    known_hosts: PathBuf,
    verdict: Arc<Mutex<Option<RvError>>>,
    lost: mpsc::UnboundedSender<()>,
}

impl Client {
    fn check(&self, key: &PublicKey) -> bool {
        let verdict = match keys::check_known_hosts_path(&self.host, self.port, key, &self.known_hosts) {
            Ok(true) => return true,
            Ok(false) => {
                let line = key.to_openssh().unwrap_or_default();
                let name = if self.port == 22 {
                    self.host.clone()
                } else {
                    format!("[{}]:{}", self.host, self.port)
                };
                RvError::HostKeyUnknown {
                    host: self.host.clone(),
                    port: self.port,
                    fingerprint: key.fingerprint(HashAlg::Sha256).to_string(),
                    key_line: format!("{name} {}\n", line.trim()),
                }
            }
            Err(error) => RvError::HostKeyChanged(error.to_string()),
        };
        *self.verdict.lock().unwrap() = Some(verdict);
        false
    }
}

impl client::Handler for Client {
    type Error = russh::Error;

    async fn check_server_key(&mut self, key: &PublicKeyOrCertificate) -> std::result::Result<bool, Self::Error> {
        Ok(match key {
            PublicKeyOrCertificate::PublicKey { key, .. } => self.check(key),
            PublicKeyOrCertificate::Certificate(_) => {
                *self.verdict.lock().unwrap() =
                    Some(RvError::HostKeyChanged("host certificates are not supported".into()));
                false
            }
        })
    }

    async fn disconnected(
        &mut self,
        _reason: client::DisconnectReason<Self::Error>,
    ) -> std::result::Result<(), Self::Error> {
        let _ = self.lost.send(());
        Ok(())
    }
}

pub struct Conn {
    pub handle: Handle<Client>,
    pub sftp: Arc<Sftp>,
    // Jump hosts stay open for as long as the tunnel through them.
    _jumps: Vec<Handle<Client>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Disconnected,
    Connecting,
    Connected,
    Reconnecting,
}

pub type StateCallback = Box<dyn Fn(State, String) + Send + Sync>;
pub type ReadyCallback = Box<dyn Fn(bool) + Send + Sync>;
pub type LostCallback = Box<dyn Fn() + Send + Sync>;

pub struct Session {
    conn: RwLock<Option<Arc<Conn>>>,
    ready: watch::Sender<bool>,
    params: Mutex<Option<Params>>,
    closing: AtomicBool,
    monitor: Mutex<Option<tokio::task::AbortHandle>>,
    pub on_state: OnceLock<StateCallback>,
    pub on_ready: OnceLock<ReadyCallback>,
    pub on_lost: OnceLock<LostCallback>,
}

fn config(params: &Params) -> Arc<client::Config> {
    Arc::new(client::Config {
        keepalive_interval: Some(Duration::from_secs(params.keepalive_interval.max(1))),
        keepalive_max: 3,
        nodelay: true,
        window_size: 16 * 1024 * 1024,
        preferred: Preferred {
            cipher: Cow::Borrowed(CIPHERS),
            compression: Cow::Borrowed(&[compression::NONE]),
            ..Preferred::default()
        },
        ..client::Config::default()
    })
}

async fn authenticate(handle: &mut Handle<Client>, hop: &Hop, params: &Params) -> Result<()> {
    let hash = handle.best_supported_rsa_hash().await?.flatten();
    let mut encrypted = false;
    for path in &params.identity_files {
        match keys::load_secret_key(path, params.passphrase.as_deref()) {
            Ok(key) => {
                let key = PrivateKeyWithHashAlg::new(Arc::new(key), hash);
                if handle.authenticate_publickey(&hop.username, key).await?.success() {
                    return Ok(());
                }
            }
            Err(keys::Error::KeyIsEncrypted) => encrypted = true,
            Err(_) => {} // unreadable or absent: the next identity may work
        }
    }
    if let Ok(mut agent) = AgentClient::connect_env().await {
        for identity in agent.request_identities().await.unwrap_or_default() {
            let key = identity.public_key().into_owned();
            let result = handle.authenticate_publickey_with(&hop.username, key, hash, &mut agent).await;
            if matches!(result, Ok(r) if r.success()) {
                return Ok(());
            }
        }
    }
    if let Some(password) = &params.password {
        if handle.authenticate_password(&hop.username, password).await?.success() {
            return Ok(());
        }
    }
    Err(RvError::Auth(if encrypted { "key needs a passphrase".into() } else { "no method succeeded".into() }))
}

async fn open(params: &Params, lost: mpsc::UnboundedSender<()>) -> Result<Conn> {
    let config = config(params);
    let mut jumps: Vec<Handle<Client>> = Vec::new();
    let mut last = None;
    for (position, hop) in params.hops.iter().enumerate() {
        let verdict = Arc::new(Mutex::new(None));
        let handler = Client {
            host: hop.host.clone(),
            port: hop.port,
            known_hosts: params.known_hosts.clone(),
            verdict: verdict.clone(),
            lost: lost.clone(),
        };
        let connected = match jumps.last() {
            None => client::connect(config.clone(), (hop.host.as_str(), hop.port), handler).await,
            Some(jump) => {
                let tunnel = jump
                    .channel_open_direct_tcpip(hop.host.clone(), hop.port as u32, "127.0.0.1", 0)
                    .await?;
                client::connect_stream(config.clone(), tunnel.into_stream(), handler).await
            }
        };
        let mut handle = match connected {
            Ok(handle) => handle,
            Err(error) => {
                let verdict = verdict.lock().unwrap().take();
                return Err(verdict.unwrap_or_else(|| match error {
                    russh::Error::IO(io) => RvError::Io(io.to_string()),
                    other => RvError::Io(other.to_string()),
                }));
            }
        };
        authenticate(&mut handle, hop, params).await?;
        if position + 1 == params.hops.len() {
            last = Some(handle);
        } else {
            jumps.push(handle);
        }
    }
    let handle = last.ok_or_else(|| RvError::Io("no host to connect to".into()))?;
    let channel = handle.channel_open_session().await?;
    let sftp = Sftp::start(channel).await?;
    Ok(Conn { handle, sftp, _jumps: jumps })
}

impl Session {
    pub fn new() -> Arc<Session> {
        Arc::new(Session {
            conn: RwLock::new(None),
            ready: watch::channel(false).0,
            params: Mutex::new(None),
            closing: AtomicBool::new(false),
            monitor: Mutex::new(None),
            on_state: OnceLock::new(),
            on_ready: OnceLock::new(),
            on_lost: OnceLock::new(),
        })
    }

    fn state(&self, state: State, detail: impl Into<String>) {
        if let Some(callback) = self.on_state.get() {
            callback(state, detail.into());
        }
    }

    fn set_conn(&self, conn: Option<Arc<Conn>>) {
        let live = conn.is_some();
        *self.conn.write().unwrap() = conn;
        self.ready.send_replace(live);
    }

    pub async fn connect(self: &Arc<Self>, params: Params) -> Result<Vec<u8>> {
        self.close().await;
        let host = params.hops.last().map(|h| h.host.clone()).unwrap_or_default();
        *self.params.lock().unwrap() = Some(params.clone());
        self.closing.store(false, Ordering::SeqCst);
        self.state(State::Connecting, host.clone());
        let (lost, lost_events) = mpsc::unbounded_channel();
        let conn = match open(&params, lost.clone()).await {
            Ok(conn) => Arc::new(conn),
            Err(error) => {
                self.state(State::Disconnected, error.to_string());
                return Err(error);
            }
        };
        self.set_conn(Some(conn.clone()));
        self.state(State::Connected, host);
        if let Some(callback) = self.on_ready.get() {
            callback(false);
        }
        let this = self.clone();
        let monitor = tokio::spawn(async move { this.monitor(lost, lost_events).await });
        *self.monitor.lock().unwrap() = Some(monitor.abort_handle());
        conn.sftp.realpath(b".").await
    }

    async fn monitor(self: Arc<Self>, lost: mpsc::UnboundedSender<()>,
                     mut lost_events: mpsc::UnboundedReceiver<()>) {
        loop {
            if lost_events.recv().await.is_none() {
                return;
            }
            while lost_events.try_recv().is_ok() {}
            self.set_conn(None);
            let params = self.params.lock().unwrap().clone();
            let Some(params) = params else { return };
            if self.closing.load(Ordering::SeqCst) || !params.reconnect {
                self.state(State::Disconnected, "connection closed");
                return;
            }
            if let Some(callback) = self.on_lost.get() {
                callback();
            }
            let host = params.hops.last().map(|h| h.host.clone()).unwrap_or_default();
            let mut delay = Duration::from_millis(250);
            loop {
                if self.closing.load(Ordering::SeqCst) {
                    return;
                }
                self.state(State::Reconnecting, host.clone());
                match open(&params, lost.clone()).await {
                    Ok(conn) => {
                        self.set_conn(Some(Arc::new(conn)));
                        break;
                    }
                    Err(_) => {
                        tokio::time::sleep(delay).await;
                        delay = (delay * 2).min(Duration::from_secs(10));
                    }
                }
            }
            self.state(State::Connected, host);
            if let Some(callback) = self.on_ready.get() {
                callback(true);
            }
        }
    }

    pub async fn close(&self) {
        self.closing.store(true, Ordering::SeqCst);
        if let Some(monitor) = self.monitor.lock().unwrap().take() {
            monitor.abort();
        }
        let conn = self.conn.read().unwrap().clone();
        self.set_conn(None);
        if let Some(conn) = conn {
            let _ = conn.handle.disconnect(russh::Disconnect::ByApplication, "", "en").await;
            self.state(State::Disconnected, "");
        }
    }

    pub fn current(&self) -> Option<Arc<Conn>> {
        self.conn.read().unwrap().clone()
    }

    pub async fn wait_ready(&self) -> Result<Arc<Conn>> {
        if let Some(conn) = self.current() {
            return Ok(conn);
        }
        let may_return = self.params.lock().unwrap().as_ref().is_some_and(|p| p.reconnect);
        if !may_return || self.closing.load(Ordering::SeqCst) {
            return Err(RvError::NotConnected);
        }
        let mut ready = self.ready.subscribe();
        let waited = tokio::time::timeout(RECONNECT_WAIT, ready.wait_for(|live| *live)).await;
        match waited.map(|outcome| outcome.is_ok()) {
            Ok(true) => self.current().ok_or(RvError::NotConnected),
            _ => Err(RvError::Lost("gave up waiting for reconnect".into())),
        }
    }

    /// Run ``operation``, once more if the connection drops under it.
    pub async fn retrying<T, F, Fut>(&self, operation: F) -> Result<T>
    where
        F: Fn(Arc<Conn>) -> Fut,
        Fut: std::future::Future<Output = Result<T>>,
    {
        let conn = self.wait_ready().await?;
        match operation(conn.clone()).await {
            Err(RvError::Lost(_)) => {
                // Give the monitor a moment to notice, so we wait on the new link.
                for _ in 0..100 {
                    if self.current().is_none_or(|now| !Arc::ptr_eq(&now, &conn)) {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                operation(self.wait_ready().await?).await
            }
            other => other,
        }
    }

    pub async fn open_exec(&self, command: &[u8]) -> Result<Channel<Msg>> {
        let conn = self.wait_ready().await?;
        let channel = conn.handle.channel_open_session().await?;
        channel.exec(true, command.to_vec()).await?;
        Ok(channel)
    }

    pub async fn run(&self, command: &[u8]) -> Result<(u32, Vec<u8>, Vec<u8>)> {
        let mut channel = self.open_exec(command).await?;
        let _ = channel.eof().await;
        let (mut stdout, mut stderr, mut status) = (Vec::new(), Vec::new(), 255);
        while let Some(message) = channel.wait().await {
            match message {
                ChannelMsg::Data { data } => stdout.extend_from_slice(&data),
                ChannelMsg::ExtendedData { data, ext: 1 } => stderr.extend_from_slice(&data),
                ChannelMsg::ExitStatus { exit_status } => status = exit_status,
                ChannelMsg::Close => break,
                _ => {}
            }
        }
        Ok((status, stdout, stderr))
    }
}
