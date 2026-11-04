import os
import time
from concurrent.futures import Future

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PySide6.QtCore import Qt

from app.viewers import table_data
from app.viewers.table import TableViewer
from app.viewers.table_data import CsvSource, RemoteFile, parse_csv
from test_ui import Harness, pump, qapp  # noqa: F401


def source(text, name="a.csv", has_header=True):
    return CsvSource(parse_csv(name, text.encode(), False), has_header)


def column(src, index):
    return [src.cell(row, index) for row in range(src.row_count)]


def test_delimiters_quotes_and_bom():
    assert source("a;b\n1;2\n").columns == ["a", "b"]
    assert source("a\tb\n1\t2\n", "x.tsv").columns == ["a", "b"]
    src = source('﻿name,note\nx,"line one\nline two, with comma"\n')
    assert src.columns == ["name", "note"] and src.row_count == 1
    assert src.cell(0, 1) == "line one\nline two, with comma"


def test_ragged_rows_and_blank_header_names():
    src = source("a,,c\n1\n1,2,3,4\n\n")
    assert src.columns == ["a", "column 2", "c", "column 4"]
    assert src.cell(0, 3) == "" and src.cell(1, 3) == "4" and src.row_count == 2


def test_no_header_keeps_first_row():
    src = source("1,2\n3,4\n", has_header=False)
    assert src.columns == ["column 1", "column 2"] and src.row_count == 2


def test_unfinished_last_line_is_held_back():
    assert parse_csv("a.csv", b"a,b\n1,2\n3,", False).rows == [["a", "b"], ["1", "2"]]
    assert parse_csv("a.csv", b"only one line", False).rows == [["only one line"]]


def test_cut_off_file_drops_the_row_that_may_be_partial():
    parsed = parse_csv("a.csv", b'a,b\n1,2\n3,"long\nmore', True)
    assert parsed.truncated and parsed.rows == [["a", "b"], ["1", "2"]]


def test_sort_is_numeric_with_text_then_blanks_last():
    src = source("v\n10\n9\nabc\n\n" + "nan\n" + ",\n1.5\n")
    src.sort(0, False)
    assert column(src, 0)[:3] == ["1.5", "9", "10"]
    assert column(src, 0)[-1] == ""
    src.sort(-1, False)
    assert column(src, 0)[0] == "10"


class FakeBackend:
    def __init__(self, data):
        self.data, self.bytes_read = data, 0

    def read(self, _path, offset, length):
        chunk = self.data[offset:offset + length]
        self.bytes_read += len(chunk)
        future = Future()
        future.set_result(chunk)
        return future

    def stat(self, _path):
        future = Future()
        future.set_result(type("Entry", (), {"size": len(self.data)}))
        return future


def write_parquet(path, rows=100000, group=1000):
    table = pa.table({"id": list(range(rows)),
                      "text": [f"random value {i * 2654435761 % 99991}" for i in range(rows)]})
    pq.write_table(table, path, row_group_size=group, compression="none")


def test_only_footer_and_requested_group_cross_the_wire(tmp_path):
    path = tmp_path / "a.parquet"
    write_parquet(path)
    backend = FakeBackend(path.read_bytes())
    handle = table_data.open_parquet(backend, "/a.parquet")
    assert handle.metadata.num_rows == 100000
    handle.read_row_group(50)
    assert backend.bytes_read < len(backend.data) / 5


def test_remote_file_seek_and_read():
    remote = RemoteFile(FakeBackend(b"0123456789"), "/f", 10)
    remote.seek(-3, 2)
    assert remote.read(10) == b"789" and remote.tell() == 10


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def open_table(ui, name):
    ui.window.list_model.reload()
    ui.select(name)
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TableViewer))
    return preview.current


def header(viewer):
    model = viewer.model
    return [model.headerData(i, Qt.Orientation.Horizontal) for i in range(model.columnCount())]


def text_at(viewer, row, col):
    model = viewer.model
    return model.data(model.index(row, col))


def test_csv_opens_sorts_and_toggles_header(ui, server):
    (server.root / "m.csv").write_text("step,loss\n1,0.9\n2,0.5\n10,0.1\n")
    viewer = open_table(ui, "m.csv")
    assert pump(lambda: viewer.model.rowCount() == 3)
    assert header(viewer) == ["step", "loss"]
    assert viewer.status.text() == "3 rows × 2 columns"
    viewer.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert text_at(viewer, 0, 0) == "10"
    viewer.header.setChecked(False)
    assert viewer.model.rowCount() == 4
    viewer.header.setChecked(True)


def test_csv_held_open_by_a_writer_still_updates(qapp, inotify_server, make_backend):
    harness = Harness(inotify_server, make_backend(), watcher="inotify")
    try:
        path = inotify_server.root / "live.csv"
        path.write_text("step,loss\n1,0.9\n")
        viewer = open_table(harness, "live.csv")
        assert pump(lambda: viewer.model.rowCount() == 1)
        assert pump(lambda: str(inotify_server.root) in harness.backend.established_watches())
        with path.open("a") as out:  # stays open: no close_write
            out.write("2,0.5\n")
            out.flush()
            assert pump(lambda: viewer.model.rowCount() == 2, 5)
    finally:
        harness.close()


def test_sort_survives_reload(ui, server):
    path = server.root / "s.csv"
    path.write_text("v\n1\n3\n")
    viewer = open_table(ui, "s.csv")
    assert pump(lambda: viewer.model.rowCount() == 2)
    viewer.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    pump(lambda: False, 0.6)  # polling baseline
    path.write_text("v\n1\n3\n7\n")
    assert pump(lambda: viewer.model.rowCount() == 3)
    assert text_at(viewer, 0, 0) == "7"


def test_big_csv_says_it_is_cut_off(ui, server, monkeypatch):
    monkeypatch.setattr(table_data, "MAX_CSV_BYTES", 1000)
    (server.root / "b.csv").write_text("a,b\n" + "1,2\n" * 5000)
    viewer = open_table(ui, "b.csv")
    assert pump(lambda: "Showing the first" in viewer.status.text())
    assert 0 < viewer.model.rowCount() < 300


def test_parquet_pages_on_demand(ui, server):
    write_parquet(server.root / "d.parquet")
    viewer = open_table(ui, "d.parquet")
    assert pump(lambda: viewer.model.rowCount() == 100000)
    assert header(viewer) == ["id", "text"]
    assert "100 row groups" in viewer.status.text()
    assert "int64" in viewer.model.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
    assert pump(lambda: text_at(viewer, 0, 0) == "0")
    assert text_at(viewer, 70123, 0) == "…"
    assert pump(lambda: text_at(viewer, 70123, 0) == "70123")
    viewer.table.scrollTo(viewer.model.index(99999, 0))
    assert pump(lambda: text_at(viewer, 99999, 0) == "99999")


def test_parquet_columns_leave_room_for_values_beyond_the_first_rows(ui, server):
    from PySide6.QtGui import QFontMetrics
    write_parquet(server.root / "w.parquet", rows=2000, group=1000)
    viewer = open_table(ui, "w.parquet")
    assert pump(lambda: viewer.model.rowCount() == 2000 and text_at(viewer, 0, 0) == "0")
    header = viewer.table.horizontalHeader()
    assert pump(lambda: header.sectionSize(0) >= QFontMetrics(viewer.font()).horizontalAdvance("15000000"))


def test_parquet_nulls_and_types(ui, server):
    table = pa.table({"n": [1, None], "b": [b"\x01\xff", None], "l": [[1, 2], None]})
    pq.write_table(table, server.root / "t.parquet")
    viewer = open_table(ui, "t.parquet")
    assert pump(lambda: viewer.model.rowCount() == 2)
    assert pump(lambda: text_at(viewer, 1, 0) == "null")
    assert text_at(viewer, 0, 1) == "01ff" and text_at(viewer, 0, 2) == "[1, 2]"


def test_parquet_rewrite_reloads_and_corrupt_keeps_old(ui, server):
    path = server.root / "r.parquet"
    write_parquet(path, rows=10, group=10)
    viewer = open_table(ui, "r.parquet")
    assert pump(lambda: viewer.model.rowCount() == 10)
    pump(lambda: False, 0.6)  # polling baseline

    path.write_bytes(b"not parquet at all")
    later = time.time() + 2
    os.utime(path, (later, later))
    assert pump(lambda: "Could not read" in viewer.status.text())
    assert viewer.model.rowCount() == 10

    write_parquet(path, rows=25, group=10)
    later += 2
    os.utime(path, (later, later))
    assert pump(lambda: viewer.model.rowCount() == 25)


def test_oversized_row_group_is_refused_with_a_message(ui, server, monkeypatch):
    monkeypatch.setattr(table_data, "MAX_GROUP_BYTES", 10)
    write_parquet(server.root / "g.parquet", rows=100, group=100)
    viewer = open_table(ui, "g.parquet")
    assert pump(lambda: viewer.model.rowCount() == 100)
    viewer.table.repaint()
    assert pump(lambda: "too large" in viewer.status.text())


def test_switching_between_formats_resets_the_view(ui, server):
    (server.root / "a.csv").write_text("x\n1\n")
    write_parquet(server.root / "b.parquet", rows=5, group=5)
    viewer = open_table(ui, "a.csv")
    assert pump(lambda: viewer.model.rowCount() == 1)
    ui.select("b.parquet")
    assert pump(lambda: viewer.model.rowCount() == 5)
    assert not viewer.table.isSortingEnabled() and not viewer.header.isVisibleTo(viewer)
    ui.select("a.csv")
    assert pump(lambda: viewer.model.rowCount() == 1)
    assert viewer.table.isSortingEnabled()
