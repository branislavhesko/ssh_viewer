"""Row sources for the table viewer. CSV is parsed up front; Parquet is paged."""
from __future__ import annotations

import csv
import io
import posixpath
from bisect import bisect_right
from collections import OrderedDict, deque
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.backend import Backend
from app.bridge import Bridge

# CSV has no index, so a row count would mean reading all of it: show the start.
MAX_CSV_BYTES = 16 * 1024 * 1024
SNIFF_CHARS = 64 * 1024
MAX_CELL_CHARS = 500
READ_TIMEOUT = 120
CACHED_GROUPS = 4
# Uncompressed. Row groups are the smallest unit Parquet can be read in.
MAX_GROUP_BYTES = 1024 * 1024 * 1024
PENDING_GROUPS = 4

csv.field_size_limit(MAX_CSV_BYTES)


@dataclass
class ParsedCsv:
    rows: list[list[str]]
    delimiter: str
    truncated: bool


def _delimiter(name: str, text: str) -> str:
    if posixpath.splitext(name)[1].lower() in (".tsv", ".tab"):
        return "\t"
    try:
        return csv.Sniffer().sniff(text[:SNIFF_CHARS], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(name: str, data: bytes, truncated: bool) -> ParsedCsv:
    if truncated or not data.endswith(b"\n"):
        # The end may be mid-row, either cut off by us or still being written.
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else data
    text = data.decode("utf-8-sig", "replace")
    delimiter = _delimiter(name, text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = [row for row in reader if row]
    if truncated and rows:
        rows.pop()  # a quoted field can span lines, so this one may still be cut
    return ParsedCsv(rows, delimiter, truncated)


def _sort_key(value: str) -> tuple:
    """Numbers by value, then text, then blanks."""
    if not value:
        return (2, 0.0, "")
    try:
        number = float(value)
    except ValueError:
        return (1, 0.0, value.casefold())
    return (0, number, "") if number == number else (1, 0.0, value.casefold())


class CsvSource:
    sortable = True

    def __init__(self, parsed: ParsedCsv, has_header: bool):
        rows = parsed.rows
        width = max((len(row) for row in rows), default=0)
        header = rows[0] if has_header and rows else []
        self.columns = [header[i] if i < len(header) and header[i] else f"column {i + 1}"
                        for i in range(width)]
        self.tooltips = list(self.columns)
        self._rows = rows[1:] if has_header else rows
        self._order: Optional[list[int]] = None

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def cell(self, row: int, column: int) -> Optional[str]:
        index = self._order[row] if self._order is not None else row
        values = self._rows[index]
        return values[column][:MAX_CELL_CHARS] if column < len(values) else ""

    def sort(self, column: int, descending: bool) -> None:
        if column < 0:
            self._order = None
            return
        rows = self._rows
        self._order = sorted(
            range(len(rows)), reverse=descending,
            key=lambda i: _sort_key(rows[i][column] if column < len(rows[i]) else ""))


class RemoteFile(io.RawIOBase):
    """Seekable file over backend.read, for pyarrow. Blocks: worker threads only."""

    def __init__(self, backend: Backend, path: str, size: int):
        super().__init__()
        self._backend, self._path, self._size = backend, path, size
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = (0, self._position, self._size)[whence]
        self._position = base + offset
        return self._position

    def readinto(self, buffer) -> int:
        data = self._backend.read(self._path, self._position, len(buffer)).result(READ_TIMEOUT)
        buffer[:len(data)] = data
        self._position += len(data)
        return len(data)


def open_parquet(backend: Backend, path: str):
    """Reads only the footer. Run on a worker thread."""
    import pyarrow.parquet as parquet

    size = backend.stat(path).result(READ_TIMEOUT).size
    # Unbuffered: pyarrow asks for exact ranges, and a buffer would over-read each one.
    return parquet.ParquetFile(RemoteFile(backend, path, size))


def format_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value[:64].hex() + ("…" if len(value) > 64 else "")
    return str(value)[:MAX_CELL_CHARS]


class ParquetSource:
    """Row groups load on demand, one at a time, most recently wanted first.
    ParquetFile isn't thread safe, so ``worker`` must be a single thread."""

    sortable = False

    def __init__(self, handle, worker: Executor, bridge: Bridge,
                 on_loaded: Callable[[int, int], None], on_error: Callable[[str], None]):
        self._handle, self._worker, self._bridge = handle, worker, bridge
        self._on_loaded, self._on_error = on_loaded, on_error
        schema = handle.schema_arrow
        self.columns = list(schema.names)
        self.tooltips = [f"{field.name}: {field.type}" for field in schema]
        metadata = handle.metadata
        self.row_count = metadata.num_rows
        self.group_count = metadata.num_row_groups
        self._starts: list[int] = []
        self._bytes: list[int] = []
        position = 0
        for index in range(self.group_count):
            group = metadata.row_group(index)
            self._starts.append(position)
            self._bytes.append(group.total_byte_size)
            position += group.num_rows
        self._groups: OrderedDict[int, Any] = OrderedDict()
        self._pending: deque[int] = deque(maxlen=PENDING_GROUPS)
        self._running: Optional[int] = None
        self._failed: set[int] = set()
        self._closed = False

    def sort(self, _column: int, _descending: bool) -> None:
        pass

    def cell(self, row: int, column: int) -> Optional[str]:
        index = bisect_right(self._starts, row) - 1
        table = self._groups.get(index)
        if table is None:
            self._want(index)
            return None
        self._groups.move_to_end(index)
        value = table.column(column)[row - self._starts[index]].as_py()
        return "null" if value is None else format_value(value)

    def _want(self, index: int) -> None:
        if index in self._failed or index == self._running or index in self._pending:
            return
        if self._bytes[index] > MAX_GROUP_BYTES:
            self._failed.add(index)
            self._on_error(f"Row group {index + 1} is too large to load "
                           f"({self._bytes[index] / 2**30:.1f} GB uncompressed)")
            return
        self._pending.append(index)
        self._pump()

    def _pump(self) -> None:
        if self._running is not None or not self._pending or self._closed:
            return
        index = self._running = self._pending.pop()

        def done(table) -> None:
            self._running = None
            if self._closed:
                return
            self._groups[index] = table
            while len(self._groups) > CACHED_GROUPS:
                self._groups.popitem(last=False)
            first = self._starts[index]
            self._on_loaded(first, first + table.num_rows - 1)
            self._pump()

        def failed(exc: Exception) -> None:
            self._running = None
            if not self._closed:
                self._failed.add(index)
                self._on_error(f"Could not read row group {index + 1}: {exc}")
                self._pump()
        self._bridge.then(self._worker.submit(self._handle.read_row_group, index), done, failed)

    def close(self) -> None:
        self._closed = True
        self._worker.submit(self._handle.close)
