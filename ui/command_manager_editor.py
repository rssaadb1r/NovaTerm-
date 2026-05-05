"""Command Manager editor — modal dialog for curating commands & groups.

Reachable from:

* right-click on the Button Bar  →  *Manage Button Bar*
* ``Options → Command Manager``

Layout: a tree of folders (groups) with their commands on the left, a
form on the right showing the selected command's *Name*, *Command Text*,
and *Description*. Toolbar buttons cover full CRUD for both folders and
commands plus per-command reordering. The dialog stages its edits in a
working buffer and only commits to the :class:`CommandStore` when the
user clicks *Save*; *Cancel* discards everything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.command_store import CommandStore


# ---------------------------------------------------------------------------
# In-memory working set
# ---------------------------------------------------------------------------


@dataclass
class _DraftCommand:
    """Editable copy of a :class:`Command` row."""

    name: str = ""
    command_text: str = ""
    description: str = ""
    db_id: int | None = None
    sort_order: int = 0


@dataclass
class _DraftGroup:
    """Editable copy of a :class:`CommandGroup` row, with its commands."""

    name: str = ""
    db_id: int | None = None
    sort_order: int = 0
    commands: list[_DraftCommand] = field(default_factory=list)


# Roles used to attach typed payloads to QTreeWidgetItem nodes.
_KIND_ROLE = Qt.ItemDataRole.UserRole
_OBJECT_ROLE = Qt.ItemDataRole.UserRole + 1


# ---------------------------------------------------------------------------
# Editor dialog
# ---------------------------------------------------------------------------


class CommandManagerEditor(QDialog):
    """Modal editor for the user's command library.

    All edits are staged in :attr:`_groups` (a list of :class:`_DraftGroup`)
    plus :attr:`_orphans` (commands that have no group); :meth:`accept`
    flushes them back to the database.
    """

    def __init__(
        self,
        store: CommandStore,
        parent: QWidget | None = None,
    ) -> None:
        """Build the dialog and snapshot the current store contents."""
        super().__init__(parent)
        self.setWindowTitle("Command Manager")
        self.setModal(True)
        self.resize(720, 520)
        self._store = store

        self._groups: list[_DraftGroup] = []
        self._orphans: list[_DraftCommand] = []
        # Track originals so we can compute the delete-set on Save.
        self._original_group_ids: set[int] = set()
        self._original_command_ids: set[int] = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        toolbar = QToolBar(self)
        toolbar.addAction("New Folder", self._new_group)
        toolbar.addAction("Rename Folder", self._rename_group)
        toolbar.addAction("Delete Folder", self._delete_group)
        toolbar.addSeparator()
        toolbar.addAction("New Command", self._new_command)
        toolbar.addAction("Delete Command", self._delete_command)
        toolbar.addAction("Move Up", lambda: self._move_command(-1))
        toolbar.addAction("Move Down", lambda: self._move_command(+1))
        outer.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        outer.addWidget(splitter, 1)

        self._tree = QTreeWidget(splitter)
        self._tree.setHeaderHidden(True)
        self._tree.currentItemChanged.connect(self._on_tree_selection)
        splitter.addWidget(self._tree)

        # -- right-hand form ----------------------------------------------
        form = QWidget(splitter)
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(8, 0, 0, 0)

        form_layout.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit(form)
        self._name_edit.editingFinished.connect(self._sync_name)
        form_layout.addWidget(self._name_edit)

        form_layout.addWidget(QLabel("Command Text:"))
        self._cmd_edit = QPlainTextEdit(form)
        self._cmd_edit.textChanged.connect(self._sync_command_text)
        form_layout.addWidget(self._cmd_edit, 1)

        form_layout.addWidget(QLabel("Description:"))
        self._desc_edit = QLineEdit(form)
        self._desc_edit.editingFinished.connect(self._sync_description)
        form_layout.addWidget(self._desc_edit)

        splitter.addWidget(form)
        splitter.setSizes([260, 460])

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._load_initial()
        self._set_form_enabled(False)

    # -- initial snapshot --------------------------------------------------

    def _load_initial(self) -> None:
        """Populate the in-memory drafts from the database and rebuild the tree."""
        groups = self._store.list_groups()
        self._original_group_ids = {int(g.id) for g in groups}
        self._groups = [
            _DraftGroup(
                name=g.name,
                db_id=int(g.id),
                sort_order=g.sort_order,
            )
            for g in groups
        ]
        commands = self._store.list_commands()
        self._original_command_ids = {int(c.id) for c in commands}
        groups_by_id = {g.db_id: g for g in self._groups}
        self._orphans = []
        for c in commands:
            draft = _DraftCommand(
                name=c.name,
                command_text=c.command_text,
                description=c.description or "",
                db_id=int(c.id),
                sort_order=int(c.sort_order),
            )
            target = groups_by_id.get(c.group_id) if c.group_id is not None else None
            if target is None:
                self._orphans.append(draft)
            else:
                target.commands.append(draft)
        self._rebuild_tree()

    # -- tree rendering ----------------------------------------------------

    def _rebuild_tree(self, *, focus: object | None = None) -> None:
        """Repaint the entire tree from the in-memory drafts.

        :param focus: Optional draft (``_DraftGroup`` or ``_DraftCommand``)
            to re-select after the rebuild — handy after a CRUD operation.
        """
        self._tree.blockSignals(True)
        self._tree.clear()
        focus_item: QTreeWidgetItem | None = None

        for group in self._groups:
            g_item = QTreeWidgetItem([group.name])
            g_item.setData(0, _KIND_ROLE, "group")
            g_item.setData(0, _OBJECT_ROLE, id(group))
            self._tree.addTopLevelItem(g_item)
            if group is focus:
                focus_item = g_item
            for cmd in group.commands:
                c_item = QTreeWidgetItem([cmd.name or "(untitled)"])
                c_item.setData(0, _KIND_ROLE, "command")
                c_item.setData(0, _OBJECT_ROLE, id(cmd))
                g_item.addChild(c_item)
                if cmd is focus:
                    focus_item = c_item

        if self._orphans:
            orphan_item = QTreeWidgetItem(["(no folder)"])
            orphan_item.setData(0, _KIND_ROLE, "orphan_root")
            orphan_item.setFlags(
                orphan_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
            )
            self._tree.addTopLevelItem(orphan_item)
            for cmd in self._orphans:
                c_item = QTreeWidgetItem([cmd.name or "(untitled)"])
                c_item.setData(0, _KIND_ROLE, "command")
                c_item.setData(0, _OBJECT_ROLE, id(cmd))
                orphan_item.addChild(c_item)
                if cmd is focus:
                    focus_item = c_item

        self._tree.expandAll()
        self._tree.blockSignals(False)
        if focus_item is not None:
            self._tree.setCurrentItem(focus_item)
        else:
            self._set_form_enabled(False)

    # -- selection lookup --------------------------------------------------

    def _all_drafts(self) -> list[object]:
        """Flat list of every draft in the working set (groups + commands)."""
        out: list[object] = list(self._groups)
        for g in self._groups:
            out.extend(g.commands)
        out.extend(self._orphans)
        return out

    def _resolve(self, item: QTreeWidgetItem | None) -> object | None:
        """Map a tree item back to the live draft instance (or ``None``)."""
        if item is None:
            return None
        oid = item.data(0, _OBJECT_ROLE)
        if not isinstance(oid, int):
            return None
        for draft in self._all_drafts():
            if id(draft) == oid:
                return draft
        return None

    def _selected(self) -> object | None:
        """Return the currently selected draft, if any."""
        return self._resolve(self._tree.currentItem())

    def _selected_group(self) -> _DraftGroup | None:
        """Return the selected group (or the parent group of a selected command)."""
        node = self._selected()
        if isinstance(node, _DraftGroup):
            return node
        if isinstance(node, _DraftCommand):
            for g in self._groups:
                if node in g.commands:
                    return g
        return None

    # -- form sync ---------------------------------------------------------

    def _set_form_enabled(self, enabled: bool, *, command_form: bool = False) -> None:
        """Enable/disable the right-hand form widgets.

        :param enabled: Master switch.
        :param command_form: Whether the command-specific fields
            (command text & description) should be enabled. Group nodes
            only edit *Name*.
        """
        self._name_edit.setEnabled(enabled)
        self._cmd_edit.setEnabled(enabled and command_form)
        self._desc_edit.setEnabled(enabled and command_form)
        if not enabled:
            for w in (self._name_edit, self._desc_edit):
                w.blockSignals(True)
                w.clear()
                w.blockSignals(False)
            self._cmd_edit.blockSignals(True)
            self._cmd_edit.setPlainText("")
            self._cmd_edit.blockSignals(False)

    def _on_tree_selection(self, current: QTreeWidgetItem | None, _previous) -> None:
        """Refill the right-hand form whenever the tree selection changes."""
        node = self._resolve(current)
        if isinstance(node, _DraftGroup):
            self._set_form_enabled(True, command_form=False)
            self._name_edit.blockSignals(True)
            self._name_edit.setText(node.name)
            self._name_edit.blockSignals(False)
        elif isinstance(node, _DraftCommand):
            self._set_form_enabled(True, command_form=True)
            self._name_edit.blockSignals(True)
            self._name_edit.setText(node.name)
            self._name_edit.blockSignals(False)
            self._cmd_edit.blockSignals(True)
            self._cmd_edit.setPlainText(node.command_text)
            self._cmd_edit.blockSignals(False)
            self._desc_edit.blockSignals(True)
            self._desc_edit.setText(node.description)
            self._desc_edit.blockSignals(False)
        else:
            self._set_form_enabled(False)

    def _sync_name(self) -> None:
        """Push the *Name* field back into the active draft."""
        node = self._selected()
        text = self._name_edit.text().strip()
        if isinstance(node, (_DraftGroup, _DraftCommand)) and text:
            node.name = text
            item = self._tree.currentItem()
            if item is not None:
                item.setText(0, text)

    def _sync_command_text(self) -> None:
        node = self._selected()
        if isinstance(node, _DraftCommand):
            node.command_text = self._cmd_edit.toPlainText()

    def _sync_description(self) -> None:
        node = self._selected()
        if isinstance(node, _DraftCommand):
            node.description = self._desc_edit.text()

    # -- CRUD --------------------------------------------------------------

    def _new_group(self) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not (ok and name.strip()):
            return
        draft = _DraftGroup(name=name.strip(), sort_order=len(self._groups))
        self._groups.append(draft)
        self._rebuild_tree(focus=draft)

    def _rename_group(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename Folder", "Folder name:", text=group.name
        )
        if not (ok and name.strip()):
            return
        group.name = name.strip()
        self._rebuild_tree(focus=group)

    def _delete_group(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        n = len(group.commands)
        msg = (
            f"Delete folder '{group.name}' and ALL {n} command(s) inside it?"
            if n
            else f"Delete folder '{group.name}'?"
        )
        if (
            QMessageBox.question(self, "Delete folder", msg)
            != QMessageBox.StandardButton.Yes
        ):
            return
        # Cascade: drop the group AND every command in it from the
        # working set. The save path will then translate the missing
        # ids into ``delete_command`` / ``delete_group`` calls.
        self._groups.remove(group)
        self._rebuild_tree()

    def _new_command(self) -> None:
        group = self._selected_group()
        draft = _DraftCommand(name="New command", command_text="", description="")
        if group is None:
            self._orphans.append(draft)
        else:
            draft.sort_order = len(group.commands)
            group.commands.append(draft)
        self._rebuild_tree(focus=draft)

    def _delete_command(self) -> None:
        node = self._selected()
        if not isinstance(node, _DraftCommand):
            return
        if (
            QMessageBox.question(self, "Delete command", f"Delete '{node.name}'?")
            != QMessageBox.StandardButton.Yes
        ):
            return
        for g in self._groups:
            if node in g.commands:
                g.commands.remove(node)
                break
        else:
            if node in self._orphans:
                self._orphans.remove(node)
        self._rebuild_tree()

    def _move_command(self, delta: int) -> None:
        node = self._selected()
        if not isinstance(node, _DraftCommand):
            return
        for bucket in (*[g.commands for g in self._groups], self._orphans):
            if node in bucket:
                pos = bucket.index(node)
                new_pos = pos + delta
                if 0 <= new_pos < len(bucket):
                    bucket[pos], bucket[new_pos] = bucket[new_pos], bucket[pos]
                break
        self._rebuild_tree(focus=node)

    # -- save --------------------------------------------------------------

    def accept(self) -> None:  # noqa: D401 (Qt API)
        """Flush every draft back to the :class:`CommandStore` and close."""
        self._sync_name()
        self._sync_command_text()
        self._sync_description()

        # 1. Delete groups + commands the user removed from the working set.
        kept_command_ids = {
            d.db_id
            for d in (
                *[c for g in self._groups for c in g.commands],
                *self._orphans,
            )
            if d.db_id is not None
        }
        for cid in self._original_command_ids - kept_command_ids:
            self._store.delete_command(cid)

        kept_group_ids = {g.db_id for g in self._groups if g.db_id is not None}
        for gid in self._original_group_ids - kept_group_ids:
            self._store.delete_group(gid)

        # 2. Upsert groups and remember their final ids so we can attach
        # commands to them.
        for idx, group in enumerate(self._groups):
            group.sort_order = idx
            if group.db_id is None:
                group.db_id = self._store.create_group(
                    name=group.name, sort_order=idx
                )
            else:
                self._store.update_group(
                    group.db_id, name=group.name, sort_order=idx
                )

        # 3. Upsert commands.
        def _upsert(cmd: _DraftCommand, gid: int | None, order: int) -> None:
            cmd.sort_order = order
            payload = dict(
                group_id=gid,
                name=cmd.name,
                command_text=cmd.command_text,
                description=cmd.description or None,
                sort_order=order,
            )
            if cmd.db_id is None:
                cmd.db_id = self._store.create_command(**payload)
            else:
                self._store.update_command(cmd.db_id, **payload)

        for group in self._groups:
            for idx, cmd in enumerate(group.commands):
                _upsert(cmd, group.db_id, idx)
        for idx, cmd in enumerate(self._orphans):
            _upsert(cmd, None, idx)

        super().accept()
