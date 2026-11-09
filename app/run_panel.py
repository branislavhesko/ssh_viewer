"""The Python panel: output, variables and an inspector for one run at a time."""
from __future__ import annotations

import codeop
import html
import posixpath
import re
import warnings
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from app.backend import Backend
from app.bridge import Bridge, display_text
from app.python_run import DEFAULT_INTERPRETER, PythonSession

MAX_OUTPUT_LINES = 20000
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_LINE_BREAKS = re.compile(r"(\r\n|\r|\n)")
EXPR_ROLE = Qt.ItemDataRole.UserRole


def _fixed_font():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


class OutputConsole(QPlainTextEdit):
    """Program output. A bare carriage return rewrites the line, as progress
    bars like tqdm expect."""

    def __init__(self):
        super().__init__(readOnly=True)
        self.setFont(_fixed_font())
        self.setMaximumBlockCount(MAX_OUTPUT_LINES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._rewrite = False

    def feed(self, text: str) -> None:
        bar = self.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for part in _LINE_BREAKS.split(_ESCAPES.sub("", text)):
            if part in ("\n", "\r\n"):
                cursor.insertText("\n")
                self._rewrite = False
            elif part == "\r":
                self._rewrite = True
            elif part:
                if self._rewrite:
                    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock,
                                        QTextCursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
                    self._rewrite = False
                cursor.insertText(part)
        if follow:
            bar.setValue(bar.maximum())

    def note(self, text: str) -> None:
        """A line from the app, not the program."""
        if not self.document().isEmpty() and not self.document().lastBlock().text() == "":
            self.feed("\n")
        self.feed(f"── {text}\n")

    def reset(self) -> None:
        self.clear()
        self._rewrite = False


ERROR_COLOR = "#e5484d"
MAX_HISTORY = 200


def is_complete(source: str) -> bool:
    """Would the Python REPL run this now, rather than wait for more lines?
    Errors count as complete: running shows them."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. invalid escape sequences
            # "single", as the REPL: a block ends at a blank line. Several
            # statements are an error here, and so run at once, which is right.
            return codeop.compile_command(source, "<console>", "single") is not None
    except (SyntaxError, ValueError, OverflowError):
        return True


class ConsoleInput(QPlainTextEdit):
    """Enter runs a complete statement and continues an open block, like the
    REPL. Shift+Enter always adds a line, Ctrl+Enter always runs. Up and Down
    on the first and last line walk the history."""

    submitted = Signal(str)

    def __init__(self):
        super().__init__()
        self.setFont(_fixed_font())
        self.setTabChangesFocus(False)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setFixedHeight(self.fontMetrics().height() * 4 + 12)
        self.history: list[str] = []
        self._position = 0
        self._draft = ""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, modifiers = event.key(), event.modifiers()
        cursor = self.textCursor()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            source = self.toPlainText()
            # A line of only the auto-indent is the blank line that ends a block.
            blank = not cursor.block().text().strip()
            candidate = source.rstrip() + "\n" if blank else source
            run = modifiers & Qt.KeyboardModifier.ControlModifier or (
                cursor.atEnd() and (not source.strip() or is_complete(candidate)))
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._newline()
            elif run:
                self._submit(source)
            else:
                self._newline()
            return
        if key == Qt.Key.Key_Tab and not modifiers:
            self.insertPlainText("    ")
            return
        if key == Qt.Key.Key_Up and cursor.blockNumber() == 0 and self.history:
            self._recall(-1)
            return
        if key == Qt.Key.Key_Down and cursor.blockNumber() == self.blockCount() - 1 \
                and self._position < len(self.history):
            self._recall(1)
            return
        super().keyPressEvent(event)

    def _newline(self) -> None:
        """Keep the indentation, and add a level after a colon."""
        line = self.textCursor().block().text()
        indent = line[:len(line) - len(line.lstrip())]
        if line.rstrip().endswith(":"):
            indent += "    "
        self.insertPlainText("\n" + indent)

    def _submit(self, source: str) -> None:
        source = source.rstrip()
        if not source:
            return
        if not self.history or self.history[-1] != source:
            self.history = (self.history + [source])[-MAX_HISTORY:]
        self._position = len(self.history)
        self._draft = ""
        self.clear()
        self.submitted.emit(source)

    def _recall(self, step: int) -> None:
        if self._position == len(self.history):
            self._draft = self.toPlainText()
        self._position = max(0, min(len(self.history), self._position + step))
        text = self.history[self._position] if self._position < len(self.history) else self._draft
        self.setPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.End)


class Console(QWidget):
    """A REPL in the paused frame: what you typed, what it printed, its value."""

    run = Signal(str)
    inspect = Signal(str)

    def __init__(self):
        super().__init__()
        self.transcript = QTextBrowser(openLinks=False, openExternalLinks=False)
        self.transcript.setFont(_fixed_font())
        self.transcript.anchorClicked.connect(lambda url: self.inspect.emit(url.fragment()))
        self.input = ConsoleInput()
        self.input.submitted.connect(self._submitted)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.transcript.clear)
        hint = QLabel("Enter runs · Shift+Enter new line · ↑↓ history", objectName="title")
        row = QHBoxLayout()
        row.addWidget(hint, 1)
        row.addWidget(clear)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.transcript, 1)
        layout.addWidget(self.input)
        layout.addLayout(row)
        self._waiting = 0
        self.set_ready(False)

    def set_ready(self, ready: bool) -> None:
        self.input.setReadOnly(not ready)
        self.input.setPlaceholderText(
            "Python in the selected frame, e.g. x.shape or x.float().mean()" if ready
            else "Pause at a breakpoint to use the console")

    def _submitted(self, source: str) -> None:
        lines = source.split("\n")
        echo = "\n".join([">>> " + lines[0]] + ["... " + line for line in lines[1:]])
        self._append(echo, "gray")
        self._waiting += 1
        self.run.emit(source)

    def show_result(self, event: dict) -> None:
        self._waiting = max(0, self._waiting - 1)
        if event.get("output"):
            self._append(event["output"].rstrip("\n"))
        if event.get("error"):
            self._append(event["error"].rstrip("\n"), ERROR_COLOR)
        if event.get("result") is not None:
            link = (f" <a href='#{html.escape(event['name'])}'>Inspect {html.escape(event['name'])}</a>"
                    if event.get("inspectable") else "")
            self._append(event["result"], link=link,
                         label=f"{event['name']} = " if event.get("name") else "")

    def _append(self, text: str, color: str = "", link: str = "", label: str = "") -> None:
        style = f" style='color:{color}'" if color else ""
        prefix = f"<span style='color:gray'>{html.escape(label)}</span>" if label else ""
        self.transcript.append(f"<pre{style}>{prefix}{html.escape(text)}{link}</pre>")
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def note(self, text: str) -> None:
        self._append(f"── {text}", "gray")


class Inspector(QWidget):
    """One value in detail: facts, then its data as a table, or its parts."""

    inspect = Signal(str)

    def __init__(self):
        super().__init__()
        self.expression = QLineEdit(placeholderText="Expression to inspect or statement to run, "
                                                    "in the selected frame")
        self.expression.setFont(_fixed_font())
        self.expression.returnPressed.connect(lambda: self.inspect.emit(self.expression.text()))
        self.back = QPushButton("Back", enabled=False)
        self.back.clicked.connect(self._back)
        self.info = QLabel(textInteractionFlags=Qt.TextInteractionFlag.TextSelectableByMouse,
                           wordWrap=True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFont(_fixed_font())
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)
        self.children = QTreeWidget()
        self.children.setHeaderLabels(["Name", "Type", "Value"])
        self.children.setRootIsDecorated(False)
        self.children.itemDoubleClicked.connect(self._child_activated)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(_fixed_font())
        row = QHBoxLayout()
        row.addWidget(self.expression, 1)
        row.addWidget(self.back)
        body = self.body = QSplitter(Qt.Orientation.Vertical)
        for widget in (self.table, self.children, self.text):
            body.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(row)
        layout.addWidget(self.info)
        layout.addWidget(body, 1)
        self._history: list[str] = []
        self.show_result(None)

    def show_result(self, result: Optional[dict]) -> None:
        for widget in (self.table, self.children, self.text):
            widget.hide()
        if result is None:
            self.info.setText("Double-click a variable, or type an expression above.")
            return
        expr = result.get("expr", "")
        if expr and (not self._history or self._history[-1] != expr):
            self._history.append(expr)
        self.back.setEnabled(len(self._history) > 1)
        self.expression.setText(expr)
        if result.get("error"):
            self.info.setText(f"<pre style='color:#e5484d'>{html.escape(result['error'])}</pre>")
            return
        # Facts flow on as few lines as fit: the table below needs the room.
        facts = " &nbsp;·&nbsp; ".join(
            f"<span style='color:gray'>{html.escape(k)}</span>&nbsp;{html.escape(v).replace(' ', '&nbsp;')}"
            for k, v in result.get("info", []))
        title = (f"<b>{html.escape(result.get('type', ''))}</b>&nbsp; "
                 f"{html.escape(result.get('summary', ''))}")
        note = self._note(result)
        self.info.setText(f"{title}<br>{facts}"
                          + (f"<br><span style='color:gray'>{html.escape(note)}</span>" if note else ""))
        table = result.get("table")
        if table:
            self._fill_table(table)
            self.table.show()
        if result.get("children"):
            self._fill_children(result["children"])
            self.children.show()
        if result.get("repr"):
            self.text.setPlainText(result["repr"])
            self.text.show()
        # A DataFrame shows both: most of the room to the data, some to its columns.
        height = max(self.body.height(), 4)
        self.body.setSizes([height * 3 // 4, height // 4, height] if table else [0, height, height])

    @staticmethod
    def _note(result: dict) -> str:
        parts = [result.get("note", "")]
        table = result.get("table")
        if table:
            shown_rows, shown_columns = len(table["rows"]), len(table["columns"])
            if table["total_rows"] > shown_rows:
                parts.append(f"first {shown_rows:,} of {table['total_rows']:,} rows")
            if table["total_columns"] > shown_columns:
                parts.append(f"first {shown_columns:,} of {table['total_columns']:,} columns")
        return " · ".join(p for p in parts if p)

    def _fill_table(self, table: dict) -> None:
        self.table.clear()
        self.table.setRowCount(len(table["rows"]))
        self.table.setColumnCount(len(table["columns"]))
        self.table.setHorizontalHeaderLabels(table["columns"])
        self.table.setVerticalHeaderLabels(table["index"])
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for r, row in enumerate(table["rows"]):
            for c, value in enumerate(row):
                item = QTableWidgetItem(value)
                if value[:1].isdigit() or value[:1] in "-+." or value in ("nan", "inf"):
                    item.setTextAlignment(right)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

    def _fill_children(self, children: list[dict]) -> None:
        self.children.clear()
        for child in children:
            item = QTreeWidgetItem([child["name"], child["type"], child["summary"]])
            item.setData(0, EXPR_ROLE, child.get("expr"))
            if not child.get("expr"):
                item.setToolTip(0, "Can't be reached by an expression")
            self.children.addTopLevelItem(item)
        self.children.resizeColumnToContents(0)

    def _child_activated(self, item: QTreeWidgetItem) -> None:
        expr = item.data(0, EXPR_ROLE)
        if expr:
            self.inspect.emit(expr)

    def _back(self) -> None:
        if len(self._history) > 1:
            self._history.pop()
            self.inspect.emit(self._history.pop())

    def reset(self) -> None:
        self._history.clear()
        self.expression.clear()
        self.show_result(None)


class RunPanel(QWidget):
    """Runs one script at a time; ``location`` says where it is paused."""

    location = Signal(str, int)  # path, line; line 0 when not paused
    close_requested = Signal()

    def __init__(self, backend: Backend, bridge: Bridge, breakpoints: dict[str, set[int]],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.backend, self.bridge = backend, bridge
        self.breakpoints = breakpoints
        self.session: Optional[PythonSession] = None
        self.paused = False
        self._post_mortem = False
        self._save_interpreter: Callable[[str], None] = lambda _value: None

        self.status = QLabel("Not running", objectName="title")
        self.interpreter = QLineEdit(placeholderText=DEFAULT_INTERPRETER)
        self.interpreter.setToolTip("The Python on the server, e.g. ~/venv/bin/python or "
                                    "~/miniconda3/envs/ml/bin/python")
        self.interpreter.setMaximumWidth(260)
        self.interpreter.editingFinished.connect(
            lambda: self._save_interpreter(self.interpreter.text().strip()))
        hide = QPushButton("Hide")
        hide.clicked.connect(self.close_requested)
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(QLabel("Python", objectName="title"))
        top.addWidget(self.interpreter)
        top.addWidget(hide)

        self.continue_button = QPushButton("Continue", toolTip="Continue (F5)")
        self.over_button = QPushButton("Over", toolTip="Step over (F10)")
        self.into_button = QPushButton("Into", toolTip="Step into (F11)")
        self.out_button = QPushButton("Out", toolTip="Step out (Shift+F11)")
        self.stop_button = QPushButton("Stop", toolTip="Stop (Shift+F5)")
        self.continue_button.clicked.connect(lambda: self.step("continue"))
        self.over_button.clicked.connect(lambda: self.step("next"))
        self.into_button.clicked.connect(lambda: self.step("step"))
        self.out_button.clicked.connect(lambda: self.step("return"))
        self.stop_button.clicked.connect(self.stop)
        self.stack = QComboBox()
        self.stack.setMinimumContentsLength(12)
        self.stack.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.stack.activated.connect(self._frame_chosen)
        controls = QHBoxLayout()
        for button in (self.continue_button, self.over_button, self.into_button, self.out_button,
                       self.stop_button):
            controls.addWidget(button)
        controls.addWidget(self.stack, 1)

        self.output = OutputConsole()
        self.variables = QTreeWidget()
        self.variables.setHeaderLabels(["Name", "Type", "Value"])
        self.variables.setRootIsDecorated(False)
        self.variables.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.variables.itemDoubleClicked.connect(
            lambda item: self.inspect(item.data(0, EXPR_ROLE)))
        self.inspector = Inspector()
        self.inspector.inspect.connect(self.inspect)
        self.console = Console()
        self.console.run.connect(self.run_code)
        self.console.inspect.connect(self.inspect)
        self.tabs = QTabWidget(documentMode=True)
        self.tabs.addTab(self.output, "Output")
        self.tabs.addTab(self.console, "Console")
        self.tabs.addTab(self.variables, "Variables")
        self.tabs.addTab(self.inspector, "Inspect")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addLayout(controls)
        layout.addWidget(self.tabs, 1)
        self._update_controls()

    # Settings ----------------------------------------------------------------
    def use_interpreter(self, value: str, save: Callable[[str], None]) -> None:
        """The saved choice for this host, and how to save a new one."""
        self.interpreter.setText(value)
        self._save_interpreter = save

    # Running -----------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.session is not None and self.session.running

    def start(self, path: str, debug: bool) -> None:
        self.stop()
        self.output.reset()
        self.variables.clear()
        self.stack.clear()
        self.inspector.reset()
        self.tabs.setCurrentWidget(self.output)
        interpreter = self.interpreter.text().strip() or DEFAULT_INTERPRETER
        self._save_interpreter(self.interpreter.text().strip())
        mode = "Debugging" if debug else "Running"
        self.output.note(f"{mode} {display_text(path)} with {interpreter}")
        self.console.note(f"{mode} {posixpath.basename(display_text(path))}")
        session = self.session = PythonSession(self.backend, self.bridge, path, interpreter,
                                               debug, self.breakpoints, self)
        session.output.connect(lambda text: session is self.session and self.output.feed(text))
        session.event.connect(lambda event: session is self.session and self._event(event))
        session.finished.connect(
            lambda status, error: session is self.session and self._finished(status, error))
        self.paused = self._post_mortem = False
        self.status.setText(f"{mode} {posixpath.basename(display_text(path))}…")
        self._update_controls()

    def stop(self) -> None:
        if self.running:
            self.session.stop()

    def step(self, command: str) -> None:
        if self.running and self.paused:
            self.paused = False
            self._update_controls()
            self.location.emit("", 0)
            self.session.send({"cmd": command})

    def inspect(self, expr: Optional[str]) -> None:
        if not expr or not (self.running and self.paused):
            return
        self.tabs.setCurrentWidget(self.inspector)
        self.session.send({"cmd": "inspect", "expr": expr})

    def run_code(self, source: str) -> None:
        if self.running and self.paused:
            self.session.send({"cmd": "exec", "code": source})
        else:
            self.console.show_result({"error": "Not paused: the console runs at a breakpoint."})

    def breakpoints_changed(self, path: str, lines: set[int]) -> None:
        if self.running:
            self.session.set_breakpoints(path, lines)

    # Events ------------------------------------------------------------------
    def _event(self, event: dict) -> None:
        kind = event.get("ev")
        if kind == "started":
            self.output.note(f"Python {event.get('python')} ({event.get('executable')}), "
                             f"pid {event.get('pid')}")
        elif kind == "paused":
            self._paused(event)
        elif kind == "running":
            self.status.setText("Running…")
        elif kind == "vars":
            self._fill_variables(event.get("vars", []))
        elif kind == "inspect":
            self.inspector.show_result(event)
        elif kind == "console":
            self.console.show_result(event)
        elif kind == "exception":
            self.output.note(f"Uncaught {event.get('text', 'exception')}")

    def _paused(self, event: dict) -> None:
        self.paused = True
        self._post_mortem = bool(event.get("post_mortem"))
        frames = event.get("stack", [])
        self.stack.clear()
        for frame in frames:
            self.stack.addItem(f"{frame['name']}  {posixpath.basename(frame['file'])}:{frame['line']}")
        selected = event.get("selected", len(frames) - 1)
        self.stack.setCurrentIndex(selected)
        self._fill_variables(event.get("vars", []))
        top = frames[-1] if frames else None
        if self._post_mortem:
            self.status.setText(f"Crashed: {event.get('exception', '')}. Inspect, then Continue to end.")
        elif top:
            self.status.setText(f"Paused at {posixpath.basename(top['file'])}:{top['line']}")
        if self.tabs.currentWidget() is self.output:
            self.tabs.setCurrentWidget(self.variables)
        self._update_controls()
        if top:
            self.location.emit(top["file"], top["line"])

    def _fill_variables(self, variables: list[dict]) -> None:
        self.variables.clear()
        for variable in variables:
            item = QTreeWidgetItem([variable["name"], variable["type"], variable["summary"]])
            item.setData(0, EXPR_ROLE, variable["expr"])
            item.setToolTip(2, variable["summary"])
            self.variables.addTopLevelItem(item)
        self.variables.resizeColumnToContents(0)
        self.variables.resizeColumnToContents(1)

    def _frame_chosen(self, index: int) -> None:
        if self.running and self.paused:
            self.session.send({"cmd": "frame", "index": index})

    def _finished(self, status: Optional[int], error: str) -> None:
        self.paused = False
        if error:
            text = f"Failed: {error}"
        elif status is None:
            text = "Stopped"
        else:
            text = f"Finished with exit code {status}"
        self.output.note(text)
        self.status.setText(text)
        self.location.emit("", 0)
        self._update_controls()

    def _update_controls(self) -> None:
        stepping = self.running and self.paused and not self._post_mortem
        self.continue_button.setEnabled(self.running and self.paused)
        for button in (self.over_button, self.into_button, self.out_button):
            button.setEnabled(stepping)
        self.stop_button.setEnabled(self.running)
        self.stack.setEnabled(self.running and self.paused)
        self.console.set_ready(self.running and self.paused)
