"""asyncssh implementation of Backend, on its own thread and event loop."""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shlex
import threading
import time
from concurrent.futures import Future
from typing import Callable, Coroutine, Iterable, Optional

import asyncssh

from app.cache import BLOCK_SIZE, BlockCache

from ..interface import (Backend, ConnectParams, ConnectionState, Event, EventKind,
                         FileEntry, HostKeyUnknown, SearchHandle, Shell, Transfer,
                         TransferCancelled)
from .range_server import RangeServer
from .scheduler import FetchScheduler, FileKey
from .session import Session, entry_from_attrs, supervised
from .watcher import Watcher

log = logging.getLogger(__name__)

STAT_TTL = 1.0
SEARCH_BATCH_SECONDS = 0.05


class _Search(SearchHandle):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.task: Optional[asyncio.Task] = None

    def cancel(self) -> None:
        if self.task:
            self._loop.call_soon_threadsafe(self.task.cancel)


class _Transfer(Transfer):
    def __init__(self, on_progress: Callable[[int], None]):
        self._on_progress = on_progress
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def step(self, done: int) -> None:
        if self._cancelled:
            raise TransferCancelled()
        self._on_progress(done)


class _Shell(Shell):
    """Input goes through a queue, so writes before the channel opens wait."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.inbox: asyncio.Queue = asyncio.Queue()

    def _post(self, item: tuple) -> None:
        try:
            self._loop.call_soon_threadsafe(self.inbox.put_nowait, item)
        except RuntimeError:  # the loop has stopped: the shell is gone anyway
            pass

    def write(self, data: bytes) -> None:
        self._post(("data", data))

    def resize(self, columns: int, rows: int) -> None:
        self._post(("resize", (columns, rows)))

    def close(self) -> None:
        self._post(("close", None))


TERM = "xterm-256color"


def shell_command(directory: Optional[str]) -> Optional[str]:
    """None asks for the plain login shell. Otherwise a command that starts it
    in ``directory``, through sh because the login shell may not be POSIX."""
    if not directory:
        return None
    script = 'cd "$1" 2>/dev/null; exec "${SHELL:-/bin/sh}" -l'
    return f"sh -c {shlex.quote(script)} sh {shlex.quote(directory)}"


def shell_environment() -> dict[str, str]:
    """Locale for the remote shell, sent as OpenSSH's SendEnv does. Servers
    only take it if AcceptEnv allows (Debian and Ubuntu do by default)."""
    names = ("LANG", "LC_ALL", "LC_CTYPE")
    found = {name: os.environ[name] for name in names if os.environ.get(name)}
    return found or {"LANG": "C.UTF-8"}


def sort_entries(entries: list[FileEntry]) -> list[FileEntry]:
    entries.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
    return entries


def search_command(root: str, pattern: str) -> str:
    glob = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    root_q, glob_q = shlex.quote(root), shlex.quote(glob)
    return supervised(
        f"if command -v fd >/dev/null 2>&1; then fd -H -I -i -g -a -- {glob_q} {root_q}; "
        f"else find {root_q} -iname {glob_q} 2>/dev/null; fi")


class PyBackend(Backend):
    def __init__(self, cache_bytes: int = 512 * 1024 * 1024):
        self._state_callback: Callable[[ConnectionState, str], None] = lambda *_: None
        self._event_callback: Callable[[list[Event]], None] = lambda _: None
        self._generations: dict[str, int] = {}
        self._stats: dict[str, tuple[float, FileKey]] = {}
        self._params: Optional[ConnectParams] = None
        self._closed = False
        self._pending: set[Future] = set()

        self._loop = asyncio.new_event_loop()
        started = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, args=(started,),
                                        name="backend", daemon=True)
        self._thread.start()
        started.wait()

        self._session = Session(self._emit_state, self._session_ready, self._session_lost)
        self.cache = BlockCache(cache_bytes)
        self._scheduler: FetchScheduler = None
        self._watcher: Watcher = None
        self._range_server: RangeServer = None
        self._submit(self._setup()).result()

    def _run_loop(self, started: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(started.set)
        self._loop.run_forever()

    def _submit(self, coroutine: Coroutine) -> Future:
        if self._closed:
            coroutine.close()
            future: Future = Future()
            future.set_exception(ConnectionError("backend is closed"))
            return future
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)
        return future

    async def _setup(self) -> None:
        self._scheduler = FetchScheduler(self.cache, self._session.read)
        self._watcher = Watcher(self._session, self._deliver_events)
        self._range_server = RangeServer(self._scheduler, self._file_key)
        await self._range_server.start()

    # Lifecycle ---------------------------------------------------------------
    def on_state(self, callback) -> None:
        self._state_callback = callback

    def on_events(self, callback) -> None:
        self._event_callback = callback

    def _emit_state(self, state: ConnectionState, detail: str) -> None:
        self._state_callback(state, detail)

    def _session_ready(self, reconnected: bool) -> None:
        self._stats.clear()
        asyncio.ensure_future(self._watcher.start(self._params.watcher, resync=reconnected))

    def _session_lost(self) -> None:
        asyncio.ensure_future(self._watcher.stop())

    def connect(self, params: ConnectParams) -> Future:
        async def run() -> str:
            await self._watcher.stop()
            # Blocks are keyed by path; another host has other files there.
            if self._params is None or self._params.host != params.host \
                    or self._params.port != params.port:
                self.cache.clear()
            self._stats.clear()
            self._params = params
            return await self._session.connect(params)
        return self._submit(run())

    def disconnect(self) -> Future:
        async def run():
            await self._watcher.stop()
            await self._session.close()
        return self._submit(run())

    def trust_host_key(self, err: HostKeyUnknown) -> Future:
        return self._submit(self._session.trust_host_key(err))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def run():
            await self._watcher.stop()
            await self._range_server.stop()
            await self._session.close()
        for future in list(self._pending):
            future.cancel()
        try:
            asyncio.run_coroutine_threadsafe(run(), self._loop).result(timeout=5)
        except Exception:
            log.debug("shutdown was not clean", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # Listings ----------------------------------------------------------------
    def listdir(self, path: str) -> Future:
        async def attempt():
            names = await self._session.sftp.readdir(path)
            return sort_entries([
                entry_from_attrs(n.filename, posixpath.join(path, n.filename), n.attrs)
                for n in names if n.filename not in (".", "..")])
        return self._submit(self._session.retrying(attempt))

    def stat(self, path: str, follow: bool = True) -> Future:
        return self._submit(self._stat(path, follow))

    async def _stat(self, path: str, follow: bool = True) -> FileEntry:
        async def attempt():
            sftp = self._session.sftp
            attrs = await (sftp.stat(path) if follow else sftp.lstat(path))
            return entry_from_attrs(posixpath.basename(path), path, attrs)
        return await self._session.retrying(attempt)

    def realpath(self, path: str) -> Future:
        async def attempt():
            return await self._session.sftp.realpath(path)
        return self._submit(self._session.retrying(attempt))

    # Data --------------------------------------------------------------------
    async def _file_key(self, path: str) -> FileKey:
        hit = self._stats.get(path)
        if hit and time.monotonic() - hit[0] < STAT_TTL:
            return hit[1]
        entry = await self._stat(path)
        key = (path, (entry.mtime, self._generations.get(path, 0)), entry.size)
        self._stats[path] = (time.monotonic(), key)
        return key

    def read(self, path: str, offset: int, length: int) -> Future:
        return self._submit(self._session.read(path, offset, length))

    def fetch(self, path: str) -> Future:
        async def run() -> bytes:
            self._stats.pop(path, None)
            key = await self._file_key(path)
            count = -(-key[2] // BLOCK_SIZE)
            blocks = await asyncio.gather(*(self._scheduler.get(key, i) for i in range(count)))
            return b"".join(blocks)
        return self._submit(run())

    def download(self, path: str, destination: str) -> Future:
        async def run() -> str:
            self._stats.pop(path, None)
            key = await self._file_key(path)
            count = -(-key[2] // BLOCK_SIZE)
            owner = object()
            try:
                with open(destination, "wb") as out:
                    for index in range(count):
                        self._scheduler.prefetch(owner, key, index + 1, 16)
                        out.write(await self._scheduler.get(key, index))
            finally:
                self._scheduler.cancel_prefetch(owner)
            return destination
        return self._submit(run())

    def download_file(self, path: str, destination: str, on_progress) -> Transfer:
        transfer = _Transfer(on_progress)
        transfer.future = self._submit(self._session.download(path, destination, transfer.step))
        return transfer

    def upload_file(self, source: str, path: str, mode: int, on_progress) -> Transfer:
        transfer = _Transfer(on_progress)

        async def run() -> None:
            await self._session.upload(source, path, mode, transfer.step)
            self._invalidate(path)
        transfer.future = self._submit(run())
        return transfer

    def mkdir(self, path: str, mode: int = 0o755) -> Future:
        return self._submit(self._session.mkdir(path, mode))

    def write(self, path: str, data: bytes) -> Future:
        async def run() -> FileEntry:
            attrs = await self._session.write(path, data)
            self._invalidate(path)
            return entry_from_attrs(posixpath.basename(path), path, attrs)
        return self._submit(run())

    def open_url(self, path: str) -> str:
        return self._range_server.url_for(path)

    def invalidate(self, path: str) -> None:
        self._loop.call_soon_threadsafe(self._invalidate, path)

    def _invalidate(self, path: str) -> None:
        self._generations[path] = self._generations.get(path, 0) + 1
        self._stats.pop(path, None)
        self._session.drop_handle(path)

    # Live updates ------------------------------------------------------------
    def _deliver_events(self, events: list[Event]) -> None:
        for event in events:
            if event.is_dir:
                continue
            if event.kind in (EventKind.MODIFY, EventKind.CLOSE_WRITE):
                # Same inode, so an open handle stays valid: only the identity changes.
                self._generations[event.path] = self._generations.get(event.path, 0) + 1
                self._stats.pop(event.path, None)
            elif event.kind is not EventKind.RESYNC:
                self._invalidate(event.path)
        self._event_callback(events)

    def watch(self, directories: Iterable[str], hot: Iterable[str] = ()) -> None:
        self._loop.call_soon_threadsafe(self._watcher.set_watched, set(directories), set(hot))

    def watcher_mode(self) -> str:
        return self._watcher.mode

    def established_watches(self) -> set[str]:
        return set(self._watcher.running_dirs)

    def reads_issued(self) -> int:
        return self._scheduler.reads_issued

    # Remote commands ---------------------------------------------------------
    def search(self, root: str, pattern: str, on_result, on_done) -> SearchHandle:
        handle = _Search(self._loop)

        async def run() -> None:
            error = None
            process = None
            try:
                await self._session._wait_ready()
                process = await self._session.conn.create_process(
                    search_command(root, pattern), encoding=None)
                batch: list[str] = []
                flushed = time.monotonic()
                async for line in process.stdout:
                    if line:  # the reader yields b"" at EOF
                        batch.append(line.rstrip(b"\n").decode("utf-8", "surrogateescape"))
                    if time.monotonic() - flushed >= SEARCH_BATCH_SECONDS:
                        on_result(batch)
                        batch, flushed = [], time.monotonic()
                if batch:
                    on_result(batch)
            except asyncio.CancelledError:
                error = "cancelled"
            except (asyncssh.Error, OSError) as exc:
                error = str(exc)
            finally:
                if process is not None:
                    process.close()
            on_done(error)

        def start() -> None:
            handle.task = self._loop.create_task(run())
        self._loop.call_soon_threadsafe(start)
        return handle

    def grep(self, path: str, needle: str, limit: int = 10000) -> Future:
        command = (f"LC_ALL=C grep -a -b -i -o -F -m {int(limit)} -- "
                   f"{shlex.quote(needle)} {shlex.quote(path)}")

        async def run() -> list[int]:
            result = await self._session.run("sh -c " + shlex.quote(command))
            if result.exit_status not in (0, 1):
                raise OSError((result.stderr or b"grep failed").decode("utf-8", "replace"))
            return [int(line.split(b":", 1)[0]) for line in result.stdout.splitlines()
                    if line[:1].isdigit()]
        return self._submit(run())

    def open_shell(self, directory, columns, rows, on_output, on_exit) -> Shell:
        shell = _Shell(self._loop)

        async def feed(process: asyncssh.SSHClientProcess) -> None:
            while True:
                kind, value = await shell.inbox.get()
                if kind == "data":
                    process.stdin.write(value)
                elif kind == "resize":
                    process.change_terminal_size(*value)
                else:
                    process.close()
                    return

        async def run() -> None:
            error = None
            process = feeder = None
            try:
                await self._session._wait_ready()
                process = await self._session.conn.create_process(
                    shell_command(directory), term_type=TERM, term_size=(columns, rows),
                    env=shell_environment(), encoding=None)
                feeder = asyncio.ensure_future(feed(process))
                while chunk := await process.stdout.read(65536):
                    on_output(chunk)
            except (asyncssh.Error, OSError) as exc:
                error = str(exc) or type(exc).__name__
            finally:
                if feeder is not None:
                    feeder.cancel()
                if process is not None:
                    process.close()
            on_exit(error)

        self._loop.call_soon_threadsafe(lambda: self._loop.create_task(run()))
        return shell
