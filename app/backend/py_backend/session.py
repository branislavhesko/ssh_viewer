"""One SSH connection: config, host keys, keepalive, reconnect, SFTP handles."""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import secrets
import shlex
import socket
import stat as statmod
from collections import OrderedDict
from dataclasses import replace
from typing import Awaitable, Callable, Optional, TypeVar

import asyncssh

from ..interface import (AuthRequired, ConnectParams, ConnectionState, FileEntry,
                         HostKeyChanged, HostKeyUnknown, split_jump_spec)

log = logging.getLogger(__name__)
T = TypeVar("T")

# AES-GCM first: hardware accelerated nearly everywhere. Compression stays off.
CIPHERS = ["aes128-gcm@openssh.com", "chacha20-poly1305@openssh.com",
           "aes256-gcm@openssh.com", "aes128-ctr", "aes256-ctr"]
SFTP_REQUEST_SIZE = 64 * 1024
SFTP_MAX_REQUESTS = 8
# Sized so a whole chunk is in flight at once on transfers.
TRANSFER_CHUNK = 4 * 1024 * 1024
TRANSFER_MAX_REQUESTS = TRANSFER_CHUNK // SFTP_REQUEST_SIZE
MAX_OPEN_HANDLES = 32
RECONNECT_WAIT = 30.0

_LOST = (asyncssh.SFTPConnectionLost, asyncssh.SFTPNoConnection,
         asyncssh.ConnectionLost, asyncssh.ChannelOpenError, ConnectionError, BrokenPipeError)


def supervised(command: str) -> str:
    """Wrap a remote command so it dies with the channel.

    sshd does not signal a command when its channel closes, so a quiet process
    like inotifywait would run forever. This kills it when stdin hits EOF. It
    goes through sh because the login shell may not be POSIX.
    """
    script = ("exec 3<&0; { " + command + "\n} 3<&- & p=$!; "
              "( cat <&3 >/dev/null 2>&1; kill $p 2>/dev/null ) >/dev/null 2>&1 & "
              "w=$!; exec 3<&-; wait $p; rc=$?; kill $w 2>/dev/null; exit $rc")
    return "sh -c " + shlex.quote(script)


def entry_from_attrs(name: str, path: str, attrs: asyncssh.SFTPAttrs) -> FileEntry:
    mode = attrs.permissions or 0
    return FileEntry(name=name, path=path, size=attrs.size or 0,
                     mtime=float(attrs.mtime or 0), mode=mode,
                     is_dir=statmod.S_ISDIR(mode), is_symlink=statmod.S_ISLNK(mode))


class Session:
    """Lives on the backend's event loop."""

    def __init__(self, on_state: Callable[[ConnectionState, str], None],
                 on_ready: Callable[[bool], None], on_lost: Callable[[], None]):
        self._on_state = on_state
        self._on_ready = on_ready
        self._on_lost = on_lost
        self._params: Optional[ConnectParams] = None
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._sftp: Optional[asyncssh.SFTPClient] = None
        self._ready = asyncio.Event()
        self._closing = False
        self._monitor: Optional[asyncio.Task] = None
        self._handles: OrderedDict[str, asyncssh.SFTPClientFile] = OrderedDict()
        self._opening: dict[str, asyncio.Task] = {}

    # Connecting --------------------------------------------------------------
    def _options(self, params: ConnectParams) -> asyncssh.SSHClientConnectionOptions:
        kwargs: dict = dict(keepalive_interval=params.keepalive_interval,
                            keepalive_count_max=3, compression_algs=None,
                            encryption_algs=CIPHERS)
        if params.username:
            kwargs["username"] = params.username
        if params.port:
            kwargs["port"] = params.port
        if params.password:
            kwargs["password"] = params.password
        if params.passphrase:
            kwargs["passphrase"] = params.passphrase
        if params.identity_files:
            kwargs["client_keys"] = params.identity_files
        if params.known_hosts:
            # An absent file means "no host known yet", which asyncssh only
            # accepts for its default path.
            exists = os.path.exists(params.known_hosts)
            kwargs["known_hosts"] = params.known_hosts if exists else b""
        if params.ssh_config:
            kwargs["config"] = [params.ssh_config]
        return asyncssh.SSHClientConnectionOptions(host=params.host, **kwargs)

    async def connect(self, params: ConnectParams) -> str:
        await self.close()  # one connection at a time
        self._params = params
        self._closing = False
        self._on_state(ConnectionState.CONNECTING, params.host)
        try:
            await self._open(params)
        except BaseException as exc:
            self._on_state(ConnectionState.DISCONNECTED, str(exc))
            raise
        self._on_state(ConnectionState.CONNECTED, params.host)
        self._on_ready(False)
        self._monitor = asyncio.ensure_future(self._watch_connection())
        return await self._sftp.realpath(".")

    async def _open(self, params: ConnectParams) -> None:
        options = self._options(params)
        tunnel = await self._open_chain(params, options)
        conn = await self._connect_one(params, options, tunnel)
        self._conn = conn
        self._sftp = await conn.start_sftp_client(path_errors="surrogateescape")
        self._ready.set()

    async def _open_chain(self, params: ConnectParams,
                          options: asyncssh.SSHClientConnectionOptions
                          ) -> Optional[asyncssh.SSHClientConnection]:
        """Hops are connected here because asyncssh's own ProxyJump handling
        would not carry our known_hosts and passphrase to them."""
        conn = None
        for host, port, username in split_jump_spec(options.config.get("ProxyJump")):
            hop = replace(params, host=host, port=port, username=username)
            hop_options = self._options(hop)
            tunnel = conn or await self._open_chain(hop, hop_options)
            conn = await self._connect_one(hop, hop_options, tunnel)
        return conn

    async def _connect_one(self, params: ConnectParams,
                           options: asyncssh.SSHClientConnectionOptions,
                           tunnel: Optional[asyncssh.SSHClientConnection]
                           ) -> asyncssh.SSHClientConnection:
        try:
            conn = await asyncssh.connect(params.host, tunnel=tunnel, options=options)
        except asyncssh.HostKeyNotVerifiable as exc:
            await self._raise_if_unknown(params, options, tunnel)
            raise HostKeyChanged(str(exc)) from exc
        except (asyncssh.PermissionDenied, asyncssh.KeyEncryptionError) as exc:
            raise AuthRequired(str(exc)) from exc
        conn.set_tunnel(tunnel)  # closes with it
        return conn

    def _known_hosts_path(self, params: ConnectParams) -> str:
        configured = self._options(params).config.get("UserKnownHostsFile")
        path = params.known_hosts or (configured[0] if configured else "~/.ssh/known_hosts")
        return os.path.expanduser(path)

    async def _raise_if_unknown(self, params: ConnectParams,
                                options: asyncssh.SSHClientConnectionOptions,
                                tunnel: Optional[asyncssh.SSHClientConnection]) -> None:
        """Turn "not verifiable" into a prompt only if we have never seen this
        host. A key that *changed* stays a hard failure."""
        path = self._known_hosts_path(params)
        host, port = options.host, options.port
        try:
            addr = socket.gethostbyname(host)
        except OSError:
            addr = host
        if os.path.exists(path):
            known = asyncssh.read_known_hosts(path).match(host, addr, port)
            if any(known[:3]):
                return
        key = await asyncssh.get_server_host_key(params.host, tunnel=tunnel, options=options)
        name = host if port == 22 else f"[{host}]:{port}"
        line = f"{name} {key.export_public_key('openssh').decode().strip()}\n"
        raise HostKeyUnknown(host, port, key.get_fingerprint(), line)

    async def trust_host_key(self, err: HostKeyUnknown) -> None:
        path = self._known_hosts_path(self._params)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as out:
            out.write(err.key_line)

    async def _watch_connection(self) -> None:
        while True:
            await self._conn.wait_closed()
            self._drop()
            if self._closing or not self._params.reconnect:
                self._on_state(ConnectionState.DISCONNECTED, "connection closed")
                return
            self._on_lost()
            delay = 0.25
            while not self._closing:
                self._on_state(ConnectionState.RECONNECTING, self._params.host)
                try:
                    await self._open(self._params)
                    break
                except (OSError, asyncssh.Error, AuthRequired, HostKeyChanged) as exc:
                    log.info("reconnect failed: %s", exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
            if self._closing:
                return
            self._on_state(ConnectionState.CONNECTED, self._params.host)
            self._on_ready(True)

    def _drop(self) -> None:
        self._ready.clear()
        self._conn = self._sftp = None
        self._handles.clear()

    async def close(self) -> None:
        self._closing = True
        if self._monitor:
            self._monitor.cancel()
        conn = self._conn
        self._drop()
        if conn:
            conn.close()
            await conn.wait_closed()
        self._on_state(ConnectionState.DISCONNECTED, "")

    # Using the connection ----------------------------------------------------
    async def _wait_ready(self) -> None:
        if self._ready.is_set():
            return
        if self._params is None or self._closing or not self._params.reconnect:
            raise ConnectionError("not connected")
        try:
            await asyncio.wait_for(self._ready.wait(), RECONNECT_WAIT)
        except asyncio.TimeoutError:
            raise ConnectionError("connection lost") from None

    async def retrying(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation``, once more if the connection drops under it."""
        await self._wait_ready()
        try:
            return await operation()
        except _LOST:
            conn = self._conn
            if conn is not None and not conn.is_closed():
                raise  # e.g. MaxSessions refused a channel: the link itself is fine
            self._ready.clear()
            await self._wait_ready()
            return await operation()

    @property
    def sftp(self) -> asyncssh.SFTPClient:
        if self._sftp is None:
            raise ConnectionError("not connected")
        return self._sftp

    @property
    def conn(self) -> asyncssh.SSHClientConnection:
        if self._conn is None:
            raise ConnectionError("not connected")
        return self._conn

    async def _handle(self, path: str) -> asyncssh.SFTPClientFile:
        handle = self._handles.get(path)
        if handle is not None:
            self._handles.move_to_end(path)
            return handle
        # Single flight: concurrent block reads share one SSH_FXP_OPEN.
        opening = self._opening.get(path)
        if opening is None:
            opening = asyncio.ensure_future(self._open_handle(path))
            self._opening[path] = opening
            opening.add_done_callback(lambda _: self._opening.pop(path, None))
        return await asyncio.shield(opening)

    async def _open_handle(self, path: str) -> asyncssh.SFTPClientFile:
        handle = await self.sftp.open(path, "rb", encoding=None,
                                      block_size=SFTP_REQUEST_SIZE,
                                      max_requests=SFTP_MAX_REQUESTS)
        self._handles[path] = handle
        while len(self._handles) > MAX_OPEN_HANDLES:
            _, old = self._handles.popitem(last=False)
            asyncio.ensure_future(self._close_quietly(old))
        return handle

    @staticmethod
    async def _close_quietly(handle: asyncssh.SFTPClientFile) -> None:
        try:
            await handle.close()
        except (asyncssh.Error, OSError):
            pass

    def drop_handle(self, path: str) -> None:
        """The path may now name a different inode; a kept handle would read
        the old one."""
        handle = self._handles.pop(path, None)
        if handle is not None:
            asyncio.ensure_future(self._close_quietly(handle))

    async def read(self, path: str, offset: int, length: int) -> bytes:
        async def attempt() -> bytes:
            handle = await self._handle(path)
            return await handle.read(length, offset)
        return await self.retrying(attempt)

    async def download(self, path: str, destination: str, step: Callable[[int], None]) -> None:
        """Own handle, not the shared one: that may still point at an old inode."""
        async def attempt() -> None:
            loop = asyncio.get_running_loop()
            async with self.sftp.open(path, "rb", encoding=None, block_size=SFTP_REQUEST_SIZE,
                                      max_requests=TRANSFER_MAX_REQUESTS) as remote:
                with open(destination, "wb") as out:
                    received = 0
                    step(received)
                    while chunk := await remote.read(TRANSFER_CHUNK, received):
                        await loop.run_in_executor(None, out.write, chunk)
                        received += len(chunk)
                        step(received)
        await self.retrying(attempt)

    async def upload(self, source: str, path: str, mode: int,
                     step: Callable[[int], None]) -> None:
        async def fill(out: asyncssh.SFTPClientFile) -> None:
            loop = asyncio.get_running_loop()
            with open(source, "rb") as local:
                sent = 0
                step(sent)
                while chunk := await loop.run_in_executor(None, local.read, TRANSFER_CHUNK):
                    await out.write(chunk, sent)
                    sent += len(chunk)
                    step(sent)
        await self._replace(path, fill, mode)

    async def mkdir(self, path: str, mode: int) -> None:
        async def attempt() -> None:
            sftp = self.sftp
            try:
                await sftp.mkdir(path, asyncssh.SFTPAttrs(permissions=mode))
            except asyncssh.SFTPError:
                # SFTP v3 has no "already exists" status, so look.
                try:
                    exists = await sftp.isdir(path)
                except asyncssh.SFTPError:
                    exists = False
                if not exists:
                    raise
        await self.retrying(attempt)

    async def write(self, path: str, data: bytes) -> asyncssh.SFTPAttrs:
        async def fill(out: asyncssh.SFTPClientFile) -> None:
            await out.write(data)
        return await self._replace(path, fill, 0o644)

    async def _replace(self, path: str, fill: Callable[[asyncssh.SFTPClientFile], Awaitable[None]],
                       new_mode: int) -> asyncssh.SFTPAttrs:
        async def attempt() -> asyncssh.SFTPAttrs:
            sftp = self.sftp
            target = await sftp.realpath(path)  # keeps a symlink a symlink
            try:
                current = await sftp.stat(target)
            except asyncssh.SFTPNoSuchFile:
                current = None
            if not await self._write_atomically(target, fill, current, new_mode):
                async with sftp.open(target, "wb", encoding=None,
                                     block_size=SFTP_REQUEST_SIZE) as out:
                    await fill(out)
            self.drop_handle(path)
            self.drop_handle(target)
            return await sftp.stat(target)
        return await self.retrying(attempt)

    async def _write_atomically(self, target: str,
                                fill: Callable[[asyncssh.SFTPClientFile], Awaitable[None]],
                                current: Optional[asyncssh.SFTPAttrs], new_mode: int) -> bool:
        """False if a rename couldn't keep the file as it was."""
        sftp = self.sftp
        # Not built from the name: a long one would leave no room for a suffix.
        temp = posixpath.join(posixpath.dirname(target), f".rv-{secrets.token_hex(4)}.tmp")
        mode = statmod.S_IMODE(current.permissions) if current else new_mode
        try:
            out = await sftp.open(temp, "xb", asyncssh.SFTPAttrs(permissions=0o600), encoding=None,
                                  block_size=SFTP_REQUEST_SIZE)
        except asyncssh.SFTPPermissionDenied:
            return False
        try:
            async with out:
                mine = await out.stat()
                if current and (mine.uid, mine.gid) != (current.uid, current.gid):
                    await self._remove_quietly(temp)
                    return False
                await fill(out)
                await out.chmod(mode)
                try:
                    await out.fsync()
                except asyncssh.SFTPOpUnsupported:
                    pass
            await sftp.posix_rename(temp, target)
        except asyncssh.SFTPOpUnsupported:
            await self._remove_quietly(temp)
            return False
        except BaseException:
            await self._remove_quietly(temp)
            raise
        return True

    async def _remove_quietly(self, path: str) -> None:
        try:
            await self.sftp.remove(path)
        except (asyncssh.Error, OSError):
            pass

    async def run(self, command: str) -> asyncssh.SSHCompletedProcess:
        async def attempt():
            return await self.conn.run(command, check=False, encoding=None, stdin=asyncssh.DEVNULL)
        return await self.retrying(attempt)
