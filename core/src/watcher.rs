//! Change detection: inotifywait over an exec channel, stat polling as fallback.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use russh::ChannelMsg;
use tokio::sync::{oneshot, Notify};
use tokio::task::JoinHandle;

use crate::error::{Result, RvError};
use crate::session::Session;

const COALESCE: Duration = Duration::from_millis(30);
const RESTART_DELAY: Duration = Duration::from_millis(150);
const HOT_POLL: Duration = Duration::from_millis(250);
const COLD_POLL: Duration = Duration::from_secs(2);
const ESTABLISH_TIMEOUT: Duration = Duration::from_secs(30);

const MODE_OFF: u8 = 0;
const MODE_INOTIFY: u8 = 1;
const MODE_POLLING: u8 = 2;

const S_IFMT: u32 = 0o170000;
const S_IFDIR: u32 = 0o040000;

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
pub struct Event {
    pub kind: &'static str,
    pub path: Vec<u8>,
    pub is_dir: bool,
}

pub type Deliver = Arc<dyn Fn(Vec<Event>) + Send + Sync>;

pub fn shell_quote(value: &[u8]) -> Vec<u8> {
    let mut quoted = vec![b'\''];
    for &byte in value {
        if byte == b'\'' {
            quoted.extend_from_slice(b"'\\''");
        } else {
            quoted.push(byte);
        }
    }
    quoted.push(b'\'');
    quoted
}

/// sshd does not signal a command when its channel closes, so a quiet process
/// like inotifywait would run forever: kill it when stdin hits EOF.
pub fn supervised(command: &[u8]) -> Vec<u8> {
    let mut script = b"exec 3<&0; { ".to_vec();
    script.extend_from_slice(command);
    script.extend_from_slice(
        b"\n} 3<&- & p=$!; ( cat <&3 >/dev/null 2>&1; kill $p 2>/dev/null ) >/dev/null 2>&1 & \
          w=$!; exec 3<&-; wait $p; rc=$?; kill $w 2>/dev/null; exit $rc",
    );
    let mut wrapped = b"sh -c ".to_vec();
    wrapped.extend(shell_quote(&script));
    wrapped
}

fn inotify_command(directories: &[Vec<u8>]) -> Vec<u8> {
    let mut command = b"set --; for d in".to_vec();
    for directory in directories {
        command.push(b' ');
        command.extend(shell_quote(directory));
    }
    command.extend_from_slice(
        b"; do [ -d \"$d\" ] && set -- \"$@\" \"$d\"; done; \
          [ $# -gt 0 ] || { echo \"Watches established.\" >&2; exec sleep 2147483647; }; \
          exec inotifywait -m -e close_write,moved_to,create,delete,moved_from,modify \
          --format '%e|%w%f' -- \"$@\"",
    );
    supervised(&command)
}

fn normalize(path: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(path.len());
    for &byte in path {
        if byte != b'/' || output.last() != Some(&b'/') {
            output.push(byte);
        }
    }
    output
}

fn parse_line(line: &[u8]) -> Vec<Event> {
    let Some(split) = line.iter().position(|&b| b == b'|') else { return Vec::new() };
    let (flags, path) = (&line[..split], &line[split + 1..]);
    if path.is_empty() {
        return Vec::new();
    }
    let path = normalize(path);
    let names: Vec<&[u8]> = flags.split(|&b| b == b',').collect();
    let is_dir = names.contains(&b"ISDIR".as_slice());
    names
        .iter()
        .filter_map(|name| match *name {
            b"CREATE" => Some("create"),
            b"DELETE" => Some("delete"),
            b"MODIFY" => Some("modify"),
            b"CLOSE_WRITE" => Some("close_write"),
            b"MOVED_FROM" => Some("moved_from"),
            b"MOVED_TO" => Some("moved_to"),
            _ => None,
        })
        .map(|kind| Event { kind, path: path.clone(), is_dir })
        .collect()
}

/// The first event after a quiet spell goes out at once, for latency; what
/// follows inside the window is collapsed into one batch.
struct Coalescer {
    pending: Mutex<(Vec<Event>, HashSet<Event>, bool)>,
    deliver: Deliver,
}

impl Coalescer {
    fn add(self: &Arc<Self>, event: Event) {
        let mut state = self.pending.lock().unwrap();
        if state.2 {
            if state.1.insert(event.clone()) {
                state.0.push(event);
            }
            return;
        }
        state.2 = true;
        drop(state);
        self.clone().hold_window();
        (self.deliver)(vec![event]);
    }

    fn hold_window(self: Arc<Self>) {
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(COALESCE).await;
                let batch = {
                    let mut state = self.pending.lock().unwrap();
                    state.1.clear();
                    let batch = std::mem::take(&mut state.0);
                    if batch.is_empty() {
                        state.2 = false;
                        return;
                    }
                    batch
                };
                (self.deliver)(batch);
            }
        });
    }
}

struct Process {
    stop: Option<oneshot::Sender<()>>,
}

impl Process {
    fn kill(mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
    }
}

#[derive(Default)]
struct PollState {
    dirs: HashMap<Vec<u8>, HashMap<Vec<u8>, (u64, u32, bool)>>,
    files: HashMap<Vec<u8>, Option<(u64, u32, bool)>>,
    unsettled: HashSet<Vec<u8>>,
    last_cold: Option<Instant>,
}

pub struct Watcher {
    session: Arc<Session>,
    coalescer: Arc<Coalescer>,
    mode: AtomicU8,
    dirs: Mutex<HashSet<Vec<u8>>>,
    hot: Mutex<HashSet<Vec<u8>>>,
    running_dirs: Mutex<HashSet<Vec<u8>>>,
    changed: Notify,
    tasks: Mutex<Vec<JoinHandle<()>>>,
    polling: Mutex<PollState>,
}

impl Watcher {
    pub fn new(session: Arc<Session>, deliver: Deliver) -> Arc<Watcher> {
        Arc::new(Watcher {
            session,
            coalescer: Arc::new(Coalescer { pending: Mutex::new((Vec::new(), HashSet::new(), false)), deliver }),
            mode: AtomicU8::new(MODE_OFF),
            dirs: Mutex::new(HashSet::new()),
            hot: Mutex::new(HashSet::new()),
            running_dirs: Mutex::new(HashSet::new()),
            changed: Notify::new(),
            tasks: Mutex::new(Vec::new()),
            polling: Mutex::new(PollState::default()),
        })
    }

    pub fn mode(&self) -> &'static str {
        match self.mode.load(Ordering::SeqCst) {
            MODE_INOTIFY => "inotify",
            MODE_POLLING => "polling",
            _ => "off",
        }
    }

    pub fn established(&self) -> Vec<Vec<u8>> {
        self.running_dirs.lock().unwrap().iter().cloned().collect()
    }

    pub async fn start(self: Arc<Self>, preference: String, resync: bool) {
        let mut mode = MODE_POLLING;
        if preference != "polling" {
            if let Ok((0, _, _)) = self.session.run(b"sh -c 'command -v inotifywait'").await {
                mode = MODE_INOTIFY;
            }
        }
        self.mode.store(mode, Ordering::SeqCst);
        let this = self.clone();
        let task = if mode == MODE_INOTIFY {
            tokio::spawn(async move { this.inotify_loop().await })
        } else {
            tokio::spawn(async move { this.poll_loop().await })
        };
        self.tasks.lock().unwrap().push(task);
        self.changed.notify_one();
        if resync {
            self.resync();
        }
    }

    pub fn stop(&self) {
        for task in self.tasks.lock().unwrap().drain(..) {
            task.abort();
        }
        self.running_dirs.lock().unwrap().clear();
        self.mode.store(MODE_OFF, Ordering::SeqCst);
    }

    pub fn resync(&self) {
        for directory in self.dirs.lock().unwrap().iter() {
            self.coalescer.add(Event { kind: "resync", path: directory.clone(), is_dir: true });
        }
    }

    pub fn set_watched(&self, directories: HashSet<Vec<u8>>, hot: HashSet<Vec<u8>>) {
        let changed = {
            let mut current = self.dirs.lock().unwrap();
            let changed = *current != directories;
            *current = directories;
            changed
        };
        *self.hot.lock().unwrap() = hot;
        if changed {
            self.changed.notify_one();
        }
    }

    // inotify -----------------------------------------------------------------
    async fn inotify_loop(self: Arc<Self>) {
        let mut current: Option<Process> = None;
        let (died_sender, mut died) = tokio::sync::mpsc::unbounded_channel::<u64>();
        let mut generation = 0u64;
        loop {
            tokio::select! {
                _ = self.changed.notified() => {}
                Some(which) = died.recv() => {
                    if which == generation {
                        self.fall_back();
                        return;
                    }
                    continue;
                }
            }
            tokio::time::sleep(RESTART_DELAY).await; // several expands = one restart
            let wanted = self.dirs.lock().unwrap().clone();
            if wanted == *self.running_dirs.lock().unwrap() {
                continue;
            }
            if wanted.is_empty() {
                if let Some(old) = current.take() {
                    old.kill();
                }
                generation += 1;
                self.running_dirs.lock().unwrap().clear();
                continue;
            }
            generation += 1;
            let mut sorted: Vec<_> = wanted.iter().cloned().collect();
            sorted.sort();
            // The new watcher starts before the old stops, so nothing is missed.
            match self.spawn_process(&sorted, generation, died_sender.clone()).await {
                Ok(process) => {
                    if let Some(old) = current.replace(process) {
                        old.kill();
                    }
                    *self.running_dirs.lock().unwrap() = wanted;
                }
                Err(_) => {
                    if let Some(old) = current.take() {
                        old.kill();
                    }
                    self.fall_back();
                    return;
                }
            }
        }
    }

    fn fall_back(self: &Arc<Self>) {
        self.running_dirs.lock().unwrap().clear();
        self.mode.store(MODE_POLLING, Ordering::SeqCst);
        let this = self.clone();
        let task = tokio::spawn(async move { this.poll_loop().await });
        self.tasks.lock().unwrap().push(task);
        self.resync();
    }

    async fn spawn_process(self: &Arc<Self>, directories: &[Vec<u8>], generation: u64,
                           died: tokio::sync::mpsc::UnboundedSender<u64>) -> Result<Process> {
        let mut channel = self.session.open_exec(&inotify_command(directories)).await?;
        let (stop, mut stopped) = oneshot::channel::<()>();
        let (established, is_established) = oneshot::channel::<bool>();
        let coalescer = self.coalescer.clone();
        tokio::spawn(async move {
            let mut established = Some(established);
            let (mut out, mut err) = (Vec::new(), Vec::new());
            loop {
                tokio::select! {
                    _ = &mut stopped => {
                        let _ = channel.eof().await;
                        let _ = channel.close().await;
                        return;
                    }
                    message = channel.wait() => match message {
                        Some(ChannelMsg::Data { data }) => {
                            out.extend_from_slice(&data);
                            while let Some(end) = out.iter().position(|&b| b == b'\n') {
                                let line: Vec<u8> = out.drain(..=end).collect();
                                for event in parse_line(&line[..line.len() - 1]) {
                                    coalescer.add(event);
                                }
                            }
                        }
                        Some(ChannelMsg::ExtendedData { data, ext: 1 }) => {
                            err.extend_from_slice(&data);
                            let text = String::from_utf8_lossy(&err);
                            if text.contains("Watches established") {
                                if let Some(sender) = established.take() {
                                    let _ = sender.send(true);
                                }
                            } else if text.contains("upper limit") || text.contains("No space left")
                                || text.contains("Couldn't") {
                                if let Some(sender) = established.take() {
                                    let _ = sender.send(false);
                                }
                            }
                        }
                        Some(ChannelMsg::Close) | None => {
                            if let Some(sender) = established.take() {
                                let _ = sender.send(false);
                            }
                            let _ = died.send(generation);
                            return;
                        }
                        _ => {}
                    }
                }
            }
        });
        let mut process = Process { stop: Some(stop) };
        match tokio::time::timeout(ESTABLISH_TIMEOUT, is_established).await {
            Ok(Ok(true)) => Ok(process),
            _ => {
                if let Some(stop) = process.stop.take() {
                    let _ = stop.send(());
                }
                Err(RvError::Io("inotifywait did not start".into()))
            }
        }
    }

    // Polling -----------------------------------------------------------------
    async fn poll_loop(self: Arc<Self>) {
        loop {
            let started = Instant::now();
            let _ = self.poll_once(started).await;
            // A huge directory must not turn polling into a constant readdir.
            tokio::time::sleep(HOT_POLL.max(started.elapsed() * 10)).await;
        }
    }

    async fn poll_once(&self, now: Instant) -> Result<()> {
        let conn = self.session.wait_ready().await?;
        let (all_dirs, hot) = (self.dirs.lock().unwrap().clone(), self.hot.lock().unwrap().clone());
        let files: Vec<_> = hot.difference(&all_dirs).cloned().collect();
        let directories: Vec<_> = {
            let mut state = self.polling.lock().unwrap();
            let cold = state.last_cold.is_none_or(|t| now.duration_since(t) >= COLD_POLL);
            if cold {
                state.last_cold = Some(now);
            }
            state.dirs.retain(|d, _| all_dirs.contains(d));
            state.files.retain(|f, _| files.contains(f));
            if cold {
                all_dirs.iter().cloned().collect()
            } else {
                hot.intersection(&all_dirs).cloned().collect()
            }
        };
        for path in files {
            let found = match conn.sftp.stat(&path, true).await {
                Ok(attrs) => Some((attrs.size, attrs.mtime, false)),
                Err(RvError::NoSuchFile(_)) => None,
                Err(error) => return Err(error),
            };
            let mut state = self.polling.lock().unwrap();
            if let Some(previous) = state.files.insert(path.clone(), found) {
                self.diff(&mut state, &path, previous, found, false);
            }
        }
        for directory in directories {
            let entries = match conn.sftp.readdir(&directory).await {
                Ok(entries) => entries,
                Err(RvError::NoSuchFile(_)) => {
                    self.polling.lock().unwrap().dirs.remove(&directory);
                    continue;
                }
                Err(error) => return Err(error),
            };
            let current: HashMap<_, _> = entries
                .into_iter()
                .map(|e| {
                    let is_dir = e.attrs.mode & S_IFMT == S_IFDIR;
                    (e.name, (e.attrs.size, e.attrs.mtime, is_dir))
                })
                .collect();
            let mut state = self.polling.lock().unwrap();
            let Some(previous) = state.dirs.insert(directory.clone(), current.clone()) else { continue };
            let names: HashSet<_> = previous.keys().chain(current.keys()).cloned().collect();
            for name in names {
                let mut path = directory.clone();
                if path.last() != Some(&b'/') {
                    path.push(b'/');
                }
                path.extend_from_slice(&name);
                if hot.contains(&path) {
                    continue; // the file poll above reports this one at full rate
                }
                let (before, after) = (previous.get(&name).copied(), current.get(&name).copied());
                let is_dir = after.or(before).is_some_and(|s| s.2);
                self.diff(&mut state, &path, before, after, is_dir);
            }
        }
        Ok(())
    }

    fn diff(&self, state: &mut PollState, path: &[u8], before: Option<(u64, u32, bool)>,
            after: Option<(u64, u32, bool)>, is_dir: bool) {
        let event = |kind| Event { kind, path: path.to_vec(), is_dir };
        match (before, after) {
            (None, Some(_)) => {
                self.coalescer.add(event("create"));
                state.unsettled.insert(path.to_vec());
            }
            (Some(_), None) => {
                self.coalescer.add(event("delete"));
                state.unsettled.remove(path);
            }
            (a, b) if a != b => {
                self.coalescer.add(event("modify"));
                state.unsettled.insert(path.to_vec());
            }
            _ => {
                // Unchanged since the last look: as close to close_write as stat gets.
                if state.unsettled.remove(path) {
                    self.coalescer.add(event("close_write"));
                }
            }
        }
    }
}
