from __future__ import annotations

import os
import posixpath
from typing import Callable, Optional

from PySide6.QtCore import QModelIndex, QPoint, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QSpinBox, QSplitter, QTableView,
                               QTabWidget, QToolBar, QTreeView)

from app import theme
from app.backend import (AuthRequired, Backend, ConnectParams, ConnectionState, Event, EventKind,
                         FileEntry, HostKeyUnknown)
from app.bridge import Bridge, display_text
from app.models.file_list_model import FileListModel
from app.models.tree_model import TreeModel
from app.settings import Settings, ssh_config_hosts
from app.transfer_panel import TransferPanel, job_summary, job_title
from app.transfers import Job
from app.viewers.base import ViewerContext
from app.viewers.preview import Preview

MAX_SEARCH_RESULTS = 10000


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
        self.cache = QSpinBox(minimum=32, maximum=65536, suffix=" MB", value=settings.cache_mb)
        self.watcher = QComboBox()
        self.watcher.addItems(["auto", "inotify", "polling"])
        self.watcher.setCurrentText(settings.watcher)
        self.theme = QComboBox()
        self.theme.addItems(["auto", "light", "dark"])
        self.theme.setCurrentText(settings.theme)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 16)
        form.addRow("Theme", self.theme)
        form.addRow("Cache size (applies on restart)", self.cache)
        form.addRow("Change detection (applies on connect)", self.watcher)
        form.addRow(buttons)

    def accept(self) -> None:
        self.settings.cache_mb = self.cache.value()
        self.settings.watcher = self.watcher.currentText()
        self.settings.theme = self.theme.currentText()
        theme.apply(QApplication.instance(), self.settings.theme)
        super().accept()


class MainWindow(QMainWindow):
    state_changed = Signal(object, str)

    def __init__(self, backend: Backend, settings: Optional[Settings] = None):
        super().__init__()
        self.backend = backend
        self.settings = settings or Settings()
        self.bridge = Bridge(self)
        self.setWindowTitle("Remote Viewer")
        self.resize(1400, 850)

        self.tree_model = TreeModel(backend, self.bridge, self)
        self.list_model = FileListModel(backend, self.bridge, self)
        self.preview = Preview(ViewerContext(backend, self.bridge))
        self.preview.message.connect(self._message)
        self.preview.stayed.connect(self._select_open_file)
        self.transfers = TransferPanel(backend, self.bridge)
        self.transfers.active_changed.connect(self._transfers_changed)
        self.transfers.finished.connect(self._transfer_finished)
        self._local_folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation)
        self._build_toolbar()
        self._build_panes()

        self._expanded: set[str] = set()
        self._search = None
        self._search_count = 0
        self._params: Optional[ConnectParams] = None
        self._initial_path: Optional[str] = None

        self.state_changed.connect(self._on_state)
        backend.on_state(lambda state, detail: self.state_changed.emit(state, detail))
        backend.on_events(lambda events: self.bridge.post(lambda: self._on_events(events)))

    # Construction ------------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = QToolBar("Connection", movable=False)
        self.addToolBar(bar)
        self.host = QComboBox(editable=True)
        self.host.setMinimumWidth(240)
        self.host.lineEdit().setPlaceholderText("user@host or ~/.ssh/config alias")
        hosts = self.settings.recent_hosts
        self.host.addItems(hosts + [h for h in ssh_config_hosts() if h not in hosts])
        self.host.lineEdit().returnPressed.connect(self.connect_clicked)
        self.connect_button = QPushButton("Connect", objectName="primary")
        self.connect_button.clicked.connect(self.connect_clicked)
        self.path = QLineEdit(placeholderText="Path")
        self.path.returnPressed.connect(lambda: self.navigate(self.path.text()))
        self.search = QLineEdit(placeholderText="Find files under this folder…",
                                clearButtonEnabled=True)
        self.search.setMaximumWidth(280)
        self.search.returnPressed.connect(self._start_search)
        upload = QPushButton("Upload")
        upload_menu = QMenu(upload)
        self._add_upload_actions(upload_menu, lambda: self.list_model.directory)
        upload.setMenu(upload_menu)
        download = QPushButton("Download")
        download.clicked.connect(lambda: self._download(self._selected_paths()))
        preferences = QPushButton("Settings")
        preferences.clicked.connect(lambda: SettingsDialog(self.settings, self).exec())
        for widget in (self.host, self.connect_button, self.path, self.search, upload, download,
                       preferences):
            bar.addWidget(widget)
        self.watcher_label = QLabel()
        self.statusBar().addPermanentWidget(self.watcher_label)

    def _build_panes(self) -> None:
        self.tree = QTreeView(objectName="sidebar")
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.expanded.connect(self._tree_expanded)
        self.tree.collapsed.connect(self._tree_collapsed)
        self.tree.selectionModel().currentChanged.connect(self._tree_selected)
        self.tree_model.load_failed.connect(lambda p, e: self._message(f"{display_text(p)}: {e}"))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)

        self.table = QTableView()
        self.table.setModel(self.list_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 10)
        # Fixed-size rows and manual columns: nothing here may touch every row.
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 175)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.selectionModel().currentRowChanged.connect(self._row_selected)
        self.table.doubleClicked.connect(self._row_activated)
        self.list_model.listing_failed.connect(lambda p, e: self._message(f"{display_text(p)}: {e}"))
        self.list_model.listing_ready.connect(self._listing_ready)

        self.results = QListWidget()
        self.results.currentItemChanged.connect(self._result_selected)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.table, "Files")
        self.tabs.addTab(self.results, "Search")
        self.tabs.addTab(self.transfers, "Transfers")

        self.tabs.setDocumentMode(True)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(1)
        split.addWidget(self.tree)
        split.addWidget(self.tabs)
        split.addWidget(self.preview)
        split.setSizes([260, 460, 680])
        split.setStretchFactor(2, 1)
        self.setCentralWidget(split)
        self.setAcceptDrops(True)

    def _message(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000 if text else 1)

    # Connection --------------------------------------------------------------
    def connect_to(self, target: str, path: Optional[str] = None) -> None:
        self.host.setCurrentText(target)
        self._initial_path = path
        self.connect_clicked()

    def connect_clicked(self) -> None:
        target = self.host.currentText().strip()
        if not target:
            return
        username, _, host = target.rpartition("@")
        self._params = ConnectParams(host=host, username=username or None,
                                     watcher=self.settings.watcher)
        self._connect()

    def _connect(self) -> None:
        # Transfers name paths, so one left running would carry on at the new host.
        self.preview.leave_editor(lambda: self.transfers.stop(self._connect_now))

    def _connect_now(self) -> None:
        self.connect_button.setEnabled(False)
        self.preview.clear()
        self.bridge.then(self.backend.connect(self._params), self._connected, self._connect_failed)

    def _connected(self, home: str) -> None:
        self.connect_button.setEnabled(True)
        self.settings.remember_host(self.host.currentText().strip())
        self.tree_model.reset_root("/")
        self._expanded = set()
        self.navigate(self._initial_path or home)

    def _connect_failed(self, exc: Exception) -> None:
        self.connect_button.setEnabled(True)
        if isinstance(exc, HostKeyUnknown):
            answer = QMessageBox.question(
                self, "Unknown host",
                f"The authenticity of {exc.host}:{exc.port} can't be established.\n\n"
                f"Key fingerprint: {exc.fingerprint}\n\nTrust this key and add it to known_hosts?")
            if answer == QMessageBox.StandardButton.Yes:
                self.bridge.then(self.backend.trust_host_key(exc), lambda _: self._connect(),
                                 self._connect_failed)
        elif isinstance(exc, AuthRequired):
            secret, accepted = QInputDialog.getText(
                self, "Authentication", f"Password or key passphrase for {self._params.host}:",
                QLineEdit.EchoMode.Password)
            if accepted and secret:
                self._params.password = self._params.passphrase = secret
                self._connect()
        else:
            QMessageBox.critical(self, "Connection failed", str(exc) or type(exc).__name__)

    def _on_state(self, state: ConnectionState, detail: str) -> None:
        text = {ConnectionState.CONNECTING: f"Connecting to {detail}…",
                ConnectionState.CONNECTED: f"Connected to {detail}",
                ConnectionState.RECONNECTING: "Connection lost, reconnecting…",
                ConnectionState.DISCONNECTED: detail or "Disconnected"}[state]
        self._message(text)
        self.watcher_label.setText(state.value)

    # Navigation --------------------------------------------------------------
    def navigate(self, path: str) -> None:
        path = posixpath.normpath(path) if path else "/"
        self.path.setText(display_text(path))
        self.list_model.set_directory(path)
        self.tabs.setCurrentWidget(self.table)
        self.tree_model.reveal(path, self.tree.expand, self._reveal_done)
        self._update_watch()

    def _reveal_done(self, index: QModelIndex) -> None:
        self.tree.blockSignals(True)
        self.tree.setCurrentIndex(index)
        self.tree.blockSignals(False)
        self.tree.scrollTo(index)

    def _tree_expanded(self, index: QModelIndex) -> None:
        self._expanded.add(index.data(Qt.ItemDataRole.UserRole))
        self._update_watch()

    def _tree_collapsed(self, index: QModelIndex) -> None:
        self._expanded.discard(index.data(Qt.ItemDataRole.UserRole))
        self._update_watch()

    def _tree_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        path = current.data(Qt.ItemDataRole.UserRole)
        if path and path != self.list_model.directory:
            self.path.setText(display_text(path))
            self.list_model.set_directory(path)
            self._update_watch()

    def _listing_ready(self, path: str) -> None:
        self._message(f"{self.list_model.rowCount():,} items")
        self.watcher_label.setText(self.backend.watcher_mode())

    def _row_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        entry = self.list_model.entry(current.row())
        if entry.is_symlink:
            self._resolve_link(entry, enter=False)
        elif not entry.is_dir:
            self._open_file(entry)

    def _row_activated(self, index: QModelIndex) -> None:
        entry = self.list_model.entry(index.row())
        if entry.is_symlink:
            self._resolve_link(entry, enter=True)
        elif entry.is_dir:
            self.navigate(entry.path)

    def _resolve_link(self, entry: FileEntry, enter: bool) -> None:
        def done(target: FileEntry) -> None:
            if not target.is_dir:
                self._open_file(target)
            elif enter:
                self.navigate(entry.path)
        self.bridge.then(self.backend.stat(entry.path), done,
                         lambda exc: self._message(f"Broken link: {exc}"))

    def _open_file(self, entry: FileEntry) -> None:
        self.preview.open(entry)
        self._update_watch()

    def _select_open_file(self) -> None:
        """Point the list back at the file still being edited."""
        row = self.list_model.row_of(self.preview.entry.path)
        if row >= 0:
            self.table.selectRow(row)
        else:
            self.table.setCurrentIndex(QModelIndex())

    def _update_watch(self) -> None:
        directories = set(self._expanded)
        hot = set()
        if self.list_model.directory:
            directories.add(self.list_model.directory)
            hot.add(self.list_model.directory)
        if self.preview.entry is not None:
            parent = posixpath.dirname(self.preview.entry.path)
            directories.add(parent)
            hot |= {parent, self.preview.entry.path}
        self.backend.watch(directories, hot)

    def _on_events(self, events: list[Event]) -> None:
        self.tree_model.apply_events(events)
        self.list_model.apply_events(events)
        self.preview.on_events(events)

    # Transfers ---------------------------------------------------------------
    def _add_upload_actions(self, menu: QMenu, folder: Callable[[], str]) -> None:
        menu.addAction("Upload files…", lambda: self._upload(self.pick_files(), folder()))
        menu.addAction("Upload folder…", lambda: self._upload(self.pick_folder("Upload folder"), folder()))

    def pick_files(self) -> list[str]:
        return QFileDialog.getOpenFileNames(self, "Upload files", self._local_folder)[0]

    def pick_folder(self, title: str) -> list[str]:
        """Dialogs for files and folders are separate on every platform."""
        folder = QFileDialog.getExistingDirectory(self, title, self._local_folder)
        return [folder] if folder else []

    def _upload(self, sources: list[str], folder: str) -> None:
        if not folder:
            self._message("Connect to a server first")
        elif sources:
            self._local_folder = os.path.dirname(os.path.normpath(sources[0]))
            self._transfer_started(self.transfers.upload(sources, folder))

    def _selected_paths(self) -> list[str]:
        if self.tabs.currentWidget() is self.results:
            return [item.data(Qt.ItemDataRole.UserRole) for item in self.results.selectedItems()]
        rows = self.table.selectionModel().selectedRows()
        return [self.list_model.entry(index.row()).path for index in rows]

    def _download(self, paths: list[str]) -> None:
        if not paths:
            self._message("Select what to download first")
            return
        for folder in self.pick_folder("Download to"):
            self._local_folder = folder
            self._transfer_started(self.transfers.download(paths, folder))

    def _table_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        paths = self._selected_paths()
        if paths:
            menu.addAction("Download…", lambda: self._download(paths))
        self._add_upload_actions(menu, lambda: self.list_model.directory)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _tree_menu(self, position: QPoint) -> None:
        path = self.tree.indexAt(position).data(Qt.ItemDataRole.UserRole)
        if path:
            menu = QMenu(self)
            menu.addAction("Download…", lambda: self._download([path]))
            self._add_upload_actions(menu, lambda: path)
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _transfer_started(self, job: Job) -> None:
        self._message(f"{job_title(job)}: see the Transfers tab")

    def _transfers_changed(self, active: int) -> None:
        self.tabs.setTabText(self.tabs.indexOf(self.transfers),
                             f"Transfers ({active})" if active else "Transfers")

    def _transfer_finished(self, job: Job) -> None:
        self._message(f"{job_title(job)}: {job_summary(job)}")
        if job.upload:
            # Polling can't see a change in the same second as its last look.
            self._on_events([Event(EventKind.RESYNC, job.destination, True)])

    def dragEnterEvent(self, event) -> None:
        if self.list_model.directory and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        self._upload([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()],
                     self.list_model.directory)
        event.acceptProposedAction()

    # Search ------------------------------------------------------------------
    def _start_search(self) -> None:
        pattern = self.search.text().strip()
        if self._search is not None:
            self._search.cancel()
            self._search = None
        self.results.clear()
        self._search_count = 0
        if not pattern or not self.list_model.directory:
            return
        self.tabs.setCurrentWidget(self.results)
        self._message("Searching…")
        token = object()
        self._search_token = token

        def results(paths: list[str]) -> None:
            self.bridge.post(lambda: self._search_token is token and self._add_results(paths))

        def finished(error: Optional[str]) -> None:
            self.bridge.post(lambda: self._search_token is token and self._search_done(error))
        self._search = self.backend.search(self.list_model.directory, pattern, results, finished)

    def _add_results(self, paths: list[str]) -> None:
        for path in paths[:MAX_SEARCH_RESULTS - self._search_count]:
            item = QListWidgetItem(display_text(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.results.addItem(item)
        self._search_count += len(paths)
        if self._search_count >= MAX_SEARCH_RESULTS and self._search is not None:
            self._search.cancel()

    def _search_done(self, error: Optional[str]) -> None:
        self._search = None
        if error and error != "cancelled":
            self._message(f"Search failed: {error}")
        else:
            self._message(f"{self.results.count():,} results")

    def _result_selected(self, item: Optional[QListWidgetItem], _previous) -> None:
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)

        def done(entry: FileEntry) -> None:
            if entry.is_dir:
                self.navigate(path)
            else:
                self._open_file(entry)
        self.bridge.then(self.backend.stat(path), done, lambda exc: self._message(str(exc)))

    def closeEvent(self, event) -> None:
        editor = self.preview.editor
        if editor is not None and editor.dirty:
            event.ignore()
            # Later, because Qt ignores close() during a close.
            self.preview.leave_editor(lambda: QTimer.singleShot(0, self.close))
            return
        if self.transfers.active():
            event.ignore()
            self.transfers.stop(lambda: QTimer.singleShot(0, self.close))
            return
        self.preview.clear()
        super().closeEvent(event)
