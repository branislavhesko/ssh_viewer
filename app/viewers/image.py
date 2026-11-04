"""Image viewer with window/level, zoom/pan and multi-page support."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (QDoubleSpinBox, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
                               QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout)

from app.backend import Event, EventKind, FileEntry

from . import image_decode
from .base import Viewer

ZOOM_STEP = 1.2


class ImageCanvas(QGraphicsView):
    def __init__(self, on_hover):
        super().__init__()
        self._on_hover = on_hover
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setMouseTracking(True)
        self.fit_mode = True

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Keeps the transform and scroll position, so reloads don't jump."""
        self.item.setPixmap(pixmap)
        self.scene().setSceneRect(self.item.boundingRect())
        if self.fit_mode:
            self.fit()

    def fit(self) -> None:
        self.fit_mode = True
        self.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

    def actual_size(self) -> None:
        self.fit_mode = False
        self.setTransform(QTransform())

    def wheelEvent(self, event) -> None:
        self.fit_mode = False
        factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
        self.scale(factor, factor)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit()

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        point: QPointF = self.mapToScene(event.position().toPoint())
        self._on_hover(int(point.x()), int(point.y()))


class ImageViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self._decoder = ThreadPoolExecutor(1, thread_name_prefix="image-decode")
        self.canvas = ImageCanvas(self._hover)
        self.low = self._level_box()
        self.high = self._level_box()
        self.page = QSpinBox(minimum=1, maximum=1, prefix="Page ")
        self.page.valueChanged.connect(self._show_page)
        self.pixel = QLabel()
        self.status = QLabel()

        auto = QPushButton("Auto")
        auto.clicked.connect(self._auto_levels)
        full = QPushButton("Full range")
        full.clicked.connect(self._full_levels)
        fit = QPushButton("Fit")
        fit.clicked.connect(self.canvas.fit)
        actual = QPushButton("100%")
        actual.clicked.connect(self.canvas.actual_size)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Min"))
        bar.addWidget(self.low)
        bar.addWidget(QLabel("Max"))
        bar.addWidget(self.high)
        for widget in (auto, full, self.page, fit, actual):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self.pixel)
        bar.addWidget(self.status)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.canvas, 1)

        self._pages: list[np.ndarray] = []
        self._levels_touched = False
        self._loading = False
        self._reload_again = False

    def _level_box(self) -> QDoubleSpinBox:
        box = QDoubleSpinBox(decimals=3, minimum=-1e12, maximum=1e12)
        box.setKeyboardTracking(False)
        box.valueChanged.connect(self._levels_edited)
        return box

    # Loading -----------------------------------------------------------------
    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self._levels_touched = False
        self._pages = []
        self.canvas.fit_mode = True
        self.canvas.item.setPixmap(QPixmap())
        self._loading = False
        self._load()

    def close_file(self) -> None:
        super().close_file()
        self._pages = []
        self.canvas.item.setPixmap(QPixmap())

    def on_events(self, events: list[Event]) -> None:
        # Never on MODIFY: that would read a half-written file.
        if any(e.kind in (EventKind.CLOSE_WRITE, EventKind.MOVED_TO, EventKind.RESYNC)
               for e in events):
            self._load()

    def _load(self) -> None:
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
                self.bridge.then(self._decoder.submit(image_decode.decode, data),
                                 lambda decoded: current() and self._decoded(decoded), failed)

        def failed(exc: Exception) -> None:
            if current():
                self._finished()
                # Keep what's on screen: the next close_write brings a good one.
                self.status.setText(f"Could not read image: {exc}")
        self.bridge.then(self.backend.fetch(self.entry.path), fetched, failed)

    def _finished(self) -> None:
        self._loading = False
        if self._reload_again:
            self._reload_again = False
            self._load()

    def _decoded(self, decoded: image_decode.Decoded) -> None:
        self._pages = decoded.pages
        self.page.blockSignals(True)
        self.page.setMaximum(len(self._pages))
        self.page.setVisible(len(self._pages) > 1)
        self.page.blockSignals(False)
        self.status.setText("")
        self._show_page()
        self._finished()

    # Display -----------------------------------------------------------------
    @property
    def array(self) -> Optional[np.ndarray]:
        index = self.page.value() - 1
        return self._pages[index] if index < len(self._pages) else None

    def _show_page(self) -> None:
        array = self.array
        if array is None:
            return
        if not self._levels_touched:
            low, high = (image_decode.value_range(array) if array.dtype == np.uint8
                         else image_decode.percentile_range(array))
            self._set_levels(low, high)
        self._render()

    def _set_levels(self, low: float, high: float) -> None:
        for box, value in ((self.low, low), (self.high, high)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)

    def _render(self) -> None:
        array = self.array
        if array is not None:
            image = image_decode.to_qimage(array, self.low.value(), self.high.value())
            self.canvas.set_pixmap(QPixmap.fromImage(image))

    def _levels_edited(self) -> None:
        self._levels_touched = True
        self._render()

    def _auto_levels(self) -> None:
        if self.array is not None:
            self._levels_touched = True
            self._set_levels(*image_decode.percentile_range(self.array))
            self._render()

    def _full_levels(self) -> None:
        if self.array is not None:
            self._levels_touched = True
            self._set_levels(*image_decode.value_range(self.array))
            self._render()

    def _hover(self, x: int, y: int) -> None:
        array = self.array
        if array is not None and 0 <= y < array.shape[0] and 0 <= x < array.shape[1]:
            self.pixel.setText(f"({x}, {y}) = {array[y, x]}")
