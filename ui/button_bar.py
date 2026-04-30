"""Button Bar — quick-send strip docked at the bottom of the terminal area.

Layout (left → right):

* a folder QComboBox listing every :class:`CommandGroup`
* one QPushButton per :class:`Command` in the selected group; the button
  text is the command's *name* (never the raw command text)

Single-click on a button sends that command to the active session
immediately. Right-click anywhere on the bar opens the Command Manager
editor so the user can curate the list. Visibility is toggled from
``View → Button Bar`` in the main menu.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from core.command_store import CommandStore


class ButtonBar(QFrame):
    """Bottom strip with a folder dropdown and one button per command.

    The widget owns no application state — it pulls everything from the
    bound :class:`CommandStore` via :meth:`refresh` and emits Qt signals
    for the main window to dispatch.
    """

    #: Emitted when the user clicks one of the per-command buttons. The
    #: payload is the raw command text (variable substitution happens in
    #: :class:`MainWindow`).
    command_clicked = pyqtSignal(str)

    #: Emitted from the right-click context menu so callers can open the
    #: Command Manager editor.
    manage_requested = pyqtSignal()

    def __init__(
        self,
        commands: CommandStore,
        parent: QWidget | None = None,
    ) -> None:
        """Build the bar and load the initial group / command list."""
        super().__init__(parent)
        self.setObjectName("ButtonBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._store = commands

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(6)

        outer.addWidget(QLabel("Folder:", self))
        self._group_combo = QComboBox(self)
        self._group_combo.setMinimumWidth(160)
        self._group_combo.currentIndexChanged.connect(self._populate_buttons)
        outer.addWidget(self._group_combo)

        # Buttons live inside a horizontally-scrollable area so a folder
        # with many commands does not push the bar off-screen.
        self._button_host = QWidget(self)
        self._button_layout = QHBoxLayout(self._button_host)
        self._button_layout.setContentsMargins(0, 0, 0, 0)
        self._button_layout.setSpacing(4)
        self._button_layout.addStretch(1)

        scroller = QScrollArea(self)
        scroller.setWidget(self._button_host)
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroller.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroller.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        outer.addWidget(scroller, 1)

        self.refresh()

    # -- public ------------------------------------------------------------

    def refresh(self) -> None:
        """Reload groups (preserving the current selection) and buttons."""
        previous = self._current_group_id()

        self._group_combo.blockSignals(True)
        self._group_combo.clear()
        self._group_combo.addItem("(All)", None)
        groups = self._store.list_groups()
        for g in groups:
            self._group_combo.addItem(g.name, int(g.id))

        if previous is not None:
            for idx in range(self._group_combo.count()):
                if self._group_combo.itemData(idx) == previous:
                    self._group_combo.setCurrentIndex(idx)
                    break
        self._group_combo.blockSignals(False)

        self._populate_buttons()

    # -- internals ---------------------------------------------------------

    def _current_group_id(self) -> int | None:
        """Return the selected ``group_id`` (``None`` for *(All)*)."""
        data = self._group_combo.currentData()
        if isinstance(data, int):
            return data
        return None

    def _populate_buttons(self) -> None:
        """Recreate the per-command buttons for the active folder."""
        # Drop existing buttons (everything except the trailing stretch).
        while self._button_layout.count() > 1:
            item = self._button_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        gid = self._current_group_id()
        commands = self._store.list_commands(group_id=gid)
        for cmd in commands:
            btn = QPushButton(cmd.name, self._button_host)
            btn.setToolTip(cmd.command_text)
            btn.clicked.connect(
                lambda _checked=False, text=cmd.command_text: self.command_clicked.emit(text)
            )
            # Insert before the trailing stretch.
            self._button_layout.insertWidget(self._button_layout.count() - 1, btn)

    def _on_context_menu(self, point) -> None:
        """Show the right-click menu (currently a single *Manage* entry)."""
        menu = QMenu(self)
        manage = QAction("Manage Button Bar\u2026", self)
        manage.triggered.connect(self.manage_requested.emit)
        menu.addAction(manage)
        menu.exec(self.mapToGlobal(point))
