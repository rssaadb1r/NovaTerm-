"""Button Bar — single-row quick-send strip docked at the bottom of the
terminal area.

Layout (left → right):

* a folder QComboBox listing every :class:`CommandGroup`
* one QPushButton per :class:`Command` in the selected group (button
  text is the command's *name* — never the raw command text)
* a trailing ``▶`` overflow :class:`QToolButton` that pops up the
  commands which did not fit in the visible row

The bar is a *thin* single-row strip — it never grows past
:data:`MAX_HEIGHT` regardless of how many commands the user has saved.
Single-click on a button sends that command to the active session.
Right-click anywhere on the bar opens the Command Manager editor.
Visibility is toggled from ``View → Button Bar`` in the main menu.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from core.command_store import CommandStore

#: The bar is a thin single-row strip — no matter how many commands the
#: user has, the height is locked here so it never grows into a panel.
MAX_HEIGHT = 36
_BUTTON_SPACING = 4


class ButtonBar(QFrame):
    """Bottom strip with a folder dropdown and one button per command.

    The widget owns no application state — it pulls everything from the
    bound :class:`CommandStore` via :meth:`refresh` and emits Qt signals
    for the main window to dispatch. The button row is laid out manually
    in :meth:`_relayout` so we can hide commands that don't fit and
    surface them through the overflow menu.
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
        self.setFixedHeight(MAX_HEIGHT)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._store = commands

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 2)
        outer.setSpacing(6)

        outer.addWidget(QLabel("Folder:", self))
        self._group_combo = QComboBox(self)
        self._group_combo.setMinimumWidth(140)
        self._group_combo.currentIndexChanged.connect(self._populate_buttons)
        outer.addWidget(self._group_combo)

        # The button host is a plain QWidget with manual geometry — every
        # time the bar resizes we walk the button list left to right and
        # hide whatever doesn't fit, exposing the leftover commands via
        # the overflow QToolButton on the right edge.
        self._button_host = QWidget(self)
        self._button_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        outer.addWidget(self._button_host, 1)

        # Shown when the database has no commands at all so the user has
        # somewhere to right-click for the *Manage Button Bar* menu.
        self._empty_label = QLabel(
            "No commands — right-click to manage", self._button_host
        )
        self._empty_label.setStyleSheet("color: palette(mid);")
        self._empty_label.hide()

        # Overflow menu trigger.
        self._overflow_menu = QMenu(self)
        self._overflow_button = QToolButton(self)
        self._overflow_button.setText("\u25b6")
        self._overflow_button.setToolTip("More commands\u2026")
        self._overflow_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._overflow_button.setMenu(self._overflow_menu)
        self._overflow_button.setFixedHeight(MAX_HEIGHT - 8)
        self._overflow_button.hide()
        outer.addWidget(self._overflow_button)

        self._buttons: list[QPushButton] = []
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

    # -- Qt overrides ------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt API)
        """Re-flow the visible buttons whenever the bar is resized."""
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt API)
        """Re-flow on show so toggling visibility doesn't strand buttons.

        When the bar is constructed hidden (its default state — the user
        opts in via *View → Button Bar*), the inner ``_button_host`` has
        zero width until the parent layout resolves. We schedule a
        :meth:`_relayout` for the next event loop iteration so the
        geometry is up-to-date by the time it runs.
        """
        super().showEvent(event)
        QTimer.singleShot(0, self._relayout)

    # -- internals ---------------------------------------------------------

    def _current_group_id(self) -> int | None:
        """Return the selected ``group_id`` (``None`` for *(All)*)."""
        data = self._group_combo.currentData()
        if isinstance(data, int):
            return data
        return None

    def _populate_buttons(self) -> None:
        """Rebuild the per-command buttons for the active folder."""
        # Drop the previous batch.
        for btn in self._buttons:
            btn.deleteLater()
        self._buttons.clear()

        gid = self._current_group_id()
        commands = self._store.list_commands(group_id=gid)
        for cmd in commands:
            btn = QPushButton(cmd.name, self._button_host)
            btn.setToolTip(cmd.command_text)
            btn.setFixedHeight(MAX_HEIGHT - 8)
            btn.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
            )
            btn.clicked.connect(
                lambda _checked=False, text=cmd.command_text: self.command_clicked.emit(text)
            )
            self._buttons.append(btn)

        self._relayout()

    def _relayout(self) -> None:
        """Place visible buttons left-to-right; overflow goes into the menu.

        We do this manually rather than relying on a QHBoxLayout so we
        can decide which buttons fit in the available width *and* know
        precisely which to expose via the overflow menu.
        """
        host = self._button_host
        available = host.width()
        if available <= 0:
            return

        # Empty state — nothing to lay out, just show the placeholder.
        if not self._buttons:
            self._empty_label.setGeometry(
                4, (MAX_HEIGHT - self._empty_label.sizeHint().height()) // 2,
                max(0, available - 8),
                self._empty_label.sizeHint().height(),
            )
            self._empty_label.show()
            self._overflow_button.hide()
            self._overflow_menu.clear()
            return
        self._empty_label.hide()

        x = 0
        y = (MAX_HEIGHT - 8 - 4) // 2  # vertically centre within host
        # Reserve room for the overflow button + spacing whenever any
        # command would otherwise overflow.
        overflow_reserve = (
            self._overflow_button.sizeHint().width() + _BUTTON_SPACING
        )

        # First pass: figure out which buttons fit. We need to know
        # whether the overflow button will be shown so we can tighten
        # the available width before laying out the visible run.
        widths = [b.sizeHint().width() for b in self._buttons]
        total_width = sum(widths) + max(0, len(widths) - 1) * _BUTTON_SPACING
        needs_overflow = total_width > available
        budget = available - (overflow_reserve if needs_overflow else 0)

        visible: list[QPushButton] = []
        hidden: list[QPushButton] = []
        for btn, w in zip(self._buttons, widths):
            slot = w + (_BUTTON_SPACING if visible else 0)
            if x + slot <= budget:
                visible.append(btn)
                x += slot
            else:
                hidden.append(btn)

        # Position visible buttons; reset hidden ones off-screen so we
        # don't leak them into the layout.
        cursor = 0
        for btn in visible:
            btn.setParent(host)
            btn.setGeometry(
                cursor,
                y,
                btn.sizeHint().width(),
                MAX_HEIGHT - 8,
            )
            btn.show()
            cursor += btn.sizeHint().width() + _BUTTON_SPACING

        for btn in hidden:
            btn.hide()

        # Rebuild the overflow menu.
        self._overflow_menu.clear()
        for btn in hidden:
            act = QAction(btn.text(), self)
            act.setToolTip(btn.toolTip())
            cmd_text = btn.toolTip()  # we stash the command in the tooltip
            act.triggered.connect(
                lambda _checked=False, text=cmd_text: self.command_clicked.emit(text)
            )
            self._overflow_menu.addAction(act)

        self._overflow_button.setVisible(bool(hidden))

    def _on_context_menu(self, point) -> None:
        """Show the right-click menu (currently a single *Manage* entry)."""
        menu = QMenu(self)
        manage = QAction("Manage Button Bar\u2026", self)
        manage.triggered.connect(self.manage_requested.emit)
        menu.addAction(manage)
        menu.exec(self.mapToGlobal(point))
