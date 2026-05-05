"""Editor for the global *Default Session* (SecureCRT-style fallback profile).

The Default Session is a singleton row stored in SQLite (see
:class:`core.session_store.DefaultSession`). Any new connection that does not
have its own saved credentials inherits these values at connect time. The
password / key passphrase are stored Fernet-encrypted via the
:class:`core.credential_vault.CredentialVault` (so the master password must be
unlocked before they can be edited or applied).
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.credential_vault import CredentialVault, VaultAuthError
from core.session_store import SessionStore


class DefaultSessionDialog(QDialog):
    """Modal editor for the global Default Session profile."""

    def __init__(
        self,
        store: SessionStore,
        vault: CredentialVault,
        parent: QWidget | None = None,
    ) -> None:
        """Build the form and pre-fill from the singleton row."""
        super().__init__(parent)
        self.setWindowTitle("Default Session")
        self.resize(460, 360)
        self._store = store
        self._vault = vault

        outer = QVBoxLayout(self)
        outer.addWidget(
            QLabel(
                "These values are used by Quick Host connections and as a "
                "fallback for any saved session whose own field is blank."
            )
        )

        form = QFormLayout()
        self.username = QLineEdit(self)
        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(
            "(unchanged)" if vault.is_unlocked() else "(vault locked)"
        )
        self.password.setEnabled(vault.is_unlocked())
        self.key_path = QLineEdit(self)
        self.key_passphrase = QLineEdit(self)
        self.key_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_passphrase.setPlaceholderText(
            "(unchanged)" if vault.is_unlocked() else "(vault locked)"
        )
        self.key_passphrase.setEnabled(vault.is_unlocked())
        self.port = QSpinBox(self)
        self.port.setRange(1, 65_535)
        self.port.setValue(22)
        self.protocol = QComboBox(self)
        self.protocol.addItems(["ssh", "telnet", "serial"])
        self.color_scheme = QComboBox(self)
        self.color_scheme.addItems(
            ["Dark", "Light", "Solarized Dark", "Dracula", "Nord", "Monokai"]
        )
        self.font_family = QLineEdit("Monospace", self)
        self.font_size = QSpinBox(self)
        self.font_size.setRange(6, 32)
        self.font_size.setValue(11)
        self.scrollback_lines = QSpinBox(self)
        self.scrollback_lines.setRange(100, 1_000_000)
        self.scrollback_lines.setValue(10_000)

        form.addRow("Username:", self.username)
        form.addRow("Password:", self.password)
        form.addRow("Key file:", self.key_path)
        form.addRow("Key passphrase:", self.key_passphrase)
        form.addRow("Port:", self.port)
        form.addRow("Protocol:", self.protocol)
        form.addRow("Color scheme:", self.color_scheme)
        form.addRow("Font family:", self.font_family)
        form.addRow("Font size:", self.font_size)
        form.addRow("Scrollback lines:", self.scrollback_lines)
        outer.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._populate()

    # -- internals ---------------------------------------------------------

    def _populate(self) -> None:
        """Pre-fill the form from the existing default-session row."""
        row = self._store.get_default_session()
        self.username.setText(row.username or "")
        self.key_path.setText(row.key_path or "")
        self.port.setValue(row.port or 22)
        idx = self.protocol.findText(row.protocol or "ssh")
        if idx >= 0:
            self.protocol.setCurrentIndex(idx)
        if row.color_scheme:
            i = self.color_scheme.findText(row.color_scheme)
            if i >= 0:
                self.color_scheme.setCurrentIndex(i)
        self.font_family.setText(row.font_family or "Monospace")
        self.font_size.setValue(row.font_size or 11)
        self.scrollback_lines.setValue(row.scrollback_lines or 10_000)

    def _accept(self) -> None:
        """Persist the form values and close the dialog."""
        fields: dict[str, object] = {
            "username": self.username.text().strip() or None,
            "key_path": self.key_path.text().strip() or None,
            "port": int(self.port.value()),
            "protocol": self.protocol.currentText(),
            "color_scheme": self.color_scheme.currentText(),
            "font_family": self.font_family.text().strip() or "Monospace",
            "font_size": int(self.font_size.value()),
            "scrollback_lines": int(self.scrollback_lines.value()),
        }

        # Only touch the encrypted columns if the user actually typed
        # something (or explicitly cleared the field) — leaving the field
        # blank means "keep the existing stored value".
        if self._vault.is_unlocked():
            pw = self.password.text()
            if pw:
                try:
                    fields["encrypted_password"] = self._vault.encrypt(pw)
                except VaultAuthError:
                    pass
            kp = self.key_passphrase.text()
            if kp:
                try:
                    fields["encrypted_key_passphrase"] = self._vault.encrypt(kp)
                except VaultAuthError:
                    pass

        self._store.update_default_session(**fields)
        self.accept()
