"""CSV and Parquet in one virtual table."""
from __future__ import annotations

import posixpath
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel,
                               QTableView, QVBoxLayout)

from app.backend import Event, EventKind, FileEntry
from app.models.file_list_model import format_size

from . import table_data
from .base import Viewer
from .table_data import CsvSource, ParquetSource, ParsedCsv

PARQUET_SUFFIXES = {".parquet", ".parq", ".pq"}
CSV_SUFFIXES = {".csv", ".tsv", ".tab"}
LIVE_RELOAD_MS = 1000
MAX_COLUMN_WIDTH = 300
# Parquet widths come from the first rows alone; later values are often longer.
MIN_PARQUET_COLUMN_WIDTH = 110
MAX_FITTED_COLUMNS = 200


class TableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = None
        self._sort = (-1, False)

    def set_source(self, source) -> None:
        self.beginResetModel()
        self.source = source
        if source is not None:
            source.sort(*self._sort)
        self.endResetModel()

    def forget_sort(self) -> None:
        self._sort = (-1, False)

    def rows_loaded(self, first: int, last: int) -> None:
        self.dataChanged.emit(self.index(first, 0), self.index(last, self.columnCount() - 1))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() or self.source is None else self.source.row_count

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() or self.source is None else len(self.source.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            value = self.source.cell(index.row(), index.column())
            return "…" if value is None else value
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if self.source is None:
            return None
        if orientation == Qt.Orientation.Vertical:
            return section + 1 if role == Qt.ItemDataRole.DisplayRole else None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.source.columns[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.source.tooltips[section]
        return None

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if self.source is None or not self.source.sortable:
            return
        self._sort = (column, order == Qt.SortOrder.DescendingOrder)
        self.layoutAboutToBeChanged.emit()
        self.source.sort(*self._sort)
        self.layoutChanged.emit()


class TableViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        # One thread: ParquetFile isn't thread safe.
        self._worker = ThreadPoolExecutor(1, thread_name_prefix="table")
        self.model = TableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.table.horizontalScrollBar().setSingleStep(20)
        vertical = self.table.verticalHeader()
        vertical.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vertical.setDefaultSectionSize(self.fontMetrics().height() + 6)
        self.table.horizontalHeader().setResizeContentsPrecision(50)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        self.header = QCheckBox("First row is header")
        self.header.setChecked(True)
        self.header.toggled.connect(self._header_toggled)
        self.status = QLabel()
        bar = QHBoxLayout()
        bar.addWidget(self.header)
        bar.addWidget(self.status, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.table, 1)

        self._throttle = QTimer(self, singleShot=True, interval=LIVE_RELOAD_MS)
        self._throttle.timeout.connect(self._load)
        self._parsed: Optional[ParsedCsv] = None
        self._loading = False
        self._reload_again = False
        self._fit_pending = False

    @property
    def is_parquet(self) -> bool:
        return (self.entry is not None
                and posixpath.splitext(self.entry.name)[1].lower() in PARQUET_SUFFIXES)

    # Opening -----------------------------------------------------------------
    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self._throttle.stop()
        self._release()
        self._parsed = None
        self._loading = self._reload_again = False
        self._fit_pending = True
        self.model.forget_sort()
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(not self.is_parquet)
        self.header.setVisible(not self.is_parquet)
        self.status.setText("Loading…")
        self._load()

    def close_file(self) -> None:
        super().close_file()
        self._throttle.stop()
        self._release()

    def _release(self) -> None:
        old = self.model.source
        self.model.set_source(None)
        if isinstance(old, ParquetSource):
            old.close()

    def on_events(self, events: list[Event]) -> None:
        kinds = {e.kind for e in events}
        if kinds & {EventKind.CLOSE_WRITE, EventKind.MOVED_TO, EventKind.RESYNC}:
            self._load()
        elif EventKind.MODIFY in kinds and not self.is_parquet and not self._throttle.isActive():
            # A logger may hold its CSV open for hours, so close_write never comes.
            # Parquet has no footer until it is closed, so it has to wait.
            self._throttle.start()

    # Loading -----------------------------------------------------------------
    def _load(self) -> None:
        if self.entry is None:
            return
        if self._loading:
            self._reload_again = True
            return
        self._loading = True
        if self.is_parquet:
            self._load_parquet()
        else:
            self._load_csv()

    def _finished(self) -> None:
        self._loading = False
        if self._reload_again:
            self._reload_again = False
            self._load()

    def _failed(self, current, exc: Exception) -> None:
        if current():
            # Keep what's on screen: the next complete write brings a good one.
            self.status.setText(f"Could not read file: {exc}")
            self._finished()

    def _load_csv(self) -> None:
        current = self._current()
        name, limit = self.entry.name, table_data.MAX_CSV_BYTES

        def fetched(data: bytes) -> None:
            if current():
                self.bridge.then(
                    self._worker.submit(table_data.parse_csv, name, data, len(data) >= limit),
                    parsed, lambda exc: self._failed(current, exc))

        def parsed(result: ParsedCsv) -> None:
            if current():
                self._parsed = result
                self._show(CsvSource(result, self.header.isChecked()))
                self._finished()
        self.bridge.then(self.backend.read(self.entry.path, 0, limit), fetched,
                         lambda exc: self._failed(current, exc))

    def _load_parquet(self) -> None:
        current = self._current()

        def opened(handle) -> None:
            if not current():
                self._worker.submit(handle.close)
                return
            self._show(ParquetSource(handle, self._worker, self.bridge, self._rows_loaded,
                                     self.status.setText))
            self._finished()
        self.bridge.then(self._worker.submit(table_data.open_parquet, self.backend, self.entry.path),
                         opened, lambda exc: self._failed(current, exc))

    def _rows_loaded(self, first: int, last: int) -> None:
        self.model.rows_loaded(first, last)
        self._fit_columns()

    # Display -----------------------------------------------------------------
    def _show(self, source) -> None:
        vertical, horizontal = self.table.verticalScrollBar(), self.table.horizontalScrollBar()
        at_end = vertical.value() > 0 and vertical.value() == vertical.maximum()
        position, across = vertical.value(), horizontal.value()
        old = self.model.source
        self.model.set_source(source)
        if isinstance(old, ParquetSource):
            old.close()
        self._describe(source)
        if not source.columns:
            return
        if not isinstance(source, ParquetSource):
            self._fit_columns()
        vertical.setValue(vertical.maximum() if at_end else position)
        horizontal.setValue(across)

    def _describe(self, source) -> None:
        shape = f"{source.row_count:,} rows × {len(source.columns):,} columns"
        if isinstance(source, ParquetSource):
            self.status.setText(f"{shape}, {source.group_count:,} row groups")
        elif self._parsed.truncated:
            self.status.setText(f"Showing the first {format_size(table_data.MAX_CSV_BYTES)} "
                                f"of {format_size(self.entry.size)}: {shape}")
        else:
            self.status.setText(shape)

    def _fit_columns(self) -> None:
        if not self._fit_pending or self.model.columnCount() > MAX_FITTED_COLUMNS:
            return
        self._fit_pending = False
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        minimum = MIN_PARQUET_COLUMN_WIDTH if self.is_parquet else 0
        for column in range(self.model.columnCount()):
            width = min(max(header.sectionSize(column), minimum), MAX_COLUMN_WIDTH)
            header.resizeSection(column, width)

    def _header_toggled(self, checked: bool) -> None:
        if self._parsed is not None:
            self._fit_pending = True
            self._show(CsvSource(self._parsed, checked))
