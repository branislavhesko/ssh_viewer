"""HTTP/1.1 server on loopback that maps byte ranges onto cached file blocks."""
from __future__ import annotations

import asyncio
import hmac
import logging
import mimetypes
import re
import secrets
from typing import Awaitable, Callable, Optional
from urllib.parse import quote, unquote

from app.cache import BLOCK_SIZE

from .scheduler import FetchScheduler, FileKey

log = logging.getLogger(__name__)

READ_AHEAD_BLOCKS = 8
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")

StatFile = Callable[[str], Awaitable[FileKey]]


def parse_range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """Inclusive (start, end). None for "serve it all"; raises ValueError if
    the range cannot be satisfied."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match or (not match[1] and not match[2]):
        return None  # multi-range or malformed: RFC 9110 lets us ignore it
    if not match[1]:
        length = int(match[2])
        if length == 0 or size == 0:
            raise ValueError(header)
        return max(size - length, 0), size - 1
    start = int(match[1])
    end = min(int(match[2]), size - 1) if match[2] else size - 1
    if start >= size or start > end:
        raise ValueError(header)
    return start, end


class RangeServer:
    def __init__(self, scheduler: FetchScheduler, stat_file: StatFile):
        self._scheduler = scheduler
        self._stat_file = stat_file
        self._token = secrets.token_urlsafe(24)
        self._server: Optional[asyncio.Server] = None
        self._connections: set[asyncio.Task] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connection, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for task in list(self._connections):
                task.cancel()
            await asyncio.gather(*self._connections, return_exceptions=True)
            await self._server.wait_closed()

    def url_for(self, path: str) -> str:
        quoted = quote(path, safe="/", errors="surrogateescape")
        return f"http://127.0.0.1:{self.port}/{self._token}{quoted}"

    async def _on_connection(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        owner = object()
        try:
            while await self._serve_one(reader, writer, owner):
                pass
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # the player went away: routine on seek and on close
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("range server connection failed")
        finally:
            self._scheduler.cancel_prefetch(owner)
            self._connections.discard(task)
            writer.close()

    async def _serve_one(self, reader, writer, owner) -> bool:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            await self._status(writer, 400, "Bad Request")
            return False
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        keep_alive = headers.get("connection", "").lower() != "close"

        if method not in ("GET", "HEAD"):
            await self._status(writer, 405, "Method Not Allowed")
            return keep_alive
        target = target.split("?", 1)[0]
        token, _, encoded = target.lstrip("/").partition("/")
        if not hmac.compare_digest(token.encode(), self._token.encode()):
            await self._status(writer, 403, "Forbidden")
            return False
        path = "/" + unquote(encoded, errors="surrogateescape")

        try:
            key = await self._stat_file(path)
        except Exception as exc:
            log.debug("stat failed for %s: %s", path, exc)
            await self._status(writer, 404, "Not Found")
            return keep_alive
        size = key[2]
        try:
            byte_range = parse_range(headers.get("range"), size)
        except ValueError:
            await self._status(writer, 416, "Range Not Satisfiable",
                               [f"Content-Range: bytes */{size}"])
            return keep_alive

        start, end = byte_range if byte_range else (0, size - 1)
        response = [
            "HTTP/1.1 206 Partial Content" if byte_range else "HTTP/1.1 200 OK",
            f"Content-Type: {mimetypes.guess_type(path)[0] or 'application/octet-stream'}",
            f"Content-Length: {end - start + 1}",
            "Accept-Ranges: bytes",
            "Cache-Control: no-store",
        ]
        if byte_range:
            response.append(f"Content-Range: bytes {start}-{end}/{size}")
        response.append(f"Connection: {'keep-alive' if keep_alive else 'close'}")
        writer.write(("\r\n".join(response) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        if method == "GET" and end >= start:
            await self._stream(reader, writer, key, start, end, owner)
        return keep_alive

    async def _stream(self, reader, writer, key: FileKey, start: int, end: int, owner) -> None:
        # A new request means the player seeked: the old read-ahead is stale.
        self._scheduler.cancel_prefetch(owner)
        offset = start
        while offset <= end:
            index = offset // BLOCK_SIZE
            self._scheduler.prefetch(owner, key, index + 1, READ_AHEAD_BLOCKS)
            # Race the read against the client hanging up, so an abandoned
            # request cancels its SFTP reads instead of finishing them.
            block_task = asyncio.ensure_future(self._scheduler.get(key, index))
            gone_task = asyncio.ensure_future(reader.read(1))
            try:
                await asyncio.wait({block_task, gone_task}, return_when=asyncio.FIRST_COMPLETED)
                if not block_task.done():
                    block_task.cancel()
                    raise ConnectionResetError("client closed during read")
                block = block_task.result()
            finally:
                gone_task.cancel()
                if not block_task.done():
                    block_task.cancel()
            if not block:
                raise ConnectionResetError("file shrank during read")
            lo = offset - index * BLOCK_SIZE
            hi = min(len(block), end + 1 - index * BLOCK_SIZE)
            writer.write(memoryview(block)[lo:hi])
            await writer.drain()
            offset = index * BLOCK_SIZE + hi

    @staticmethod
    async def _status(writer, code: int, reason: str, extra: list[str] = ()) -> None:
        lines = [f"HTTP/1.1 {code} {reason}", "Content-Length: 0", *extra]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
