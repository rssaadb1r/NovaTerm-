"""Cluster Send input bar + selection dialog (Feature 7)."""
from __future__ import annotations

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
        tabs: list[tuple[int, str]],
        already_selected: set[int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """:param tabs: ``(tab_index, label)`` pairs."""
        super().__init__(parent)
        self.setWindowTitle("Cluster Send — select sessions")
        already = already_selected or set()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select sessions to include in the cluster:"))

        self._checkboxes: list[tuple[int, QCheckBox]] = []
        for idx, label in tabs:
            cb = QCheckBox(label, self)
            cb.setChecked(idx in already)
            self._checkboxes.append((idx, cb))
            layout.addWidget(cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self) -> set[int]:
        """Return the set of tab indices the user kept checked."""
        return {i for i, cb in self._checkboxes if cb.isChecked()}
