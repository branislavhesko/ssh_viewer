"""Lazy folder tree: one readdir per expand, patched in place from events."""
from __future__ import annotations

import bisect
import posixpath
from typing import Callable, Optional

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt, Signal

from app import theme
from app.backend import Backend, Event, EventKind, FileEntry
from app.bridge import Bridge, display_text


class Node:
    __slots__ = ("path", "name", "parent", "children", "loading", "is_symlink", "waiters")

    def __init__(self, path: str, name: str, parent: Optional["Node"], is_symlink: bool = False):
        self.path, self.name, self.parent = path, name, parent
        self.is_symlink = is_symlink
        self.children: Optional[list[Node]] = None  # None = not listed yet
        self.loading = False
        self.waiters: list[Callable[[], None]] = []

    @property
    def sort_key(self) -> str:
        return self.name.casefold()


class TreeModel(QAbstractItemModel):
    load_failed = Signal(str, str)

    def __init__(self, backend: Backend, bridge: Bridge, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._backend, self._bridge = backend, bridge
        self._root = Node("", "", None)
        self._root.children = []
        self._nodes: dict[str, Node] = {}
        self._folder_icon = theme.icon("folder")
        self._link_icon = theme.icon("folder-link")

    def reset_root(self, path: str = "/") -> None:
        self.beginResetModel()
        node = Node(path, path, self._root)
        self._root.children = [node]
        self._nodes = {path: node}
        self.endResetModel()

    # QAbstractItemModel ------------------------------------------------------
    def _node(self, index: QModelIndex) -> Node:
        return index.internalPointer() if index.isValid() else self._root

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        children = self._node(parent).children or []
        if 0 <= row < len(children) and column == 0:
            return self.createIndex(row, 0, children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        return self.index_of(index.internalPointer().parent)

    def index_of(self, node: Node) -> QModelIndex:
        if node is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(self._row(node), 0, node)

    def _row(self, node: Node) -> int:
        siblings = node.parent.children
        row = bisect.bisect_left(siblings, node.sort_key, key=lambda n: n.sort_key)
        if row < len(siblings) and siblings[row] is node:
            return row
        return siblings.index(node)  # names that casefold alike

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._node(parent).children or [])

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = self._node(parent)
        return node.children is None or bool(node.children)

    def canFetchMore(self, parent: QModelIndex) -> bool:
        node = self._node(parent)
        return node.children is None and not node.loading

    def fetchMore(self, parent: QModelIndex) -> None:
        self.load(self._node(parent))

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self._node(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return display_text(node.name)
        if role == Qt.ItemDataRole.DecorationRole:
            return self._link_icon if node.is_symlink else self._folder_icon
        if role == Qt.ItemDataRole.UserRole:
            return node.path
        return None

    # Loading -----------------------------------------------------------------
    def load(self, node: Node, then: Optional[Callable[[], None]] = None) -> None:
        if then is not None:
            node.waiters.append(then)
        if node.loading:
            return
        node.loading = True
        self._bridge.then(self._backend.listdir(node.path),
                          lambda entries: self._loaded(node, entries),
                          lambda exc: self._failed(node, exc))

    def _loaded(self, node: Node, entries: list[FileEntry]) -> None:
        node.loading = False
        if self._nodes.get(node.path) is not node:
            return
        self._merge(node, [e for e in entries if e.is_dir or e.is_symlink])
        waiters, node.waiters = node.waiters, []
        for waiter in waiters:
            waiter()

    def _failed(self, node: Node, exc: Exception) -> None:
        node.loading = False
        node.waiters.clear()
        if self._nodes.get(node.path) is not node:
            return
        # A symlink to a file looks expandable until we try.
        was_unlisted = node.children is None
        node.children = node.children or []
        if was_unlisted:
            index = self.index_of(node)
            self.dataChanged.emit(index, index)
        if not node.is_symlink:
            self.load_failed.emit(node.path, str(exc))

    def _merge(self, node: Node, entries: list[FileEntry]) -> None:
        parent_index = self.index_of(node)
        if node.children is None:
            children = [Node(e.path, e.name, node, e.is_symlink) for e in entries]
            children.sort(key=lambda n: n.sort_key)
            if children:
                self.beginInsertRows(parent_index, 0, len(children) - 1)
            node.children = children
            for child in children:
                self._nodes[child.path] = child
            if children:
                self.endInsertRows()
            else:
                self.dataChanged.emit(parent_index, parent_index)
            return
        wanted = {e.path: e for e in entries}
        for child in [c for c in node.children if c.path not in wanted]:
            self._remove(child)
        for path, entry in wanted.items():
            if path not in self._nodes:
                self._insert(node, entry.name, path, entry.is_symlink)

    def _insert(self, parent: Node, name: str, path: str, is_symlink: bool) -> None:
        child = Node(path, name, parent, is_symlink)
        row = bisect.bisect_left(parent.children, child.sort_key, key=lambda n: n.sort_key)
        self.beginInsertRows(self.index_of(parent), row, row)
        parent.children.insert(row, child)
        self._nodes[path] = child
        self.endInsertRows()

    def _remove(self, node: Node) -> None:
        row = self._row(node)
        self.beginRemoveRows(self.index_of(node.parent), row, row)
        node.parent.children.pop(row)
        self._forget(node)
        self.endRemoveRows()

    def _forget(self, node: Node) -> None:
        self._nodes.pop(node.path, None)
        for child in node.children or ():
            self._forget(child)

    # Patching ----------------------------------------------------------------
    def apply_events(self, events: list[Event]) -> None:
        for event in events:
            if event.kind is EventKind.RESYNC:
                node = self._nodes.get(event.path)
                if node is not None and node.children is not None:
                    self.load(node)
            elif event.is_dir:
                parent = self._nodes.get(posixpath.dirname(event.path))
                if parent is None or parent.children is None:
                    continue
                existing = self._nodes.get(event.path)
                if event.kind in (EventKind.CREATE, EventKind.MOVED_TO) and existing is None:
                    self._insert(parent, posixpath.basename(event.path), event.path, False)
                elif event.kind in (EventKind.DELETE, EventKind.MOVED_FROM) and existing:
                    self._remove(existing)

    # Navigation --------------------------------------------------------------
    def reveal(self, path: str, on_step: Callable[[QModelIndex], None],
               on_done: Callable[[QModelIndex], None]) -> None:
        """Load each folder down to ``path``. on_step gets every ancestor."""
        root = self._root.children[0] if self._root.children else None
        if root is None:
            return
        tail = path[len(root.path):].strip("/")
        parts = tail.split("/") if tail else []

        def descend(node: Node, remaining: list[str]) -> None:
            if not remaining:
                on_done(self.index_of(node))
                return
            on_step(self.index_of(node))

            def next_step() -> None:
                child = self._nodes.get(posixpath.join(node.path, remaining[0]))
                if child is not None:
                    descend(child, remaining[1:])
                else:
                    on_done(self.index_of(node))
            if node.children is None:
                self.load(node, next_step)
            else:
                next_step()
        descend(root, parts)
