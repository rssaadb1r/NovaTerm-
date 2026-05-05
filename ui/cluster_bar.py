"""Cluster Send input bar + selection dialog (Feature 7)."""
from __future__ import annotations

from typing import Hashable, Iterable, TypeVar

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

K = TypeVar("K", bound=Hashable)


class ClusterInputBar(QWidget):
    """Single-line input that broadcasts every keystroke to all targets."""

    char_typed = pyqtSignal(str)  # individual character / chunk to broadcast
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the bar."""
        super().__init__(parent)
        self.setObjectName("ClusterBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        layout.addWidget(QLabel("📡 Cluster:", self))

        self._input = _BroadcastLineEdit(self)
        self._input.char_typed.connect(self.char_typed.emit)
        layout.addWidget(self._input, 1)

        close = QPushButton("Disable cluster", self)
        close.clicked.connect(self.closed.emit)
        layout.addWidget(close)

    def focus(self) -> None:
        """Give the input field focus."""
        self._input.setFocus()


class _BroadcastLineEdit(QLineEdit):
    """``QLineEdit`` that emits ``char_typed`` for every keystroke."""

    char_typed = pyqtSignal(str)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt API
        """Forward every typed character upstream as it arrives."""
        text = event.text()
        if text:
            self.char_typed.emit(text)
        super().keyPressEvent(event)


class ClusterSelectDialog(QDialog):
    """Modal dialog letting the user check which open tabs to broadcast to."""

    def __init__(
        self,
        tabs: Iterable[tuple[Hashable, str]],
        already_selected: set | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """:param tabs: ``(key, label)`` pairs.

        ``key`` is any hashable identifier (typically the
        :class:`TabContent` widget itself) that the caller will use to
        look the selection back up.
        """
        super().__init__(parent)
        self.setWindowTitle("Cluster Send — select sessions")
        already = already_selected or set()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select sessions to include in the cluster:"))

        self._checkboxes: list[tuple[Hashable, QCheckBox]] = []
        for key, label in tabs:
            cb = QCheckBox(label, self)
            cb.setChecked(key in already)
            self._checkboxes.append((key, cb))
            layout.addWidget(cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self) -> set:
        """Return the set of keys whose checkboxes the user kept checked."""
        return {key for key, cb in self._checkboxes if cb.isChecked()}
