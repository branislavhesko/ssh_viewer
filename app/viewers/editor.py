"""Editing a remote text file.

Saving must not change anything the user didn't change, so a file only opens for
editing if it survives the trip through the text widget byte for byte; and must
not overwrite someone else's work, so the server copy is compared first.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFontDatabase, QKeySequence, QPainter, QPalette, QShortcut,
                           QSyntaxHighlighter, QTextBlockUserData, QTextCharFormat, QTextCursor,
                           QTextFormat)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QTextEdit, QVBoxLayout, QWidget)

from app.backend import Event, EventKind, FileEntry

from . import syntax
from .ansi import Span
from .base import Viewer

MAX_EDIT_BYTES = 8 * 1024 * 1024
# Above this, recolouring after each pause would make typing lag.
MAX_LIVE_HIGHLIGHT_BYTES = 512 * 1024
HIGHLIGHT_DELAY_MS = 300
TAB_STOP_COLUMNS = 4
BOM = b"\xef\xbb\xbf"
BREAKPOINT_COLOR = "#e5484d"
EXECUTION_COLOR = "#f5c518"
PARAGRAPH = " "  # how Qt's raw text separates blocks


class TextCodec:
    """Turns file bytes into editor text and back, remembering what the widget
    can't hold: the line ending and the byte order mark."""

    def __init__(self, data: bytes):
        self.bom = data.startswith(BOM)
        self.crlf = b"\r\n" in data

    def decode(self, data: bytes) -> str:
        body = data[len(BOM):] if self.bom else data
        text = body.decode("utf-8")
        return text.replace("\r\n", "\n") if self.crlf else text

    def encode(self, raw_text: str) -> bytes:
        text = raw_text.replace(PARAGRAPH, "\n")
        if self.crlf:
            text = text.replace("\n", "\r\n")
        return (BOM if self.bom else b"") + text.encode("utf-8")


class Highlighter(QSyntaxHighlighter):
    """Colours come from a whole-file pass, so a block only takes them if its
    text still matches; the next pass catches up after an edit."""

    def __init__(self, document):
        super().__init__(document)
        self.lines: list[list[Span]] = []
        self._formats: dict[tuple, QTextCharFormat] = {}

    def _format(self, span: Span) -> QTextCharFormat:
        key = (span.color, span.bold)
        made = self._formats.get(key)
        if made is None:
            made = self._formats[key] = QTextCharFormat()
            if span.color:
                made.setForeground(QColor(span.color))
            if span.bold:
                made.setFontWeight(700)
        return made

    def highlightBlock(self, text: str) -> None:
        number = self.currentBlock().blockNumber()
        spans = self.lines[number] if number < len(self.lines) else None
        if not spans or "".join(s.text for s in spans) != text:
            return
        position = 0
        for span in spans:
            # Qt counts UTF-16 units, so an emoji is two.
            length = len(span.text) if span.text.isascii() else len(span.text.encode("utf-16-le")) // 2
            if span.color or span.bold:
                self.setFormat(position, length, self._format(span))
            position += length


class Breakpoint(QTextBlockUserData):
    """Marks a block. Block data moves with the line as text is edited above
    it, which line numbers would not."""


class LineNumbers(QWidget):
    def __init__(self, editor: "CodeEdit"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor.paint_gutter(event)

    def mousePressEvent(self, event) -> None:
        if self._editor.breakpoints_enabled and event.button() == Qt.MouseButton.LeftButton:
            block = self._editor.cursorForPosition(event.position().toPoint()).block()
            self._editor.toggle_breakpoint(block.blockNumber() + 1)


class CodeEdit(QPlainTextEdit):
    breakpoints_toggled = Signal()

    def __init__(self):
        super().__init__()
        self.breakpoints_enabled = False
        self.execution_line = 0  # 1-based; 0 for none
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * TAB_STOP_COLUMNS)
        self._gutter = LineNumbers(self)
        self.blockCountChanged.connect(self._fit_gutter)
        self.updateRequest.connect(self._scroll_gutter)
        self.cursorPositionChanged.connect(self._mark_line)
        self._fit_gutter()

    def gutter_width(self) -> int:
        digits = len(str(max(self.blockCount(), 1)))
        marker = self.fontMetrics().height() if self.breakpoints_enabled else 0
        return 12 + marker + self.fontMetrics().horizontalAdvance("9") * max(digits, 3)

    def set_breakpoints_enabled(self, enabled: bool) -> None:
        self.breakpoints_enabled = enabled
        self._fit_gutter()
        self._gutter.update()

    # Breakpoints -------------------------------------------------------------
    def breakpoints(self) -> set[int]:
        lines, block = set(), self.document().firstBlock()
        while block.isValid():
            if isinstance(block.userData(), Breakpoint):
                lines.add(block.blockNumber() + 1)
            block = block.next()
        return lines

    def set_breakpoints(self, lines: set[int]) -> None:
        block = self.document().firstBlock()
        while block.isValid():
            block.setUserData(Breakpoint() if block.blockNumber() + 1 in lines else None)
            block = block.next()
        self._gutter.update()

    def toggle_breakpoint(self, line: int) -> None:
        block = self.document().findBlockByNumber(line - 1)
        if block.isValid():
            block.setUserData(None if isinstance(block.userData(), Breakpoint) else Breakpoint())
            self._gutter.update()
            self.breakpoints_toggled.emit()

    def set_execution_line(self, line: int) -> None:
        self.execution_line = line
        if line:
            block = self.document().findBlockByNumber(line - 1)
            if block.isValid():
                self.setTextCursor(QTextCursor(block))
                self.centerCursor()
        self._mark_line()
        self._gutter.update()

    def _fit_gutter(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _scroll_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        area = self.contentsRect()
        self._gutter.setGeometry(QRect(area.left(), area.top(), self.gutter_width(), area.height()))

    def paint_gutter(self, event) -> None:
        painter = QPainter(self._gutter)
        palette = self.palette()
        painter.fillRect(event.rect(), palette.color(QPalette.ColorRole.AlternateBase))
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        height = self.fontMetrics().height()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible():
                number = block.blockNumber() + 1
                if self.breakpoints_enabled:
                    dot = height - 6
                    if isinstance(block.userData(), Breakpoint):
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor(BREAKPOINT_COLOR))
                        painter.drawEllipse(4, int(top) + 3, dot, dot)
                    if number == self.execution_line:
                        painter.setPen(QColor(EXECUTION_COLOR).darker(130))
                        painter.setBrush(QColor(EXECUTION_COLOR))
                        y = int(top) + height // 2
                        painter.drawPolygon([QPoint(4, y - dot // 2), QPoint(4 + dot, y),
                                             QPoint(4, y + dot // 2)])
                    painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
                painter.drawText(0, int(top), self._gutter.width() - 6, height,
                                 Qt.AlignmentFlag.AlignRight, str(number))
            top += self.blockBoundingRect(block).height()
            block = block.next()

    def _mark_line(self) -> None:
        line = QTextEdit.ExtraSelection()
        color = self.palette().color(QPalette.ColorRole.Highlight)
        color.setAlpha(30)
        line.format.setBackground(color)
        line.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        line.cursor = self.textCursor()
        line.cursor.clearSelection()
        selections = [line]
        block = self.document().findBlockByNumber(self.execution_line - 1)
        if self.execution_line and block.isValid():
            paused = QTextEdit.ExtraSelection()
            color = QColor(EXECUTION_COLOR)
            color.setAlpha(70)
            paused.format.setBackground(color)
            paused.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            paused.cursor = QTextCursor(block)
            selections.append(paused)
        self.setExtraSelections(selections)

    def raw_text(self) -> str:
        # Not toPlainText(): it turns non-breaking spaces into plain ones.
        return self.document().toRawText()


def is_python(name: str) -> bool:
    return name.endswith((".py", ".pyw"))


class EditorViewer(Viewer):
    closed = Signal()
    run_requested = Signal(str, bool)          # path, debug
    breakpoints_changed = Signal(str, object)  # path, set of lines

    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self._breakpoints = context.breakpoints
        self._execution_line = 0
        self._colorer = ThreadPoolExecutor(1, thread_name_prefix="editor-syntax")
        self.edit = CodeEdit()
        self.highlighter = Highlighter(self.edit.document())
        self.edit.document().modificationChanged.connect(self._modified_changed)
        self.edit.textChanged.connect(self._text_changed)

        self.save_button = QPushButton("Save", objectName="primary")
        self.save_button.clicked.connect(lambda: self.save())
        self.done_button = QPushButton("Done")
        self.done_button.clicked.connect(self._done)
        self.status = QLabel()
        self.reload_button = QPushButton("Reload from server")
        self.reload_button.clicked.connect(self._reload_clicked)
        self.reload_button.hide()
        self.run_button = QPushButton("Run", toolTip="Run on the server (Ctrl+F5)")
        self.run_button.clicked.connect(lambda: self.request_run(False))
        self.debug_button = QPushButton("Debug", toolTip="Run under the debugger (F5). "
                                        "Click a line number to set a breakpoint.")
        self.debug_button.clicked.connect(lambda: self.request_run(True))
        self.edit.breakpoints_toggled.connect(self._breakpoints_toggled)
        bar = QHBoxLayout()
        bar.addWidget(self.save_button)
        bar.addWidget(self.done_button)
        bar.addWidget(self.status, 1)
        bar.addWidget(self.reload_button)
        bar.addWidget(self.run_button)
        bar.addWidget(self.debug_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.edit, 1)
        save = QShortcut(QKeySequence.StandardKey.Save, self)
        save.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save.activated.connect(lambda: self.save())

        self._color_timer = QTimer(self, singleShot=True, interval=HIGHLIGHT_DELAY_MS)
        self._color_timer.timeout.connect(self._color)
        self._codec: Optional[TextCodec] = None
        self._base: Optional[bytes] = None  # the server's bytes as we last knew them
        self._saving = False
        self._otherwise: Optional[Callable[[], None]] = None
        self._applying = False

    # Asking the user: separate so tests can answer ---------------------------
    def ask_conflict(self) -> str:
        """"overwrite", "reload" or "cancel"."""
        box = QMessageBox(QMessageBox.Icon.Warning, "File changed on the server",
                          f"{self.entry.name} changed on the server since you opened it.",
                          parent=self)
        overwrite = box.addButton("Overwrite server copy", QMessageBox.ButtonRole.DestructiveRole)
        reload_ = box.addButton("Discard my edits", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        return "overwrite" if clicked is overwrite else "reload" if clicked is reload_ else "cancel"

    def ask_unsaved(self) -> str:
        """"save", "discard" or "cancel"."""
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save changes to {self.entry.name}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        return {QMessageBox.StandardButton.Save: "save",
                QMessageBox.StandardButton.Discard: "discard"}.get(answer, "cancel")

    # Opening -----------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self.entry is not None and self.edit.document().isModified()

    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        python = is_python(entry.name)
        self.run_button.setVisible(python)
        self.debug_button.setVisible(python)
        self.edit.set_breakpoints_enabled(python)
        self._base = self._codec = None
        self._saving = False
        self.reload_button.hide()
        self.edit.setReadOnly(True)
        self.save_button.setEnabled(False)
        self._apply_text("")
        self.status.setText("Loading…")
        self._read(self._loaded, lambda exc: self._refuse(f"Could not read file: {exc}"))

    def close_file(self) -> None:
        super().close_file()
        self._execution_line = 0
        self._color_timer.stop()
        self._base = self._codec = None
        self._apply_text("")

    def _read(self, done: Callable[[bytes], None], failed: Callable[[Exception], None]) -> None:
        """Fresh bytes, past any cached handle: the file may have been replaced."""
        self.backend.invalidate(self.entry.path)
        current = self._current()
        self.bridge.then(self.backend.read(self.entry.path, 0, MAX_EDIT_BYTES + 1),
                         lambda data: current() and done(data),
                         lambda exc: current() and failed(exc))

    def _refuse(self, reason: str) -> None:
        self.status.setText(reason)
        self.edit.setReadOnly(True)
        self.save_button.setEnabled(False)

    def _loaded(self, data: bytes) -> None:
        if len(data) > MAX_EDIT_BYTES:
            self._refuse("Too large to edit")
            return
        codec = TextCodec(data)
        try:
            text = codec.decode(data)
        except UnicodeDecodeError:
            self._refuse("Read only: this file isn't UTF-8, so saving would damage it")
            return
        self._apply_text(text)
        if codec.encode(self.edit.raw_text()) != data:
            self._apply_text(text)
            self._refuse("Read only: mixed line endings or special characters "
                         "that saving would change")
            return
        self._codec, self._base = codec, data
        self.edit.setReadOnly(False)
        self.status.setText("")
        self.edit.setFocus()
        self._color()

    def _apply_text(self, text: str) -> None:
        self._applying = True
        cursor = self.edit.textCursor().position()
        scroll = self.edit.verticalScrollBar().value()
        self.highlighter.lines = []
        self.edit.setPlainText(text)
        moved = self.edit.textCursor()
        moved.setPosition(min(cursor, self.edit.document().characterCount() - 1))
        self.edit.setTextCursor(moved)
        self.edit.verticalScrollBar().setValue(scroll)
        self.edit.document().setModified(False)
        if self.entry is not None:
            self.edit.set_breakpoints(self._breakpoints.get(self.entry.path, set()))
        self.edit.set_execution_line(self._execution_line)
        self._applying = False

    # Running -----------------------------------------------------------------
    def request_run(self, debug: bool) -> None:
        """Save first if needed: the server runs what is on disk."""
        if self.entry is None or not is_python(self.entry.name):
            return
        path = self.entry.path
        if self.dirty:
            self.save(lambda: self.run_requested.emit(path, debug))
        else:
            self.run_requested.emit(path, debug)

    def set_execution_line(self, line: int) -> None:
        """Where the debugger is paused in this file; 0 for nowhere."""
        self._execution_line = line
        self.edit.set_execution_line(line)

    def _breakpoints_toggled(self) -> None:
        if self.entry is not None:
            lines = self.edit.breakpoints()
            self._breakpoints[self.entry.path] = lines
            self.breakpoints_changed.emit(self.entry.path, lines)

    # Colours -----------------------------------------------------------------
    def _text_changed(self) -> None:
        if not self._applying and self.entry is not None \
                and self.edit.document().characterCount() <= MAX_LIVE_HIGHLIGHT_BYTES:
            self._color_timer.start()

    def _color(self) -> None:
        if self.entry is None or not syntax.worth_fetching(self.entry.name):
            return
        data = self.edit.raw_text().replace(PARAGRAPH, "\n").encode("utf-8", "replace")
        if len(data) > syntax.MAX_HIGHLIGHT_BYTES:
            return
        dark = self.palette().color(QPalette.ColorRole.Base).lightness() < 128
        current = self._current()

        def done(result: Optional[syntax.Highlighted]) -> None:
            if current() and result is not None:
                self.highlighter.lines = list(result.lines.values())
                self.highlighter.rehighlight()
        self.bridge.then(self._colorer.submit(syntax.highlight, self.entry.name, data, dark, False),
                         done)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # Light and dark themes use different colours.
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "entry", None) is not None:
            self._color()

    # Saving ------------------------------------------------------------------
    def _modified_changed(self, modified: bool) -> None:
        if self._codec is not None:
            self.save_button.setEnabled(modified)
            if not self._saving:
                self.status.setText("Modified" if modified else "")

    def save(self, then: Optional[Callable[[], None]] = None,
             otherwise: Optional[Callable[[], None]] = None) -> None:
        """``then`` runs if the save succeeds, ``otherwise`` if it doesn't."""
        if self._saving or self._codec is None or not self.dirty:
            if otherwise is not None:
                otherwise()
            return
        self._saving = True
        self._otherwise = otherwise
        self.status.setText("Saving…")
        self._read(lambda data: self._compared(data, then), lambda _exc: self._compared(None, then))

    def _compared(self, server: Optional[bytes], then) -> None:
        if server != self._base:
            choice = self.ask_conflict()
            if choice == "reload":
                self._save_abandoned("")
                self.open(self.entry)
                return
            if choice != "overwrite":
                self._save_abandoned("Not saved")
                return
        data = self._codec.encode(self.edit.raw_text())
        current = self._current()

        def done(entry: FileEntry) -> None:
            if current():
                self.entry = entry
                self._base = data
                self._saving = False
                self.reload_button.hide()
                # Typing during the save leaves those edits unsaved.
                if self._codec.encode(self.edit.raw_text()) == data:
                    self.edit.document().setModified(False)
                self.status.setText("Saved")
                if then is not None:
                    then()

        def failed(exc: Exception) -> None:
            if current():
                self._save_abandoned(f"Save failed: {exc}")
        self.bridge.then(self.backend.write(self.entry.path, data), done, failed)

    def _save_abandoned(self, message: str) -> None:
        self._saving = False
        self.status.setText(message)
        otherwise, self._otherwise = self._otherwise, None
        if otherwise is not None:
            otherwise()

    # Live updates ------------------------------------------------------------
    def on_events(self, events: list[Event]) -> None:
        kinds = {e.kind for e in events}
        if self._saving or self._base is None:
            return
        if kinds & {EventKind.DELETE, EventKind.MOVED_FROM} \
                and not kinds & {EventKind.CREATE, EventKind.MOVED_TO}:
            self.status.setText("Deleted on the server. Saving will recreate it.")
        elif kinds & {EventKind.CLOSE_WRITE, EventKind.MOVED_TO, EventKind.RESYNC}:
            self._read(self._remote_changed, lambda _exc: None)

    def _remote_changed(self, data: bytes) -> None:
        if self._saving or data == self._base:
            return  # usually our own save coming back
        if self.dirty:
            self.status.setText("Changed on the server. Saving will ask what to do.")
            self.reload_button.show()
        else:
            self._loaded(data)

    def _reload_clicked(self) -> None:
        self.reload_button.hide()
        self.open(self.entry)

    # Leaving -----------------------------------------------------------------
    def leave(self, then: Callable[[], None],
              otherwise: Callable[[], None] = lambda: None) -> None:
        """Run ``then`` once it's safe to stop editing, or ``otherwise`` if
        editing goes on."""
        if not self.dirty:
            then()
            return
        choice = self.ask_unsaved()
        if choice == "discard":
            self.edit.document().setModified(False)
            then()
        elif choice == "save":
            self.save(then, otherwise)
        else:
            otherwise()

    def _done(self) -> None:
        self.leave(self.closed.emit)
