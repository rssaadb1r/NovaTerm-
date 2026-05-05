"""Main NovaTerm window — toolbar, menus, status bar and global shortcuts."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.command_store import CommandStore, substitute_variables
from core.credential_vault import CredentialVault, VaultAuthError
from core.session_store import SessionStore
from core.ssh_client import AsyncSSHClient, HopConfig, SSHConnectConfig

from .button_bar import ButtonBar
from .host_input import HostInputLineEdit
from .cluster_bar import ClusterInputBar, ClusterSelectDialog
from .command_manager import CommandFuzzyPopup
from .command_manager_editor import CommandManagerEditor
from .command_window import DEFAULT_HEIGHT as _CMD_WINDOW_DEFAULT_HEIGHT, CommandWindow
from .default_session_dialog import DefaultSessionDialog
from .quick_connect import QuickConnectDialog
from .session_dialog import SessionDialog
from .securecrt_import_dialog import SecureCRTImportDialog
from .session_manager import SessionManagerPanel
from .settings_dialog import SettingsDialog, load_settings
from .tab_manager import (
    CONNECTED,
    DISCONNECTED,
    ERROR,
    NovaTabWidget,
    TabContent,
)
from .terminal_widget import TerminalWidget

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level NovaTerm window."""

    def __init__(
        self,
        store: SessionStore,
        vault: CredentialVault,
        commands: CommandStore,
    ) -> None:
        """Wire up the entire UI shell."""
        super().__init__()
        self.setWindowTitle("NovaTerm")
        self.resize(1280, 800)

        self._store = store
        self._vault = vault
        self._commands = commands
        self._settings = load_settings()
        # Cluster target set & SSH clients are keyed by ``TabContent`` rather
        # than by tab index — the index changes when tabs are closed,
        # detached, or reordered, but the widget object is stable.
        self._cluster_targets: set[TabContent] = set()
        self._ssh_clients: dict[TabContent, AsyncSSHClient] = {}
        # Detached floating windows must outlive the method that creates
        # them — PyQt6 destroys parentless QWidgets the moment their Python
        # wrapper goes out of scope. We keep strong references here.
        self._floating_windows: list[QMainWindow] = []

        # -- central layout ------------------------------------------------
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, central)
        self._sidebar = SessionManagerPanel(store, self)
        self._sidebar.session_activated.connect(self._open_saved_session)
        self._sidebar.session_new_requested.connect(self._on_new_session)
        self._sidebar.session_edit_requested.connect(self._on_edit_session)
        self._sidebar.session_clone_requested.connect(self._on_clone_session)
        self._sidebar.session_delete_requested.connect(self._on_delete_session)
        self._sidebar.default_session_edit_requested.connect(
            self._on_edit_default_session
        )
        self._splitter.addWidget(self._sidebar)

        # The right-hand pane is a vertical splitter containing two
        # children: a top stack (tabs + Button Bar) and the Command
        # Window. The vertical splitter is what gives the Command Window
        # its 'drag the top edge to resize' behaviour required by the
        # spec.
        self._tabs = NovaTabWidget(self)
        self._tabs.close_requested.connect(self._close_tab)
        self._tabs.disconnect_requested.connect(self._disconnect_tab)
        self._tabs.reconnect_requested.connect(self._reconnect_tab)
        self._tabs.detach_requested.connect(self._detach_tab)
        self._tabs.clone_requested.connect(self._clone_tab)
        self._tabs.rename_requested.connect(self._rename_tab)
        self._tabs.split_horizontal_requested.connect(
            lambda i: self._split_tab(i, Qt.Orientation.Horizontal)
        )
        self._tabs.split_vertical_requested.connect(
            lambda i: self._split_tab(i, Qt.Orientation.Vertical)
        )

        self._button_bar = ButtonBar(commands, self)
        self._button_bar.command_clicked.connect(self._on_button_bar_clicked)
        self._button_bar.manage_requested.connect(self._on_open_command_manager)
        self._button_bar.hide()  # toggled on via View menu

        top_pane = QWidget(self)
        top_layout = QVBoxLayout(top_pane)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        top_layout.addWidget(self._tabs, 1)
        top_layout.addWidget(self._button_bar, 0)

        self._command_window = CommandWindow(self)
        self._command_window.send_to_active.connect(
            self._on_command_window_send_active
        )
        self._command_window.send_to_all.connect(
            self._on_command_window_send_all
        )
        self._command_window.hide()  # toggled on via View menu / Ctrl+Shift+C

        self._right_splitter = QSplitter(Qt.Orientation.Vertical, central)
        self._right_splitter.addWidget(top_pane)
        self._right_splitter.addWidget(self._command_window)
        self._right_splitter.setStretchFactor(0, 1)
        self._right_splitter.setStretchFactor(1, 0)
        self._right_splitter.setCollapsible(1, False)
        self._right_splitter.setSizes([800, _CMD_WINDOW_DEFAULT_HEIGHT])

        self._splitter.addWidget(self._right_splitter)
        self._splitter.setSizes([250, 1030])
        outer.addWidget(self._splitter, 1)

        # Cluster bar (initially hidden).
        self._cluster_bar = ClusterInputBar(central)
        self._cluster_bar.char_typed.connect(self._broadcast_chars)
        self._cluster_bar.closed.connect(self._toggle_cluster_off)
        self._cluster_bar.hide()
        outer.addWidget(self._cluster_bar)

        self.setCentralWidget(central)

        self._build_menus()
        self._build_toolbar()
        self.setStatusBar(QStatusBar(self))
        self._install_shortcuts()

    # ------------------------------------------------------------------
    # Menus / toolbar / shortcuts
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        """Build the menu bar."""
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        file_menu.addAction(self._make_action("Quick Connect…", self._on_quick_connect, "Ctrl+Q"))
        file_menu.addAction(self._make_action("New Session…", self._on_new_session, "Ctrl+N"))
        file_menu.addSeparator()
        # File → Import → … submenu (NovaTerm JSON + SecureCRT Config tree).
        import_menu = file_menu.addMenu("&Import")
        import_menu.addAction(
            self._make_action("Import Sessions\u2026", self._on_import_sessions)
        )
        import_menu.addAction(
            self._make_action(
                "Import from SecureCRT\u2026", self._on_import_securecrt
            )
        )
        file_menu.addAction(self._make_action("Export Sessions…", self._on_export_sessions))
        file_menu.addSeparator()
        file_menu.addAction(self._make_action("Quit", self.close, "Ctrl+Shift+Q"))

        edit_menu = mb.addMenu("&Edit")
        edit_menu.addAction(self._make_action("Find in Terminal…", self._on_find, "Ctrl+F"))
        edit_menu.addAction(self._make_action("Settings…", self._on_settings, "Ctrl+,"))

        opt_menu = mb.addMenu("&Options")
        opt_menu.addAction(
            self._make_action("Default Session…", self._on_edit_default_session)
        )
        opt_menu.addAction(
            self._make_action("Command Manager…", self._on_open_command_manager)
        )

        view_menu = mb.addMenu("&View")
        view_menu.addAction(
            self._make_action("Toggle Session Manager", self._toggle_sidebar)
        )
        # Independent toggles — Button Bar and Command Window can be on or
        # off in any combination per the Feature spec.
        self._toggle_button_bar_action = QAction("Button Bar", self)
        self._toggle_button_bar_action.setCheckable(True)
        self._toggle_button_bar_action.toggled.connect(self._toggle_button_bar)
        view_menu.addAction(self._toggle_button_bar_action)

        self._toggle_command_window_action = QAction("Command Window", self)
        self._toggle_command_window_action.setCheckable(True)
        self._toggle_command_window_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self._toggle_command_window_action.toggled.connect(self._toggle_command_window)
        view_menu.addAction(self._toggle_command_window_action)

        view_menu.addSeparator()
        view_menu.addAction(self._make_action("Toggle Cluster Mode…", self._on_open_cluster))

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self._make_action("About NovaTerm", self._on_about))

    def _build_toolbar(self) -> None:
        """Build the main toolbar."""
        bar = QToolBar("Main", self)
        self.addToolBar(bar)
        bar.addAction(self._make_action("Quick Connect", self._on_quick_connect))
        bar.addAction(self._make_action("New Session", self._on_new_session))
        bar.addAction(self._make_action("Cluster", self._on_open_cluster))
        bar.addAction(self._make_action("SFTP", self._on_open_sftp))
        bar.addAction(self._make_action("Find", self._on_find))
        bar.addAction(self._make_action("Settings", self._on_settings))
        bar.addAction(
            self._make_action("Default Session…", self._on_edit_default_session)
        )

        # SecureCRT-style Quick Host Bar. Type a hostname, press Enter,
        # connect using the Default Session credentials. The protocol is
        # taken from the Default Session row — the toolbar never asks
        # the user to pick a protocol per the UX spec.
        bar.addSeparator()
        self._host_bar = HostInputLineEdit(self._store, self)
        self._host_bar.setMaximumWidth(200)
        self._host_bar.returnPressed.connect(self._on_host_bar_connect)
        bar.addWidget(self._host_bar)

    def _install_shortcuts(self) -> None:
        """Install global QShortcut bindings (see CLAUDE.md §9)."""
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self._on_quick_connect)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=lambda: self._close_tab(self._tabs.currentIndex()))
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self._cycle_tab(+1))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self, activated=lambda: self._cycle_tab(-1))
        for n in range(1, 10):
            QShortcut(
                QKeySequence(f"Ctrl+{n}"),
                self,
                activated=lambda i=n - 1: self._tabs.setCurrentIndex(min(i, self._tabs.count() - 1)),
            )
        QShortcut(QKeySequence("Ctrl+Shift+Space"), self, activated=self._on_fuzzy_command)
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, activated=self._on_open_sftp)

    def _make_action(self, name: str, slot, shortcut: str | None = None) -> QAction:
        """Build a menu/toolbar QAction."""
        a = QAction(name, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        return a

    # ------------------------------------------------------------------
    # Tab plumbing
    # ------------------------------------------------------------------

    def _new_terminal_tab(
        self,
        *,
        session_id: int | None,
        name: str,
        color_tag: str | None,
    ) -> TabContent:
        """Create a fresh tab and return its :class:`TabContent` widget."""
        terminal = TerminalWidget()
        terminal.set_broadcast_callback(self._broadcast_text_from_terminal)
        content = TabContent(
            terminal,
            session_id=session_id,
            session_name=name,
            color_tag=color_tag,
        )
        idx = self._tabs.add_tab(content)
        self._tabs.setCurrentIndex(idx)
        # Hand the keyboard focus to the new terminal so the user can
        # start typing immediately after a saved-session, Quick Connect
        # or Quick-Host-Bar invocation — otherwise focus stays on
        # whatever widget triggered the connect (the Host bar, the
        # sidebar, etc.) and the first keystroke is dropped on the floor.
        terminal.setFocus(Qt.FocusReason.OtherFocusReason)
        return content

    def _content_at(self, index: int) -> TabContent | None:
        """Return the :class:`TabContent` at ``index``, if any."""
        widget = self._tabs.widget(index)
        return widget if isinstance(widget, TabContent) else None

    def _current_terminal(self) -> TerminalWidget | None:
        """Return the active tab's :class:`TerminalWidget`, if any."""
        widget = self._tabs.currentWidget()
        if isinstance(widget, TabContent):
            return widget.terminal()
        return None

    def _close_tab(self, index: int) -> None:
        """Disconnect & remove the tab at ``index``.

        If the tab has a live SSH client, the user gets a confirmation
        dialog first — closing the tab disconnects the underlying
        Paramiko transport, and we don't want a stray Ctrl+W to drop
        an active production session.
        """
        if index < 0 or index >= self._tabs.count():
            return
        content = self._content_at(index)
        client = self._ssh_clients.get(content) if content is not None else None
        if client is not None and getattr(client, "connected", False):
            box = QMessageBox(self)
            box.setWindowTitle("Close Session")
            box.setText("This will disconnect the SSH session. Are you sure?")
            box.setIcon(QMessageBox.Icon.Question)
            disconnect_btn = box.addButton(
                "Disconnect && Close", QMessageBox.ButtonRole.AcceptRole
            )
            cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(cancel_btn)
            box.exec()
            if box.clickedButton() is not disconnect_btn:
                return
        if content is not None:
            self._cluster_targets.discard(content)
            client = self._ssh_clients.pop(content, None)
            if client is not None:
                asyncio.ensure_future(client.disconnect())
        self._tabs.removeTab(index)

    def closeEvent(self, event):  # noqa: N802 — Qt API
        """Confirm exit when there are still active SSH sessions.

        Single confirmation covers all open sessions; on accept, every
        live :class:`AsyncSSHClient` is asked to disconnect cleanly so
        no orphan Paramiko transports / sockets / SFTP threads outlive
        the GUI.
        """
        live = [
            (content, client)
            for content, client in self._ssh_clients.items()
            if getattr(client, "connected", False)
        ]
        if live:
            box = QMessageBox(self)
            box.setWindowTitle("Exit NovaTerm")
            box.setText(
                f"You have {len(live)} active session(s). Close all and exit?"
            )
            box.setIcon(QMessageBox.Icon.Question)
            exit_btn = box.addButton("Exit", QMessageBox.ButtonRole.AcceptRole)
            cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(cancel_btn)
            box.exec()
            if box.clickedButton() is not exit_btn:
                event.ignore()
                return
            for _content, client in live:
                try:
                    asyncio.ensure_future(client.disconnect())
                except Exception:  # pragma: no cover — best-effort cleanup
                    pass
        event.accept()

    def _cycle_tab(self, delta: int) -> None:
        """Cycle to the next/prev tab."""
        n = self._tabs.count()
        if n == 0:
            return
        self._tabs.setCurrentIndex((self._tabs.currentIndex() + delta) % n)

    def _disconnect_tab(self, index: int) -> None:
        """Disconnect the SSH backend for tab ``index`` (keeps the tab open).

        The client is *removed* from ``_ssh_clients`` rather than merely
        looked up; otherwise the dict accumulates stale entries that the
        cluster broadcaster and *Send to All* helpers would still iterate
        over (only their ``client.connected`` guards stop them from
        emitting bytes to a closed channel).
        """
        content = self._content_at(index)
        if content is not None:
            client = self._ssh_clients.pop(content, None)
            if client is not None:
                asyncio.ensure_future(client.disconnect())
        self._tabs.set_tab_status(index, DISCONNECTED)

    def _reconnect_tab(self, index: int) -> None:
        """Reconnect the tab using the saved session id, if any.

        Reuses the existing :class:`TabContent` (and its terminal widget)
        rather than creating a duplicate tab — this keeps the user’s
        scrollback in the same place and avoids ending up with one
        DISCONNECTED orphan tab next to the freshly-connected one.
        """
        widget = self._tabs.widget(index)
        if isinstance(widget, TabContent) and widget.session_id is not None:
            self._open_saved_session(widget.session_id, into=widget)

    def _detach_tab(self, index: int) -> None:
        """Move the tab into a free-floating window.

        The :class:`TabContent` widget keeps its identity when it leaves
        the tab bar. Its entry in ``_ssh_clients`` / ``_cluster_targets``
        is still valid, but it can no longer be addressed via the cluster
        bar or right-click menu — so we drop it from those collections to
        avoid 'phantom' broadcasts. The new :class:`QMainWindow` is also
        retained on ``self._floating_windows`` so PyQt6 doesn't garbage-
        collect it the moment this method returns.
        """
        content = self._content_at(index)
        if content is None:
            return
        client = self._ssh_clients.pop(content, None)
        self._cluster_targets.discard(content)
        self._tabs.removeTab(index)
        floating = QMainWindow()
        floating.setWindowTitle(content.session_name)
        floating.setCentralWidget(content)
        floating.resize(900, 600)
        floating.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        floating.destroyed.connect(
            lambda _obj=None, w=floating: self._floating_windows.remove(w)
            if w in self._floating_windows else None
        )
        self._floating_windows.append(floating)
        floating.show()
        # Keep the SSH session alive in the floating window: pin the client
        # on the widget itself so it isn't garbage-collected.
        if client is not None:
            content._detached_client = client  # type: ignore[attr-defined]

    def _clone_tab(self, index: int) -> None:
        """Open a new tab with the same session settings."""
        widget = self._tabs.widget(index)
        if isinstance(widget, TabContent) and widget.session_id is not None:
            self._open_saved_session(widget.session_id)

    def _rename_tab(self, index: int) -> None:
        """Prompt for a new tab name."""
        widget = self._tabs.widget(index)
        if not isinstance(widget, TabContent):
            return
        text, ok = QInputDialog.getText(
            self, "Rename tab", "New name:", QLineEdit.EchoMode.Normal, widget.session_name
        )
        if ok and text:
            widget.session_name = text
            self._tabs.refresh_tab(index)

    def _split_tab(self, index: int, orientation: Qt.Orientation) -> None:
        """Split the current tab horizontally / vertically."""
        widget = self._tabs.widget(index)
        if not isinstance(widget, TabContent):
            return
        new_term = TerminalWidget()
        new_term.set_broadcast_callback(self._broadcast_text_from_terminal)
        widget.split(orientation, new_term)

    # ------------------------------------------------------------------
    # File menu actions
    # ------------------------------------------------------------------

    def _on_quick_connect(self) -> None:
        """Show the Quick Connect dialog and open a tab on Ok."""
        dlg = QuickConnectDialog(self._store, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        data = dlg.result_data()
        if not data.hostname:
            return
        # Tab label is the hostname only — no user / port / protocol.
        content = self._new_terminal_tab(
            session_id=None,
            name=data.hostname,
            color_tag=None,
        )
        if data.save_as_session:
            self._store.create_session(
                name=f"{data.username}@{data.hostname}" if data.username else data.hostname,
                hostname=data.hostname,
                port=data.port,
                protocol=data.protocol,
                username=data.username or None,
                auth_type="ask",
            )
            self._sidebar.refresh()
        self._store.add_recent_connection(
            data.hostname, data.port, data.protocol, data.username or None
        )
        if data.protocol == "ssh":
            self._connect_ssh(
                content,
                hostname=data.hostname,
                port=data.port,
                username=data.username,
                password=None,
                jumps=[],
            )

    def _on_new_session(self, folder_id: object | None = None) -> None:
        """Open the New Session dialog and persist on Ok.

        :param folder_id: Optional folder id passed from the sidebar context
            menu so the new session pre-targets the folder the user clicked.
            QActions emit ``triggered(checked: bool)`` — we ignore that
            payload because :class:`bool` is an :class:`int` subclass and
            we don't want ``True`` to be misread as folder id 1.
        """
        target_folder: int | None = None
        if (
            isinstance(folder_id, int)
            and not isinstance(folder_id, bool)
            and folder_id > 0
        ):
            target_folder = int(folder_id)
        dlg = SessionDialog(
            self._store, parent=self, default_folder_id=target_folder
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        fields = dlg.fields()
        new_id = self._store.create_session(**fields)
        # Expand the destination folder so the freshly-created session is
        # immediately visible (otherwise it lands inside a collapsed
        # folder and the user reasonably believes the create failed).
        chosen_folder = fields.get("folder_id")
        if isinstance(chosen_folder, int) and chosen_folder > 0:
            self._store.set_folder_expanded(chosen_folder, True)
        self._sidebar.refresh()
        self._sidebar.select_session(new_id)

    def _on_edit_default_session(self) -> None:
        """Open the Default Session editor."""
        dlg = DefaultSessionDialog(self._store, self._vault, self)
        dlg.exec()
        # The session editor pulls the latest defaults each time it opens,
        # so no extra refresh is needed here.

    def _on_host_bar_connect(self) -> None:
        """Connect to the hostname typed in the toolbar Quick Host Bar.

        Uses the Default Session username / password / port / protocol
        (decrypting the saved password through the vault when unlocked).
        If the connection fails on the first attempt the Quick Connect
        dialog is opened pre-filled with the typed hostname so the user
        can override credentials interactively.
        """
        host = self._host_bar.text().strip()
        if not host:
            return
        defaults = self._store.get_default_session()
        password: str | None = None
        if (
            defaults.encrypted_password is not None
            and self._vault.is_unlocked()
        ):
            try:
                password = self._vault.decrypt(defaults.encrypted_password)
            except VaultAuthError:
                password = None

        username = defaults.username or None
        # The protocol comes from the Default Session row — the toolbar
        # has no protocol picker (per the UX spec).
        protocol = defaults.protocol or "ssh"
        port = defaults.port or (23 if protocol == "telnet" else 22)
        # Tab label is the hostname only — no user / port / protocol.
        content = self._new_terminal_tab(
            session_id=None, name=host, color_tag=None
        )
        self._store.add_recent_connection(host, port, protocol, username)
        if protocol == "ssh":
            self._connect_ssh(
                content,
                hostname=host,
                port=port,
                username=username,
                password=password,
                jumps=[],
                on_failure=lambda: self._open_quick_connect_with_host(host),
            )
        self._host_bar.clear()

    def _open_quick_connect_with_host(self, host: str) -> None:
        """Open the Quick Connect dialog pre-filled with ``host``.

        Used as the fallback when a Default-Session-driven connection from
        the toolbar Quick Host Bar fails.
        """
        dlg = QuickConnectDialog(self._store, self)
        if hasattr(dlg, "hostname"):
            dlg.hostname.setText(host)  # type: ignore[attr-defined]
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        data = dlg.result_data()
        if not data.hostname:
            return
        # Tab label is the hostname only — no user / port / protocol.
        content = self._new_terminal_tab(
            session_id=None,
            name=data.hostname,
            color_tag=None,
        )
        self._store.add_recent_connection(
            data.hostname, data.port, data.protocol, data.username or None
        )
        if data.protocol == "ssh":
            self._connect_ssh(
                content,
                hostname=data.hostname,
                port=data.port,
                username=data.username,
                password=None,
                jumps=[],
            )

    def _on_edit_session(self, sid: int | bool) -> None:
        """Edit an existing session row."""
        if isinstance(sid, bool):
            return
        sess = self._store.get_session(int(sid))
        sid = int(sid)
        if sess is None:
            return
        dlg = SessionDialog(self._store, existing=sess, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        self._store.update_session(sid, **dlg.fields())
        self._sidebar.refresh()

    def _on_clone_session(self, sid: int) -> None:
        """Clone an existing session row."""
        sess = self._store.get_session(sid)
        if sess is None:
            return
        new_id = self._store.create_session(
            name=f"{sess.name} (copy)",
            hostname=sess.hostname,
            port=sess.port,
            protocol=sess.protocol,
            username=sess.username,
            auth_type=sess.auth_type,
            color_tag=sess.color_tag,
            group_tag=sess.group_tag,
        )
        self._sidebar.refresh()
        return None

    def _on_delete_session(self, sid: int) -> None:
        """Delete a saved session after confirming."""
        if QMessageBox.question(self, "Delete", "Delete this session?") != QMessageBox.StandardButton.Yes:
            return
        self._store.delete_session(sid)
        self._sidebar.refresh()

    def _on_import_sessions(self) -> None:
        """Import sessions from a JSON file."""
        path, _ = QFileDialog.getOpenFileName(self, "Import sessions", "", "JSON (*.json)")
        if not path:
            return
        with open(path, "r", encoding="utf-8") as fh:
            payload = fh.read()
        added = self._store.import_sessions_json(payload)
        QMessageBox.information(self, "Import", f"Imported {added} session(s)")
        self._sidebar.refresh()

    def _on_import_securecrt(self) -> None:
        """Open the *Import from SecureCRT* dialog (sessions + button bar)."""
        dlg = SecureCRTImportDialog(self._store, self._commands, self)
        dlg.exec()
        # The dialog refreshes the button bar itself; just refresh the
        # sidebar tree so any newly-imported sessions show up without
        # the user having to restart.
        self._sidebar.refresh()

    def _on_export_sessions(self) -> None:
        """Export sessions to a JSON file."""
        path, _ = QFileDialog.getSaveFileName(self, "Export sessions", "", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._store.export_sessions_json())
        QMessageBox.information(self, "Export", f"Saved to {path}")

    def _open_saved_session(
        self, sid: int, *, into: TabContent | None = None
    ) -> None:
        """Activate a saved session.

        :param sid:  Session id to open.
        :param into: Optional existing :class:`TabContent` to reuse instead
            of opening a fresh tab — used by the *Reconnect* action so the
            user sees the reconnection happen in the same tab.
        """
        sess = self._store.get_session(sid)
        if sess is None:
            return
        if into is not None:
            content = into
            # Drop any stale SSH client / cluster membership tied to the
            # previous incarnation of this tab so we start clean.
            old_client = self._ssh_clients.pop(content, None)
            if old_client is not None:
                asyncio.ensure_future(old_client.disconnect())
            content.session_id = sess.id
            content.session_name = sess.name
            content.color_tag = sess.color_tag
            idx = self._tabs.indexOf(content)
            if idx >= 0:
                # Tab label is the hostname only — not the session name.
                self._tabs.setTabText(idx, sess.hostname or sess.name)
        else:
            content = self._new_terminal_tab(
                session_id=sess.id,
                name=sess.hostname or sess.name,
                color_tag=sess.color_tag,
            )
        if sess.protocol == "ssh":
            password: str | None = None
            if sess.auth_type == "password" and sess.encrypted_credential and self._vault.is_unlocked():
                try:
                    password = self._vault.decrypt(sess.encrypted_credential)
                except VaultAuthError:
                    password = None

            # Default Session fallback — inherit any field the saved session
            # leaves blank (Feature: SecureCRT-style Default Session).
            defaults = self._store.get_default_session()
            username = sess.username or defaults.username or ""
            if password is None and self._vault.is_unlocked() and defaults.encrypted_password:
                try:
                    password = self._vault.decrypt(defaults.encrypted_password)
                except VaultAuthError:
                    password = None

            jumps = self._resolve_jump_chain(sess.jump_host_chain or [])
            self._connect_ssh(
                content,
                hostname=sess.hostname,
                port=sess.port,
                username=username,
                password=password,
                jumps=jumps,
            )
        self._store.touch_last_connected(sid)

    def _resolve_jump_chain(self, bastion_ids: list[int]) -> list[HopConfig]:
        """Turn a list of :class:`BastionProfile` ids into ``HopConfig`` hops.

        Decrypts each bastion's stored credential through the vault when the
        vault is unlocked. Bastions whose IDs no longer exist are skipped.
        """
        hops: list[HopConfig] = []
        for bid in bastion_ids:
            bastion = self._store.get_bastion(int(bid))
            if bastion is None:
                logger.warning("jump-host id %s missing, skipping", bid)
                continue
            password: str | None = None
            if (
                bastion.auth_type == "password"
                and bastion.encrypted_credential
                and self._vault.is_unlocked()
            ):
                try:
                    password = self._vault.decrypt(bastion.encrypted_credential)
                except VaultAuthError:
                    password = None
            hops.append(
                HopConfig(
                    hostname=bastion.hostname,
                    port=bastion.port,
                    username=bastion.username or "",
                    password=password,
                    key_path=bastion.key_path,
                )
            )
        return hops

    def _connect_ssh(
        self,
        content: TabContent,
        *,
        hostname: str,
        port: int,
        username: str | None,
        password: str | None,
        jumps: list[HopConfig] | None = None,
        on_failure: Any = None,
    ) -> None:
        """Spawn the asyncio task that brings up the SSH connection.

        ``jumps`` is an ordered list of :class:`HopConfig` describing the
        ProxyJump chain (resolved from the session's ``jump_host_chain``).
        ``on_failure`` is an optional zero-arg callable invoked on the Qt
        main thread when the *initial* ``client.connect`` raises — used
        by the toolbar Quick Host Bar to fall back to the Quick Connect
        dialog when the Default Session credentials don't authenticate.
        """
        config = SSHConnectConfig(
            target=HopConfig(
                hostname=hostname,
                port=port,
                username=username or "",
                password=password,
            ),
            jumps=list(jumps or []),
            keepalive_interval=int(self._settings["advanced"]["ssh_keepalive_seconds"]),
            connect_timeout=int(self._settings["advanced"]["connect_timeout_seconds"]),
            auto_reconnect_retries=int(self._settings["advanced"]["auto_reconnect_retries"]),
        )
        client = AsyncSSHClient(config)
        self._ssh_clients[content] = client

        async def _runner() -> None:
            terminal = content.terminal()
            connect_failed = False
            # Phase 1 — initial handshake. Only failures here should fire
            # ``on_failure`` (and paint the ERROR dot); a later read-loop
            # exception means we *had* a working session that subsequently
            # dropped, which is normal disconnect territory.
            try:
                # ``progress`` is invoked on the main loop (see ssh_client.py).
                await client.connect(
                    progress=lambda m: self.statusBar().showMessage(m)
                )
            except Exception as exc:
                logger.exception("SSH connect failed")
                self._set_status_for_content(content, ERROR)
                terminal.append_output(
                    f"\r\n[novaterm] connection error: {exc}\r\n"
                )
                connect_failed = True
                if on_failure is not None:
                    on_failure()
                return

            # Phase 2 — connected; pump bytes until the remote side closes
            # or the user disconnects. A drop here just means "session
            # ended", so we paint DISCONNECTED, never re-trigger the
            # Quick-Connect fallback dialog.
            self._set_status_for_content(content, CONNECTED)
            terminal_signal_pipe(terminal, client)
            try:
                while client.connected:
                    chunk = await client.read(4096)
                    if not chunk:
                        break
                    terminal.append_output(
                        chunk.decode("utf-8", errors="replace")
                    )
            except Exception as exc:
                logger.exception("SSH read loop ended with error")
                terminal.append_output(
                    f"\r\n[novaterm] session ended: {exc}\r\n"
                )
            finally:
                # If a *new* connection has already replaced this client
                # in ``_ssh_clients`` (e.g. the user clicked Reconnect),
                # do not paint DISCONNECTED — that would race against the
                # new runner's CONNECTED status update and leave the tab
                # showing a grey dot even though SSH is alive.
                if self._ssh_clients.get(content) is client:
                    self._set_status_for_content(content, DISCONNECTED)

        asyncio.ensure_future(_runner())

    def _set_status_for_content(self, content: TabContent, status: str) -> None:
        """Update the status dot for a tab, looking up its current index."""
        idx = self._tabs.indexOf(content)
        if idx >= 0:
            self._tabs.set_tab_status(idx, status)



    # ------------------------------------------------------------------
    # Cluster send
    # ------------------------------------------------------------------

    def _on_open_cluster(self) -> None:
        """Open the dialog letting the user pick cluster targets."""
        tabs: list[tuple[TabContent, str]] = []
        for i in range(self._tabs.count()):
            content = self._content_at(i)
            if content is not None:
                tabs.append((content, content.session_name))
        dlg = ClusterSelectDialog(tabs, self._cluster_targets, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        self._cluster_targets = dlg.selected()
        for i in range(self._tabs.count()):
            content = self._content_at(i)
            self._tabs.set_broadcast_indicator(i, content in self._cluster_targets)
        if self._cluster_targets:
            self._cluster_bar.show()
            self._cluster_bar.focus()
        else:
            self._cluster_bar.hide()

    def _toggle_cluster_off(self) -> None:
        """Disable cluster mode."""
        for content in self._cluster_targets:
            idx = self._tabs.indexOf(content)
            if idx >= 0:
                self._tabs.set_broadcast_indicator(idx, False)
        self._cluster_targets.clear()
        self._cluster_bar.hide()

    def _broadcast_chars(self, chunk: str) -> None:
        """Forward a typed chunk to all cluster targets."""
        for content in list(self._cluster_targets):
            client = self._ssh_clients.get(content)
            if client is None or not client.connected:
                continue
            asyncio.ensure_future(client.write(chunk.encode("utf-8")))

    def _broadcast_text_from_terminal(self, text: str) -> None:
        """Slot for terminal "Send to All" right-click action."""
        for client in list(self._ssh_clients.values()):
            if not client.connected:
                continue
            asyncio.ensure_future(client.write(text.encode("utf-8")))

    # ------------------------------------------------------------------
    # Command Manager send
    # ------------------------------------------------------------------

    def _resolve_session_vars(self, content: TabContent) -> tuple[str | None, str | None, str | None]:
        """Look up ``(hostname, username, session_name)`` for a tab's session."""
        if content.session_id is None:
            return None, None, content.session_name
        sess = self._store.get_session(content.session_id)
        if sess is None:
            return None, None, content.session_name
        return sess.hostname, sess.username, sess.name

    def _send_command_to_active(self, text: str, append_enter: bool) -> None:
        """Send a command to the active tab only."""
        content = self._content_at(self._tabs.currentIndex())
        if content is None:
            return
        client = self._ssh_clients.get(content)
        if client is None or not client.connected:
            return
        host, user, sess_name = self._resolve_session_vars(content)
        payload = substitute_variables(text, hostname=host, username=user, session_name=sess_name)
        if append_enter:
            payload += "\n"
        asyncio.ensure_future(client.write(payload.encode("utf-8")))

    def _send_command_to_all(self, text: str, append_enter: bool) -> None:
        """Send a command to every connected tab."""
        for content, client in list(self._ssh_clients.items()):
            if not client.connected:
                continue
            host, user, sess_name = self._resolve_session_vars(content)
            payload = substitute_variables(text, hostname=host, username=user, session_name=sess_name)
            if append_enter:
                payload += "\n"
            asyncio.ensure_future(client.write(payload.encode("utf-8")))

    def _on_fuzzy_command(self) -> None:
        """Show the Ctrl+Shift+Space command picker."""
        popup = CommandFuzzyPopup(self._commands, self)
        popup.command_chosen.connect(self._send_command_to_active)
        popup.exec()

    # ------------------------------------------------------------------
    # SFTP
    # ------------------------------------------------------------------

    def _on_open_sftp(self) -> None:
        """Open an SFTP panel for the current SSH connection.

        The dock is set to ``WA_DeleteOnClose`` and the panel's
        ``cleanup()`` runs on ``destroyed`` so closing the dock actually
        releases the 3-thread :class:`ThreadPoolExecutor` and the 500ms
        refresh ``QTimer`` (otherwise reopening SFTP repeatedly during a
        session would accumulate orphaned threads / timers forever).
        Only one SFTP dock is kept alive at a time — opening a second
        one closes the previous panel first.
        """
        content = self._content_at(self._tabs.currentIndex())
        client = self._ssh_clients.get(content) if content is not None else None
        if client is None or not client.connected:
            QMessageBox.warning(self, "SFTP", "Connect to an SSH session first.")
            return
        from core.sftp_client import SFTPClient, SFTPTransferQueue

        from .sftp_panel import SFTPPanel

        # Drop any previous SFTP dock so we never run two thread pools
        # at once.
        previous = getattr(self, "_sftp_dock", None)
        if previous is not None:
            previous.close()
            self._sftp_dock = None

        sftp = SFTPClient(client)
        queue = SFTPTransferQueue(
            sftp,
            max_workers=int(self._settings["sftp"]["max_concurrent_transfers"]),
        )
        panel = SFTPPanel(sftp, queue)
        dock = QDockWidget("SFTP", self)
        dock.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dock.setWidget(panel)
        # Belt-and-braces: when the dock is destroyed, run panel.cleanup()
        # in case ``WA_DeleteOnClose`` skips ``QWidget.closeEvent`` on the
        # child (some Qt platforms route close through hide()).
        dock.destroyed.connect(lambda _o=None, p=panel: p.cleanup())
        self._sftp_dock = dock
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    # ------------------------------------------------------------------
    # Misc actions
    # ------------------------------------------------------------------

    def _on_find(self) -> None:
        """Open the find bar in the active terminal."""
        terminal = self._current_terminal()
        if terminal is not None:
            terminal.open_find_bar()

    def _on_settings(self) -> None:
        """Open the settings dialog and reload local config on accept."""
        dlg = SettingsDialog(self, vault=self._vault)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._settings = load_settings()

    def _toggle_sidebar(self) -> None:
        """Show / hide the session manager sidebar."""
        self._sidebar.setVisible(not self._sidebar.isVisible())

    # ------------------------------------------------------------------
    # Button Bar / Command Window
    # ------------------------------------------------------------------

    def _toggle_button_bar(self, checked: bool) -> None:
        """Show / hide the bottom Button Bar (View menu toggle)."""
        self._button_bar.setVisible(checked)

    def _toggle_command_window(self, checked: bool) -> None:
        """Show / hide the Command Window (View menu toggle / Ctrl+Shift+C)."""
        self._command_window.setVisible(checked)
        if checked:
            # Restore the default 80px height if the user previously
            # collapsed the splitter past it.
            sizes = self._right_splitter.sizes()
            if len(sizes) == 2 and sizes[1] < _CMD_WINDOW_DEFAULT_HEIGHT:
                top = max(0, sum(sizes) - _CMD_WINDOW_DEFAULT_HEIGHT)
                self._right_splitter.setSizes([top, _CMD_WINDOW_DEFAULT_HEIGHT])
            self._command_window.focus_editor()

    def _on_button_bar_clicked(self, text: str) -> None:
        """Send a Button-Bar command to the active session.

        The bar's whole purpose is one-click execution, so we append a
        trailing newline to actually run the command on the remote shell.
        """
        self._send_command_to_active(text, True)

    def _on_command_window_send_active(self, text: str) -> None:
        """Forward the Command Window text to the active session."""
        self._send_command_to_active(text, False)

    def _on_command_window_send_all(self, text: str) -> None:
        """Forward the Command Window text to every connected session."""
        self._send_command_to_all(text, False)

    def _on_open_command_manager(self) -> None:
        """Open the Command Manager editor and refresh the bar on save."""
        dlg = CommandManagerEditor(self._commands, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._button_bar.refresh()

    def _on_about(self) -> None:
        """Show the about dialog."""
        QMessageBox.about(
            self,
            "About NovaTerm",
            "NovaTerm — SecureCRT-inspired SSH/Telnet client\nBuilt with Python 3.12 + PyQt6",
        )


def terminal_signal_pipe(terminal: TerminalWidget | None, client: AsyncSSHClient) -> None:
    """Wire ``terminal.text_input`` → ``client.write`` for keystroke forwarding.

    The previous slot (if any) is disconnected first so that reconnecting a
    tab via ``Reconnect`` doesn't leave dead closures attached to the same
    ``text_input`` signal — each closure pins the now-defunct
    :class:`AsyncSSHClient` it captured, and accumulating them across many
    reconnects leaks both signal slots and SSH client objects. We stash the
    most recently installed slot on the terminal widget itself so the next
    call can disconnect it without having to introspect Qt's connection list.
    """
    if terminal is None:
        return

    previous = getattr(terminal, "_novaterm_input_slot", None)
    if previous is not None:
        try:
            terminal.text_input.disconnect(previous)
        except (TypeError, RuntimeError):
            # Either the slot was never connected on this signal, or the
            # underlying C++ widget has been torn down; either way there is
            # nothing more to clean up.
            pass

    def _on_input(text: str) -> None:
        if not client.connected:
            return
        asyncio.ensure_future(client.write(text.encode("utf-8")))

    terminal.text_input.connect(_on_input)
    terminal._novaterm_input_slot = _on_input  # type: ignore[attr-defined]
