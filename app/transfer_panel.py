"""The Transfers tab: one row per job, refreshed by a timer while any is active."""
from __future__ import annotations

import os
import posixpath
import time
from concurrent.futures import Future
from typing import Callable, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QMessageBox, QProgressBar, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from app.backend import Backend
from app.bridge import Bridge, display_text
from app.models.file_list_model import format_size
from app.transfers import Decision, Job, JobState, TransferManager

REFRESH_MS = 250
MAX_LISTED = 200
MAX_NAMED_CLASHES = 5
COLUMN_ITEM, COLUMN_PROGRESS, COLUMN_STATUS = range(3)


def job_title(job: Job) -> str:
    arrow = "↑" if job.upload else "↓"
    if len(job.sources) > 1:
        return f"{arrow} {len(job.sources)} items"
    name = os.path.basename(job.sources[0].rstrip(os.sep)) if job.upload \
        else posixpath.basename(job.sources[0].rstrip("/"))
    return f"{arrow} {display_text(name)}"


def job_summary(job: Job) -> str:
    """How a finished job went."""
    if job.state is JobState.FAILED:
        return f"Failed: {job.error}"
    if job.state is JobState.CANCELLED:
        return "Cancelled"
    text = f"Done, {job.done_files:,} {'file' if job.done_files == 1 else 'files'}"
    if job.failures:
        text += f", {len(job.failures):,} failed"
    if job.skipped:
        text += f", {len(job.skipped):,} skipped"
    return text


class _Row:
    def __init__(self, job: Job, item: QTreeWidgetItem, bar: QProgressBar):
        self.job, self.item, self.bar = job, item, bar
        self.seen_bytes = 0
        self.seen_at = time.monotonic()
        self.speed = 0.0
        self.closed = False


class TransferPanel(QWidget):
    active_changed = Signal(int)
    finished = Signal(object)

    def __init__(self, backend: Backend, bridge: Bridge, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._bridge = bridge
        self._manager = TransferManager(backend, self._ask)
        self._rows: list[_Row] = []
        self._after_stop: Optional[Callable[[], None]] = None

        self.view = QTreeWidget()
        self.view.setHeaderLabels(["Item", "Progress", "Status"])
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.view.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.view.setColumnWidth(COLUMN_ITEM, 150)
        self.view.setColumnWidth(COLUMN_PROGRESS, 90)
        self.view.itemActivated.connect(self._show_details)
        self.view.itemSelectionChanged.connect(self._update_buttons)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.clear_button = QPushButton("Clear finished")
        self.clear_button.clicked.connect(self._clear_finished)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.clear_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.addWidget(self.view)
        layout.addLayout(buttons)

        self._timer = QTimer(self, interval=REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._update_buttons()

    # Starting and stopping ---------------------------------------------------
    def upload(self, sources: list[str], directory: str) -> Job:
        return self._add(self._manager.upload(sources, directory))

    def download(self, paths: list[str], directory: str) -> Job:
        return self._add(self._manager.download(paths, directory))

    def _add(self, job: Job) -> Job:
        item = QTreeWidgetItem([job_title(job), "", "Waiting"])
        sources = "\n".join(display_text(source) for source in job.sources[:20])
        item.setToolTip(COLUMN_ITEM, f"{sources}\nto {display_text(job.destination)}")
        self.view.addTopLevelItem(item)
        bar = QProgressBar(maximum=1000, textVisible=False)
        self.view.setItemWidget(item, COLUMN_PROGRESS, bar)
        nothing_to_cancel = all(row.job.finished for row in self._selected())
        self._rows.append(_Row(job, item, bar))
        if nothing_to_cancel:
            self.view.clearSelection()
            item.setSelected(True)  # so Cancel works without a click first
        self._timer.start()
        self.active_changed.emit(self.active())
        return job

    def active(self) -> int:
        return sum(not row.job.finished for row in self._rows)

    def stop(self, then: Callable[[], None]) -> None:
        """Run ``then`` once no job is running, if the user agrees to cancel them."""
        count = self.active()
        if not count:
            then()
        elif self.confirm_stop(count):
            self._after_stop = then
            for row in self._rows:
                row.job.cancel()

    def confirm_stop(self, count: int) -> bool:
        answer = QMessageBox.question(self, "Transfers running",
                                      f"{count} transfer(s) are still running. Cancel them?")
        return answer == QMessageBox.StandardButton.Yes

    # Clashes -----------------------------------------------------------------
    def _ask(self, job: Job, targets: list[str]) -> Decision:
        """On a job thread: blocks it, not the UI, until the user answers."""
        answer: Future = Future()
        self._bridge.post(lambda: answer.set_result(self.confirm_replace(job, targets)))
        return answer.result()

    def confirm_replace(self, job: Job, targets: list[str]) -> Decision:
        names = [display_text(target) for target in targets[:MAX_NAMED_CLASHES]]
        if len(targets) > len(names):
            names.append(f"…and {len(targets) - len(names):,} more")
        box = QMessageBox(QMessageBox.Icon.Question, "Files already exist",
                          f"{len(targets):,} file(s) already exist:\n\n" + "\n".join(names), parent=self)
        replace = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        skip = box.addButton("Skip", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        return Decision.REPLACE if clicked is replace else Decision.SKIP if clicked is skip \
            else Decision.CANCEL

    # Rows --------------------------------------------------------------------
    def _refresh(self) -> None:
        for row in self._rows:
            if not row.closed:
                self._refresh_row(row)
        self._update_buttons()
        if not self.active():
            self._timer.stop()
            then, self._after_stop = self._after_stop, None
            if then is not None:
                then()

    def _refresh_row(self, row: _Row) -> None:
        job = row.job
        if job.total_bytes:
            row.bar.setValue(min(1000, job.done_bytes * 1000 // job.total_bytes))
        now = time.monotonic()
        elapsed = now - row.seen_at
        if elapsed > 0:
            row.speed = 0.7 * row.speed + 0.3 * (job.done_bytes - row.seen_bytes) / elapsed
        row.seen_bytes, row.seen_at = job.done_bytes, now
        if job.finished:
            self._close_row(row)
        elif job.state is JobState.SCANNING:
            row.item.setText(COLUMN_STATUS, "Scanning…")
        elif job.state is JobState.RUNNING:
            row.item.setText(COLUMN_STATUS,
                             f"{job.done_files:,} of {job.total_files:,} files, "
                             f"{format_size(job.done_bytes)} of {format_size(job.total_bytes)}, "
                             f"{format_size(int(max(row.speed, 0)))}/s")

    def _close_row(self, row: _Row) -> None:
        job = row.job
        row.closed = True
        if job.state is JobState.DONE and not job.failures:
            row.bar.setValue(1000)
        summary = job_summary(job)
        row.item.setText(COLUMN_STATUS, summary)
        if job.failures or job.skipped:
            row.item.setToolTip(COLUMN_STATUS, "Double-click for details")
        self.active_changed.emit(self.active())
        self.finished.emit(job)

    def _selected(self) -> list[_Row]:
        return [row for row in self._rows if row.item.isSelected()]

    def _update_buttons(self) -> None:
        self.cancel_button.setEnabled(any(not row.job.finished for row in self._selected()))
        self.clear_button.setEnabled(any(row.closed for row in self._rows))

    def _cancel_selected(self) -> None:
        for row in self._selected():
            row.job.cancel()

    def _clear_finished(self) -> None:
        for row in [row for row in self._rows if row.closed]:
            self._rows.remove(row)
            self.view.takeTopLevelItem(self.view.indexOfTopLevelItem(row.item))
        self._update_buttons()

    def _show_details(self, item: QTreeWidgetItem) -> None:
        job = next(row.job for row in self._rows if row.item is item)
        lines = [f"Failed: {line}" for line in job.failures] \
            + [f"Skipped: {line}" for line in job.skipped]
        if not lines:
            return
        hidden = len(lines) - MAX_LISTED
        text = "\n".join(display_text(line) for line in lines[:MAX_LISTED])
        box = QMessageBox(QMessageBox.Icon.Information, "Transfer details", job_summary(job),
                          parent=self)
        box.setDetailedText(text + (f"\n…and {hidden:,} more" if hidden > 0 else ""))
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.exec()
