"""Virtualized text and log viewer: renders only visible rows, follows appends."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication,
                           QKeySequence, QPainter, QPalette)
from PySide6.QtWidgets import (QAbstractScrollArea, QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from app.backend import Event, EventKind, FileEntry

from . import ansi, syntax
from .base import Viewer
from .log_buffer import LogBuffer

TAIL_CHUNK = 1024 * 1024
SCROLL_RANGE = 1 << 30
MAX_ROW_CHARS = 8192
MAX_COPY_BYTES = 16 * 1024 * 1024
HIGHLIGHT = QColor(255, 210, 60, 150)
TAB_WIDTH = 8
LOG_SUFFIXES = (".log", ".out", ".err")
RECENT_SECONDS = 300


def is_live_log(entry: FileEntry) -> bool:
    name = entry.name.lower()
    if name.endswith(LOG_SUFFIXES) or ".log." in name:
        return True
    # A source file saved a minute ago is still something to read from the top.
    return syntax.lexer_for_name(entry.name) is None and time.time() - entry.mtime < RECENT_SECONDS


@dataclass
class Row:
    offset: int  # byte offset of the row's first character
    text: str
    spans: list[ansi.Span]


class LogView(QAbstractScrollArea):
    """Scrolls by byte offset. ``top`` is the first visible byte."""

    user_scrolled = Signal()

    def __init__(self, buffer: LogBuffer, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("logView")
        self.buffer = buffer
        self.top = 0
        self.wrap = False
        self.colors = True
        self.needle = ""
        self.follow_end = False
        self.syntax: dict[int, list[ansi.Span]] = {}
        self._rows: list[Row] = []
        self._selection: Optional[tuple[int, int]] = None
        self._dragging = False
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_metrics()
        self.verticalScrollBar().setRange(0, SCROLL_RANGE)
        self.verticalScrollBar().setPageStep(SCROLL_RANGE // 50)
        self.verticalScrollBar().actionTriggered.connect(self._scrollbar_action)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.viewport().update())

    def _apply_metrics(self) -> None:
        metrics = QFontMetricsF(self.font())
        self._row_height = max(round(metrics.lineSpacing()), 1)
        # Fractional on purpose: each coloured span starts at column * width, so
        # a rounded width would make neighbouring spans overlap or gap.
        self._char_width = max(metrics.horizontalAdvance("M" * 100) / 100, 1.0)
        self._ascent = metrics.ascent()

    # Geometry ----------------------------------------------------------------
    @property
    def visible_rows(self) -> int:
        return max(self.viewport().height() // self._row_height, 1)

    @property
    def columns(self) -> int:
        return max(int(self.viewport().width() / self._char_width), 8)

    # Layout of rows ----------------------------------------------------------
    def _layout(self) -> list[Row]:
        rows: list[Row] = []
        wanted = self.visible_rows + 1
        lines, _ = self.buffer.lines_from(self.top, wanted)
        for start, raw in lines:
            text = raw.rstrip(b"\r").decode("utf-8", "replace").expandtabs(TAB_WIDTH)
            spans = self._spans(start, text)
            plain = "".join(s.text for s in spans)
            if self.wrap and len(plain) > self.columns:
                rows.extend(self._wrapped(start, spans, plain))
            else:
                rows.append(Row(start, plain[:MAX_ROW_CHARS], spans))
            if len(rows) >= wanted:
                break
        return rows[:wanted]

    def _spans(self, start: int, text: str) -> list[ansi.Span]:
        if not self.colors:
            return [ansi.Span(text, None, False)]
        known = self.syntax.get(start)
        # The file may have changed since it was tokenized: stale colours on the
        # wrong text would mislead, so such a line stays plain until the next pass.
        if known is not None and "".join(s.text for s in known) == text:
            return known
        return ansi.parse(text)

    def _wrapped(self, start: int, spans: list[ansi.Span], plain: str) -> list[Row]:
        width, rows = self.columns, []
        limit = min(len(plain), width * (self.visible_rows + 1))
        for column in range(0, limit, width):
            segment = _slice_spans(spans, column, column + width)
            offset = start + len(plain[:column].encode("utf-8", "replace"))
            rows.append(Row(offset, plain[column:column + width], segment))
        return rows

    def paintEvent(self, _event) -> None:
        painter = QPainter(self.viewport())
        palette = self.palette()
        painter.fillRect(self.viewport().rect(), palette.color(QPalette.ColorRole.Base))
        text_color = palette.color(QPalette.ColorRole.Text)
        self._rows = self._layout()
        x_origin = 2 - self.horizontalScrollBar().value() * self._char_width
        widest = 0
        for number, row in enumerate(self._rows):
            y = number * self._row_height
            widest = max(widest, len(row.text))
            self._paint_selection(painter, row, x_origin, y)
            self._paint_matches(painter, row, x_origin, y)
            x = x_origin
            for span in row.spans:
                if span.bold != painter.font().bold():
                    font = painter.font()
                    font.setBold(span.bold)
                    painter.setFont(font)
                painter.setPen(QColor(span.color) if span.color else text_color)
                painter.drawText(QPointF(x, y + self._ascent), span.text[:MAX_ROW_CHARS])
                x += len(span.text) * self._char_width
        if not self._rows and self.buffer.size:
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, "Loading…")
        painter.end()
        bar = self.horizontalScrollBar()
        bar.setRange(0, 0 if self.wrap else max(widest - self.columns + 2, 0))
        bar.setPageStep(self.columns)
        self._sync_scrollbar()

    def _paint_matches(self, painter: QPainter, row: Row, x_origin: int, y: int) -> None:
        if not self.needle:
            return
        haystack, needle = row.text.lower(), self.needle.lower()
        position = haystack.find(needle)
        while position >= 0:
            painter.fillRect(QRectF(x_origin + position * self._char_width, y,
                                    len(needle) * self._char_width, self._row_height), HIGHLIGHT)
            position = haystack.find(needle, position + len(needle))

    def _paint_selection(self, painter: QPainter, row: Row, x_origin: int, y: int) -> None:
        if not self._selection:
            return
        low, high = sorted(self._selection)
        row_end = row.offset + len(row.text.encode("utf-8", "replace"))
        if high <= row.offset or low > row_end:
            return
        first = self._column_of(row, max(low, row.offset))
        last = self._column_of(row, min(high, row_end))
        color = self.palette().color(QPalette.ColorRole.Highlight)
        color.setAlpha(90)  # text is painted over it in its own colours
        painter.fillRect(QRectF(x_origin + first * self._char_width, y,
                                max(last - first, 1) * self._char_width, self._row_height), color)

    @staticmethod
    def _column_of(row: Row, offset: int) -> int:
        encoded = row.text.encode("utf-8", "replace")[:offset - row.offset]
        return len(encoded.decode("utf-8", "ignore"))

    # Scrolling ---------------------------------------------------------------
    def _sync_scrollbar(self) -> None:
        bar = self.verticalScrollBar()
        if not bar.isSliderDown() and self.buffer.size:
            bar.blockSignals(True)
            bar.setValue(int(self.top / self.buffer.size * SCROLL_RANGE))
            bar.blockSignals(False)

    def _scrollbar_action(self, action: int) -> None:
        slider = self.verticalScrollBar()
        if action == slider.SliderAction.SliderSingleStepAdd:
            self.scroll_lines(1)
        elif action == slider.SliderAction.SliderSingleStepSub:
            self.scroll_lines(-1)
        elif action == slider.SliderAction.SliderPageStepAdd:
            self.scroll_lines(self.visible_rows - 1)
        elif action == slider.SliderAction.SliderPageStepSub:
            self.scroll_lines(-(self.visible_rows - 1))
        elif action in (slider.SliderAction.SliderMove, slider.SliderAction.SliderToMaximum,
                        slider.SliderAction.SliderToMinimum):
            position = slider.sliderPosition()
            if position >= SCROLL_RANGE:
                self.scroll_to_end()
            else:
                self.jump_to(int(position / SCROLL_RANGE * self.buffer.size))
            self.user_scrolled.emit()
            return
        self.user_scrolled.emit()

    def jump_to(self, offset: int) -> None:
        start = self.buffer.line_start(offset)
        self.top = start if start is not None else offset
        self._retry_if_missing(start is None, lambda: self.jump_to(offset))
        self.viewport().update()

    def scroll_to_end(self) -> None:
        position = self.buffer.size
        if position and self.buffer.get(position - 1, position) == b"\n":
            position -= 1  # the empty "line" after a trailing newline
        top = self.buffer.line_start(position)
        if top is not None:
            top = self.buffer.back_lines(top, self.visible_rows - 1)
        if top is not None:
            self.top = top
        self.viewport().update()

    def scroll_lines(self, delta: int) -> None:
        if delta > 0:
            for _ in range(delta):
                self._step_down()
        else:
            for _ in range(-delta):
                self._step_up()
        self.viewport().update()

    def _step_down(self) -> None:
        lines, _ = self.buffer.lines_from(self.top, 2)
        if not lines:
            return
        length = len(lines[0][1])
        if self.wrap and length > self.columns:
            self.top += self.columns
        elif len(lines) > 1:
            self.top = lines[1][0]

    def _step_up(self) -> None:
        start = self.buffer.line_start(self.top)
        if start is None:
            return
        if start < self.top:
            self.top = max(start, self.top - self.columns) if self.wrap else start
            return
        previous = self.buffer.previous_line(self.top)
        if previous is not None:
            self.top = previous

    def _retry_if_missing(self, missing: bool, retry) -> None:
        if missing:
            QTimer.singleShot(30, retry)

    def wheelEvent(self, event) -> None:
        rows = -round(event.angleDelta().y() / 40)
        if rows:
            self.scroll_lines(rows)
            self.user_scrolled.emit()
        steps = event.angleDelta().x()
        if steps:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - steps // 20)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
        elif key == Qt.Key.Key_Down:
            self.scroll_lines(1)
        elif key == Qt.Key.Key_Up:
            self.scroll_lines(-1)
        elif key == Qt.Key.Key_PageDown:
            self.scroll_lines(self.visible_rows - 1)
        elif key == Qt.Key.Key_PageUp:
            self.scroll_lines(-(self.visible_rows - 1))
        elif key == Qt.Key.Key_Home:
            self.jump_to(0)
        elif key == Qt.Key.Key_End:
            self.scroll_to_end()
        else:
            super().keyPressEvent(event)
            return
        self.user_scrolled.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.follow_end:
            self.scroll_to_end()

    # Selection ---------------------------------------------------------------
    def _offset_at(self, point: QPoint) -> Optional[int]:
        if not self._rows:
            return None
        row = self._rows[min(max(point.y() // self._row_height, 0), len(self._rows) - 1)]
        column = max(int((point.x() - 2) / self._char_width) + self.horizontalScrollBar().value(), 0)
        return row.offset + len(row.text[:column].encode("utf-8", "replace"))

    def mousePressEvent(self, event) -> None:
        offset = self._offset_at(event.position().toPoint())
        self._selection = (offset, offset) if offset is not None else None
        self._dragging = offset is not None
        self.viewport().update()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            offset = self._offset_at(event.position().toPoint())
            if offset is not None:
                self._selection = (self._selection[0], offset)
                self.viewport().update()

    def mouseReleaseEvent(self, _event) -> None:
        self._dragging = False

    def copy_selection(self) -> None:
        if not self._selection:
            return
        low, high = sorted(self._selection)
        data = self.buffer.get(low, min(high, low + MAX_COPY_BYTES))
        if data:
            text = data.decode("utf-8", "replace")
            QGuiApplication.clipboard().setText(ansi.strip(text) if self.colors else text)


def _slice_spans(spans: list[ansi.Span], start: int, end: int) -> list[ansi.Span]:
    result, position = [], 0
    for span in spans:
        stop = position + len(span.text)
        if stop > start and position < end:
            piece = span.text[max(start - position, 0):end - position]
            result.append(ansi.Span(piece, span.color, span.bold))
        position = stop
    return result


class TextLogViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self.buffer = LogBuffer(self._read_block)
        self.view = LogView(self.buffer)
        self.view.user_scrolled.connect(self._on_user_scroll)

        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        self.follow.toggled.connect(self._follow_toggled)
        self.pause = QCheckBox("Pause")
        self.pause.toggled.connect(self._pause_toggled)
        self.wrap = QCheckBox("Wrap")
        self.wrap.toggled.connect(self._wrap_toggled)
        self.colors = QCheckBox("Colors")
        self.colors.setChecked(True)
        self.colors.toggled.connect(self._colors_toggled)
        self.search = QLineEdit(placeholderText="Search file…", clearButtonEnabled=True)
        self.search.returnPressed.connect(self._search_or_next)
        self.search.textChanged.connect(self._search_changed)
        self.previous_hit = QPushButton("◀")
        self.next_hit = QPushButton("▶")
        self.previous_hit.setFixedWidth(32)
        self.next_hit.setFixedWidth(32)
        self.previous_hit.clicked.connect(lambda: self._step_hit(-1))
        self.next_hit.clicked.connect(lambda: self._step_hit(1))
        self.status = QLabel()
        self.language = QLabel()

        bar = QHBoxLayout()
        for widget in (self.follow, self.pause, self.wrap, self.colors):
            bar.addWidget(widget)
        bar.addWidget(self.search, 1)
        for widget in (self.previous_hit, self.next_hit, self.language, self.status):
            bar.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

        self._tail_running = False
        self._tail_again = False
        self._hits: list[int] = []
        self._hit = -1
        self._searched = ""
        self._highlighter = ThreadPoolExecutor(1, thread_name_prefix="syntax")
        self._highlighting = False
        self._highlight_again = False
        self._live = False

    # Opening -----------------------------------------------------------------
    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self.buffer.reset(entry.size)
        self.view.top = 0
        self.view.follow_end = False
        self._tail_again = False
        self._clear_hits()
        self.view.viewport().update()
        self._update_status()
        # Logs open at the end, ready to follow; other text from the top.
        follow = self._live = is_live_log(entry)
        self.follow.blockSignals(True)
        self.follow.setChecked(follow)
        self.follow.blockSignals(False)
        self.view.follow_end = follow
        if follow:
            self.view.scroll_to_end()
        self._clear_syntax()
        self._highlight()

    def close_file(self) -> None:
        super().close_file()
        self.buffer.reset()
        self._clear_syntax()
        self.view.viewport().update()

    # Syntax colours ----------------------------------------------------------
    def _clear_syntax(self) -> None:
        self.view.syntax = {}
        self.language.setText("")
        self._highlighting = self._highlight_again = False

    def _highlight(self) -> None:
        entry = self.entry
        if (entry is None or not self.colors.isChecked() or entry.size == 0
                or entry.size > syntax.MAX_HIGHLIGHT_BYTES or not syntax.worth_fetching(entry.name)):
            return
        if self._highlighting:
            self._highlight_again = True
            return
        self._highlighting = True
        current = self._current()
        dark = self.palette().color(QPalette.ColorRole.Base).lightness() < 128

        def fetched(data: bytes) -> None:
            if current():
                self.bridge.then(
                    self._highlighter.submit(syntax.highlight, entry.name, data, dark),
                    lambda result: current() and self._highlighted(result), failed)

        def failed(_exc: Exception) -> None:
            if current():
                self._highlighted(None)  # plain text is still a fine view
        self.bridge.then(self.backend.fetch(entry.path), fetched, failed)

    def _highlighted(self, result: Optional[syntax.Highlighted]) -> None:
        self._highlighting = False
        if result is not None:
            self.view.syntax = result.lines
            self.language.setText(result.language)
            self.view.viewport().update()
        if self._highlight_again:
            self._highlight_again = False
            self._highlight()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # Light and dark themes use different colours.
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "entry", None) is not None:
            self._highlight()

    def _read_block(self, offset: int, length: int) -> None:
        if self.entry is None:
            return
        current = self._current()

        def done(data: bytes) -> None:
            if current():
                self.buffer.store(offset, data)
                self._refresh()

        def failed(exc: Exception) -> None:
            if current():
                self.buffer.fail(offset)
                self.status.setText(f"Read failed: {exc}")
        self.bridge.then(self.backend.read(self.entry.path, offset, length), done, failed)

    def _refresh(self) -> None:
        if self.view.follow_end:
            self.view.scroll_to_end()
        self.view.viewport().update()

    # Live updates ------------------------------------------------------------
    def on_events(self, events: list[Event]) -> None:
        kinds = {e.kind for e in events}
        if kinds & {EventKind.CLOSE_WRITE, EventKind.MOVED_TO, EventKind.RESYNC}:
            self.bridge.then(self.backend.stat(self.entry.path), self._file_closed)
        if kinds & {EventKind.DELETE, EventKind.MOVED_FROM}:
            self.status.setText("File removed; waiting for it to return…")
        if kinds & {EventKind.CREATE, EventKind.MOVED_TO}:
            self._restart()  # rotated: a new file now has this name
        elif kinds & {EventKind.MODIFY, EventKind.CLOSE_WRITE, EventKind.RESYNC}:
            self._tail()

    def _file_closed(self, entry: FileEntry) -> None:
        if self.entry is None or entry.path != self.entry.path:
            return
        self.entry = entry
        if not self._live:
            # Unlike a log, this may have been rewritten anywhere, not appended to.
            self._generation += 1  # orphans reads of the old content
            self._tail_running = self._highlighting = False
            self.buffer.reset(entry.size)
            self.view.jump_to(min(self.view.top, entry.size))
            self._update_status()
        self._highlight()

    def _restart(self) -> None:
        self._highlighting = False  # the bump below orphans any pass in flight
        self.buffer.reset()
        self.view.top = 0
        self._generation += 1
        self._tail_running = False
        self._tail()

    def _tail(self) -> None:
        if self.entry is None or self.pause.isChecked():
            return
        if self._tail_running:
            self._tail_again = True
            return
        self._tail_running = True
        current = self._current()
        path = self.entry.path
        offset = self.buffer.size
        # Stat and read go out together: new bytes cost one round trip.
        stat_future = self.backend.stat(path)
        read_future = self.backend.read(path, offset, TAIL_CHUNK)

        def stat_done(entry: FileEntry) -> None:
            def read_done(data: bytes) -> None:
                if current():
                    self._tail_finished(offset, entry, data)
            self.bridge.then(read_future, read_done, lambda _e: current() and self._tail_failed())
        self.bridge.then(stat_future, stat_done, lambda _e: current() and self._tail_failed())

    def _tail_failed(self) -> None:
        self._tail_running = False

    def _tail_finished(self, offset: int, entry: FileEntry, data: bytes) -> None:
        self._tail_running = False
        if entry.size < offset:
            self._restart()  # truncated
            return
        self.buffer.size = max(entry.size, offset + len(data))
        if data:
            self.buffer.store(offset, data)
        self._update_status()
        self._refresh()
        if self._tail_again or offset + len(data) < entry.size:
            self._tail_again = False
            self._tail()

    # Controls ----------------------------------------------------------------
    def _on_user_scroll(self) -> None:
        if self.view.follow_end:
            lines, complete = self.buffer.lines_from(self.view.top, self.view.visible_rows + 1)
            if complete and len(lines) > self.view.visible_rows:
                self.follow.setChecked(False)

    def _follow_toggled(self, checked: bool) -> None:
        self.view.follow_end = checked
        if checked:
            self.view.scroll_to_end()

    def _pause_toggled(self, paused: bool) -> None:
        if not paused:
            self._tail()

    def _wrap_toggled(self, checked: bool) -> None:
        self.view.wrap = checked
        self.view.viewport().update()

    def _colors_toggled(self, checked: bool) -> None:
        self.view.colors = checked
        if checked and not self.view.syntax:
            self._highlight()
        self.view.viewport().update()

    def _update_status(self) -> None:
        if self._hits:
            self.status.setText(f"{self._hit + 1}/{len(self._hits)}")
        elif self._searched:
            self.status.setText("No matches")
        else:
            from app.models.file_list_model import format_size
            self.status.setText(format_size(self.buffer.size))

    # Search ------------------------------------------------------------------
    def _clear_hits(self) -> None:
        self._hits, self._hit, self._searched = [], -1, ""
        self.view.needle = ""

    def _search_changed(self, _text: str) -> None:
        self._clear_hits()
        self._update_status()
        self.view.viewport().update()

    def _search_or_next(self) -> None:
        needle = self.search.text()
        if not needle or self.entry is None:
            return
        if needle == self._searched and self._hits:
            self._step_hit(1)
            return
        self.status.setText("Searching…")
        current = self._current()

        def done(offsets: list[int]) -> None:
            if not current() or self.search.text() != needle:
                return
            self._hits, self._searched = offsets, needle
            self.view.needle = needle
            self._hit = -1
            if offsets:
                self.follow.setChecked(False)
                # Start from what's on screen rather than the top of the file.
                self._hit = next((i - 1 for i, o in enumerate(offsets) if o >= self.view.top),
                                 len(offsets) - 1)
                self._step_hit(1)
            self._update_status()

        def failed(exc: Exception) -> None:
            if current():
                self.status.setText(f"Search failed: {exc}")
        self.bridge.then(self.backend.grep(self.entry.path, needle), done, failed)

    def _step_hit(self, delta: int) -> None:
        if not self._hits:
            return
        self.follow.setChecked(False)
        self._hit = (self._hit + delta) % len(self._hits)
        self.view.jump_to(self._hits[self._hit])
        self._update_status()
