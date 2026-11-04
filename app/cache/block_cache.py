"""LRU of fixed-size file blocks, keyed by file identity and block index."""
from __future__ import annotations

from collections import OrderedDict
from typing import Hashable, Optional

BLOCK_SIZE = 256 * 1024


class BlockCache:
    """Not thread safe; owned by the backend's event loop."""

    def __init__(self, max_bytes: int = 512 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.used = 0
        self._blocks: OrderedDict[tuple[Hashable, int], bytes] = OrderedDict()

    def clear(self) -> None:
        self._blocks.clear()
        self.used = 0

    def get(self, key: Hashable, index: int) -> Optional[bytes]:
        data = self._blocks.get((key, index))
        if data is not None:
            self._blocks.move_to_end((key, index))
        return data

    def put(self, key: Hashable, index: int, data: bytes) -> None:
        old = self._blocks.pop((key, index), None)
        if old is not None:
            self.used -= len(old)
        self._blocks[(key, index)] = data
        self.used += len(data)
        while self.used > self.max_bytes and self._blocks:
            _, evicted = self._blocks.popitem(last=False)
            self.used -= len(evicted)
