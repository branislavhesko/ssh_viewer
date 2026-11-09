# Remote Viewer

Desktop app for browsing an SSH server's files and watching images, logs, text,
PDFs and video update live, over one SSH connection.

```
remote-viewer                       # pick a host in the window
remote-viewer myserver /var/log     # alias from ~/.ssh/config, or user@host
remote-viewer --backend python ...  # force a backend (default: auto)
```

## Setup

```
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e '.[dev]'

# Optional: the Rust core, about 3.7x the throughput. `auto` picks it up.
# Unset CONDA_PREFIX first if a conda env is active, or it installs there.
cd core && env -u CONDA_PREFIX VIRTUAL_ENV=$PWD/../.venv ../.venv/bin/maturin develop --release
```

On the server you need sshd with SFTP. `inotify-tools` is optional: without it,
changes are found by polling.

The theme follows the system by default; Settings can force light or dark.

## Layout

- `app/backend/interface.py`: the contract. The UI only ever talks to this.
- `app/backend/py_backend/`: asyncssh implementation, on its own thread and loop.
- `app/backend/rust_backend.py` and `core/`: Rust implementation (russh, tokio, hyper).
- `app/transfers.py`: folder uploads and downloads, over the backend's per-file calls.
- `app/terminal.py`: the Terminal tab, a login shell on a pty, emulated with pyte.
- `app/run_panel.py`, `app/python_run.py`, `app/debug_agent.py`: running and debugging
  Python on the server. The agent is sent over stdin each run; nothing is installed.
- `app/models/`, `app/viewers/`, `app/main_window.py`, `app/transfer_panel.py`: the Qt UI.
- `bench/run.py`: benchmarks. `tests/`: run against a real local sshd.

## Testing

```
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests --timeout=60
.venv/bin/python bench/run.py --backend rust
```

Tests start their own sshd as the current user on a random port, with throwaway
keys. Backend, watcher, UI and video tests run on both backends. If `inotifywait`
isn't installed, those tests skip unless `RV_TEST_INOTIFY_DIR` points to a
directory containing it.

## Behaviour worth knowing

- **Polling sees whole seconds.** SFTP v3 mtimes have 1 s resolution, so without
  inotify, a same-size rewrite inside the same second as the last poll goes
  unnoticed until the next change. inotify has no such gap.
- **Logs have no line numbers.** Numbering a remote 1 GB log would mean
  downloading it, so positions are byte offsets, as in `less`.
- **In-file search is ASCII case-insensitive.** It runs `grep` on the server.
- **Watches are per folder**, covering expanded and selected folders only, to
  stay inside `fs.inotify.max_user_watches`.
- **Syntax colours** cover files up to 2 MB; larger source stays plain. After an
  edit, lines keep their old colours only where the text still matches.
- **Markdown** up to 2 MB is rendered; larger files open as text. Images in it
  are not loaded, and clicks only follow http(s) links.
- **CSV** shows the first 16 MB, since a row count would mean reading it all, and
  says so when a file is cut off. Sorting applies to what's loaded.
- **Parquet** loads only the footer and the row groups you scroll to. A row group
  is the smallest readable unit, so very large ones (over 1 GB) are refused.
  Parquet can't be sorted, and a file being written appears once it is closed.
- **Editing** covers UTF-8 text up to 8 MB. Saves go through a temp file and an
  atomic rename, except in a folder we can't write to or for a file owned by
  someone else: those are written in place, so a dropped connection there can
  leave a partial file. The server copy is compared before saving. Files that
  wouldn't survive the editor unchanged (not UTF-8, mixed line endings) open
  read-only.
- **Transfers** take files and folders both ways: the Upload and Download
  buttons, right-click in the list or tree, or dropping onto the window. Existing
  folders merge, and existing files prompt once per transfer. Files arrive through
  a temp file, so a failure leaves any old copy alone. Uploads replace files as
  saves do, with the same in-place exceptions, and there a failure or Cancel can
  leave a partial file. A dropped connection restarts the file once, and can
  leave a hidden `.rv-*.tmp` on the server. Permissions are kept, within the
  local umask; modified times are not. Links to files are copied as files; links
  to folders and special files are skipped and listed.
- **Host keys:** unknown keys prompt; changed keys are refused with no override.
- **The terminal** is your login shell on the server, over the same connection.
  It starts in the folder the file list shows; "Open in terminal" on a folder
  `cd`s there. Copy and paste are Ctrl+Shift+C / Ctrl+Shift+V (Cmd on macOS),
  Shift+PgUp/PgDn scrolls back. It sends `LANG` like `ssh` does, so UTF-8 needs
  the server's `AcceptEnv` to allow it. A dropped connection ends the shell;
  press Enter for a new one.
- **The dock icon on Linux** comes from `~/.local/share/applications/remote-viewer.desktop`
  and its icon, which the app writes at startup: GNOME on Wayland ignores icons
  set by the window. The macOS bundle's icon is `packaging/icon.png`, rendered
  by `python -m app.icon packaging/icon.png`.
- **Running Python**: open a `.py` file, Edit, then Run (Ctrl+F5) or Debug (F5).
  It runs on the server with the interpreter set in the Python panel (per host;
  give a venv or conda path, e.g. `~/miniconda3/envs/ml/bin/python`), in the
  script's folder. Click a line number for a breakpoint; F10/F11/Shift+F11 step,
  Shift+F5 stops, taking child processes with it. Variables shows tensors,
  arrays and DataFrames by shape, dtype and device; Inspect adds statistics
  (min, max, mean, std, NaN and inf counts) and the values, and takes any
  expression or statement. An uncaught exception pauses where it was raised,
  so the state at the crash can be inspected, in Run as well as Debug. The Console
  tab is a REPL in the selected frame while paused: Enter runs (or continues an
  open block, ending at a blank line), Shift+Enter adds a line, ↑/↓ recall.
  Values are kept as `_1`, `_2`… for Inspect. Assigning to the script's own
  variables changes them (for function locals, from Python 3.13); other new
  names stay in the console. The
  debugger traces only the main thread, and slows pure-Python loops; Run
  doesn't trace. The script gets no stdin.
