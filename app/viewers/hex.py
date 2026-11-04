"""Fallback for anything without a dedicated viewer."""
from __future__ import annotations

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout

from app.backend import FileEntry

from .base import Viewer

PREVIEW_BYTES = 64 * 1024


def hex_dump(data: bytes, base: int = 0) -> str:
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + offset:08x}  {hexes:<47}  {text}")
    return "\n".join(lines)


class HexViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.status = QLabel()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.status)
        layout.addWidget(self.text, 1)

    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self.text.clear()
        current = self._current()

        def done(data: bytes) -> None:
            if current():
                self.text.setPlainText(hex_dump(data))
                shown = len(data)
                self.status.setText(f"First {shown:,} of {entry.size:,} bytes" if shown < entry.size
                                    else f"{entry.size:,} bytes")

        def failed(exc: Exception) -> None:
            if current():
                self.status.setText(f"Read failed: {exc}")
        self.bridge.then(self.backend.read(entry.path, 0, PREVIEW_BYTES), done, failed)
