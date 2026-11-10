"""Rendered Markdown, with a toggle to the source. Reloads keep the scroll position."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QUrl, Qt, Signal
from PySide6.QtGui import (QDesktopServices, QFont, QFontDatabase, QPalette, QTextBlockFormat,
                           QTextCharFormat, QTextCursor, QTextFormat, QTextFrameFormat, QTextTable)
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout

from app.backend import Event, EventKind, FileEntry

from .base import Viewer

# Qt lays the document out on the UI thread; bigger files open as plain text.
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
EXTERNAL_SCHEMES = ("http", "https")

# Qt's Markdown import sets no line height and no space around headings, which
# reads as a wall of text; these restore some air.
DOCUMENT_MARGIN = 16
LINE_HEIGHT_PERCENT = 140
PARAGRAPH_SPACING = 8
LIST_ITEM_SPACING = 2
CODE_PADDING = 10
TABLE_CELL_PADDING = 6
# Space above and below a heading, by level.
HEADING_MARGINS = {1: (20, 10), 2: (18, 8), 3: (14, 6)}
SMALL_HEADING_MARGINS = (12, 4)
PROPORTIONAL = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
FIXED = QTextBlockFormat.LineHeightTypes.FixedHeight.value
CODE_FRAME = QTextFormat.Property.UserProperty.value


def _is_monospace(chars: QTextCharFormat) -> bool:
    return chars.fontFixedPitch() or "monospace" in (chars.fontFamilies() or [])


class MarkdownBrowser(QTextBrowser):
    double_clicked = Signal(object)  # the QTextCursor under the mouse

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.cursorForPosition(event.position().toPoint()))
        else:
            super().mouseDoubleClickEvent(event)

    def loadResource(self, _type, _name):
        # The text is from the server, so its image paths must not reach local files.
        return None


class MarkdownViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self.browser = MarkdownBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._link_clicked)
        self.browser.double_clicked.connect(self._double_clicked)
        self.source = QCheckBox("Source")
        self.source.toggled.connect(self._render)
        self.status = QLabel()

        bar = QHBoxLayout()
        bar.addWidget(self.source)
        bar.addStretch(1)
        bar.addWidget(self.status)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.browser, 1)

        self._text = ""
        self._loading = False
        self._reload_again = False

    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self._text = ""
        self._loading = self._reload_again = False
        self.browser.clear()
        self._load()

    def close_file(self) -> None:
        super().close_file()
        self._text = ""
        self.browser.clear()

    def on_events(self, events: list[Event]) -> None:
        # Not on MODIFY: that would render a half-written file.
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
        current = self._current()

        def fetched(data: bytes) -> None:
            if current():
                self._text = data.decode("utf-8", "replace")
                self.status.setText("")
                self._render()
                self._finished()

        def failed(exc: Exception) -> None:
            if current():
                self.status.setText(f"Could not read file: {exc}")
                self._finished()
        self.bridge.then(self.backend.fetch(self.entry.path), fetched, failed)

    def _finished(self) -> None:
        self._loading = False
        if self._reload_again:
            self._reload_again = False
            self._load()

    def _render(self) -> None:
        bar = self.browser.verticalScrollBar()
        position = bar.value()
        if self.source.isChecked():
            self.browser.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            self.browser.setPlainText(self._text)
        else:
            font = self.font()
            font.setPointSizeF(font.pointSizeF() + 1)
            self.browser.setFont(font)
            self.browser.setMarkdown(self._text)
            self._style_document()
        bar.setValue(position)

    def _style_document(self) -> None:
        document = self.browser.document()
        document.setDocumentMargin(DOCUMENT_MARGIN)
        palette = self.browser.palette()
        code_background = palette.color(QPalette.ColorRole.Window)
        code_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        code_chars = QTextCharFormat()
        code_chars.setFontFamilies(code_font.families())
        code_chars.setFontFixedPitch(True)
        inline_code = QTextCharFormat(code_chars)
        inline_code.setBackground(code_background)
        quote_chars = QTextCharFormat()
        quote_chars.setForeground(palette.color(QPalette.ColorRole.PlaceholderText))
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        self._tidy_tables(cursor)
        runs: list[list[int]] = []  # [start, end] of each run of code lines
        block = document.begin()
        while block.isValid():
            fmt = block.blockFormat()
            code = fmt.nonBreakableLines()  # how the importer marks fenced and indented code
            level = fmt.headingLevel()
            cursor.setPosition(block.position())
            in_table = cursor.currentTable() is not None
            if code:
                fmt.setLineHeight(100, PROPORTIONAL)
                fmt.setTopMargin(0)
                fmt.setBottomMargin(0)
                previous = block.previous()
                if not (previous.isValid() and previous.blockFormat().nonBreakableLines()):
                    runs.append([block.position(), 0])
                runs[-1][1] = block.position() + block.length() - 1
            elif level:
                top, bottom = HEADING_MARGINS.get(level, SMALL_HEADING_MARGINS)
                fmt.setLineHeight(120, PROPORTIONAL)
                fmt.setTopMargin(top if block != document.begin() else 0)
                fmt.setBottomMargin(bottom)
            else:
                fmt.setLineHeight(LINE_HEIGHT_PERCENT, PROPORTIONAL)
                if in_table:
                    fmt.setLineHeight(120, PROPORTIONAL)
                    fmt.setTopMargin(0)
                    fmt.setBottomMargin(0)
                elif block.textList() is not None:
                    fmt.setTopMargin(LIST_ITEM_SPACING)
                    fmt.setBottomMargin(LIST_ITEM_SPACING)
                elif fmt.topMargin() or fmt.bottomMargin():
                    fmt.setTopMargin(PARAGRAPH_SPACING / 2)
                    fmt.setBottomMargin(PARAGRAPH_SPACING)
            cursor.setBlockFormat(fmt)
            if code:
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                    QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(code_chars)
            else:
                if fmt.hasProperty(QTextFormat.Property.BlockQuoteLevel):
                    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                        QTextCursor.MoveMode.KeepAnchor)
                    cursor.mergeCharFormat(quote_chars)
                self._style_inline_code(block, inline_code)
            block = block.next()
        # Block backgrounds stop at the text, so each run of code lines goes
        # into a frame, which can be padded. Last first: frames shift positions.
        code_frame = QTextFrameFormat()
        code_frame.setBackground(code_background)
        code_frame.setPadding(CODE_PADDING)
        code_frame.setTopMargin(PARAGRAPH_SPACING)
        code_frame.setBottomMargin(PARAGRAPH_SPACING)
        code_frame.setBorder(0)
        code_frame.setProperty(CODE_FRAME, True)
        code_line = QTextBlockFormat()
        code_line.setNonBreakableLines(True)
        code_line.setLineHeight(100, PROPORTIONAL)
        for start, end in reversed(runs):
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            frame = cursor.insertFrame(code_frame)
            # Moving the lines in resets the first one's format.
            inner = QTextCursor(frame)
            inner.setPosition(frame.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
            inner.setBlockFormat(code_line)
            self._collapse(document.findBlock(frame.firstPosition() - 1))
            self._collapse(document.findBlock(frame.lastPosition() + 1))
        for frame in document.rootFrame().childFrames():
            fmt = frame.frameFormat()
            if fmt.isTableFormat():
                table = fmt.toTableFormat()
                table.setCellPadding(TABLE_CELL_PADDING)
                table.setCellSpacing(0)
                table.setBorder(1)
                table.setBorderBrush(palette.color(QPalette.ColorRole.Mid))
                table.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                table.setBorderCollapse(True)
                table.setTopMargin(PARAGRAPH_SPACING)
                table.setBottomMargin(PARAGRAPH_SPACING)
                frame.setFrameFormat(table)
        cursor.endEditBlock()

    @staticmethod
    def _tidy_tables(cursor: QTextCursor) -> None:
        """A table right after a code block gets an empty line at the top of
        its first cell, and that cell loses its header bold."""
        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        for frame in cursor.document().rootFrame().childFrames():
            if not isinstance(frame, QTextTable):
                continue
            for column in range(frame.columns()):
                cell = frame.cellAt(0, column)
                first = cell.firstCursorPosition()
                if not first.block().text() and first.block().next().position() < cell.lastPosition():
                    first.deleteChar()
                # Markdown tables always have a header row.
                cell = frame.cellAt(0, column)
                span = cell.firstCursorPosition()
                span.setPosition(cell.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
                span.mergeCharFormat(bold)

    @staticmethod
    def _style_inline_code(block, chars: QTextCharFormat) -> None:
        """`code` spans: the importer marks them with a generic monospace family."""
        spans = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and _is_monospace(fragment.charFormat()):
                spans.append((fragment.position(), fragment.length()))
            it += 1
        cursor = QTextCursor(block)
        for position, length in spans:
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(chars)

    @staticmethod
    def _collapse(block) -> None:
        """Hide the empty block a frame leaves beside it: its frame margin is enough."""
        if not block.isValid() or block.text():
            return
        cursor = QTextCursor(block)
        if cursor.currentFrame().frameFormat().property(CODE_FRAME):
            return
        fmt = QTextBlockFormat()
        fmt.setLineHeight(0, FIXED)
        cursor.setBlockFormat(fmt)
        tiny = QTextCharFormat()
        tiny.setFontPointSize(1)
        cursor.setBlockCharFormat(tiny)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # Code backgrounds and table borders come from the palette.
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.FontChange) \
                and getattr(self, "_text", "") and not self.source.isChecked():
            self._render()

    def _double_clicked(self, cursor) -> None:
        if not self.source.isChecked():
            self.edit_requested.emit(-1)  # rendered text doesn't map back to the source
            return
        line = cursor.blockNumber()
        before = "\n".join(self._text.split("\n")[:line])
        offset = len(before.encode("utf-8")) + (1 if line else 0)
        # Qt counts UTF-16 units within the block.
        units = cursor.block().text().encode("utf-16-le")[:2 * cursor.positionInBlock()]
        offset += len(units.decode("utf-16-le", "ignore").encode("utf-8"))
        self.edit_requested.emit(offset)

    def _link_clicked(self, url: QUrl) -> None:
        if url.scheme() in EXTERNAL_SCHEMES:
            QDesktopServices.openUrl(url)
        elif url.hasFragment() and not url.path():
            self.browser.scrollToAnchor(url.fragment())
