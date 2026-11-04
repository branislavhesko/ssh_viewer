from app.viewers import ansi
from app.viewers.log_buffer import BLOCK, LogBuffer


class Remote:
    def __init__(self, data):
        self.data = data
        self.requests = []
        self.buffer = LogBuffer(lambda offset, length: self.requests.append((offset, length)))
        self.buffer.reset(len(data))

    def answer(self):
        while self.requests:
            offset, length = self.requests.pop()
            self.buffer.store(offset, self.data[offset:offset + length])


def test_only_requested_blocks_are_read():
    remote = Remote(b"x" * (BLOCK * 10))
    assert remote.buffer.get(BLOCK * 5 + 3, BLOCK * 5 + 10) is None
    assert remote.requests == [(BLOCK * 5, BLOCK)]
    remote.answer()
    assert remote.buffer.get(BLOCK * 5 + 3, BLOCK * 5 + 10) == b"x" * 7


def test_lines_across_block_boundary():
    line = b"a" * 1000 + b"\n"
    data = line * 600
    remote = Remote(data)
    remote.buffer.lines_from(BLOCK - 100, 1)
    remote.answer()
    remote.buffer.line_start(BLOCK + 5)
    remote.answer()
    start = remote.buffer.line_start(BLOCK + 5)
    assert start % 1001 == 0 and start <= BLOCK + 5
    lines, complete = remote.buffer.lines_from(start, 2)
    if not complete:
        remote.answer()
        lines, complete = remote.buffer.lines_from(start, 2)
    assert complete and lines[0] == (start, b"a" * 1000)


def test_tail_append_extends_partial_block():
    remote = Remote(b"one\n")
    remote.buffer.get(0, 4)
    remote.answer()
    remote.buffer.size = 8
    remote.buffer.store(4, b"two\n")
    assert remote.buffer.lines_from(0, 5) == ([(0, b"one"), (4, b"two")], True)
    assert not remote.requests


def test_tail_store_into_gap_is_refetched_not_corrupted():
    remote = Remote(b"z" * 100)
    remote.buffer.size = 100
    remote.buffer.store(50, b"z" * 50)  # block 0 not loaded: must not be misplaced
    assert remote.buffer.get(0, 100) is None
    remote.answer()
    assert remote.buffer.get(0, 100) == b"z" * 100


def test_back_lines():
    remote = Remote(b"a\nbb\nccc\n")
    remote.buffer.get(0, 9)
    remote.answer()
    assert remote.buffer.back_lines(9, 2) == 2
    assert remote.buffer.back_lines(9, 99) == 0


def test_ansi():
    spans = ansi.parse("plain \x1b[1;31mred\x1b[0m end")
    assert [s.text for s in spans] == ["plain ", "red", " end"]
    assert spans[1].bold and spans[1].color == ansi.PALETTE[1] and spans[2].color is None
    assert ansi.strip("\x1b[2K\x1b[38;5;196mx\x1b[m") == "x"
