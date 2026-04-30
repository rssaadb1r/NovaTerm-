"""Sidebar tree of saved sessions (Feature 1, Feature 8 colour stripes)."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.session_store import Folder, Session, SessionStore

from .colors import PALETTE, color_for


class SessionManagerPanel(QWidget):
    """Collapsible left sidebar with tree, filter bar and color-dot filters."""

    session_activated = pyqtSignal(int)  # session id (double-click)
    session_edit_requested = pyqtSignal(int)
    session_clone_requested = pyqtSignal(int)
    session_delete_requested = pyqtSignal(int)
    session_new_requested = pyqtSignal()

    def __init__(self, store: SessionStore, parent: QWidget | None = None) -> None:
        """Build the panel widgets and load sessions from ``store``."""
        super().__init__(parent)
        self._store = store
        self._color_filter: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Filter bar.
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("Filter sessions …")
        self._filter.textChanged.connect(self.refresh)
        layout.addWidget(self._filter)

        # Color-dot filter row.
        dot_row = QHBoxLayout()
        dot_row.setContentsMargins(0, 0, 0, 0)
        dot_row.setSpacing(2)
        for tag in PALETTE:
            btn = QPushButton(self)
            btn.setFixedSize(18, 18)
            btn.setCheckable(True)
            btn.setStyleSheet(
                f"QPushButton {{ background:{PALETTE[tag]}; border-radius:9px; }}"
                "QPushButton:checked { border:2px solid white; }"
            )
            btn.clicked.connect(self._make_color_filter(tag))
            dot_row.addWidget(btn)
        dot_row.addStretch(1)
        layout.addLayout(dot_row)

        # Tree.
        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._tree, 1)

        self.refresh()

    # -- public API --------------------------------------------------------

    def refresh(self) -> None:
        """Reload the tree contents from the database."""
        self._tree.clear()
        query = self._filter.text().strip().lower()

        folders = {f.id: f for f in self._store.list_folders()}
        folder_items: dict[int | None, QTreeWidgetItem] = {None: self._tree.invisibleRootItem()}

        # Folders.
        for fid, folder in folders.items():
            item = QTreeWidgetItem([folder.name])
            item.setData(0, Qt.ItemDataRole.UserRole, ("folder", fid))
            folder_items[fid] = item

        for fid, folder in folders.items():
            parent = folder_items.get(folder.parent_id, self._tree.invisibleRootItem())
            parent.addChild(folder_items[fid])

        # Sessions.
        sessions = self._store.list_sessions()
        for sess in sessions:
            if query and query not in (sess.name or "").lower() \
                    and query not in (sess.hostname or "").lower() \
                    and query not in (sess.group_tag or "").lower():
                continue
            if self._color_filter and (sess.color_tag or "").lower() != self._color_filter:
                continue
            item = QTreeWidgetItem([sess.name])
            item.setData(0, Qt.ItemDataRole.UserRole, ("session", sess.id))
            color = color_for(sess.color_tag)
            if color is not None:
                pix = QPixmap(12, 12)
                pix.fill(color)
                item.setIcon(0, QIcon(pix))
                item.setForeground(0, QBrush(QColor("#dddddd")))
            parent = folder_items.get(sess.folder_id, self._tree.invisibleRootItem())
            parent.addChild(item)

        self._tree.expandAll()

    # -- internals ---------------------------------------------------------

    def _make_color_filter(self, tag: str) -> Callable[[bool], None]:
        """Closure factory for the colour-dot filter buttons."""

        def toggle(checked: bool) -> None:
            self._color_filter = tag if checked else None
            # Untoggle other dots.
            for child in self.findChildren(QPushButton):
                if child.isCheckable() and child.isChecked() and child.styleSheet().find(PALETTE[tag]) == -1:
                    child.setChecked(False)
            self.refresh()

        return toggle

    def _selected_session_id(self) -> int | None:
        """Return the id of the currently selected *session* row, if any."""
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data[0] == "session":
            return int(data[1])
        return None

    def _on_double_click(self, item: QTreeWidgetItem) -> None:
        """Activate (open a tab for) the session under the cursor."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data[0] == "session":
            self.session_activated.emit(int(data[1]))

    def _show_context_menu(self, point) -> None:
        """Right-click: New / Edit / Clone / Delete / Rename."""
        menu = QMenu(self)
        new_act = QAction("New session …", self)
        new_act.triggered.connect(self.session_new_requested.emit)
        menu.addAction(new_act)

        sid = self._selected_session_id()
        if sid is not None:
            edit = QAction("Edit", self)
            edit.triggered.connect(lambda: self.session_edit_requested.emit(sid))
            menu.addAction(edit)
            clone = QAction("Clone", self)
            clone.triggered.connect(lambda: self.session_clone_requested.emit(sid))
            menu.addAction(clone)
            delete = QAction("Delete", self)
            delete.triggered.connect(lambda: self.session_delete_requested.emit(sid))
            menu.addAction(delete)

        menu.exec(self._tree.mapToGlobal(point))
