"""*File → Import → Import from SecureCRT* dialog.

Drives :mod:`core.securecrt_import` to parse a SecureCRT *Config* tree
and writes the resulting sessions / button-bar commands into NovaTerm's
SQLite store. The user picks the *Config* folder via a Browse button,
toggles which classes of records to import, and gets a summary
(``Imported X sessions and Y commands successfully``) once the
write-back finishes.

Per the spec:

* Passwords are *not* imported (SecureCRT encrypts them with a
  proprietary, non-portable scheme); imported sessions are stored with
  ``auth_type="ask"`` so NovaTerm prompts on first connect.
* Duplicate detection compares ``(hostname, name)``; matches are
  skipped and counted in the summary.
* Imported commands land in a single Command Group named
  *Imported from SecureCRT*; if it already exists, additional commands
  are appended.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.command_store import CommandStore
from core.securecrt_import import (
    ImportedCommand,
    ImportedSession,
    parse_config,
)
from core.session_store import SessionStore

logger = logging.getLogger(__name__)

#: Name of the destination Command Group for imported button-bar commands.
_IMPORTED_GROUP_NAME = "Imported from SecureCRT"


def _ensure_folder_path(
    sessions: SessionStore, parts: tuple[str, ...]
) -> int | None:
    """Create (or look up) the nested folder hierarchy ``parts``.

    Returns the leaf folder's id, or ``None`` for an empty path (the
    caller will store the session at root). Existing folders with the
    same ``(name, parent_id)`` are reused so re-imports don't duplicate
    the tree on every run.
    """
    if not parts:
        return None
    parent_id: int | None = None
    existing = sessions.list_folders()
    by_key: dict[tuple[int | None, str], int] = {
        (f.parent_id, f.name): f.id for f in existing
    }
    for name in parts:
        key = (parent_id, name)
        if key in by_key:
            parent_id = by_key[key]
            continue
        new_id = sessions.create_folder(name=name, parent_id=parent_id)
        by_key[key] = new_id
        parent_id = new_id
    return parent_id


def _import_sessions(
    sessions_store: SessionStore,
    rows: list[ImportedSession],
) -> tuple[int, int]:
    """Insert ``rows`` into the SQLite store.

    Returns ``(added, duplicates)``. A row is considered a duplicate
    when an existing :class:`Session` shares both the same ``hostname``
    *and* the same ``name`` (per the spec).
    """
    existing = {(s.hostname, s.name) for s in sessions_store.list_sessions()}
    added = 0
    duplicates = 0
    for row in rows:
        key = (row.hostname, row.name)
        if key in existing:
            duplicates += 1
            continue
        folder_id = _ensure_folder_path(sessions_store, row.folder_path)
        sessions_store.create_session(
            name=row.name,
            folder_id=folder_id,
            hostname=row.hostname,
            port=row.port,
            protocol=row.protocol,
            username=row.username,
            auth_type="ask",
        )
        existing.add(key)
        added += 1
    return added, duplicates


def _import_commands(
    commands_store: CommandStore,
    rows: list[ImportedCommand],
) -> int:
    """Insert ``rows`` under the *Imported from SecureCRT* group.

    Returns the number of newly-created :class:`Command` rows.
    Duplicate commands inside the destination group (matched by name)
    are skipped silently so re-imports are idempotent.
    """
    if not rows:
        return 0
    groups = commands_store.list_groups()
    target_id: int | None = next(
        (g.id for g in groups if g.name == _IMPORTED_GROUP_NAME), None
    )
    if target_id is None:
        target_id = commands_store.create_group(_IMPORTED_GROUP_NAME)
    existing_names = {
        c.name for c in commands_store.list_commands(group_id=target_id)
    }
    added = 0
    for cmd in rows:
        if cmd.name in existing_names:
            continue
        commands_store.create_command(
            group_id=target_id, name=cmd.name, command_text=cmd.command_text
        )
        existing_names.add(cmd.name)
        added += 1
    return added


class SecureCRTImportDialog(QDialog):
    """Folder picker + per-class checkboxes + result summary."""

    def __init__(
        self,
        sessions: SessionStore,
        commands: CommandStore,
        parent: QWidget | None = None,
    ) -> None:
        """Build the dialog wired to ``sessions`` / ``commands`` stores."""
        super().__init__(parent)
        self.setWindowTitle("Import from SecureCRT")
        self.setModal(True)
        self.resize(540, 220)

        self._sessions = sessions
        self._commands = commands
        self._refresh_button_bar = getattr(parent, "_button_bar", None)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Select the SecureCRT <b>Config</b> folder "
                "(usually contains a <i>Sessions</i> subfolder):",
                self,
            )
        )

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit(self)
        self._path_edit.setPlaceholderText(
            str(self._guess_default_config_dir())
        )
        path_row.addWidget(self._path_edit, 1)
        browse = QPushButton("Browse\u2026", self)
        browse.clicked.connect(self._on_browse)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        self._import_sessions_cb = QCheckBox("Import Sessions", self)
        self._import_sessions_cb.setChecked(True)
        layout.addWidget(self._import_sessions_cb)

        self._import_commands_cb = QCheckBox(
            "Import Button Bar Commands", self
        )
        self._import_commands_cb.setChecked(True)
        layout.addWidget(self._import_commands_cb)

        self._summary_label = QLabel("", self)
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)
        layout.addStretch(1)

        bb = QDialogButtonBox(self)
        self._import_btn = bb.addButton(
            "Import", QDialogButtonBox.ButtonRole.AcceptRole
        )
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        self._import_btn.clicked.connect(self._on_import)
        layout.addWidget(bb)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _guess_default_config_dir() -> Path:
        """Return SecureCRT's default Linux Config path (best-effort hint)."""
        return Path.home() / ".vandyke" / "SecureCRT" / "Config"

    def _on_browse(self) -> None:
        """File-picker for the *Config* folder."""
        start = self._path_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Select SecureCRT Config Folder", start
        )
        if chosen:
            self._path_edit.setText(chosen)

    def _on_import(self) -> None:
        """Run the import and update the summary label."""
        path_str = self._path_edit.text().strip() or str(
            self._guess_default_config_dir()
        )
        config_dir = Path(path_str)
        if not config_dir.is_dir():
            QMessageBox.warning(
                self,
                "Import from SecureCRT",
                f"Folder not found:\n{config_dir}",
            )
            return

        result = parse_config(config_dir)
        sessions_added = sessions_dupes = commands_added = 0

        if self._import_sessions_cb.isChecked():
            sessions_added, sessions_dupes = _import_sessions(
                self._sessions, result.sessions
            )
        if self._import_commands_cb.isChecked():
            commands_added = _import_commands(self._commands, result.commands)

        # Refresh the button bar in the parent main window so newly
        # imported commands appear without an app restart.
        if commands_added and self._refresh_button_bar is not None:
            try:
                self._refresh_button_bar.refresh()
            except Exception:  # pragma: no cover — defensive
                logger.exception("ButtonBar refresh after SecureCRT import failed")

        summary = (
            f"Imported {sessions_added} sessions and "
            f"{commands_added} commands successfully."
        )
        if sessions_dupes:
            summary += f" Skipped {sessions_dupes} duplicate session(s)."
        self._summary_label.setText(summary)
        logger.info(
            "SecureCRT import: sessions=%d (dupes=%d) commands=%d",
            sessions_added,
            sessions_dupes,
            commands_added,
        )
