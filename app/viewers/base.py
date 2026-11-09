from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtWidgets import QWidget

from app.backend import Backend, Event, FileEntry
from app.bridge import Bridge


@dataclass
class ViewerContext:
    backend: Backend
    bridge: Bridge
    # Python breakpoints: remote path to 1-based line numbers. Shared, so they
    # outlive the editor switching files.
    breakpoints: dict[str, set[int]] = field(default_factory=dict)


class Viewer(QWidget):
    """One widget per file type, reused from file to file."""

    def __init__(self, context: ViewerContext, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.backend = context.backend
        self.bridge = context.bridge
        self.entry: Optional[FileEntry] = None
        self._generation = 0

    def open(self, entry: FileEntry) -> None:
        self.entry = entry
        self._generation += 1

    def close_file(self) -> None:
        self.entry = None
        self._generation += 1

    def on_events(self, events: list[Event]) -> None:
        """Events whose path is the open file."""

    def _current(self):
        """A check for async results: false once another file has been opened."""
        generation = self._generation
        return lambda: generation == self._generation
