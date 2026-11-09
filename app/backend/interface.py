"""The contract between the UI and any backend.

The UI never touches SSH. Every call here is non-blocking and safe from any
thread: work runs on the backend's own threads and comes back as a
``concurrent.futures.Future``. Callbacks fire on a backend thread, so the UI
must marshal them to Qt (see ``app/bridge.py``), and they must never block:
waiting on a Future from inside a callback deadlocks the backend.
"""
from __future__ import annotations

import enum
import stat as statmod
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


class ConnectionState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class EventKind(enum.Enum):
    CREATE = "create"
    DELETE = "delete"
    MODIFY = "modify"
    CLOSE_WRITE = "close_write"
    MOVED_FROM = "moved_from"
    MOVED_TO = "moved_to"
    # Events may have been lost (reconnect, watcher restart): re-list ``path``.
    RESYNC = "resync"


@dataclass(frozen=True)
class Event:
    kind: EventKind
    path: str
    is_dir: bool = False


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    size: int
    mtime: float
    mode: int
    is_dir: bool
    is_symlink: bool = False

    @property
    def permissions(self) -> str:
        return statmod.filemode(self.mode)


@dataclass
class ConnectParams:
    """``host`` may be an alias from ~/.ssh/config; unset fields come from there."""
    host: str
    username: Optional[str] = None
    port: Optional[int] = None
    password: Optional[str] = None
    passphrase: Optional[str] = None
    identity_files: list[str] = field(default_factory=list)
    # None means the user's ~/.ssh/known_hosts and ~/.ssh/config.
    known_hosts: Optional[str] = None
    ssh_config: Optional[str] = None
    keepalive_interval: int = 15
    reconnect: bool = True
    watcher: str = "auto"  # "auto", "inotify" or "polling"


class HostKeyUnknown(Exception):
    """The server's key is not in known_hosts; the user must decide."""

    def __init__(self, host: str, port: int, fingerprint: str, key_line: str):
        super().__init__(f"Unknown host key for {host}:{port}: {fingerprint}")
        self.host, self.port = host, port
        self.fingerprint, self.key_line = fingerprint, key_line


def split_jump_spec(spec: Optional[str]) -> list[tuple[str, Optional[int], Optional[str]]]:
    """ProxyJump value as (host, port, user) per hop."""
    hops = []
    for item in (spec or "").split(","):
        item = item.strip()
        if item and item.lower() != "none":
            user, _, rest = item.rpartition("@")
            host, _, port = rest.partition(":")
            hops.append((host, int(port) if port else None, user or None))
    return hops


class HostKeyChanged(Exception):
    """The server's key differs from known_hosts. Never offered as a prompt."""


class AuthRequired(Exception):
    """Keys and agent failed; a password or key passphrase is needed."""


class TransferCancelled(Exception):
    """A transfer stopped because of ``Transfer.cancel``."""


class SearchHandle(ABC):
    @abstractmethod
    def cancel(self) -> None: ...


class Transfer(ABC):
    """One file on its way. ``future`` resolves to None, or fails with
    TransferCancelled after ``cancel``."""
    future: Future

    @abstractmethod
    def cancel(self) -> None:
        """Stop at the next chunk. Upload temp files are removed first."""


class Shell(ABC):
    """An interactive login shell on a remote pseudo-terminal."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send keystrokes. Safe before the shell has started: they queue."""

    @abstractmethod
    def resize(self, columns: int, rows: int) -> None: ...

    @abstractmethod
    def close(self) -> None:
        """Hang up. ``on_exit`` still fires, once."""


class Process(ABC):
    """A remote command without a terminal: separate stdout and stderr."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send to its stdin. Safe before it has started: writes queue."""

    @abstractmethod
    def close(self) -> None:
        """Close stdin and the channel. ``on_exit`` still fires, once."""


class Backend(ABC):
    # Lifecycle ---------------------------------------------------------------
    @abstractmethod
    def connect(self, params: ConnectParams) -> Future:
        """Resolves to the remote home directory."""

    @abstractmethod
    def disconnect(self) -> Future: ...

    @abstractmethod
    def trust_host_key(self, err: HostKeyUnknown) -> Future:
        """Add the key from ``err`` to known_hosts."""

    @abstractmethod
    def close(self) -> None:
        """Stop for good. Blocks until backend threads have exited. Pending and
        later calls fail at once, so nothing waiting on them can hang the exit."""

    @abstractmethod
    def on_state(self, callback: Callable[[ConnectionState, str], None]) -> None: ...

    # Listings ----------------------------------------------------------------
    @abstractmethod
    def listdir(self, path: str) -> Future:
        """Resolves to list[FileEntry], folders first. Uses readdir attrs only."""

    @abstractmethod
    def stat(self, path: str, follow: bool = True) -> Future:
        """Resolves to a FileEntry."""

    @abstractmethod
    def realpath(self, path: str) -> Future: ...

    # Data --------------------------------------------------------------------
    @abstractmethod
    def read(self, path: str, offset: int, length: int) -> Future:
        """Uncached read of the current bytes. Short or empty at EOF."""

    @abstractmethod
    def fetch(self, path: str) -> Future:
        """Whole file as bytes, through the block cache."""

    @abstractmethod
    def download(self, path: str, destination: str) -> Future:
        """Copy the file to a local path through the block cache."""

    @abstractmethod
    def download_file(self, path: str, destination: str,
                      on_progress: Callable[[int], None]) -> Transfer:
        """Stream the file to a local path, past the block cache.

        ``on_progress`` gets the bytes done so far in this file, and starts
        again from 0 if a reconnect restarts it.
        """

    @abstractmethod
    def upload_file(self, source: str, path: str, mode: int,
                    on_progress: Callable[[int], None]) -> Transfer:
        """Stream a local file to ``path``, replacing it the way ``write`` does.

        ``mode`` applies to a new file; an existing one keeps its own.
        ``on_progress`` behaves as in ``download_file``.
        """

    @abstractmethod
    def mkdir(self, path: str, mode: int = 0o755) -> Future:
        """Create one folder. A folder already there counts as success."""

    @abstractmethod
    def write(self, path: str, data: bytes) -> Future:
        """Replace the file's content. Resolves to the new FileEntry.

        Goes through a temp file and an atomic rename, so a failed save leaves
        the old content. Symlinks stay symlinks, and mode is kept. Falls back to
        writing in place where a rename can't keep the file as it was: a folder
        we can't write to, or a file owned by someone else.
        """

    @abstractmethod
    def open_url(self, path: str) -> str:
        """Loopback URL that serves ``path`` with Range support."""

    @abstractmethod
    def invalidate(self, path: str) -> None:
        """Drop open handles and cached metadata for ``path``."""

    # Live updates ------------------------------------------------------------
    @abstractmethod
    def on_events(self, callback: Callable[[list[Event]], None]) -> None:
        """``callback`` gets coalesced batches."""

    @abstractmethod
    def watch(self, directories: Iterable[str], hot: Iterable[str] = ()) -> None:
        """Replace the watched set. ``hot`` paths are what the user is looking
        at; the polling fallback checks them at full rate."""

    @abstractmethod
    def watcher_mode(self) -> str:
        """"inotify", "polling" or "off"."""

    @abstractmethod
    def established_watches(self) -> set[str]:
        """Directories the running inotifywait is confirmed to be watching."""

    @abstractmethod
    def reads_issued(self) -> int:
        """Block reads sent to the server so far: tells cache hits from misses."""

    # Remote commands ---------------------------------------------------------
    @abstractmethod
    def search(self, root: str, pattern: str, on_result: Callable[[list[str]], None],
               on_done: Callable[[Optional[str]], None]) -> SearchHandle:
        """Find file names under ``root``; results stream in batches."""

    @abstractmethod
    def grep(self, path: str, needle: str, limit: int = 10000) -> Future:
        """Byte offsets of lines containing ``needle`` (fixed string, case
        insensitive) in one file, found server side."""

    @abstractmethod
    def open_shell(self, directory: Optional[str], columns: int, rows: int,
                   on_output: Callable[[bytes], None],
                   on_exit: Callable[[Optional[str]], None]) -> Shell:
        """Start the user's login shell on a pty, in ``directory`` if given.

        ``on_output`` gets raw terminal bytes in order. ``on_exit`` fires once,
        after the last output, with None when the shell ended or was closed,
        or an error message. A dropped connection ends the shell.
        """

    @abstractmethod
    def start_process(self, command: str, on_stdout: Callable[[bytes], None],
                      on_stderr: Callable[[bytes], None],
                      on_exit: Callable[[Optional[int], Optional[str]], None]) -> Process:
        """Run ``command`` through the login shell, streaming both outputs.

        ``on_exit(status, error)`` fires once, after the last output: the exit
        status when the server sent one, and an error message if the channel
        failed. Closing it, or a dropped connection, ends it.
        """
