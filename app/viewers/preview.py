"""Chooses a viewer per file and hosts them in one stack."""
from __future__ import annotations

import hashlib
import os
import posixpath
from typing import Optional

from PySide6.QtCore import QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from app.backend import Event, FileEntry
from app.bridge import display_text

from .base import Viewer, ViewerContext
from .editor import MAX_EDIT_BYTES, EditorViewer
from .hex import HexViewer
from .image import ImageViewer
from .markdown import MAX_MARKDOWN_BYTES, MarkdownViewer
from .pdf import PdfViewer
from .table import CSV_SUFFIXES, PARQUET_SUFFIXES, TableViewer
from .text_log import TextLogViewer
from .video import VideoViewer

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ppm",
                  ".pgm", ".pbm", ".tga", ".ico"}
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".mts", ".mpg",
                  ".mpeg", ".wmv", ".flv", ".ogv"}
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown"}
SNIFF_BYTES = 8192


def kind_for_name(name: str) -> Optional[str]:
    suffix = posixpath.splitext(name)[1].lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix == ".pdf":
        return "pdf"
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in CSV_SUFFIXES or suffix in PARQUET_SUFFIXES:
        return "table"
    return None


def looks_binary(data: bytes) -> bool:
    return b"\0" in data


class Preview(QWidget):
    message = Signal(str)
    stayed = Signal()  # a switch of file was refused: the editor is still open

    def __init__(self, context: ViewerContext, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context = context
        self._viewers: dict[str, Viewer] = {}
        self._factories = {"text": TextLogViewer, "image": ImageViewer, "pdf": PdfViewer,
                           "video": VideoViewer, "hex": HexViewer, "markdown": MarkdownViewer,
                           "table": TableViewer, "editor": EditorViewer}
        self.stack = QStackedWidget()
        self._empty = QLabel("Select a file to preview it", objectName="empty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self._empty)
        self.title = QLabel(objectName="title")
        self.edit_button = QPushButton("Edit")
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self.edit)
        self.open_external = QPushButton("Open externally")
        self.open_external.setEnabled(False)
        self.open_external.clicked.connect(self._open_externally)

        bar = QHBoxLayout()
        bar.addWidget(self.title, 1)
        bar.addWidget(self.edit_button)
        bar.addWidget(self.open_external)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)
        layout.addLayout(bar)
        layout.addWidget(self.stack, 1)
        self.current: Optional[Viewer] = None
        self.entry: Optional[FileEntry] = None
        self._request = 0

    def viewer(self, kind: str) -> Viewer:
        viewer = self._viewers.get(kind)
        if viewer is None:
            viewer = self._factories[kind](self._context)
            self._viewers[kind] = viewer
            if kind == "editor":
                viewer.closed.connect(self._editor_closed)
            self.stack.addWidget(viewer)
        return viewer

    @property
    def editor(self) -> Optional[EditorViewer]:
        return self.current if isinstance(self.current, EditorViewer) else None

    def leave_editor(self, then, otherwise=lambda: None) -> None:
        """Run ``then`` now, or once unsaved edits have been dealt with;
        ``otherwise`` if the user chooses to keep editing."""
        if self.editor is not None:
            self.editor.leave(then, otherwise)
        else:
            then()

    def open(self, entry: FileEntry) -> None:
        if self.entry is not None and entry.path == self.entry.path and self.editor is not None:
            return
        self.leave_editor(lambda: self._open(entry), self.stayed.emit)

    def edit(self) -> None:
        if self.entry is not None and self.editor is None:
            self.edit_button.setEnabled(False)
            self._show("editor", self.entry)

    def _editor_closed(self) -> None:
        entry = self.editor.entry
        self.editor.close_file()
        self.current = None
        self._open(entry)

    def _open(self, entry: FileEntry) -> None:
        self._request += 1
        self.entry = entry
        self.title.setText(display_text(entry.path))
        self.open_external.setEnabled(True)
        kind = kind_for_name(entry.name)
        if kind == "markdown" and entry.size > MAX_MARKDOWN_BYTES:
            kind = "text"
        if kind is not None:
            self._show(kind, entry)
            return
        if entry.size == 0:
            self._show("text", entry)
            return
        # Unknown type: the first bytes decide.
        request = self._request

        def sniffed(data: bytes) -> None:
            if request == self._request:
                self._show("hex" if looks_binary(data) else "text", entry)

        def failed(exc: Exception) -> None:
            if request == self._request:
                self.message.emit(f"Cannot read {display_text(entry.name)}: {exc}")
        self._context.bridge.then(self._context.backend.read(entry.path, 0, SNIFF_BYTES),
                                  sniffed, failed)

    def _show(self, kind: str, entry: FileEntry) -> None:
        viewer = self.viewer(kind)
        if self.current is not None:
            self.current.close_file()
        self.current = viewer
        self.stack.setCurrentWidget(viewer)
        viewer.open(entry)
        self.edit_button.setEnabled(self._editable(kind, entry))

    @staticmethod
    def _editable(kind: str, entry: FileEntry) -> bool:
        is_csv = posixpath.splitext(entry.name)[1].lower() in CSV_SUFFIXES
        return entry.size <= MAX_EDIT_BYTES and (kind in ("text", "markdown") or is_csv)

    def clear(self) -> None:
        self._request += 1
        if self.current is not None:
            self.current.close_file()
        self.current = self.entry = None
        self.title.setText("")
        self.edit_button.setEnabled(False)
        self.open_external.setEnabled(False)
        self.stack.setCurrentWidget(self._empty)

    def on_events(self, events: list[Event]) -> None:
        if self.current is None or self.entry is None:
            return
        mine = [e for e in events if e.path == self.entry.path
                or (e.is_dir and e.path == posixpath.dirname(self.entry.path))]
        if mine:
            self.current.on_events(mine)

    def _open_externally(self) -> None:
        entry = self.entry
        if entry is None:
            return
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        digest = hashlib.sha256(entry.path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
        folder = os.path.join(base, "external", digest)
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, display_text(entry.name).replace("�", "_"))
        self.message.emit(f"Downloading {display_text(entry.name)}…")
        self._context.bridge.then(
            self._context.backend.download(entry.path, target),
            lambda path: (self.message.emit(""),
                          QDesktopServices.openUrl(QUrl.fromLocalFile(path))),
            lambda exc: self.message.emit(f"Download failed: {exc}"))
