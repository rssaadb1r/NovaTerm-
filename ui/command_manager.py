"""Command Manager dock + fuzzy-search popup (Feature 5)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.command_store import CommandStore


class CommandManagerPanel(QWidget):
    """Bottom dockable panel listing all commands, grouped by folder."""

    send_to_active = pyqtSignal(str, bool)  # text, append_enter
    send_to_all = pyqtSignal(str, bool)
    edit_command = pyqtSignal(int)
    new_command = pyqtSignal()
    delete_command = pyqtSignal(int)

    def __init__(self, store: CommandStore, parent: QWidget | None = None) -> None:
        """Build the panel and load the command list from ``store``."""
        super().__init__(parent)
        self._store = store

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        self._broadcast_cb = QCheckBox("Send to all", self)
        toolbar.addWidget(self._broadcast_cb)
        toolbar.addStretch(1)
        new_btn = QPushButton("New …", self)
        new_btn.clicked.connect(self.new_command.emit)
        toolbar.addWidget(new_btn)
        layout.addLayout(toolbar)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.itemClicked.connect(self._on_single_click)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._tree, 1)

        self.refresh()

    def refresh(self) -> None:
        """Reload commands + groups from the database."""
        self._tree.clear()
        groups = {g.id: g for g in self._store.list_groups()}
        group_items: dict[int | None, QTreeWidgetItem] = {None: self._tree.invisibleRootItem()}

        for gid, g in groups.items():
            item = QTreeWidgetItem([g.name])
            item.setData(0, Qt.ItemDataRole.UserRole, ("group", gid))
            self._tree.addTopLevelItem(item)
            group_items[gid] = item

        for cmd in self._store.list_commands():
            item = QTreeWidgetItem([cmd.name])
            item.setToolTip(0, cmd.command_text)
            item.setData(0, Qt.ItemDataRole.UserRole, ("command", cmd.id, cmd.command_text))
            parent = group_items.get(cmd.group_id, self._tree.invisibleRootItem())
            parent.addChild(item)

        self._tree.expandAll()

    # -- internals ---------------------------------------------------------

    def _payload(self, item: QTreeWidgetItem) -> tuple[str, int, str] | None:
        """Return ``("command", id, text)`` if ``item`` represents a command."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data[0] == "command":
            return data
        return None

    def _on_single_click(self, item: QTreeWidgetItem) -> None:
        """Single click: send command without trailing newline."""
        cmd = self._payload(item)
        if cmd is None:
            return
        self._dispatch(cmd[2], append_enter=False)

    def _on_double_click(self, item: QTreeWidgetItem) -> None:
        """Double click: send command + Enter."""
        cmd = self._payload(item)
        if cmd is None:
            return
        self._dispatch(cmd[2], append_enter=True)

    def _dispatch(self, text: str, *, append_enter: bool) -> None:
        """Forward to all-or-active depending on the toggle."""
        if self._broadcast_cb.isChecked():
            self.send_to_all.emit(text, append_enter)
        else:
            self.send_to_active.emit(text, append_enter)

    def _show_context_menu(self, point) -> None:
        """Right-click: edit / delete commands."""
        item = self._tree.itemAt(point)
        if item is None:
            return
        cmd = self._payload(item)
        if cmd is None:
            return

        menu = QMenu(self)
        edit = QAction("Edit …", self)
        edit.triggered.connect(lambda: self.edit_command.emit(cmd[1]))
        menu.addAction(edit)
        delete = QAction("Delete", self)
        delete.triggered.connect(lambda: self.delete_command.emit(cmd[1]))
        menu.addAction(delete)
        menu.exec(self._tree.mapToGlobal(point))


class CommandFuzzyPopup(QDialog):
    """Ctrl+Shift+Space popup for quickly running a command by name."""

    command_chosen = pyqtSignal(str, bool)  # text, append_enter

    def __init__(self, store: CommandStore, parent: QWidget | None = None) -> None:
        """Build the popup."""
        super().__init__(parent)
        self.setWindowTitle("Run Command")
        self.setModal(True)
        self.resize(480, 320)
        self._store = store

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Find command (fuzzy):"))

        self._input = QLineEdit(self)
        self._input.textChanged.connect(self._refilter)
        layout.addWidget(self._input)

        self._list = QListWidget(self)
        self._list.itemActivated.connect(self._send)
        layout.addWidget(self._list, 1)

        self._refilter("")

    def _refilter(self, text: str) -> None:
        """Update the list to match the current query."""
        self._list.clear()
        results = self._store.search_commands(text) if text else self._store.list_commands()
        for cmd in results:
            item = QListWidgetItem(f"{cmd.name}  —  {cmd.command_text}")
            item.setData(Qt.ItemDataRole.UserRole, cmd.command_text)
            self._list.addItem(item)

    def _send(self, item: QListWidgetItem) -> None:
        """Emit ``command_chosen`` and close the dialog."""
        text = str(item.data(Qt.ItemDataRole.UserRole))
        self.command_chosen.emit(text, True)
        self.accept()
