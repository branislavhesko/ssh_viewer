"""Moves backend results and callbacks onto the Qt thread."""
from __future__ import annotations

from concurrent.futures import CancelledError, Future
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot


class Bridge(QObject):
    _posted = Signal(object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def post(self, function: Callable[[], None]) -> None:
        """Run ``function`` on the Qt thread. Safe from any thread."""
        self._posted.emit(function)

    def then(self, future: Future, on_success: Callable[[Any], None],
             on_error: Optional[Callable[[Exception], None]] = None) -> None:
        def finished(done: Future) -> None:
            try:
                result = done.result()
            except CancelledError:
                return
            except Exception as exc:
                if on_error is not None:
                    # Bind now: Python unsets ``exc`` when this block ends.
                    self.post(lambda error=exc: on_error(error))
                return
            self.post(lambda: on_success(result))
        future.add_done_callback(finished)

    @Slot(object)
    def _run(self, function: Callable[[], None]) -> None:
        function()


def display_text(name: str) -> str:
    """Remote names can hold undecodable bytes; Qt strings cannot."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
