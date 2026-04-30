"""New / Edit session dialog (incl. jump-host chain editor — Feature 6)."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.session_store import Session, SessionStore

from .colors import GROUP_PRESETS, PALETTE


class SessionDialog(QDialog):
    """Modal dialog for creating or editing a saved :class:`Session`."""

    def __init__(
        self,
        store: SessionStore,
        existing: Session | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Build a tabbed dialog and pre-fill from ``existing`` if provided."""
        super().__init__(parent)
        self.setWindowTitle("Edit session" if existing else "New session")
        self._store = store
        self._existing = existing
        self.resize(560, 480)

        outer = QVBoxLayout(self)
        tabs = QTabWidget(self)

        # -------- Connection tab ---------------------------------------
        conn_w = QWidget()
        conn = QFormLayout(conn_w)
        self.name = QLineEdit(conn_w)
        self.hostname = QLineEdit(conn_w)
        self.port = QSpinBox(conn_w)
        self.port.setRange(1, 65_535)
        self.port.setValue(22)
        self.protocol = QComboBox(conn_w)
        self.protocol.addItems(["ssh", "telnet", "serial"])
        self.username = QLineEdit(conn_w)
        self.auth_type = QComboBox(conn_w)
        self.auth_type.addItems(["password", "key", "ask"])
        self.password = QLineEdit(conn_w)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_path = QLineEdit(conn_w)
        self.key_passphrase = QLineEdit(conn_w)
        self.key_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        conn.addRow("Name:", self.name)
        conn.addRow("Hostname:", self.hostname)
        conn.addRow("Port:", self.port)
        conn.addRow("Protocol:", self.protocol)
        conn.addRow("Username:", self.username)
        conn.addRow("Auth type:", self.auth_type)
        conn.addRow("Password:", self.password)
        conn.addRow("Key file:", self.key_path)
        conn.addRow("Key passphrase:", self.key_passphrase)
        tabs.addTab(conn_w, "Connection")

        # -------- Jump host chain tab ----------------------------------
        jump_w = QWidget()
        jump_l = QVBoxLayout(jump_w)
        jump_l.addWidget(QLabel("Up to 3 hops, in order:"))
        self.chain_list = QListWidget(jump_w)
        jump_l.addWidget(self.chain_list, 1)
        row = QHBoxLayout()
        self.bastion_combo = QComboBox(jump_w)
        self.bastion_combo.addItem("— Select bastion profile —", None)
        for b in store.list_bastions():
            self.bastion_combo.addItem(f"{b.name} ({b.username}@{b.hostname}:{b.port})", b.id)
        row.addWidget(self.bastion_combo, 1)
        add_btn = QPushButton("Add hop", jump_w)
        add_btn.clicked.connect(self._add_hop)
        row.addWidget(add_btn)
        rm_btn = QPushButton("Remove", jump_w)
        rm_btn.clicked.connect(self._remove_hop)
        row.addWidget(rm_btn)
        jump_l.addLayout(row)
        tabs.addTab(jump_w, "Jump Hosts")

        # -------- Appearance tab ---------------------------------------
        appear_w = QWidget()
        appear = QFormLayout(appear_w)
        self.color_tag = QComboBox(appear_w)
        self.color_tag.addItem("(none)", None)
        for tag in PALETTE:
            self.color_tag.addItem(tag, tag)
        self.group_tag = QComboBox(appear_w)
        self.group_tag.setEditable(True)
        self.group_tag.addItem("")
        for g in GROUP_PRESETS:
            self.group_tag.addItem(g)
        self.color_scheme = QComboBox(appear_w)
        self.color_scheme.addItems(["Dark", "Light", "Solarized Dark", "Dracula", "Nord", "Monokai"])
        self.font_family = QLineEdit("Monospace", appear_w)
        self.font_size = QSpinBox(appear_w)
        self.font_size.setRange(6, 32)
        self.font_size.setValue(11)
        appear.addRow("Color tag:", self.color_tag)
        appear.addRow("Group:", self.group_tag)
        appear.addRow("Color scheme:", self.color_scheme)
        appear.addRow("Font family:", self.font_family)
        appear.addRow("Font size:", self.font_size)
        tabs.addTab(appear_w, "Appearance")

        # -------- Logging / notes tab ----------------------------------
        log_w = QWidget()
        log = QFormLayout(log_w)
        self.log_enabled = QCheckBox("Enable session logging", log_w)
        self.log_path = QLineEdit(log_w)
        self.log_mode = QComboBox(log_w)
        self.log_mode.addItems(["append", "overwrite"])
        self.notes = QTextEdit(log_w)
        log.addRow("", self.log_enabled)
        log.addRow("Log path:", self.log_path)
        log.addRow("Log mode:", self.log_mode)
        log.addRow("Notes:", self.notes)
        tabs.addTab(log_w, "Logging / Notes")

        outer.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        if existing is not None:
            self._populate(existing)

    # -- internals ---------------------------------------------------------

    def _add_hop(self) -> None:
        """Append the selected bastion to the chain (max 3)."""
        bid = self.bastion_combo.currentData()
        if bid is None:
            return
        if self.chain_list.count() >= 3:
            return
        item = QListWidgetItem(self.bastion_combo.currentText())
        item.setData(0x0100, int(bid))  # Qt.UserRole
        self.chain_list.addItem(item)

    def _remove_hop(self) -> None:
        """Remove the currently selected hop."""
        for item in self.chain_list.selectedItems():
            self.chain_list.takeItem(self.chain_list.row(item))

    def _populate(self, sess: Session) -> None:
        """Fill the form from an existing :class:`Session`."""
        self.name.setText(sess.name)
        self.hostname.setText(sess.hostname or "")
        self.port.setValue(sess.port or 22)
        idx = self.protocol.findText(sess.protocol or "ssh")
        if idx >= 0:
            self.protocol.setCurrentIndex(idx)
        self.username.setText(sess.username or "")
        idx = self.auth_type.findText(sess.auth_type or "ask")
        if idx >= 0:
            self.auth_type.setCurrentIndex(idx)
        self.key_path.setText(sess.key_path or "")
        idx = self.color_tag.findData(sess.color_tag)
        if idx >= 0:
            self.color_tag.setCurrentIndex(idx)
        self.group_tag.setEditText(sess.group_tag or "")
        if sess.color_scheme:
            i = self.color_scheme.findText(sess.color_scheme)
            if i >= 0:
                self.color_scheme.setCurrentIndex(i)
        self.font_family.setText(sess.font_family or "Monospace")
        self.font_size.setValue(sess.font_size or 11)
        self.log_enabled.setChecked(bool(sess.log_enabled))
        self.log_path.setText(sess.log_path or "")
        idx = self.log_mode.findText(sess.log_mode or "append")
        if idx >= 0:
            self.log_mode.setCurrentIndex(idx)
        self.notes.setPlainText(sess.notes or "")
        for bid in (sess.jump_host_chain or []):
            b = self._store.get_bastion(int(bid))
            if b is None:
                continue
            item = QListWidgetItem(f"{b.name} ({b.username}@{b.hostname}:{b.port})")
            item.setData(0x0100, int(bid))
            self.chain_list.addItem(item)

    def fields(self) -> dict[str, object]:
        """Return a kwargs dict suitable for :meth:`SessionStore.create_session`."""
        chain: list[int] = []
        for i in range(self.chain_list.count()):
            chain.append(int(self.chain_list.item(i).data(0x0100)))

        return {
            "name": self.name.text().strip() or "Untitled",
            "hostname": self.hostname.text().strip(),
            "port": int(self.port.value()),
            "protocol": self.protocol.currentText(),
            "username": self.username.text().strip() or None,
            "auth_type": self.auth_type.currentText(),
            "key_path": self.key_path.text().strip() or None,
            "color_tag": self.color_tag.currentData(),
            "group_tag": self.group_tag.currentText().strip() or None,
            "color_scheme": self.color_scheme.currentText(),
            "font_family": self.font_family.text().strip() or "Monospace",
            "font_size": int(self.font_size.value()),
            "log_enabled": self.log_enabled.isChecked(),
            "log_path": self.log_path.text().strip() or None,
            "log_mode": self.log_mode.currentText(),
            "notes": self.notes.toPlainText().strip() or None,
            "jump_host_chain": chain,
        }
