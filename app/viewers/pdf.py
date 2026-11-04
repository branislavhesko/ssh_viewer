"""PDF viewer over a fully fetched file; reloads keep page and zoom."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout

from app.backend import Event, EventKind, FileEntry

from .base import Viewer


class PdfViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self.document = QPdfDocument(self)
        self.view = QPdfView()
        self.view.setDocument(self.document)
        self.view.setPageMode(QPdfView.PageMode.MultiPage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.view.pageNavigator().currentPageChanged.connect(self._page_changed)

        self.page = QSpinBox(minimum=1, maximum=1, prefix="Page ")
        self.page.valueChanged.connect(self._go_to_page)
        self.pages = QLabel()
        self.status = QLabel()
        zoom_in, zoom_out, fit = QPushButton("+"), QPushButton("−"), QPushButton("Fit width")
        zoom_in.clicked.connect(lambda: self._zoom(1.25))
        zoom_out.clicked.connect(lambda: self._zoom(0.8))
        fit.clicked.connect(lambda: self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth))

        bar = QHBoxLayout()
        for widget in (self.page, self.pages, zoom_out, zoom_in, fit):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self.status)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

        self._buffer: Optional[QBuffer] = None
        self._loading = False
        self._reload_again = False

    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self._loading = False
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self._load(restore=False)

    def close_file(self) -> None:
        super().close_file()
        self.document.close()

    def on_events(self, events: list[Event]) -> None:
        if any(e.kind in (EventKind.CLOSE_WRITE, EventKind.MOVED_TO, EventKind.RESYNC)
               for e in events):
            self._load(restore=True)

    def _load(self, restore: bool) -> None:
        if self.entry is None:
            return
        if self._loading:
            self._reload_again = True
            return
        self._loading = True
        self.status.setText("Loading…")
        current = self._current()

        def fetched(data: bytes) -> None:
            if current():
                self._show(data, restore)
                self._finished()

        def failed(exc: Exception) -> None:
            if current():
                self.status.setText(f"Could not read PDF: {exc}")
                self._finished()
        self.bridge.then(self.backend.fetch(self.entry.path), fetched, failed)

    def _finished(self) -> None:
        self._loading = False
        if self._reload_again:
            self._reload_again = False
            self._load(restore=True)

    def _show(self, data: bytes, restore: bool) -> None:
        page = self.view.pageNavigator().currentPage()
        zoom_mode, zoom = self.view.zoomMode(), self.view.zoomFactor()
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.document.load(buffer)
        if self._buffer is not None:
            self._buffer.deleteLater()
        self._buffer = buffer  # the document reads from it for as long as it's open
        if self.document.status() != QPdfDocument.Status.Ready:
            self.status.setText(f"Could not open PDF: {self.document.error().name}")
            return
        self.status.setText("")
        count = self.document.pageCount()
        self.page.setMaximum(max(count, 1))
        self.pages.setText(f"of {count}")
        if restore:
            self.view.setZoomMode(zoom_mode)
            if zoom_mode == QPdfView.ZoomMode.Custom:
                self.view.setZoomFactor(zoom)
            self.view.pageNavigator().jump(min(page, max(count - 1, 0)), QPointF(), 0)

    def _page_changed(self, page: int) -> None:
        self.page.blockSignals(True)
        self.page.setValue(page + 1)
        self.page.blockSignals(False)

    def _go_to_page(self, value: int) -> None:
        self.view.pageNavigator().jump(value - 1, QPointF(), 0)

    def _zoom(self, factor: float) -> None:
        current = self.view.zoomFactor()
        self.view.setZoomMode(QPdfView.ZoomMode.Custom)
        self.view.setZoomFactor(current * factor)
