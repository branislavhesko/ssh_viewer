"""Metadata table for one directory, patched in place from events."""
from __future__ import annotations

import bisect
import posixpath
import time
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal

from app import theme
from app.backend import Backend, Event, EventKind, FileEntry
from app.bridge import Bridge, display_text

COLUMNS = ("Name", "Size", "Modified", "Permissions")
# Past this, one re-list is cheaper than a stat per changed file.
RELIST_THRESHOLD = 200


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


class FileListModel(QAbstractTableModel):
    listing_ready = Signal(str)
    listing_failed = Signal(str, str)

    def __init__(self, backend: Backend, bridge: Bridge, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._backend, self._bridge = backend, bridge
        self.directory = ""
        self._entries: list[FileEntry] = []
        self._column = 0
        self._descending = False
        self._request = 0
        self._icons = {"dir": theme.icon("folder"), "file": theme.icon("file"),
                       "link": theme.icon("file-link")}

    # Sorting -----------------------------------------------------------------
    def _key(self, entry: FileEntry):
        value = (entry.name.casefold(), entry.size, entry.mtime, entry.mode)[self._column]
        # Folders stay on top either way.
        return (entry.is_dir == self._descending, value, entry.name)

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        self.layoutAboutToBeChanged.emit()
        self._column = column
        self._descending = order == Qt.SortOrder.DescendingOrder
        self._entries.sort(key=self._key, reverse=self._descending)
        self.layoutChanged.emit()

    def _position(self, entry: FileEntry) -> int:
        if self._descending:
            lo, hi, key = 0, len(self._entries), self._key(entry)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._key(self._entries[mid]) > key:
                    lo = mid + 1
                else:
                    hi = mid
            return lo
        return bisect.bisect_left(self._entries, self._key(entry), key=self._key)

    # Table -------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        entry = self._entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return display_text(entry.name)
            if column == 1:
                return "" if entry.is_dir else format_size(entry.size)
            if column == 2:
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.mtime))
            return entry.permissions
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            kind = "link" if entry.is_symlink else "dir" if entry.is_dir else "file"
            return self._icons[kind]
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def entry(self, row: int) -> FileEntry:
        return self._entries[row]

    def row_of(self, path: str) -> int:
        for row, entry in enumerate(self._entries):
            if entry.path == path:
                return row
        return -1

    # Loading -----------------------------------------------------------------
    def set_directory(self, path: str) -> None:
        self.directory = path
        self.reload()

    def clear(self) -> None:
        self._request += 1
        self.directory = ""
        self.beginResetModel()
        self._entries = []
        self.endResetModel()

    def reload(self) -> None:
        self._request += 1
        request, path = self._request, self.directory

        def done(entries: list[FileEntry]) -> None:
            if request == self._request:
                self._replace(entries)
                self.listing_ready.emit(path)

        def failed(exc: Exception) -> None:
            if request == self._request:
                self.listing_failed.emit(path, str(exc))
        self._bridge.then(self._backend.listdir(path), done, failed)

    def _replace(self, entries: list[FileEntry]) -> None:
        entries.sort(key=self._key, reverse=self._descending)
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()

    # Patching ----------------------------------------------------------------
    def apply_events(self, events: list[Event]) -> None:
        mine = [e for e in events if posixpath.dirname(e.path) == self.directory
                or (e.kind is EventKind.RESYNC and e.path == self.directory)]
        if not mine:
            return
        if len(mine) > RELIST_THRESHOLD or any(e.kind is EventKind.RESYNC for e in mine):
            self.reload()
            return
        latest: dict[str, Event] = {e.path: e for e in mine}  # last word per path
        for path, event in latest.items():
            if event.kind in (EventKind.DELETE, EventKind.MOVED_FROM):
                self._remove(path)
            else:
                self._refresh(path)

    def _refresh(self, path: str) -> None:
        directory = self.directory

        def done(entry: FileEntry) -> None:
            if directory == self.directory:
                self._upsert(entry)

        def failed(_exc: Exception) -> None:
            if directory == self.directory:
                self._remove(path)  # gone again before we looked
        self._bridge.then(self._backend.stat(path, follow=False), done, failed)

    def _upsert(self, entry: FileEntry) -> None:
        row = self.row_of(entry.path)
        if row >= 0 and self._key(self._entries[row]) == self._key(entry):
            self._entries[row] = entry
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
            return
        if row >= 0:
            self._remove(entry.path)
        row = self._position(entry)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.insert(row, entry)
        self.endInsertRows()

    def _remove(self, path: str) -> None:
        row = self.row_of(path)
        if row >= 0:
            self.beginRemoveRows(QModelIndex(), row, row)
            self._entries.pop(row)
            self.endRemoveRows()
