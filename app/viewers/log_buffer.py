"""Sparse window onto a remote file.

A remote 1 GB log can't be indexed without downloading it, so positions are
byte offsets (as in ``less``), not line numbers. Blocks arrive on demand and
line starts are found by scanning nearby bytes.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Optional

BLOCK = 256 * 1024
MAX_BLOCKS = 256
# How far a single line is followed before it is treated as cut.
MAX_LINE_SCAN = 64 * 1024

ReadRequest = Callable[[int, int], None]


class LogBuffer:
    def __init__(self, request: ReadRequest):
        self._request = request  # asks for (offset, length); answer via store()
        self.size = 0
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._pending: set[int] = set()

    def reset(self, size: int = 0) -> None:
        self.size = size
        self._blocks.clear()
        self._pending.clear()

    def _expected(self, index: int) -> int:
        return max(0, min(BLOCK, self.size - index * BLOCK))

    def _complete(self, index: int) -> bool:
        block = self._blocks.get(index)
        return block is not None and len(block) >= self._expected(index)

    def store(self, offset: int, data: bytes) -> None:
        """Take bytes from a block read or a tail read."""
        index, within = divmod(offset, BLOCK)
        self._pending.discard(index)
        while data:
            room = BLOCK - within
            piece, data = data[:room], data[room:]
            old = self._blocks.get(index, b"")
            if within <= len(old):
                self._blocks[index] = old[:within] + piece + old[within + len(piece):]
                self._blocks.move_to_end(index)
            # Otherwise there's a gap: leave it to be read on demand.
            index, within = index + 1, 0
        while len(self._blocks) > MAX_BLOCKS:
            self._blocks.popitem(last=False)

    def fail(self, offset: int) -> None:
        self._pending.discard(offset // BLOCK)

    def get(self, start: int, end: int) -> Optional[bytes]:
        """Bytes in [start, end), or None after requesting what's missing."""
        start, end = max(start, 0), min(end, self.size)
        if start >= end:
            return b""
        first, last = start // BLOCK, (end - 1) // BLOCK
        missing = [i for i in range(first, last + 1) if not self._complete(i)]
        if missing:
            for index in missing:
                if index not in self._pending:
                    self._pending.add(index)
                    self._request(index * BLOCK, BLOCK)
            return None
        for index in range(first, last + 1):
            self._blocks.move_to_end(index)
        joined = b"".join(self._blocks[i] for i in range(first, last + 1))
        return joined[start - first * BLOCK:end - first * BLOCK]

    def line_start(self, offset: int) -> Optional[int]:
        """Start of the line holding ``offset``; None while bytes are missing."""
        offset = min(offset, self.size)
        floor = max(0, offset - MAX_LINE_SCAN)
        data = self.get(floor, offset)
        if data is None:
            return None
        newline = data.rfind(b"\n")
        return floor + newline + 1 if newline >= 0 else floor

    def lines_from(self, offset: int, count: int) -> tuple[list[tuple[int, bytes]], bool]:
        """Up to ``count`` (start, bytes-without-newline) pairs from ``offset``.
        The flag is false if bytes were missing, so the list may be short."""
        lines: list[tuple[int, bytes]] = []
        position = offset
        while len(lines) < count and position < self.size:
            data = self.get(position, position + MAX_LINE_SCAN)
            if data is None:
                return lines, False
            newline = data.find(b"\n")
            if newline >= 0:
                lines.append((position, data[:newline]))
                position += newline + 1
            else:
                lines.append((position, data))
                position += len(data)
        return lines, True

    def previous_line(self, offset: int) -> Optional[int]:
        """Start of the line before the one starting at ``offset``."""
        if offset <= 0:
            return 0
        return self.line_start(offset - 1)

    def back_lines(self, offset: int, count: int) -> Optional[int]:
        position = offset
        for _ in range(count):
            if position <= 0:
                return 0
            position = self.previous_line(position)
            if position is None:
                return None
        return position
