"""Master password dialogs (set on first launch, unlock on later launches).

Both dialogs are intentionally tiny self-contained QDialogs so the launch
flow in :mod:`main` can drive them without dragging in the rest of the UI.
The set-password flow includes a *Confirm* field to avoid lockouts caused
by typos at first launch — see CLAUDE.md §6 for the security model.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)


class SetMasterPasswordDialog(QDialog):
    """First-launch dialog — pick + confirm a brand new master password."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the form."""
        super().__init__(parent)
        self.setWindowTitle("NovaTerm \u2014 Set master password")
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.addWidget(
            QLabel(
                "Choose a master password. It will encrypt every saved "
                "credential.\nNovaTerm cannot recover this password if you "
                "forget it.",
                self,
            )
        )

        form = QFormLayout()
        self._pw1 = QLineEdit(self)
        self._pw1.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw2 = QLineEdit(self)
        self._pw2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("New password:", self._pw1)
        form.addRow("Confirm password:", self._pw2)
        outer.addLayout(form)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    # -- public ------------------------------------------------------------

    def password(self) -> str:
        """Return the password the user typed (only valid after accept)."""
        return self._pw1.text()

    # -- internals ---------------------------------------------------------

    def _on_ok(self) -> None:
        """Validate the form before accepting."""
        pw1 = self._pw1.text()
        pw2 = self._pw2.text()
        if not pw1:
            QMessageBox.warning(self, "NovaTerm", "Password cannot be empty.")
            return
        if pw1 != pw2:
            QMessageBox.warning(
                self,
                "NovaTerm",
                "Password and confirmation do not match.",
            )
            self._pw2.clear()
            self._pw2.setFocus()
            return
        self.accept()


class UnlockMasterPasswordDialog(QDialog):
    """Returning-launch dialog — type the master password to unlock."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the form."""
        super().__init__(parent)
        self.setWindowTitle("NovaTerm \u2014 Master password")
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Enter your master password:", self))

        self._pw = QLineEdit(self)
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        outer.addWidget(self._pw)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def password(self) -> str:
        """Return the typed password (only valid after accept)."""
        return self._pw.text()
