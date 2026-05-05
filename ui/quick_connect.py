"""Quick Connect dialog (Feature 11)."""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.session_store import SessionStore


@dataclass(slots=True)
class QuickConnectResult:
    """Return type for :meth:`QuickConnectDialog.result_data`."""

    hostname: str
    port: int
    protocol: str
    username: str
    save_as_session: bool


class QuickConnectDialog(QDialog):
    """``Ctrl+Q`` popup for ad-hoc connections."""

    def __init__(self, store: SessionStore, parent: QWidget | None = None) -> None:
        """Build the dialog and pre-populate from recent connections."""
        super().__init__(parent)
        self.setWindowTitle("Quick Connect")
        self._store = store

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.hostname = QLineEdit(self)
        self.port = QSpinBox(self)
        self.port.setRange(1, 65_535)
        self.port.setValue(22)
        self.protocol = QComboBox(self)
        self.protocol.addItems(["ssh", "telnet"])
        self.username = QLineEdit(self)

        recent = QComboBox(self)
        recent.addItem("— Recent connections —", None)
        for r in store.list_recent_connections():
            recent.addItem(
                f"{r.username + '@' if r.username else ''}{r.hostname}:{r.port} ({r.protocol})",
                (r.hostname, r.port, r.protocol, r.username),
            )
        recent.currentIndexChanged.connect(lambda _i: self._fill_from_recent(recent.currentData()))

        form.addRow("Recent:", recent)
        form.addRow("Hostname:", self.hostname)
        form.addRow("Port:", self.port)
        form.addRow("Protocol:", self.protocol)
        form.addRow("Username:", self.username)

        self.save_cb = QCheckBox("Save as new session", self)
        form.addRow("", self.save_cb)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill_from_recent(self, data: object) -> None:
        """Pre-fill the form from a recent-connection tuple."""
        if not isinstance(data, tuple):
            return
        host, port, proto, user = data
        self.hostname.setText(host)
        self.port.setValue(int(port))
        idx = self.protocol.findText(proto)
        if idx >= 0:
            self.protocol.setCurrentIndex(idx)
        self.username.setText(user or "")

    def result_data(self) -> QuickConnectResult:
        """Return the values the user entered."""
        return QuickConnectResult(
            hostname=self.hostname.text().strip(),
            port=int(self.port.value()),
            protocol=self.protocol.currentText(),
            username=self.username.text().strip(),
            save_as_session=self.save_cb.isChecked(),
        )
