"""NovaTerm entry-point.

Boots the qasync event loop, prompts for the master password (creating it on
first launch), instantiates the core stores and shows the main window.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import qasync
from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

from core.command_store import CommandStore
from core.credential_vault import CredentialVault, VaultAuthError
from core.session_store import SessionStore
from ui.main_window import MainWindow


def _prompt_master_password(vault: CredentialVault, app: QApplication) -> bool:
    """Walk the user through master-password creation/unlock.

    Returns ``True`` on success, ``False`` if the user cancelled.
    """
    if not vault.is_initialized():
        text, ok = QInputDialog.getText(
            None,
            "NovaTerm — Set master password",
            "Choose a master password to encrypt saved credentials:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not text:
            return False
        vault.initialize(text)
        return True

    for _ in range(3):
        text, ok = QInputDialog.getText(
            None,
            "NovaTerm — Master password",
            "Enter your master password:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return False
        try:
            vault.unlock(text)
            return True
        except VaultAuthError:
            QMessageBox.warning(None, "NovaTerm", "Wrong password — try again.")
    return False


def main(argv: Optional[list[str]] = None) -> int:
    """Application main entry point. Returns the Qt exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = QApplication(argv if argv is not None else sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    store = SessionStore()
    vault = CredentialVault(store)
    commands = CommandStore(store)

    if not _prompt_master_password(vault, app):
        return 0

    window = MainWindow(store, vault, commands)
    window.show()

    with loop:
        return loop.run_forever() or 0


if __name__ == "__main__":
    sys.exit(main())
