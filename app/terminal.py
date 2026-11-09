"""Built-in terminal: a login shell on the server, over the same connection.

pyte does the VT emulation; ``TerminalView`` paints its screen and turns keys
into bytes, and ``TerminalPanel`` owns the shell's lifetime.
"""
from __future__ import annotations

import shlex
import sys
import threading
from collections import deque
from typing import Callable, Optional

import pyte
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QKeyEvent,
                           QPainter, QPalette)
from PySide6.QtWidgets import (QAbstractScrollArea, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QVBoxLayout, QWidget)

from app.backend import Backend, Shell
from app.bridge import Bridge, display_text
from app.viewers.ansi import BRIGHT, PALETTE

SCROLLBACK = 5000
_ALTERNATE = (47, 1047, 1049)
_APP_CURSOR = 1 << 5          # DECCKM, as pyte stores private modes
_BRACKETED_PASTE = 2004 << 5
_MAC = sys.platform == "darwin"

_NAMES = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")
COLORS = {name: PALETTE[i] for i, name in enumerate(_NAMES)}
COLORS.update({"bright" + name: BRIGHT[i] for i, name in enumerate(_NAMES)})
COLORS["bfightmagenta"] = COLORS["brightmagenta"]  # sic, pyte 0.8's name for 105

_KEYS = {
    Qt.Key.Key_Return: b"\r", Qt.Key.Key_Enter: b"\r", Qt.Key.Key_Backspace: b"\x7f",
    Qt.Key.Key_Tab: b"\t", Qt.Key.Key_Backtab: b"\x1b[Z", Qt.Key.Key_Escape: b"\x1b",
    Qt.Key.Key_Insert: b"\x1b[2~", Qt.Key.Key_Delete: b"\x1b[3~",
    Qt.Key.Key_PageUp: b"\x1b[5~", Qt.Key.Key_PageDown: b"\x1b[6~",
    Qt.Key.Key_F1: b"\x1bOP", Qt.Key.Key_F2: b"\x1bOQ", Qt.Key.Key_F3: b"\x1bOR",
    Qt.Key.Key_F4: b"\x1bOS", Qt.Key.Key_F5: b"\x1b[15~", Qt.Key.Key_F6: b"\x1b[17~",
    Qt.Key.Key_F7: b"\x1b[18~", Qt.Key.Key_F8: b"\x1b[19~", Qt.Key.Key_F9: b"\x1b[20~",
    Qt.Key.Key_F10: b"\x1b[21~", Qt.Key.Key_F11: b"\x1b[23~", Qt.Key.Key_F12: b"\x1b[24~",
}
# The final byte: after "\x1b[" normally, after "\x1bO" in application cursor mode.
_CURSOR_KEYS = {Qt.Key.Key_Up: b"A", Qt.Key.Key_Down: b"B", Qt.Key.Key_Right: b"C",
                Qt.Key.Key_Left: b"D", Qt.Key.Key_Home: b"H", Qt.Key.Key_End: b"F"}


def key_bytes(event: QKeyEvent, app_cursor: bool) -> bytes:
    """What a terminal sends for ``event``; empty for keys it ignores."""
    key, modifiers = event.key(), event.modifiers()
    # On macOS Qt reports Command as Control and the Control key as Meta.
    control = bool(modifiers & (Qt.KeyboardModifier.MetaModifier if _MAC
                                else Qt.KeyboardModifier.ControlModifier))
    alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
    if key in _CURSOR_KEYS:
        return (b"\x1bO" if app_cursor else b"\x1b[") + _CURSOR_KEYS[key]
    if key in _KEYS:
        data = _KEYS[key]
    elif control and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        data = bytes([key - Qt.Key.Key_A + 1])
    elif control and key in (Qt.Key.Key_Space, Qt.Key.Key_2, Qt.Key.Key_At):
        data = b"\x00"
    elif control and Qt.Key.Key_BracketLeft <= key <= Qt.Key.Key_Underscore:
        data = bytes([key - Qt.Key.Key_BracketLeft + 0x1b])  # ^[ ^\ ^] ^^ ^_
    else:
        data = event.text().encode("utf-8")
    return b"\x1b" + data if alt and data else data


class TerminalScreen(pyte.Screen):
    """pyte's screen plus scrollback, the alternate screen, and replies."""

    def __init__(self, columns: int, lines: int, reply: Callable[[str], None]):
        self.history: deque = deque(maxlen=SCROLLBACK)
        self.history_total = 0  # lines ever pushed: makes positions absolute
        self._reply = reply
        self._saved: Optional[tuple] = None
        super().__init__(columns, lines)

    @property
    def alternate(self) -> bool:
        return self._saved is not None

    def first_line(self) -> int:
        """Absolute number of the oldest line still in history."""
        return self.history_total - len(self.history)

    def line(self, number: int) -> dict:
        """Absolute line ``number``, from history or the screen."""
        index = number - self.first_line()
        if index < len(self.history):
            return self.history[index] if index >= 0 else {}
        return self.buffer.get(index - len(self.history), {})

    def _push(self, line: dict) -> None:
        self.history.append(line)
        self.history_total += 1

    def write_process_input(self, data: str) -> None:
        self._reply(data)

    def index(self) -> None:
        top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and not self.alternate:
            self._push(self.buffer.get(0, {}))
        super().index()

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how == 3:  # "clear" also asks to drop the scrollback
            self.history.clear()
            self.history_total = 0
            return
        super().erase_in_display(how, *args, **kwargs)

    def set_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private") and not self.alternate and any(m in _ALTERNATE for m in modes):
            self._saved = (dict(self.buffer), self.cursor)
            self.cursor = pyte.screens.Cursor(self.cursor.x, self.cursor.y, self.cursor.attrs)
            self.buffer.clear()
            self.dirty.update(range(self.lines))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private") and self.alternate and any(m in _ALTERNATE for m in modes):
            buffer, self.cursor = self._saved
            self._saved = None
            self.buffer.clear()
            self.buffer.update((y, line) for y, line in buffer.items() if y < self.lines)
            self.cursor.x = min(self.cursor.x, self.columns - 1)
            self.cursor.y = min(self.cursor.y, self.lines - 1)
            self.dirty.update(range(self.lines))
        super().reset_mode(*modes, **kwargs)

    def resize(self, lines: Optional[int] = None, columns: Optional[int] = None) -> None:
        """pyte drops lines from the top when shrinking, even blank screen
        under the prompt. Drop only what the cursor needs, into history."""
        lines, columns = lines or self.lines, columns or self.columns
        if (lines, columns) == (self.lines, self.columns):
            return
        if lines < self.lines:
            drop = max(0, self.cursor.y + 1 - lines)
            rows = [self.buffer.get(y) for y in range(self.lines)]
            if not self.alternate:
                for row in rows[:drop]:
                    self._push(row if row is not None else {})
            self.buffer.clear()
            self.buffer.update((y, row) for y, row in enumerate(rows[drop:drop + lines])
                               if row is not None)
            self.cursor.y -= drop
        if columns < self.columns:
            for row in self.buffer.values():
                for x in range(columns, self.columns):
                    row.pop(x, None)
        self.lines, self.columns = lines, columns
        self.tabstops = {x for x in self.tabstops if x < columns} | set(range(8, columns, 8))
        self.set_margins()
        self.cursor.x = min(self.cursor.x, columns - 1)
        self.cursor.y = min(self.cursor.y, lines - 1)
        self.dirty.update(range(lines))


class TerminalView(QAbstractScrollArea):
    """Paints a TerminalScreen; keys and pastes come out as ``typed`` bytes."""

    typed = Signal(bytes)
    resized = Signal(int, int)  # columns, rows
    title_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.setFont(font)
        self.screen = TerminalScreen(80, 24, lambda text: self.typed.emit(text.encode("utf-8")))
        self.stream = pyte.ByteStream(self.screen)
        self._title = ""
        self._follow = True      # stick to the bottom as output arrives
        self._top = 0            # absolute line at the top while scrolled back
        self._anchor: Optional[tuple[int, int]] = None
        self._selection: Optional[tuple[tuple[int, int], tuple[int, int]]] = None
        self._metrics()
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    # Geometry ----------------------------------------------------------------
    def _metrics(self) -> None:
        metrics = QFontMetrics(self.font())
        self.cell_width = max(1, metrics.horizontalAdvance("M"))
        self.cell_height = max(1, metrics.height())
        self.ascent = metrics.ascent()
        self.bold_font = QFont(self.font())
        self.bold_font.setBold(True)

    def grid_size(self) -> tuple[int, int]:
        size = self.viewport().size()
        return (max(2, (size.width() - 4) // self.cell_width),
                max(2, size.height() // self.cell_height))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        columns, rows = self.grid_size()
        if (columns, rows) != (self.screen.columns, self.screen.lines):
            self.screen.resize(rows, columns)
            self._sync_scrollbar()
            self.resized.emit(columns, rows)

    # Content -----------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self.stream.feed(data)
        if self.screen.title != self._title:
            self._title = self.screen.title
            self.title_changed.emit(self._title)
        self._sync_scrollbar()
        self.viewport().update()

    def write_note(self, text: str) -> None:
        """A line from the app itself, set apart from the shell's output."""
        prefix = "\r\n" if self.screen.cursor.x else ""
        self.feed(f"{prefix}\x1b[0;2m{text}\x1b[0m\r\n".encode("utf-8"))

    def clear(self) -> None:
        """Empty the screen and scrollback: a fresh terminal."""
        self.screen.reset()
        self.screen.history.clear()
        self.screen.history_total = 0
        self.screen._saved = None
        self._selection = None
        self._follow = True
        self._sync_scrollbar()
        self.viewport().update()

    def scroll_to_bottom(self) -> None:
        self._follow = True
        self._sync_scrollbar()
        self.viewport().update()

    def _sync_scrollbar(self) -> None:
        bar, screen = self.verticalScrollBar(), self.screen
        bar.blockSignals(True)
        bar.setRange(0, len(screen.history))
        bar.setPageStep(screen.lines)
        bar.setSingleStep(1)
        if self._follow:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(max(0, self._top - screen.first_line()))
            self._follow = bar.value() == bar.maximum()
        bar.blockSignals(False)

    def _scrolled(self, value: int) -> None:
        bar = self.verticalScrollBar()
        self._follow = value == bar.maximum()
        self._top = self.screen.first_line() + value
        self.viewport().update()

    def top_line(self) -> int:
        return self.screen.first_line() + self.verticalScrollBar().value()

    def visible_text(self) -> str:
        """The rows on screen as plain text, trailing blanks trimmed."""
        top = self.top_line()
        return "\n".join(self._text(top + y, 0, self.screen.columns)
                         for y in range(self.screen.lines))

    def _text(self, number: int, start: int, end: int) -> str:
        line = self.screen.line(number)
        return "".join(line[x].data if x in line else " " for x in range(start, end)).rstrip()

    # Painting ----------------------------------------------------------------
    def _color(self, name: str, default: QColor) -> QColor:
        if name == "default":
            return default
        if name in COLORS:
            return QColor(COLORS[name])
        return QColor("#" + name) if len(name) == 6 else default

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        palette = self.palette()
        background = palette.color(QPalette.ColorRole.Base)
        foreground = palette.color(QPalette.ColorRole.Text)
        selected = palette.color(QPalette.ColorRole.Highlight)
        painter.fillRect(self.viewport().rect(), background)
        screen, width, height = self.screen, self.cell_width, self.cell_height
        top = self.top_line()
        cursor_line = screen.history_total + screen.cursor.y
        selection = self._selection
        for y in range(screen.lines):
            number = top + y
            line = screen.line(number)
            if not line and not selection:
                continue
            x = 0
            while x < screen.columns:
                char = line.get(x)
                if char is None:
                    char = screen.default_char
                in_selection = bool(selection) and self._selected(number, x)
                # A run: cells drawn together, all ASCII with one style.
                end = x + 1
                if char.data.isascii():
                    while end < screen.columns:
                        following = line.get(end, screen.default_char)
                        if (not following.data.isascii() or following[1:] != char[1:]
                                or (bool(selection) and self._selected(number, end)) != in_selection):
                            break
                        end += 1
                elif end < screen.columns and line.get(end, screen.default_char).data == "":
                    end += 1  # a wide character covers two cells
                fg, bg = self._color(char.fg, foreground), self._color(char.bg, background)
                if char.reverse:
                    fg, bg = bg, fg
                if in_selection:
                    bg = selected
                rect = QRect(2 + x * width, y * height, (end - x) * width, height)
                if bg != background:
                    painter.fillRect(rect, bg)
                text = "".join(line[i].data if i in line else " " for i in range(x, end))
                if text.strip():
                    painter.setFont(self.bold_font if char.bold else self.font())
                    painter.setPen(fg)
                    painter.drawText(QPoint(rect.x(), rect.y() + self.ascent), text)
                    if char.underscore:
                        painter.drawLine(rect.x(), rect.bottom(), rect.right(), rect.bottom())
                x = end
            if number == cursor_line and not screen.cursor.hidden:
                rect = QRect(2 + screen.cursor.x * width, y * height, width, height)
                if self.hasFocus():
                    painter.fillRect(rect, foreground)
                    char = line.get(screen.cursor.x)
                    if char is not None and char.data.strip():
                        painter.setPen(background)
                        painter.setFont(self.bold_font if char.bold else self.font())
                        painter.drawText(QPoint(rect.x(), rect.y() + self.ascent), char.data)
                else:
                    painter.setPen(foreground)
                    painter.drawRect(rect.adjusted(0, 0, -1, -1))

    # Selection and clipboard -------------------------------------------------
    def _position(self, point: QPoint) -> tuple[int, int]:
        column = min(max(0, (point.x() - 2 + self.cell_width // 2) // self.cell_width),
                     self.screen.columns)
        row = min(max(0, point.y() // self.cell_height), self.screen.lines - 1)
        return self.top_line() + row, column

    def _selected(self, number: int, x: int) -> bool:
        (start_line, start_x), (end_line, end_x) = self._selection
        if not start_line <= number <= end_line:
            return False
        if number == start_line and x < start_x:
            return False
        return not (number == end_line and x >= end_x)

    def selected_text(self) -> str:
        if not self._selection:
            return ""
        (start_line, start_x), (end_line, end_x) = self._selection
        rows = []
        for number in range(start_line, end_line + 1):
            first = start_x if number == start_line else 0
            last = end_x if number == end_line else self.screen.columns
            rows.append(self._text(number, first, last))
        return "\n".join(rows)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._anchor = self._position(event.position().toPoint())
            self._selection = None
            self.viewport().update()
        elif event.button() == Qt.MouseButton.MiddleButton:
            clipboard = QGuiApplication.clipboard()
            if clipboard.supportsSelection():
                self.paste(clipboard.text(clipboard.Mode.Selection))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._anchor is not None:
            here = self._position(event.position().toPoint())
            start, end = sorted((self._anchor, here))
            self._selection = (start, end) if start != end else None
            self.viewport().update()

    def mouseReleaseEvent(self, event) -> None:
        self._anchor = None
        clipboard = QGuiApplication.clipboard()
        if self._selection and clipboard.supportsSelection():
            clipboard.setText(self.selected_text(), clipboard.Mode.Selection)
        super().mouseReleaseEvent(event)

    def copy(self) -> None:
        if self._selection:
            QGuiApplication.clipboard().setText(self.selected_text())

    def paste(self, text: Optional[str] = None) -> None:
        text = QGuiApplication.clipboard().text() if text is None else text
        if not text:
            return
        data = text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8")
        if _BRACKETED_PASTE in self.screen.mode:
            data = b"\x1b[200~" + data.replace(b"\x1b", b"") + b"\x1b[201~"
        self.scroll_to_bottom()
        self.typed.emit(data)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("Copy", self.copy).setEnabled(bool(self._selection))
        menu.addAction("Paste", self.paste)
        menu.addSeparator()
        menu.addAction("Clear scrollback", self._clear_scrollback)
        menu.exec(event.globalPos())

    def _clear_scrollback(self) -> None:
        self.screen.erase_in_display(3)
        self._selection = None
        self._follow = True
        self._sync_scrollbar()
        self.viewport().update()

    # Keyboard ----------------------------------------------------------------
    def focusNextPrevChild(self, _next: bool) -> bool:
        return False  # Tab belongs to the shell

    def event(self, event) -> bool:
        # Keep window shortcuts from taking keys the shell wants.
        if event.type() == event.Type.ShortcutOverride:
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, modifiers = event.key(), event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        command = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if command and (_MAC or shift) and key == Qt.Key.Key_C:
            self.copy()
            return
        if command and (_MAC or shift) and key == Qt.Key.Key_V:
            self.paste()
            return
        if shift and key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            bar = self.verticalScrollBar()
            step = bar.pageStep() if key == Qt.Key.Key_PageDown else -bar.pageStep()
            bar.setValue(bar.value() + step)
            return
        data = key_bytes(event, _APP_CURSOR in self.screen.mode)
        if data:
            self._selection = None
            self.scroll_to_bottom()
            self.typed.emit(data)

    def inputMethodEvent(self, event) -> None:
        if event.commitString():
            self.scroll_to_bottom()
            self.typed.emit(event.commitString().encode("utf-8"))
        event.accept()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.viewport().update()


class _Session:
    """One shell. Output is gathered on the backend thread and drained on the
    Qt thread in one go, so a flood of small chunks costs one repaint."""

    def __init__(self):
        self.shell: Optional[Shell] = None
        self.lock = threading.Lock()
        self.incoming: list[bytes] = []
        self.scheduled = False
        self.running = True


class TerminalPanel(QWidget):
    """The Terminal tab: one shell at a time, started when first shown."""

    def __init__(self, backend: Backend, bridge: Bridge, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.backend, self.bridge = backend, bridge
        self.view = TerminalView()
        self.view.typed.connect(self._typed)
        self.view.resized.connect(self._resized)
        self.view.title_changed.connect(lambda title: self._set_status(display_text(title)))
        self.status = QLabel(objectName="muted")
        self.restart = QPushButton("New shell")
        self.restart.clicked.connect(lambda: self.start(self._directory()))
        header = QHBoxLayout()
        header.setContentsMargins(10, 4, 6, 4)
        header.addWidget(self.status, 1)
        header.addWidget(self.restart)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        self._session: Optional[_Session] = None
        self._connected = False
        self._directory: Callable[[], Optional[str]] = lambda: None
        self._set_status("Not connected")
        self.restart.setEnabled(False)

    def set_directory_source(self, source: Callable[[], Optional[str]]) -> None:
        """Where a new shell starts: the folder the file list is showing."""
        self._directory = source

    def running(self) -> bool:
        return self._session is not None and self._session.running

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    # Connection --------------------------------------------------------------
    def connected(self) -> None:
        self._connected = True
        self.restart.setEnabled(True)
        self._set_status("Ready")
        if self.isVisible() and not self.running():
            self.start(self._directory())

    def disconnected(self) -> None:
        """A new host is coming: the old shell must not carry on."""
        self._connected = False
        self.restart.setEnabled(False)
        self.shutdown()
        self._set_status("Not connected")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._connected and self._session is None:
            QTimer.singleShot(0, lambda: self._connected and self._session is None
                              and self.start(self._directory()))
        self.view.setFocus()

    # Shell -------------------------------------------------------------------
    def start(self, directory: Optional[str]) -> None:
        self.shutdown()
        self.view.clear()
        session = self._session = _Session()
        columns, rows = self.view.grid_size()
        self.view.screen.resize(rows, columns)
        self._set_status("Starting shell…")

        def output(data: bytes) -> None:
            with session.lock:
                session.incoming.append(data)
                if session.scheduled:
                    return
                session.scheduled = True
            self.bridge.post(lambda: self._drain(session))

        def exited(error: Optional[str]) -> None:
            self.bridge.post(lambda: self._exited(session, error))
        session.shell = self.backend.open_shell(directory, columns, rows, output, exited)
        self.view.setFocus()

    def cd(self, directory: str) -> None:
        """Move the running shell to ``directory``, or start one there."""
        if self.running():
            # Leading space: shells set to ignore such lines keep it out of history.
            self._typed(b"\x15 cd " + shlex.quote(display_text(directory)).encode("utf-8") + b"\r")
            self.view.scroll_to_bottom()
        elif self._connected:
            self.start(directory)
        self.view.setFocus()

    def shutdown(self) -> None:
        session, self._session = self._session, None
        if session is not None and session.running:
            session.running = False
            if session.shell is not None:
                session.shell.close()

    def _drain(self, session: _Session) -> None:
        with session.lock:
            data = b"".join(session.incoming)
            session.incoming.clear()
            session.scheduled = False
        if session is self._session and data:
            if self.status.text() == "Starting shell…":
                self._set_status("Shell")
            self.view.feed(data)

    def _exited(self, session: _Session, error: Optional[str]) -> None:
        if session is not self._session:
            return
        self._drain(session)
        session.running = False
        self.view.write_note(f"[{'Shell failed: ' + error if error else 'Shell ended'}. "
                             "Press Enter for a new one.]")
        self._set_status("Shell ended" if not error else f"Shell failed: {error}")

    def _typed(self, data: bytes) -> None:
        session = self._session
        if session is not None and session.running:
            session.shell.write(data)
        elif data == b"\r" and self._connected:
            self.start(self._directory())

    def _resized(self, columns: int, rows: int) -> None:
        if self.running():
            self._session.shell.resize(columns, rows)
