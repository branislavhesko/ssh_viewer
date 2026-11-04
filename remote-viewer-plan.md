# Remote Live Viewer — Build Plan

Desktop app (macOS, Windows, Linux) for browsing an SSH server's file tree and viewing images, logs, text, PDFs and video **live**, with low latency, over a single SSH connection.

---

## 1. Decisions

| Area | Choice | Why |
|---|---|---|
| UI | **Qt 6 via PySide6** (LGPL) | Native widgets, QtMultimedia (FFmpeg backend), QtPdf, fast to build |
| Transport | **SSH**: exec channel for events, SFTP for data | No extra ports; one authenticated connection |
| Change detection | Remote `inotifywait -m` streamed over exec channel; polling fallback | Push events instead of polling SFTP |
| Prototype backend | Python: `asyncssh` + `qasync` | Fastest path to end-to-end |
| Production backend | **Rust core** (`russh`, `russh-sftp`, `tokio`) as a PyO3 extension | Native hot path, no GIL in streaming |
| Media/data delivery to UI | **Local HTTP range server** on `127.0.0.1` | Players and viewers just open a URL; backend is swappable |
| Video | `QMediaPlayer` on an `http://127.0.0.1` URL; fallback `python-mpv` (libmpv) | FFmpeg plays nearly any codec; range requests give streaming + seeking |

**Key rule:** the UI never touches SSH directly. It talks to a `Backend` interface (events, directory listings, URLs). The Python prototype and the Rust core implement the same interface, so swapping them doesn't change the UI.

---

## 2. Architecture

```
┌──────────────────────── Desktop app ────────────────────────┐
│  PySide6 UI                                                  │
│   ├─ Tree (QTreeView + lazy QAbstractItemModel)              │
│   ├─ File list (metadata)                                    │
│   └─ Preview: Text/Log │ Image │ PDF │ Video │ QuickLook-ish │
│            ▲ events / listings        ▲ http://127.0.0.1/... │
│  ┌─────────┴──────────────────────────┴───────────────────┐  │
│  │ Backend (Python prototype → Rust core via PyO3)        │  │
│  │  ├─ Session manager (1 SSH conn, keepalive, reconnect) │  │
│  │  ├─ Watcher channel (exec: inotifywait)                │  │
│  │  ├─ SFTP pool (pipelined reads)                        │  │
│  │  ├─ Block cache (path+mtime+size → 256 KB blocks)      │  │
│  │  ├─ Fetch scheduler (priority, cancel, read-ahead)     │  │
│  │  └─ Range server (127.0.0.1:random, token-protected)   │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────┬───────────────────────────────┘
                               │ single SSH connection
┌──────────────────────────────┴───────────────────────────────┐
│ Server: sshd + sftp-server, inotify-tools                    │
│ Optional later: rv-helper (Rust static binary) for           │
│ thumbnails, PDF rasterization, ffmpeg transcode              │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Repo layout

```
remote-viewer/
├─ app/                      # Python package (PySide6 UI)
│  ├─ main.py
│  ├─ backend/
│  │  ├─ interface.py        # Backend protocol (abstract)
│  │  ├─ py_backend/         # asyncssh prototype
│  │  └─ rust_backend.py     # thin wrapper over rv_core
│  ├─ models/tree_model.py   # lazy QAbstractItemModel
│  ├─ viewers/               # text_log.py, image.py, pdf.py, video.py
│  └─ cache/
├─ core/                     # Rust crate, built with maturin → rv_core
│  ├─ src/session.rs
│  ├─ src/sftp_pool.rs
│  ├─ src/watcher.rs
│  ├─ src/cache.rs
│  ├─ src/scheduler.rs
│  ├─ src/range_server.rs    # hyper/axum, Range + HEAD support
│  └─ src/lib.rs             # PyO3 bindings
├─ helper/                   # (Phase 6) remote Rust helper binary
├─ bench/                    # latency/throughput benchmarks
├─ tests/
└─ packaging/                # PyInstaller/Nuitka specs, signing scripts
```

---

## 4. Phases

### Phase 0: Spikes and baseline (1–2 days)
- [ ] Measure RTT and raw throughput to target servers (`ssh`, `scp`, `sftp` with large file).
- [ ] Baseline video: `mpv sftp://user@host/path/video.mp4` and record time to first frame and seek latency.
- [ ] Spike `asyncssh` SFTP: sequential vs pipelined reads (16–64 outstanding requests, 32–64 KB each).
- [ ] Spike `QMediaPlayer` on a local range server serving a local MP4 file; confirm seeking issues range requests.
- [ ] Check `inotifywait` availability on target servers.

**Done when:** you have baseline numbers to beat and confirmation that QMediaPlayer streams over HTTP ranges.

### Phase 1: Connection and file tree
- [ ] `Backend` interface: `connect`, `listdir`, `stat`, `open_url(path)`, `events()` stream.
- [ ] Connection: honor `~/.ssh/config` (Host, User, Port, IdentityFile, ProxyJump), ssh-agent, `known_hosts` verification, keepalive, auto-reconnect.
- [ ] Lazy tree model: `readdir` on expand only, use returned attrs (no extra `stat`), sort folders first.
- [ ] File list pane: name, size, mtime, permissions; symlinks shown, resolved on demand.
- [ ] Remote search: `find`/`fd` over exec channel, streamed results.

**Done when:** a directory with 50k entries opens without UI freeze and expanding a folder takes ~1 RTT.

### Phase 2: Live updates and logs/text
- [ ] Watcher: `inotifywait -m -r -e close_write,moved_to,create,delete,moved_from,modify --format '%e|%w%f' <root>`.
  - Watch only the expanded subtree + the selected file's directory (inotify watch limits).
  - Debounce/coalesce bursts (e.g. 20–50 ms window).
  - Fallback: `stat` polling every 250 ms on the selected file and its directory.
- [ ] Tree patching from events (insert/remove/rename in place).
- [ ] Log viewer: track offset, SFTP-read only appended bytes on `modify`; detect truncation/rotation (size shrink → reopen).
- [ ] Virtualized text view (line index + visible-range rendering) for files of hundreds of MB.
- [ ] Follow mode, pause, search, wrap toggle; optional ANSI color.

**Done when:** a line appended remotely appears in under RTT + 50 ms, and a 1 GB log scrolls smoothly.

### Phase 3: Images and PDF
- [ ] Image viewer: decode via Pillow/tifffile + numpy → `QImage`; 8/16-bit, float, multi-page TIFF.
- [ ] Window/level (min/max, percentile auto-contrast) for 16-bit/scientific data; zoom/pan.
- [ ] Reload on `close_write`, keeping zoom/pan.
- [ ] PDF viewer: `QPdfDocument` + `QPdfView` from a fully fetched file; reload on change, restore page and zoom.
- [ ] Generic fallback: hex/text preview and "open externally" (download to cache, then OS default app).

**Done when:** a new image appears within ~RTT + transfer time after it's written, with no partial-file reads.

### Phase 4: Video streaming
- [ ] Range server: `GET`/`HEAD` with `Range`, `Content-Length`, `Accept-Ranges`; maps ranges to cache blocks → SFTP reads.
- [ ] Single-flight per block (no duplicate SFTP reads), read-ahead of N blocks after the playhead.
- [ ] Cancel pending reads when the player seeks or the file is closed.
- [ ] Bind `127.0.0.1` on a random port with a per-session token in the URL.
- [ ] `QMediaPlayer.setSource(QUrl(...))`; playback controls, seek bar, frame step if needed.
- [ ] Growing files: support fragmented MP4/MKV; plain MP4 marked "playable when finished".
- [ ] Fallback player: `python-mpv` embedded via window ID if QMediaPlayer has codec or start-latency issues (libmpv allows tuning cache/probe settings).

**Done when:** time to first frame ≤ baseline from Phase 0, seeks in a 5 GB file stay under ~300 ms on LAN, and there's no full download.

### Phase 5: Rust core (performance)
- [ ] `core/` crate with `russh` + `russh-sftp`, `tokio` runtime on its own threads.
- [ ] Port in order of impact: SFTP pool + block cache → range server → watcher → listings.
- [ ] PyO3 API mirrors the `Backend` interface; events are delivered to Qt via a thread-safe callback → `QMetaObject.invokeMethod` / signal.
- [ ] Build with `maturin`, wheels for macOS (arm64/x86_64), Windows, Linux.
- [ ] Benchmarks in `bench/`: throughput, time to first byte, time to first frame, seek latency; compare against the Python backend.

**Done when:** the Rust backend beats the Python backend on every benchmark and the UI code is unchanged.

### Phase 6 (optional): Server-side rendering helper
- [ ] `rv-helper` static Rust binary, uploaded via SFTP on connect if missing or outdated (checksum).
- [ ] Commands over exec channel: thumbnails (via vips or the `image` crate), PDF page raster (`pdftoppm`), video transcode to fragmented MP4 H.264 (`ffmpeg`, if present).
- [ ] Adaptive policy: raw fetch for small files or fast links; server render above a size threshold or on slow links.
- [ ] Optional: helper replaces `inotifywait` (no dependency on inotify-tools).

**Done when:** browsing a folder of multi-GB TIFFs over VPN feels as fast as on LAN.

### Phase 7: Packaging and distribution
- [ ] Nuitka or PyInstaller bundles per OS.
- [ ] macOS: sign all dylibs, hardened runtime, notarize; `.dmg`.
- [ ] Windows: signed installer (MSIX or Inno Setup).
- [ ] Linux: AppImage (bundle Qt FFmpeg libs; don't rely on system GStreamer).
- [ ] Settings: saved hosts, cache size limit, cipher preference, watcher mode.
- [ ] CI: build matrix + smoke test against a Docker sshd.

---

## 5. Performance targets

| Metric | Target (LAN, ~1 ms RTT) | Target (VPN, ~30 ms RTT) |
|---|---|---|
| Expand folder (≤1k entries) | < 20 ms | < 80 ms |
| Log line appears after write | < 50 ms | < 100 ms |
| New image visible (5 MB) | < 150 ms | < 1 s (bandwidth-bound) |
| Video time to first frame | < 300 ms | < 1 s |
| Video seek | < 200 ms | < 600 ms |

Levers that matter most: SFTP pipelining, a single persistent connection, prefetch on selection, `aes128-gcm` or `chacha20-poly1305`, compression off, and faststart/fragmented MP4.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| `inotifywait` missing on server | Polling fallback; Phase 6 helper ships its own watcher |
| inotify watch limit on large trees | Watch expanded/selected dirs only; document `fs.inotify.max_user_watches` |
| Reading partially written files | Trigger on `close_write`/`moved_to`; recommend write-temp-then-rename |
| QMediaPlayer exposes few FFmpeg tuning options | `python-mpv` fallback |
| `russh` doesn't fully parse `~/.ssh/config` | Parse config in Python (`paramiko.SSHConfig` or `asyncssh`) and pass resolved params to Rust |
| macOS signing of bundled Python + Qt + FFmpeg dylibs | Set up signing in CI early (Phase 1), not at the end |
| Local range server exposed to other local processes | Bind loopback only, random port, per-session token |
| Python ↔ Rust event threading | Rust never calls Qt directly; marshal to the Qt thread via signals |

---

## 7. Open questions

- Platforms: desktop only, or iOS/Android later? (Mobile would change the UI to Flutter; the Rust core stays.)
- Server OS: Linux only, or macOS/Windows servers too? (Changes the watcher.)
- Typical link: LAN, VPN, or internet?
- Typical file sizes and video formats (raw/uncompressed AVI, MJPEG, H.264)?
- Read-only viewer, or also upload, delete and rename?
- Multiple servers open at once?
