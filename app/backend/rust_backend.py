"""Thin wrapper over rv_core, the Rust implementation of Backend.

russh can't read ~/.ssh/config, so it is resolved here and Rust gets plain
values. Everything else lives in core/.
"""
from __future__ import annotations

import os
import posixpath
import shlex
import stat as statmod
from concurrent.futures import Future
from typing import Any, Callable, Iterable, Optional

import asyncssh
import rv_core

from .interface import (AuthRequired, Backend, ConnectParams, ConnectionState, Event, EventKind,
                        FileEntry, HostKeyChanged, HostKeyUnknown, Process, SearchHandle, Shell,
                        Transfer, TransferCancelled, split_jump_spec)
from .py_backend.backend import (search_command, shell_command, shell_environment,
                                 sort_entries)

DEFAULT_IDENTITIES = ("~/.ssh/id_ed25519", "~/.ssh/id_ecdsa", "~/.ssh/id_rsa")


def encode(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


def decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def make_entry(path: str, size: int, mtime: int, mode: int) -> FileEntry:
    return FileEntry(name=posixpath.basename(path), path=path, size=size, mtime=float(mtime),
                     mode=mode, is_dir=statmod.S_ISDIR(mode), is_symlink=statmod.S_ISLNK(mode))


def to_exception(error: tuple) -> Exception:
    kind, message = error[0], error[1]
    if kind == "hostkey_unknown":
        return HostKeyUnknown(error[2], int(error[3]), error[4], error[5])
    if kind == "hostkey_changed":
        return HostKeyChanged(message)
    if kind == "auth":
        return AuthRequired(message)
    if kind == "not_found":
        return FileNotFoundError(message)
    if kind == "denied":
        return PermissionError(message)
    if kind == "lost":
        return ConnectionError(message)
    if kind == "cancelled":
        return TransferCancelled(message)
    return OSError(message)


def _resolve_host(host: str, params: ConnectParams, port: Any = (), username: Any = ()):
    config = [params.ssh_config] if params.ssh_config else ()
    return asyncssh.SSHClientConnectionOptions(host=host, port=port, username=username,
                                               config=config, client_keys=None)


def _jump_hops(spec: Optional[str], params: ConnectParams) -> list[tuple[str, int, str]]:
    hops = []
    for host, port, username in split_jump_spec(spec):
        options = _resolve_host(host, params, port or (), username or ())
        # A jump host can have its own ProxyJump.
        hops += _jump_hops(options.config.get("ProxyJump"), params)
        hops.append((options.host, options.port, options.username))
    return hops


def resolve(params: ConnectParams) -> dict:
    options = _resolve_host(params.host, params, params.port or (), params.username or ())
    identities = params.identity_files or options.config.get("IdentityFile") or DEFAULT_IDENTITIES
    configured_hosts = options.config.get("UserKnownHostsFile")
    known_hosts = params.known_hosts or (configured_hosts[0] if configured_hosts
                                         else "~/.ssh/known_hosts")
    hops = _jump_hops(options.config.get("ProxyJump"), params)
    hops.append((options.host, options.port, options.username))
    return dict(hops=hops, identity_files=[os.path.expanduser(str(i)) for i in identities],
                password=params.password, passphrase=params.passphrase,
                known_hosts=os.path.expanduser(known_hosts),
                keepalive_interval=params.keepalive_interval, reconnect=params.reconnect)


class _Search(SearchHandle):
    def __init__(self, core, ident: int):
        self._core, self._ident = core, ident

    def cancel(self) -> None:
        self._core.cancel_exec(self._ident)


class _Channel(Shell, Process):
    def __init__(self, core, ident: int):
        self._core, self._ident = core, ident

    def _send(self, method: str, *args) -> None:
        try:
            getattr(self._core, method)(self._ident, *args)
        except RuntimeError:  # the core is closed: the shell is gone anyway
            pass

    def write(self, data: bytes) -> None:
        self._send("shell_write", bytes(data))

    def resize(self, columns: int, rows: int) -> None:
        self._send("shell_resize", columns, rows)

    def close(self) -> None:
        self._send("shell_close")


class _Transfer(Transfer):
    def __init__(self, core):
        self._core = core
        self._ident: Optional[int] = None

    def started(self, ident: int) -> None:
        self._ident = ident

    def cancel(self) -> None:
        if self._ident is not None:
            self._core.cancel_transfer(self._ident)


class RustBackend(Backend):
    def __init__(self, cache_bytes: int = 512 * 1024 * 1024):
        self._state_callback: Callable[[ConnectionState, str], None] = lambda *_: None
        self._event_callback: Callable[[list[Event]], None] = lambda _: None
        self._known_hosts = ""
        self._pending: set[Future] = set()
        self._core = rv_core.Core(cache_bytes, self._on_state, self._on_events)

    def _on_state(self, state: str, detail: str) -> None:
        self._state_callback(ConnectionState(state), detail)

    def _on_events(self, items: list[tuple[str, bytes, bool]]) -> None:
        self._event_callback([Event(EventKind(kind), decode(path), is_dir)
                              for kind, path, is_dir in items])

    def _call(self, start: Callable[[Callable], None],
              convert: Callable[[Any], Any] = lambda value: value) -> Future:
        future: Future = Future()

        def done(ok: bool, value: Any) -> None:
            self._pending.discard(future)
            if future.done():
                return  # failed by close()
            try:
                if ok:
                    future.set_result(convert(value))
                else:
                    future.set_exception(to_exception(value))
            except Exception as exc:
                future.set_exception(exc)
        self._pending.add(future)
        try:
            start(done)
        except RuntimeError as exc:  # the core is closed
            self._pending.discard(future)
            future.set_exception(ConnectionError(str(exc)))
        return future

    # Lifecycle ---------------------------------------------------------------
    def on_state(self, callback) -> None:
        self._state_callback = callback

    def on_events(self, callback) -> None:
        self._event_callback = callback

    def connect(self, params: ConnectParams) -> Future:
        resolved = resolve(params)
        self._known_hosts = resolved["known_hosts"]
        return self._call(lambda done: self._core.connect(resolved, params.watcher, done), decode)

    def disconnect(self) -> Future:
        return self._call(self._core.disconnect)

    def trust_host_key(self, err: HostKeyUnknown) -> Future:
        future: Future = Future()
        try:
            os.makedirs(os.path.dirname(self._known_hosts), mode=0o700, exist_ok=True)
            with open(self._known_hosts, "a", encoding="utf-8") as out:
                out.write(err.key_line)
            future.set_result(None)
        except OSError as exc:
            future.set_exception(exc)
        return future

    def close(self) -> None:
        self._core.close()
        for future in list(self._pending):
            if not future.done():
                future.set_exception(ConnectionError("backend is closed"))
        self._pending.clear()

    # Listings ----------------------------------------------------------------
    def listdir(self, path: str) -> Future:
        def convert(items: list) -> list[FileEntry]:
            return sort_entries([make_entry(posixpath.join(path, decode(name)), size, mtime, mode)
                                 for name, size, mtime, mode in items])
        return self._call(lambda done: self._core.listdir(encode(path), done), convert)

    def stat(self, path: str, follow: bool = True) -> Future:
        return self._call(lambda done: self._core.stat(encode(path), follow, done),
                          lambda item: make_entry(path, item[1], item[2], item[3]))

    def realpath(self, path: str) -> Future:
        return self._call(lambda done: self._core.realpath(encode(path), done), decode)

    # Data --------------------------------------------------------------------
    def read(self, path: str, offset: int, length: int) -> Future:
        return self._call(lambda done: self._core.read(encode(path), offset, length, done))

    def fetch(self, path: str) -> Future:
        return self._call(lambda done: self._core.fetch(encode(path), done))

    def download(self, path: str, destination: str) -> Future:
        return self._call(lambda done: self._core.download(encode(path), destination, done),
                          lambda _: destination)

    def _transfer(self, start: Callable[[Callable], int]) -> Transfer:
        transfer = _Transfer(self._core)
        transfer.future = self._call(lambda done: transfer.started(start(done)))
        return transfer

    def download_file(self, path: str, destination: str, on_progress) -> Transfer:
        return self._transfer(lambda done: self._core.download_file(
            encode(path), destination, on_progress, done))

    def upload_file(self, source: str, path: str, mode: int, on_progress) -> Transfer:
        return self._transfer(lambda done: self._core.upload_file(
            source, encode(path), mode, on_progress, done))

    def mkdir(self, path: str, mode: int = 0o755) -> Future:
        return self._call(lambda done: self._core.mkdir(encode(path), mode, done))

    def write(self, path: str, data: bytes) -> Future:
        return self._call(lambda done: self._core.write(encode(path), data, done),
                          lambda item: make_entry(path, item[1], item[2], item[3]))

    def open_url(self, path: str) -> str:
        return self._core.open_url(encode(path))

    def invalidate(self, path: str) -> None:
        self._core.invalidate(encode(path))

    # Live updates ------------------------------------------------------------
    def watch(self, directories: Iterable[str], hot: Iterable[str] = ()) -> None:
        self._core.watch([encode(d) for d in directories], [encode(h) for h in hot])

    def watcher_mode(self) -> str:
        return self._core.watcher_mode()

    def established_watches(self) -> set[str]:
        return {decode(path) for path in self._core.established_watches()}

    def reads_issued(self) -> int:
        return self._core.reads_issued()

    # Remote commands ---------------------------------------------------------
    def search(self, root: str, pattern: str, on_result, on_done) -> SearchHandle:
        ident = self._core.exec_lines(
            encode(search_command(root, pattern)),
            lambda lines: on_result([decode(line) for line in lines]), on_done)
        return _Search(self._core, ident)

    def grep(self, path: str, needle: str, limit: int = 10000) -> Future:
        command = (f"LC_ALL=C grep -a -b -i -o -F -m {int(limit)} -- "
                   f"{shlex.quote(needle)} {shlex.quote(path)}")

        def convert(result: tuple[int, bytes, bytes]) -> list[int]:
            status, stdout, stderr = result
            if status not in (0, 1):
                raise OSError((stderr or b"grep failed").decode("utf-8", "replace"))
            return [int(line.split(b":", 1)[0]) for line in stdout.splitlines()
                    if line[:1].isdigit()]
        return self._call(lambda done: self._core.run(encode("sh -c " + shlex.quote(command)), done),
                          convert)

    def open_shell(self, directory, columns, rows, on_output, on_exit) -> Shell:
        command = shell_command(directory)
        try:
            ident = self._core.open_shell(None if command is None else encode(command),
                                          list(shell_environment().items()),
                                          columns, rows, on_output,
                                          lambda _status, error: on_exit(error))
        except RuntimeError as exc:  # the core is closed
            on_exit(str(exc))
            ident = 0
        return _Channel(self._core, ident)

    def start_process(self, command, on_stdout, on_stderr, on_exit) -> Process:
        try:
            ident = self._core.start_process(encode(command), on_stdout, on_stderr, on_exit)
        except RuntimeError as exc:  # the core is closed
            on_exit(None, str(exc))
            ident = 0
        return _Channel(self._core, ident)
