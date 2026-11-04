import asyncio

import pytest

from app.backend.py_backend.scheduler import FetchScheduler
from app.cache import BLOCK_SIZE, BlockCache

KEY = ("/f", (1.0, 0), BLOCK_SIZE * 100)


class Reads:
    def __init__(self):
        self.started = []
        self.cancelled = []
        self.gate = asyncio.Event()

    async def __call__(self, path, offset, length):
        self.started.append(offset // BLOCK_SIZE)
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(offset // BLOCK_SIZE)
            raise
        return b"x" * length


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 5))


def test_single_flight():
    async def main():
        reads = Reads()
        scheduler = FetchScheduler(BlockCache(), reads)
        tasks = [asyncio.ensure_future(scheduler.get(KEY, 3)) for _ in range(5)]
        await settle()
        reads.gate.set()
        await asyncio.gather(*tasks)
        assert reads.started == [3]
    run(main())


def test_demand_jumps_ahead_of_read_ahead():
    async def main():
        reads = Reads()
        scheduler = FetchScheduler(BlockCache(), reads, workers=1)
        scheduler.prefetch("owner", KEY, 0, 5)
        await settle()
        demand = asyncio.ensure_future(scheduler.get(KEY, 4))
        await settle()
        reads.gate.set()
        await demand
        assert reads.started[:2] == [0, 4]
        scheduler.cancel_prefetch("owner")
    run(main())


def test_seek_cancels_read_ahead_running_and_queued():
    async def main():
        reads = Reads()
        scheduler = FetchScheduler(BlockCache(), reads, workers=2)
        scheduler.prefetch("owner", KEY, 0, 6)
        await settle()
        assert reads.started == [0, 1]
        scheduler.cancel_prefetch("owner")
        await settle()
        assert sorted(reads.cancelled) == [0, 1]
        reads.gate.set()
        await settle()
        assert reads.started == [0, 1]  # the queued four never ran
    run(main())


def test_cancelling_one_waiter_keeps_the_read_for_the_other():
    async def main():
        reads = Reads()
        scheduler = FetchScheduler(BlockCache(), reads)
        first = asyncio.ensure_future(scheduler.get(KEY, 2))
        second = asyncio.ensure_future(scheduler.get(KEY, 2))
        await settle()
        first.cancel()
        await settle()
        assert reads.cancelled == []
        reads.gate.set()
        assert len(await second) == BLOCK_SIZE
    run(main())


def test_abandoned_demand_read_is_cancelled():
    async def main():
        reads = Reads()
        scheduler = FetchScheduler(BlockCache(), reads)
        task = asyncio.ensure_future(scheduler.get(KEY, 2))
        await settle()
        task.cancel()
        await settle()
        assert reads.cancelled == [2]
    run(main())


def test_failed_read_reaches_waiter_and_can_be_retried():
    async def main():
        calls = []

        async def read(path, offset, length):
            calls.append(offset)
            if len(calls) == 1:
                raise OSError("boom")
            return b"ok"
        scheduler = FetchScheduler(BlockCache(), read)
        with pytest.raises(OSError):
            await scheduler.get(KEY, 0)
        assert await scheduler.get(KEY, 0) == b"ok"
    run(main())


def test_long_playback_does_not_pin_finished_blocks():
    async def main():
        async def read(path, offset, length):
            return b"x" * 1000
        scheduler = FetchScheduler(BlockCache(max_bytes=2000), read)
        for index in range(50):
            scheduler.prefetch("player", KEY, index + 1, 2)
            await scheduler.get(KEY, index)
            await settle()
        assert len(scheduler._owned["player"]) <= 3
        scheduler.cancel_prefetch("player")
    run(main())
