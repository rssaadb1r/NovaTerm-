"""Sidebar tree of saved sessions (Feature 1, Feature 8 colour stripes).

Also hosts the SecureCRT-style pinned *Default Session* entry at the top
of the tree, the folder context menu (New Folder / New Subfolder /
Rename / Delete), and drag-and-drop of sessions between folders.
"""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.session_store import SessionStore

from .colors import PALETTE, color_for


# A custom Qt UserRole offset for the (kind, id) tuple we attach to items.
_ROLE = Qt.ItemDataRole.UserRole

# Sentinel id used by the pinned default-session entry. Real session/folder
# rows are positive integers, so 0 is safe to flag the synthetic top entry.
_DEFAULT_SESSION_ID = 0


def _make_default_icon() -> QIcon:
    """Return a small gear-ish icon used for the pinned default entry."""
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor("#3498db"), 1.5))
    p.setBrush(QColor("#1abc9c"))
    p.drawEllipse(2, 2, 10, 10)
    p.setPen(QPen(QColor("white"), 1))
    p.drawLine(7, 5, 7, 9)
    p.drawLine(5, 7, 9, 7)
    p.end()
    return QIcon(pix)


def _make_folder_icon() -> QIcon:
    """Return a small folder pictogram for folder nodes."""
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#e2b53d"))
    p.drawRect(1, 5, 12, 7)
    p.setBrush(QColor("#f1c40f"))
    p.drawRect(1, 3, 5, 3)
    p.end()
    return QIcon(pix)


class _SessionTreeWidget(QTreeWidget):
    """`QTreeWidget` subclass that handles drag-drop of sessions into folders.

    Default :class:`QTreeWidget` already supports `InternalMove`, but the
    bookkeeping (which folder a session ends up in) lives in the database,
    not in the widget. We override :meth:`dropEvent` to: (1) accept the
    drop, (2) introspect the source/target items, (3) emit a signal so the
    panel can update SQLite + refresh the tree from authoritative data.
    """

    session_moved_to_folder = pyqtSignal(int, object)  # session id, folder id | None
    folder_moved = pyqtSignal(int, object)  # folder id, new parent id | None

    def dropEvent(self, event):  # noqa: N802 — Qt API
        target_item = self.itemAt(event.position().toPoint())
        source_items = self.selectedItems()
        if not source_items:
            super().dropEvent(event)
            return

        target_kind: str | None = None
        target_id: int | None = None
        if target_item is not None:
            data = target_item.data(0, _ROLE)
            if isinstance(data, tuple):
                target_kind, target_id = data[0], int(data[1])

        moves: list[tuple[str, int, int | None]] = []
        for src in source_items:
            data = src.data(0, _ROLE)
            if not isinstance(data, tuple):
                continue
            kind, _id = data
            if kind == "session":
                # A session can be dropped onto a folder (move into it),
                # onto a sibling session inside a folder (same target
                # folder), or onto empty space (no folder).
                if target_kind == "folder":
                    moves.append(("session", int(_id), target_id))
                elif target_kind == "session" and target_item is not None:
                    parent = target_item.parent()
                    if parent is not None and isinstance(
                        parent.data(0, _ROLE), tuple
                    ) and parent.data(0, _ROLE)[0] == "folder":
                        moves.append(("session", int(_id), int(parent.data(0, _ROLE)[1])))
                    else:
                        moves.append(("session", int(_id), None))
                else:
                    moves.append(("session", int(_id), None))
            elif kind == "folder":
                # Don't allow dropping a folder onto itself or its own
                # descendants — Qt's default move would produce a cycle.
                if target_kind == "folder" and target_id == int(_id):
                    continue
                if target_kind == "folder":
                    moves.append(("folder", int(_id), target_id))
                else:
                    moves.append(("folder", int(_id), None))

        # Suppress Qt's default move; we'll repaint from authoritative data
        # after the parent panel applies the change.
        event.ignore()
        for kind, mid, parent in moves:
            if kind == "session":
                self.session_moved_to_folder.emit(mid, parent)
            else:
                self.folder_moved.emit(mid, parent)


class SessionManagerPanel(QWidget):
    """Collapsible left sidebar with tree, filter bar and color-dot filters."""

    session_activated = pyqtSignal(int)  # session id (double-click)
    session_edit_requested = pyqtSignal(int)
    session_clone_requested = pyqtSignal(int)
    session_delete_requested = pyqtSignal(int)
    session_new_requested = pyqtSignal(object)  # parent folder id | None
    default_session_edit_requested = pyqtSignal()

    def __init__(self, store: SessionStore, parent: QWidget | None = None) -> None:
        """Build the panel widgets and load sessions from ``store``."""
        super().__init__(parent)
        self._store = store
        self._color_filter: str | None = None
        # Suppress side-effects in ``itemExpanded`` / ``itemCollapsed`` while
        # we programmatically restore the persisted state during ``refresh``.
        self._restoring_state = False

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
        self._tree = _SessionTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setDragEnabled(True)
        self._tree.setAcceptDrops(True)
        self._tree.setDropIndicatorShown(True)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.itemExpanded.connect(self._on_expanded)
        self._tree.itemCollapsed.connect(self._on_collapsed)
        self._tree.session_moved_to_folder.connect(self._on_session_moved)
        self._tree.folder_moved.connect(self._on_folder_moved)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._tree, 1)

        self.refresh()

    # -- public API --------------------------------------------------------

    def refresh(self) -> None:
        """Reload the tree contents from the database."""
        self._restoring_state = True
        try:
            self._tree.clear()
            query = self._filter.text().strip().lower()

            # Note: the Default Session is intentionally *not* mounted in
            # the sidebar tree — it has its own toolbar / Options menu
            # entries and the tree should only show folders + sessions.

            folders = {f.id: f for f in self._store.list_folders()}
            folder_items: dict[int | None, QTreeWidgetItem] = {
                None: self._tree.invisibleRootItem(),
            }

            # Folders.
            for fid, folder in folders.items():
                item = QTreeWidgetItem([folder.name])
                item.setData(0, _ROLE, ("folder", fid))
                item.setIcon(0, _make_folder_icon())
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled
                    | Qt.ItemFlag.ItemIsEditable
                )
                folder_items[fid] = item

            for fid, folder in folders.items():
                parent = folder_items.get(folder.parent_id, self._tree.invisibleRootItem())
                parent.addChild(folder_items[fid])

            # Sessions.
            sessions = self._store.list_sessions()
            for sess in sessions:
                if (
                    query
                    and query not in (sess.name or "").lower()
                    and query not in (sess.hostname or "").lower()
                    and query not in (sess.group_tag or "").lower()
                ):
                    continue
                if (
                    self._color_filter
                    and (sess.color_tag or "").lower() != self._color_filter
                ):
                    continue
                item = QTreeWidgetItem([sess.name])
                item.setData(0, _ROLE, ("session", sess.id))
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsDragEnabled
                )
                color = color_for(sess.color_tag)
                if color is not None:
                    pix = QPixmap(12, 12)
                    pix.fill(color)
                    item.setIcon(0, QIcon(pix))
                    item.setForeground(0, QBrush(QColor("#dddddd")))
                parent = folder_items.get(
                    sess.folder_id, self._tree.invisibleRootItem()
                )
                parent.addChild(item)

            # Restore expand/collapse state.
            for fid, folder in folders.items():
                folder_items[fid].setExpanded(bool(folder.is_expanded))
        finally:
            self._restoring_state = False

    # -- selection helpers ------------------------------------------------

    def _selected_session_id(self) -> int | None:
        """Return the id of the currently selected *session* row, if any."""
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, _ROLE)
        if isinstance(data, tuple) and data[0] == "session":
            return int(data[1])
        return None

    def _selected_folder_id(self) -> int | None:
        """Return the id of the currently selected *folder* row, if any."""
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, _ROLE)
        if isinstance(data, tuple) and data[0] == "folder":
            return int(data[1])
        return None

    # -- event handlers ---------------------------------------------------

    def _on_double_click(self, item: QTreeWidgetItem) -> None:
        """Activate the session under the cursor / open the default-session editor."""
        data = item.data(0, _ROLE)
        if not isinstance(data, tuple):
            return
        kind = data[0]
        if kind == "session":
            self.session_activated.emit(int(data[1]))
        elif kind == "default_session":
            self.default_session_edit_requested.emit()
        elif kind == "folder":
            # Inline rename via double-click. The widget's ``editItem``
            # API is enough; we persist the new name via ``itemChanged``.
            self._tree.editItem(item, 0)
            # Hook up a one-shot rename committer.
            self._tree.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        """Commit a folder rename if the user finished an inline edit."""
        if self._restoring_state:
            return
        data = item.data(0, _ROLE)
        if isinstance(data, tuple) and data[0] == "folder":
            new_name = item.text(0).strip() or "Untitled"
            try:
                self._store.update_folder(int(data[1]), name=new_name)
            except KeyError:
                pass
        # Detach this one-shot connection — refresh() rebuilds the tree
        # afterwards anyway.
        try:
            self._tree.itemChanged.disconnect(self._on_item_changed)
        except TypeError:
            pass

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        """Persist the expanded state for a folder node."""
        if self._restoring_state:
            return
        data = item.data(0, _ROLE)
        if isinstance(data, tuple) and data[0] == "folder":
            self._store.set_folder_expanded(int(data[1]), True)

    def _on_collapsed(self, item: QTreeWidgetItem) -> None:
        """Persist the collapsed state for a folder node."""
        if self._restoring_state:
            return
        data = item.data(0, _ROLE)
        if isinstance(data, tuple) and data[0] == "folder":
            self._store.set_folder_expanded(int(data[1]), False)

    def _on_session_moved(self, sid: int, folder_id: object) -> None:
        """Persist a drag-drop move of a session into a folder."""
        target = None if folder_id is None else int(folder_id)  # type: ignore[arg-type]
        try:
            self._store.update_session(sid, folder_id=target)
        except KeyError:
            return
        self.refresh()

    def _on_folder_moved(self, fid: int, new_parent: object) -> None:
        """Persist a drag-drop move of a folder under a new parent."""
        if new_parent is not None and self._creates_folder_cycle(fid, int(new_parent)):
            QMessageBox.warning(
                self, "Move folder", "Cannot move a folder into one of its descendants."
            )
            self.refresh()
            return
        target = None if new_parent is None else int(new_parent)  # type: ignore[arg-type]
        try:
            self._store.update_folder(fid, parent_id=target)
        except KeyError:
            return
        self.refresh()

    def _creates_folder_cycle(self, fid: int, new_parent: int) -> bool:
        """Return ``True`` if reparenting ``fid`` under ``new_parent`` would loop."""
        folders = {f.id: f for f in self._store.list_folders()}
        cur = folders.get(new_parent)
        while cur is not None:
            if cur.id == fid:
                return True
            if cur.parent_id is None:
                return False
            cur = folders.get(cur.parent_id)
        return False

    # -- context menu -----------------------------------------------------

    def _show_context_menu(self, point) -> None:
        """Right-click: New / Edit / Clone / Delete + folder ops."""
        menu = QMenu(self)

        item = self._tree.itemAt(point)
        kind: str | None = None
        target_id: int | None = None
        if item is not None:
            data = item.data(0, _ROLE)
            if isinstance(data, tuple):
                kind, target_id = data[0], int(data[1])

        if kind == "default_session":
            edit_default = QAction("Edit Default Session…", self)
            edit_default.triggered.connect(self.default_session_edit_requested.emit)
            menu.addAction(edit_default)
            menu.exec(self._tree.mapToGlobal(point))
            return

        # New session — defaults to the folder under the cursor when right-
        # clicking a folder, otherwise the top level.
        new_act = QAction("New session…", self)
        parent_folder = target_id if kind == "folder" else None
        new_act.triggered.connect(
            lambda: self.session_new_requested.emit(parent_folder)
        )
        menu.addAction(new_act)

        if kind == "session" and target_id is not None:
            edit = QAction("Edit", self)
            edit.triggered.connect(
                lambda _checked=False, sid=target_id: self.session_edit_requested.emit(sid)
            )
            menu.addAction(edit)
            clone = QAction("Clone", self)
            clone.triggered.connect(
                lambda _checked=False, sid=target_id: self.session_clone_requested.emit(sid)
            )
            menu.addAction(clone)
            delete = QAction("Delete", self)
            delete.triggered.connect(
                lambda _checked=False, sid=target_id: self.session_delete_requested.emit(sid)
            )
            menu.addAction(delete)

        menu.addSeparator()
        new_folder_act = QAction(
            "New subfolder…" if kind == "folder" else "New folder…", self
        )
        parent_for_folder = target_id if kind == "folder" else None
        new_folder_act.triggered.connect(
            lambda: self._create_folder(parent_for_folder)
        )
        menu.addAction(new_folder_act)

        if kind == "folder" and target_id is not None:
            rename = QAction("Rename folder…", self)
            rename.triggered.connect(
                lambda _checked=False, fid=target_id: self._rename_folder(fid)
            )
            menu.addAction(rename)
            del_folder = QAction("Delete folder", self)
            del_folder.triggered.connect(
                lambda _checked=False, fid=target_id: self._delete_folder(fid)
            )
            menu.addAction(del_folder)

        menu.exec(self._tree.mapToGlobal(point))

    # -- folder operations ------------------------------------------------

    def _create_folder(self, parent_id: int | None) -> None:
        """Prompt for a folder name and persist it under ``parent_id``."""
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        self._store.create_folder(name.strip(), parent_id=parent_id)
        self.refresh()

    def _rename_folder(self, fid: int) -> None:
        """Prompt for a new name for an existing folder."""
        existing = next(
            (f for f in self._store.list_folders() if f.id == fid), None
        )
        if existing is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename folder", "New name:", QLineEdit.EchoMode.Normal, existing.name
        )
        if not ok or not name.strip():
            return
        self._store.update_folder(fid, name=name.strip())
        self.refresh()

    def _delete_folder(self, fid: int) -> None:
        """Confirm and delete a folder; sessions inside drop to the top level."""
        if QMessageBox.question(
            self,
            "Delete folder",
            "Delete this folder? Sessions inside will move to the top level.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._store.delete_folder(fid)
        self.refresh()

    # -- color filter -----------------------------------------------------

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
