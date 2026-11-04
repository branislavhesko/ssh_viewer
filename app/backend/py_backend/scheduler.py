"""Block fetching: single flight per block, priorities, cancellation."""
from __future__ import annotations

import asyncio
import heapq
import itertools
from typing import Awaitable, Callable, Hashable, Optional

from app.cache import BLOCK_SIZE, BlockCache

DEMAND = 0
READ_AHEAD = 1

# Key is (path, version, size), where version is (mtime, generation). SFTP v3
# mtimes are whole seconds, so same-size rewrites within a second are only told
# apart by the generation, which change events bump.
FileKey = tuple[str, tuple[float, int], int]
ReadBlock = Callable[[str, int, int], Awaitable[bytes]]


class _Job:
    __slots__ = ("key", "index", "future", "refs", "task", "priority")

    def __init__(self, key: FileKey, index: int, future: asyncio.Future, priority: int):
        self.key, self.index, self.future = key, index, future
        self.refs = 0
        self.task: Optional[asyncio.Task] = None
        self.priority = priority


class FetchScheduler:
    def __init__(self, cache: BlockCache, read_block: ReadBlock, workers: int = 8):
        self.cache = cache
        self._read_block = read_block
        self._max_running = workers
        self._running = 0
        self._jobs: dict[tuple[FileKey, int], _Job] = {}
        self._queue: list[tuple[int, int, _Job]] = []
        self._seq = itertools.count()
        # Read-ahead jobs held per consumer, so a seek or close can drop them.
        self._owned: dict[Hashable, set[_Job]] = {}
        self.reads_issued = 0

    async def get(self, key: FileKey, index: int) -> bytes:
        cached = self.cache.get(key, index)
        if cached is not None:
            return cached
        job = self._submit(key, index, DEMAND)
        job.refs += 1
        try:
            return await asyncio.shield(job.future)
        finally:
            self._release(job)

    def prefetch(self, owner: Hashable, key: FileKey, first: int, count: int) -> None:
        last = (key[2] - 1) // BLOCK_SIZE
        held = self._owned.setdefault(owner, set())
        # A finished job's future holds its block: keeping it would pin every
        # block played so far, outside the cache's limit.
        for job in [j for j in held if j.future.done()]:
            held.discard(job)
            job.refs -= 1
        for index in range(first, min(first + count, last + 1)):
            if self.cache.get(key, index) is None:
                job = self._submit(key, index, READ_AHEAD)
                if job not in held:
                    job.refs += 1
                    held.add(job)

    def cancel_prefetch(self, owner: Hashable) -> None:
        for job in self._owned.pop(owner, ()):
            self._release(job)

    def _submit(self, key: FileKey, index: int, priority: int) -> _Job:
        job = self._jobs.get((key, index))
        if job is None:
            future = asyncio.get_running_loop().create_future()
            # A job can fail with nobody left waiting; don't warn about that.
            future.add_done_callback(lambda f: f.cancelled() or f.exception())
            job = _Job(key, index, future, priority)
            self._jobs[(key, index)] = job
            heapq.heappush(self._queue, (priority, next(self._seq), job))
        elif priority < job.priority and job.task is None:
            job.priority = priority
            heapq.heappush(self._queue, (priority, next(self._seq), job))
        self._pump()
        return job

    def _release(self, job: _Job) -> None:
        job.refs -= 1
        if job.refs > 0 or job.future.done():
            return
        # Nobody wants it any more: stop the SFTP read, or never start it.
        self._jobs.pop((job.key, job.index), None)
        if job.task is not None:
            job.task.cancel()
        job.future.cancel()

    def _pump(self) -> None:
        while self._running < self._max_running and self._queue:
            _, _, job = heapq.heappop(self._queue)
            if job.future.done() or job.task is not None:
                continue  # cancelled, or a stale entry from a priority raise
            self._running += 1
            job.task = asyncio.ensure_future(self._run(job))

    async def _run(self, job: _Job) -> None:
        try:
            self.reads_issued += 1
            data = await self._read_block(job.key[0], job.index * BLOCK_SIZE, BLOCK_SIZE)
            self.cache.put(job.key, job.index, data)
            if not job.future.done():
                job.future.set_result(data)
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.cancel()
        except Exception as exc:
            if not job.future.done():
                job.future.set_exception(exc)
        finally:
            self._jobs.pop((job.key, job.index), None)
            self._running -= 1
            self._pump()
