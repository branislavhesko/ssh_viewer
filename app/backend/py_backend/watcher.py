"""Change detection: inotifywait over an exec channel, stat polling as fallback."""
from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex
import time
from typing import Callable, Optional

import asyncssh

from ..interface import Event, EventKind
from .session import Session, entry_from_attrs, supervised

log = logging.getLogger(__name__)

COALESCE_SECONDS = 0.03
RESTART_DELAY = 0.15
HOT_POLL = 0.25
COLD_POLL = 2.0
ESTABLISH_TIMEOUT = 30.0

_KINDS = {b"CREATE": EventKind.CREATE, b"DELETE": EventKind.DELETE,
          b"MODIFY": EventKind.MODIFY, b"CLOSE_WRITE": EventKind.CLOSE_WRITE,
          b"MOVED_FROM": EventKind.MOVED_FROM, b"MOVED_TO": EventKind.MOVED_TO}


def parse_event_line(line: bytes) -> list[Event]:
    flags, sep, raw_path = line.rstrip(b"\r\n").partition(b"|")
    if not sep or not raw_path:
        return []
    path = posixpath.normpath(raw_path.decode("utf-8", "surrogateescape"))
    names = flags.split(b",")
    is_dir = b"ISDIR" in names
    return [Event(_KINDS[name], path, is_dir) for name in names if name in _KINDS]


def inotify_command(directories: list[str]) -> str:
    # Only directories that still exist: one bad path would abort the watcher.
    quoted = " ".join(shlex.quote(d) for d in directories)
    return supervised(
        f'set --; for d in {quoted}; do [ -d "$d" ] && set -- "$@" "$d"; done; '
        '[ $# -gt 0 ] || { echo "Watches established." >&2; exec sleep 2147483647; }; '
        "exec inotifywait -m -e close_write,moved_to,create,delete,moved_from,modify "
        "--format '%e|%w%f' -- \"$@\"")


class Coalescer:
    """The first event after a quiet spell goes out at once, for latency; what
    follows inside the window is collapsed into one batch. Lives on the loop."""

    def __init__(self, deliver: Callable[[list[Event]], None], window: float = COALESCE_SECONDS):
        self._deliver = deliver
        self._window = window
        self._pending: dict[Event, None] = {}
        self._timer: Optional[asyncio.TimerHandle] = None

    def add(self, event: Event) -> None:
        if self._timer is not None:
            self._pending[event] = None
            return
        self._hold_window()
        self._deliver([event])

    def _hold_window(self) -> None:
        self._timer = asyncio.get_running_loop().call_later(self._window, self._flush)

    def _flush(self) -> None:
        batch, self._pending = list(self._pending), {}
        if batch:
            self._hold_window()  # still bursting
            self._deliver(batch)
        else:
            self._timer = None


class Watcher:
    def __init__(self, session: Session, deliver: Callable[[list[Event]], None]):
        self._session = session
        self._coalescer = Coalescer(deliver)
        self._dirs: set[str] = set()
        self._hot: set[str] = set()
        self._preference = "auto"
        self.mode = "off"
        self._process: Optional[asyncssh.SSHClientProcess] = None
        self.running_dirs: set[str] = set()
        self._apply_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._dirty = False
        self._seen_dirs: dict[str, dict[str, tuple]] = {}
        self._seen_files: dict[str, Optional[tuple]] = {}
        self._unsettled: set[str] = set()
        self._last_cold = 0.0

    async def start(self, preference: str, resync: bool = False) -> None:
        self._preference = preference
        mode = "polling"
        if preference != "polling":
            found = await self._session.run("sh -c 'command -v inotifywait'")
            if found.exit_status == 0:
                mode = "inotify"
            elif preference == "inotify":
                log.warning("inotifywait not found on server; using polling")
        self.mode = mode
        log.info("watcher mode: %s", self.mode)
        if self.mode == "polling":
            self._start_polling()
        self._schedule()
        if resync:
            self.resync()

    def resync(self) -> None:
        for directory in self._dirs:
            self._coalescer.add(Event(EventKind.RESYNC, directory, True))

    async def stop(self) -> None:
        for task in (self._apply_task, self._poll_task):
            if task:
                task.cancel()
        self._apply_task = self._poll_task = None
        self._kill(self._process)
        self._process = None
        self.running_dirs = set()
        self.mode = "off"

    def set_watched(self, directories: set[str], hot: set[str]) -> None:
        changed = directories != self._dirs
        self._dirs, self._hot = directories, hot
        if changed and self.mode == "inotify":
            self._schedule()

    # inotify -----------------------------------------------------------------
    def _schedule(self) -> None:
        self._dirty = True
        if self.mode == "inotify" and (self._apply_task is None or self._apply_task.done()):
            self._apply_task = asyncio.ensure_future(self._apply())

    async def _apply(self) -> None:
        while self._dirty:
            self._dirty = False
            await asyncio.sleep(RESTART_DELAY)  # expanding several folders = one restart
            if self._dirs == self.running_dirs:
                continue
            try:
                await self._replace_process(sorted(self._dirs))
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                log.warning("inotifywait failed (%s); using polling", exc)
                self._fall_back()
                return

    async def _replace_process(self, directories: list[str]) -> None:
        """Start the new watcher before stopping the old, so nothing is missed."""
        old = self._process
        if not directories:
            self._process, self.running_dirs = None, set()
            self._kill(old)
            return
        process = await self._session.conn.create_process(
            inotify_command(directories), encoding=None)
        established = asyncio.get_running_loop().create_future()
        asyncio.ensure_future(self._read_stderr(process, established))
        asyncio.ensure_future(self._read_events(process))
        try:
            await asyncio.wait_for(established, ESTABLISH_TIMEOUT)
        except BaseException:
            self._kill(process)
            raise
        self._process, self.running_dirs = process, set(directories)
        self._kill(old)

    @staticmethod
    def _kill(process: Optional[asyncssh.SSHClientProcess]) -> None:
        if process is not None:
            process.close()  # its stdin hits EOF, and supervised() does the rest

    async def _read_stderr(self, process, established: asyncio.Future) -> None:
        try:
            async for line in process.stderr:
                if b"Watches established" in line and not established.done():
                    established.set_result(True)
                elif b"upper limit" in line or b"No space left" in line or b"Couldn't" in line:
                    log.warning("inotifywait: %s", line.decode("utf-8", "replace").strip())
                    if not established.done():
                        established.set_exception(OSError("inotify watch limit reached"))
        except (asyncssh.Error, OSError):
            pass
        if not established.done():
            established.set_exception(OSError("inotifywait exited"))

    async def _read_events(self, process) -> None:
        try:
            async for line in process.stdout:
                for event in parse_event_line(line):
                    self._coalescer.add(event)
        except (asyncssh.Error, OSError):
            pass
        # Replaced on purpose, or the connection went (the session restarts us).
        if process is self._process and self.mode == "inotify":
            log.warning("inotifywait stopped unexpectedly; using polling")
            self._process = None
            self._fall_back()

    def _fall_back(self) -> None:
        self.mode = "polling"
        self._kill(self._process)
        self._process = None
        self.running_dirs = set()
        self._start_polling()
        self.resync()

    # Polling -----------------------------------------------------------------
    def _start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        delay = HOT_POLL
        while True:
            started = time.monotonic()
            try:
                await self._poll_once(started)
            except (asyncssh.Error, OSError) as exc:
                log.debug("poll failed: %s", exc)
            # A huge directory must not turn polling into a constant readdir.
            spent = time.monotonic() - started
            delay = max(HOT_POLL, spent * 10)
            await asyncio.sleep(delay)

    async def _poll_once(self, now: float) -> None:
        directories = {d for d in self._hot if d in self._dirs}
        if now - self._last_cold >= COLD_POLL:
            self._last_cold = now
            directories = set(self._dirs)
        files = self._hot - self._dirs
        self._seen_dirs = {d: v for d, v in self._seen_dirs.items() if d in self._dirs}
        self._seen_files = {f: v for f, v in self._seen_files.items() if f in files}
        await asyncio.gather(*(self._poll_file(f) for f in files),
                             *(self._poll_dir(d) for d in directories))

    async def _poll_file(self, path: str) -> None:
        try:
            attrs = await self._session.sftp.stat(path)
            state = (attrs.size, attrs.mtime)
        except asyncssh.SFTPNoSuchFile:
            state = None
        if path not in self._seen_files:
            self._seen_files[path] = state
            return
        previous = self._seen_files[path]
        self._seen_files[path] = state
        self._diff(path, previous, state, False)

    async def _poll_dir(self, directory: str) -> None:
        try:
            names = await self._session.sftp.readdir(directory)
        except asyncssh.SFTPNoSuchFile:
            self._seen_dirs.pop(directory, None)
            return
        current = {}
        for name in names:
            if name.filename not in (".", ".."):
                entry = entry_from_attrs(name.filename, "", name.attrs)
                current[name.filename] = (entry.size, entry.mtime, entry.is_dir)
        previous = self._seen_dirs.get(directory)
        self._seen_dirs[directory] = current
        if previous is None:
            return
        for name in previous.keys() | current.keys():
            path = posixpath.join(directory, name)
            if path in self._hot:
                continue  # _poll_file reports this one at full rate
            before, after = previous.get(name), current.get(name)
            is_dir = (after or before)[2]
            self._diff(path, before, after, is_dir)

    def _diff(self, path: str, before: Optional[tuple], after: Optional[tuple],
              is_dir: bool) -> None:
        if before is None and after is not None:
            self._coalescer.add(Event(EventKind.CREATE, path, is_dir))
            self._unsettled.add(path)
        elif after is None and before is not None:
            self._coalescer.add(Event(EventKind.DELETE, path, is_dir))
            self._unsettled.discard(path)
        elif before != after:
            self._coalescer.add(Event(EventKind.MODIFY, path, is_dir))
            self._unsettled.add(path)
        elif path in self._unsettled:
            # Unchanged since the last look: as close to close_write as stat gets.
            self._unsettled.discard(path)
            self._coalescer.add(Event(EventKind.CLOSE_WRITE, path, is_dir))
