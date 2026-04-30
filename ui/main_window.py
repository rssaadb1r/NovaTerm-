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

from .cluster_bar import ClusterInputBar, ClusterSelectDialog
from .command_manager import CommandFuzzyPopup, CommandManagerPanel
from .quick_connect import QuickConnectDialog
from .session_dialog import SessionDialog
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
        self._splitter.addWidget(self._sidebar)

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
        self._splitter.addWidget(self._tabs)
        self._splitter.setSizes([250, 1030])

        outer.addWidget(self._splitter, 1)

        # Cluster bar (initially hidden).
        self._cluster_bar = ClusterInputBar(central)
        self._cluster_bar.char_typed.connect(self._broadcast_chars)
        self._cluster_bar.closed.connect(self._toggle_cluster_off)
        self._cluster_bar.hide()
        outer.addWidget(self._cluster_bar)

        self.setCentralWidget(central)

        # -- command manager dock ----------------------------------------
        self._commands_panel = CommandManagerPanel(commands, self)
        self._commands_panel.send_to_active.connect(self._send_command_to_active)
        self._commands_panel.send_to_all.connect(self._send_command_to_all)
        dock = QDockWidget("Commands", self)
        dock.setWidget(self._commands_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

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
        file_menu.addAction(self._make_action("Import Sessions…", self._on_import_sessions))
        file_menu.addAction(self._make_action("Export Sessions…", self._on_export_sessions))
        file_menu.addSeparator()
        file_menu.addAction(self._make_action("Quit", self.close, "Ctrl+Shift+Q"))

        edit_menu = mb.addMenu("&Edit")
        edit_menu.addAction(self._make_action("Find in Terminal…", self._on_find, "Ctrl+F"))
        edit_menu.addAction(self._make_action("Settings…", self._on_settings, "Ctrl+,"))

        view_menu = mb.addMenu("&View")
        view_menu.addAction(
            self._make_action("Toggle Session Manager", self._toggle_sidebar)
        )
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
        """Disconnect & remove the tab at ``index``."""
        if index < 0 or index >= self._tabs.count():
            return
        content = self._content_at(index)
        if content is not None:
            self._cluster_targets.discard(content)
            client = self._ssh_clients.pop(content, None)
            if client is not None:
                asyncio.ensure_future(client.disconnect())
        self._tabs.removeTab(index)

    def _cycle_tab(self, delta: int) -> None:
        """Cycle to the next/prev tab."""
        n = self._tabs.count()
        if n == 0:
            return
        self._tabs.setCurrentIndex((self._tabs.currentIndex() + delta) % n)

    def _disconnect_tab(self, index: int) -> None:
        """Disconnect the SSH backend for tab ``index`` (keeps the tab open)."""
        content = self._content_at(index)
        if content is not None:
            client = self._ssh_clients.get(content)
            if client is not None:
                asyncio.ensure_future(client.disconnect())
        self._tabs.set_tab_status(index, DISCONNECTED)

    def _reconnect_tab(self, index: int) -> None:
        """Reconnect the tab using the saved session id, if any."""
        widget = self._tabs.widget(index)
        if isinstance(widget, TabContent) and widget.session_id is not None:
            self._open_saved_session(widget.session_id)

    def _detach_tab(self, index: int) -> None:
        """Move the tab into a free-floating window.

        The :class:`TabContent` widget retains its identity as it moves out of
        the tab bar, so its entry in ``_ssh_clients`` and ``_cluster_targets``
        remains valid — but it can no longer be addressed via the cluster
        bar / right-click menu, so we drop it from those collections to
        avoid 'phantom' broadcasts.
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
        content = self._new_terminal_tab(
            session_id=None,
            name=f"{data.username}@{data.hostname}" if data.username else data.hostname,
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
            )

    def _on_new_session(self) -> None:
        """Open the New Session dialog and persist on Ok."""
        dlg = SessionDialog(self._store, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        fields = dlg.fields()
        self._store.create_session(**fields)
        self._sidebar.refresh()

    def _on_edit_session(self, sid: int) -> None:
        """Edit an existing session row."""
        sess = self._store.get_session(sid)
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
        added = self._store.import_sessions_json(open(path).read())
        QMessageBox.information(self, "Import", f"Imported {added} session(s)")
        self._sidebar.refresh()

    def _on_export_sessions(self) -> None:
        """Export sessions to a JSON file."""
        path, _ = QFileDialog.getSaveFileName(self, "Export sessions", "", "JSON (*.json)")
        if not path:
            return
        with open(path, "w") as fh:
            fh.write(self._store.export_sessions_json())
        QMessageBox.information(self, "Export", f"Saved to {path}")

    def _open_saved_session(self, sid: int) -> None:
        """Activate a saved session (opens a tab + connects)."""
        sess = self._store.get_session(sid)
        if sess is None:
            return
        content = self._new_terminal_tab(
            session_id=sess.id, name=sess.name, color_tag=sess.color_tag
        )
        if sess.protocol == "ssh":
            password: str | None = None
            if sess.auth_type == "password" and sess.encrypted_credential and self._vault.is_unlocked():
                try:
                    password = self._vault.decrypt(sess.encrypted_credential)
                except VaultAuthError:
                    password = None
            self._connect_ssh(
                content,
                hostname=sess.hostname,
                port=sess.port,
                username=sess.username or "",
                password=password,
            )
        self._store.touch_last_connected(sid)

    def _connect_ssh(
        self,
        content: TabContent,
        *,
        hostname: str,
        port: int,
        username: str | None,
        password: str | None,
    ) -> None:
        """Spawn the asyncio task that brings up the SSH connection."""
        config = SSHConnectConfig(
            target=HopConfig(
                hostname=hostname,
                port=port,
                username=username or "",
                password=password,
            ),
            jumps=[],
            keepalive_interval=int(self._settings["advanced"]["ssh_keepalive_seconds"]),
            connect_timeout=int(self._settings["advanced"]["connect_timeout_seconds"]),
            auto_reconnect_retries=int(self._settings["advanced"]["auto_reconnect_retries"]),
        )
        client = AsyncSSHClient(config)
        self._ssh_clients[content] = client

        async def _runner() -> None:
            terminal = content.terminal()
            try:
                # ``progress`` is invoked on the main loop (see ssh_client.py).
                await client.connect(progress=lambda m: self.statusBar().showMessage(m))
                self._set_status_for_content(content, CONNECTED)
                terminal_signal_pipe(terminal, client)
                while client.connected:
                    chunk = await client.read(4096)
                    if not chunk:
                        break
                    terminal.append_output(chunk.decode("utf-8", errors="replace"))
            except Exception as exc:
                logger.exception("SSH session failed")
                self._set_status_for_content(content, ERROR)
                terminal.append_output(f"\r\n[novaterm] connection error: {exc}\r\n")
            finally:
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
        """Open an SFTP panel for the current SSH connection."""
        content = self._content_at(self._tabs.currentIndex())
        client = self._ssh_clients.get(content) if content is not None else None
        if client is None or not client.connected:
            QMessageBox.warning(self, "SFTP", "Connect to an SSH session first.")
            return
        from core.sftp_client import SFTPClient, SFTPTransferQueue

        from .sftp_panel import SFTPPanel

        sftp = SFTPClient(client)
        queue = SFTPTransferQueue(
            sftp,
            max_workers=int(self._settings["sftp"]["max_concurrent_transfers"]),
        )
        panel = SFTPPanel(sftp, queue)
        dock = QDockWidget("SFTP", self)
        dock.setWidget(panel)
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
        dlg = SettingsDialog(self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._settings = load_settings()

    def _toggle_sidebar(self) -> None:
        """Show / hide the session manager sidebar."""
        self._sidebar.setVisible(not self._sidebar.isVisible())

    def _on_about(self) -> None:
        """Show the about dialog."""
        QMessageBox.about(
            self,
            "About NovaTerm",
            "NovaTerm — SecureCRT-inspired SSH/Telnet client\nBuilt with Python 3.12 + PyQt6",
        )


def terminal_signal_pipe(terminal: TerminalWidget | None, client: AsyncSSHClient) -> None:
    """Wire ``terminal.text_input`` → ``client.write`` for keystroke forwarding."""
    if terminal is None:
        return

    def _on_input(text: str) -> None:
        if not client.connected:
            return
        asyncio.ensure_future(client.write(text.encode("utf-8")))

    terminal.text_input.connect(_on_input)
