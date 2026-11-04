"""Rendered Markdown, with a toggle to the source. Reloads keep the scroll position."""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout

from app.backend import Event, EventKind, FileEntry

from .base import Viewer

# Qt lays the document out on the UI thread; bigger files open as plain text.
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
EXTERNAL_SCHEMES = ("http", "https")


class MarkdownBrowser(QTextBrowser):
    def loadResource(self, _type, _name):
        # The text is from the server, so its image paths must not reach local files.
        return None


class MarkdownViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self.browser = MarkdownBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._link_clicked)
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
            self.browser.setFont(self.font())
            self.browser.setMarkdown(self._text)
        bar.setValue(position)

    def _link_clicked(self, url: QUrl) -> None:
        if url.scheme() in EXTERNAL_SCHEMES:
            QDesktopServices.openUrl(url)
        elif url.hasFragment() and not url.path():
            self.browser.scrollToAnchor(url.fragment())
