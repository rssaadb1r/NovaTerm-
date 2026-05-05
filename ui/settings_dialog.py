"""Global preferences dialog (Feature 14)."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core import paths
from core.credential_vault import CredentialVault, VaultAuthError


DEFAULT_SETTINGS: dict[str, Any] = {
    "general": {
        "confirm_on_close": True,
        "default_protocol": "ssh",
        "default_port": 22,
        "startup_show_session_manager": True,
    },
    "appearance": {
        "default_font_family": "Monospace",
        "default_font_size": 11,
        "default_color_scheme": "Dark",
        "terminal_transparency_pct": 0,
    },
    "terminal": {
        "default_encoding": "utf-8",
        "scrollback_lines": 10_000,
        "mouse_select_copy": True,
        "bell_type": "visual",  # visual | audio | none
    },
    "keyboard": {
        # Reserved for future custom mappings — see CLAUDE.md §9.
    },
    "groups": {
        "Production": "red",
        "Staging": "yellow",
        "Dev": "green",
        "Local": "teal",
    },
    "sftp": {
        "default_open_mode": "panel",  # panel | tab
        "max_concurrent_transfers": 3,
    },
    "security": {
        # Master password is set via dedicated dialog flow.
    },
    "proxy": {
        "type": "none",  # none | http | socks4 | socks5
        "host": "",
        "port": 0,
        "username": "",
    },
    "advanced": {
        "ssh_keepalive_seconds": 30,
        "connect_timeout_seconds": 15,
        "auto_reconnect_retries": 0,
    },
}


def load_settings() -> dict[str, Any]:
    """Read settings from the user TOML file (returns defaults if absent)."""
    path = paths.settings_file()
    if not path.exists():
        return _deep_copy(DEFAULT_SETTINGS)
    with path.open("rb") as fh:
        loaded = tomllib.load(fh)
    return _merge(DEFAULT_SETTINGS, loaded)


def save_settings(settings: dict[str, Any]) -> Path:
    """Persist settings to the user TOML file. Returns the path written to."""
    path = paths.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(settings, fh)
    return path


def _deep_copy(d: dict[str, Any]) -> dict[str, Any]:
    """Recursive shallow-key dict clone."""
    return {k: (_deep_copy(v) if isinstance(v, dict) else v) for k, v in d.items()}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` into ``base`` (returns a new dict)."""
    result = _deep_copy(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], val)
        else:
            result[key] = val
    return result


class SettingsDialog(QDialog):
    """Tabbed settings editor."""

    def __init__(
        self,
        parent: QWidget | None = None,
        vault: CredentialVault | None = None,
    ) -> None:
        """Load current settings and build the tab widget.

        ``vault`` enables the *Change Master Password* form on the
        Security tab; when ``None`` (e.g. tests) that section is hidden.
        """
        super().__init__(parent)
        self.setWindowTitle("NovaTerm — Preferences")
        self.resize(560, 420)
        self._settings = load_settings()
        self._vault = vault

        outer = QVBoxLayout(self)
        tabs = QTabWidget(self)

        tabs.addTab(self._build_general(), "General")
        tabs.addTab(self._build_appearance(), "Appearance")
        tabs.addTab(self._build_terminal(), "Terminal")
        tabs.addTab(self._build_keyboard(), "Keyboard")
        tabs.addTab(self._build_groups(), "Groups")
        tabs.addTab(self._build_sftp(), "SFTP")
        tabs.addTab(self._build_security(), "Security")
        tabs.addTab(self._build_proxy(), "Proxy")
        tabs.addTab(self._build_advanced(), "Advanced")

        outer.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -- per-tab builders --------------------------------------------------

    def _build_general(self) -> QWidget:
        """Build the General tab."""
        w = QWidget()
        f = QFormLayout(w)
        s = self._settings["general"]
        self.gen_confirm_close = QCheckBox(w)
        self.gen_confirm_close.setChecked(bool(s["confirm_on_close"]))
        self.gen_default_proto = QComboBox(w)
        self.gen_default_proto.addItems(["ssh", "telnet"])
        self.gen_default_proto.setCurrentText(s["default_protocol"])
        self.gen_default_port = QSpinBox(w)
        self.gen_default_port.setRange(1, 65_535)
        self.gen_default_port.setValue(int(s["default_port"]))
        self.gen_show_sm = QCheckBox(w)
        self.gen_show_sm.setChecked(bool(s["startup_show_session_manager"]))
        f.addRow("Confirm on close:", self.gen_confirm_close)
        f.addRow("Default protocol:", self.gen_default_proto)
        f.addRow("Default port:", self.gen_default_port)
        f.addRow("Show session manager at startup:", self.gen_show_sm)
        return w

    def _build_appearance(self) -> QWidget:
        """Build the Appearance tab."""
        w = QWidget()
        f = QFormLayout(w)
        s = self._settings["appearance"]
        self.app_font_family = QLineEdit(s["default_font_family"], w)
        self.app_font_size = QSpinBox(w)
        self.app_font_size.setRange(6, 32)
        self.app_font_size.setValue(int(s["default_font_size"]))
        self.app_scheme = QComboBox(w)
        self.app_scheme.addItems(["Dark", "Light", "Solarized Dark", "Dracula", "Nord", "Monokai"])
        self.app_scheme.setCurrentText(s["default_color_scheme"])
        self.app_transp = QSpinBox(w)
        self.app_transp.setRange(0, 30)
        self.app_transp.setValue(int(s["terminal_transparency_pct"]))
        f.addRow("Default font family:", self.app_font_family)
        f.addRow("Default font size:", self.app_font_size)
        f.addRow("Default color scheme:", self.app_scheme)
        f.addRow("Transparency (%):", self.app_transp)
        return w

    def _build_terminal(self) -> QWidget:
        """Build the Terminal tab."""
        w = QWidget()
        f = QFormLayout(w)
        s = self._settings["terminal"]
        self.term_encoding = QLineEdit(s["default_encoding"], w)
        self.term_scrollback = QSpinBox(w)
        self.term_scrollback.setRange(100, 1_000_000)
        self.term_scrollback.setValue(int(s["scrollback_lines"]))
        self.term_mouse_copy = QCheckBox(w)
        self.term_mouse_copy.setChecked(bool(s["mouse_select_copy"]))
        self.term_bell = QComboBox(w)
        self.term_bell.addItems(["visual", "audio", "none"])
        self.term_bell.setCurrentText(s["bell_type"])
        f.addRow("Default encoding:", self.term_encoding)
        f.addRow("Scrollback lines:", self.term_scrollback)
        f.addRow("Mouse select = copy:", self.term_mouse_copy)
        f.addRow("Bell type:", self.term_bell)
        return w

    def _build_keyboard(self) -> QWidget:
        """Build the Keyboard tab (placeholder — see CLAUDE.md §9)."""
        w = QWidget()
        QFormLayout(w)
        return w

    def _build_groups(self) -> QWidget:
        """Build the Groups tab — colour ↔ group-name mapping."""
        w = QWidget()
        f = QFormLayout(w)
        self.group_inputs: dict[str, QLineEdit] = {}
        for name, color in self._settings["groups"].items():
            le = QLineEdit(color, w)
            self.group_inputs[name] = le
            f.addRow(f"{name} color:", le)
        return w

    def _build_sftp(self) -> QWidget:
        """Build the SFTP tab."""
        w = QWidget()
        f = QFormLayout(w)
        s = self._settings["sftp"]
        self.sftp_open_mode = QComboBox(w)
        self.sftp_open_mode.addItems(["panel", "tab"])
        self.sftp_open_mode.setCurrentText(s["default_open_mode"])
        self.sftp_max = QSpinBox(w)
        self.sftp_max.setRange(1, 32)
        self.sftp_max.setValue(int(s["max_concurrent_transfers"]))
        f.addRow("Default open mode:", self.sftp_open_mode)
        f.addRow("Max concurrent transfers:", self.sftp_max)
        return w

    def _build_security(self) -> QWidget:
        """Build the Security tab.

        Hosts the *Change Master Password* form. The fields are wiped
        immediately after a successful (or failed) submission so the
        plaintext password never lingers on the form.
        """
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.addWidget(QLabel("<b>Change Master Password</b>", w))
        outer.addWidget(
            QLabel(
                "The master password unlocks the credential vault. "
                "Changing it re-encrypts every saved credential atomically.",
                w,
            )
        )

        form = QFormLayout()
        self.sec_current = QLineEdit(w)
        self.sec_current.setEchoMode(QLineEdit.EchoMode.Password)
        self.sec_new = QLineEdit(w)
        self.sec_new.setEchoMode(QLineEdit.EchoMode.Password)
        self.sec_confirm = QLineEdit(w)
        self.sec_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Current password:", self.sec_current)
        form.addRow("New password:", self.sec_new)
        form.addRow("Confirm new password:", self.sec_confirm)
        outer.addLayout(form)

        self.sec_change_btn = QPushButton("Change master password", w)
        self.sec_change_btn.clicked.connect(self._on_change_master_password)
        if self._vault is None:
            # Vault not wired (e.g. when SettingsDialog is opened from a
            # context that didn't pass it). Surface the form but keep the
            # action disabled rather than silently doing nothing.
            self.sec_change_btn.setEnabled(False)
            self.sec_change_btn.setToolTip(
                "Master password rotation is unavailable in this context."
            )
        outer.addWidget(self.sec_change_btn)
        outer.addStretch(1)
        return w

    def _on_change_master_password(self) -> None:
        """Validate the form and rotate the master password via the vault.

        The vault rotation itself is atomic (single SQLAlchemy
        transaction) — we do the form-side validation here and surface
        a single dialog with the outcome.
        """
        if self._vault is None:
            return
        current = self.sec_current.text()
        new = self.sec_new.text()
        confirm = self.sec_confirm.text()

        if not current or not new:
            QMessageBox.warning(
                self, "NovaTerm", "Current and new password cannot be empty."
            )
            return
        if new != confirm:
            QMessageBox.warning(
                self,
                "NovaTerm",
                "New password and confirmation do not match.",
            )
            return

        try:
            self._vault.change_password(current, new)
        except VaultAuthError:
            QMessageBox.critical(self, "NovaTerm", "Current password is wrong")
            self.sec_current.clear()
            self.sec_current.setFocus()
            return
        except Exception as exc:  # noqa: BLE001 — surface to user verbatim
            QMessageBox.critical(
                self, "NovaTerm", f"Could not change master password: {exc}"
            )
            return
        finally:
            # Always wipe the plaintext fields so they cannot be re-read.
            self.sec_new.clear()
            self.sec_confirm.clear()

        self.sec_current.clear()
        QMessageBox.information(
            self, "NovaTerm", "Master password updated successfully"
        )

    def _build_proxy(self) -> QWidget:
        """Build the Proxy tab."""
        w = QWidget()
        f = QFormLayout(w)
        s = self._settings["proxy"]
        self.proxy_type = QComboBox(w)
        self.proxy_type.addItems(["none", "http", "socks4", "socks5"])
        self.proxy_type.setCurrentText(s["type"])
        self.proxy_host = QLineEdit(s["host"], w)
        self.proxy_port = QSpinBox(w)
        self.proxy_port.setRange(0, 65_535)
        self.proxy_port.setValue(int(s["port"]))
        self.proxy_user = QLineEdit(s.get("username", ""), w)
        f.addRow("Type:", self.proxy_type)
        f.addRow("Host:", self.proxy_host)
        f.addRow("Port:", self.proxy_port)
        f.addRow("Username:", self.proxy_user)
        return w

    def _build_advanced(self) -> QWidget:
        """Build the Advanced tab."""
        w = QWidget()
        f = QFormLayout(w)
        s = self._settings["advanced"]
        self.adv_keepalive = QSpinBox(w)
        self.adv_keepalive.setRange(0, 600)
        self.adv_keepalive.setValue(int(s["ssh_keepalive_seconds"]))
        self.adv_timeout = QSpinBox(w)
        self.adv_timeout.setRange(1, 600)
        self.adv_timeout.setValue(int(s["connect_timeout_seconds"]))
        self.adv_retries = QSpinBox(w)
        self.adv_retries.setRange(0, 100)
        self.adv_retries.setValue(int(s["auto_reconnect_retries"]))
        f.addRow("SSH keepalive (s):", self.adv_keepalive)
        f.addRow("Connect timeout (s):", self.adv_timeout)
        f.addRow("Auto-reconnect retries:", self.adv_retries)
        return w

    # -- save --------------------------------------------------------------

    def _accept(self) -> None:
        """Persist the form into the settings TOML and close."""
        self._settings["general"] = {
            "confirm_on_close": self.gen_confirm_close.isChecked(),
            "default_protocol": self.gen_default_proto.currentText(),
            "default_port": int(self.gen_default_port.value()),
            "startup_show_session_manager": self.gen_show_sm.isChecked(),
        }
        self._settings["appearance"] = {
            "default_font_family": self.app_font_family.text() or "Monospace",
            "default_font_size": int(self.app_font_size.value()),
            "default_color_scheme": self.app_scheme.currentText(),
            "terminal_transparency_pct": int(self.app_transp.value()),
        }
        self._settings["terminal"] = {
            "default_encoding": self.term_encoding.text() or "utf-8",
            "scrollback_lines": int(self.term_scrollback.value()),
            "mouse_select_copy": self.term_mouse_copy.isChecked(),
            "bell_type": self.term_bell.currentText(),
        }
        self._settings["groups"] = {
            name: le.text() for name, le in self.group_inputs.items()
        }
        self._settings["sftp"] = {
            "default_open_mode": self.sftp_open_mode.currentText(),
            "max_concurrent_transfers": int(self.sftp_max.value()),
        }
        self._settings["proxy"] = {
            "type": self.proxy_type.currentText(),
            "host": self.proxy_host.text(),
            "port": int(self.proxy_port.value()),
            "username": self.proxy_user.text(),
        }
        self._settings["advanced"] = {
            "ssh_keepalive_seconds": int(self.adv_keepalive.value()),
            "connect_timeout_seconds": int(self.adv_timeout.value()),
            "auto_reconnect_retries": int(self.adv_retries.value()),
        }
        save_settings(self._settings)
        self.accept()
