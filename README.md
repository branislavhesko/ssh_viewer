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
