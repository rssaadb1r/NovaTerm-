"""Two-pane SFTP file manager + transfer queue panel (Feature 10)."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFileSystemModel
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from core.sftp_client import SFTPClient, SFTPTransferQueue


class _LocalPane(QWidget):
    """Left pane: local Fedora filesystem browser."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build a tree-view backed by a :class:`QFileSystemModel`."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        nav_row = QHBoxLayout()
        self.path_input = QLineEdit(str(Path.home()), self)
        nav_row.addWidget(self.path_input, 1)
        layout.addLayout(nav_row)

        self.model = QFileSystemModel(self)
        self.model.setRootPath(str(Path.home()))
        self.view = QTreeView(self)
        self.view.setModel(self.model)
        self.view.setRootIndex(self.model.index(str(Path.home())))
        layout.addWidget(self.view, 1)

        self.path_input.returnPressed.connect(self._navigate)

    def _navigate(self) -> None:
        """Navigate to the path in the input box."""
        path = self.path_input.text()
        if Path(path).exists():
            self.view.setRootIndex(self.model.index(path))


class _RemotePane(QWidget):
    """Right pane: remote SFTP filesystem browser."""

    upload_requested = pyqtSignal(str)  # remote target dir
    refresh_requested = pyqtSignal()

    def __init__(self, sftp: SFTPClient, parent: QWidget | None = None) -> None:
        """Build the list-based remote browser."""
        super().__init__(parent)
        self._sftp = sftp
        self._cwd = "."

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        nav_row = QHBoxLayout()
        self.path_input = QLineEdit(self._cwd, self)
        nav_row.addWidget(self.path_input, 1)
        up_btn = QPushButton("⬆", self)
        up_btn.setFixedWidth(28)
        up_btn.clicked.connect(self._go_up)
        nav_row.addWidget(up_btn)
        refresh_btn = QPushButton("⟳", self)
        refresh_btn.setFixedWidth(28)
        refresh_btn.clicked.connect(self.refresh)
        nav_row.addWidget(refresh_btn)
        layout.addLayout(nav_row)

        self.list_widget = QListWidget(self)
        self.list_widget.itemDoubleClicked.connect(self._open)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.list_widget, 1)

        self.path_input.returnPressed.connect(self._navigate)
        self.refresh()

    def refresh(self) -> None:
        """Re-list the current remote directory."""
        self.list_widget.clear()
        try:
            entries = self._sftp.listdir(self._cwd)
        except Exception as exc:
            self.list_widget.addItem(f"<error: {exc}>")
            return
        for entry in entries:
            label = ("📁 " if entry.is_dir else "📄 ") + entry.name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.list_widget.addItem(item)
        self.path_input.setText(self._cwd)

    def _open(self, item: QListWidgetItem) -> None:
        """Enter a directory; ignore files for now."""
        entry = item.data(Qt.ItemDataRole.UserRole)
        if entry is None or not entry.is_dir:
            return
        self._cwd = entry.path
        self.refresh()

    def _go_up(self) -> None:
        """Navigate one directory up."""
        if self._cwd in {".", "/"}:
            return
        # If the current path is a single relative component (e.g. ``documents``
        # reached from the initial ``"."`` listing), splitting gives ``[""]``
        # and we'd jump to the filesystem root. Fall back to ``"."`` so we
        # land back in the SFTP session's starting directory.
        if self._cwd.startswith("/"):
            self._cwd = "/".join(self._cwd.rstrip("/").split("/")[:-1]) or "/"
        else:
            self._cwd = "/".join(self._cwd.rstrip("/").split("/")[:-1]) or "."
        self.refresh()

    def _navigate(self) -> None:
        """Jump to the path in the navigation bar."""
        self._cwd = self.path_input.text() or "."
        self.refresh()

    def _menu(self, point) -> None:
        """Right-click menu on the remote pane."""
        menu = QMenu(self)
        upload = QAction("Upload here …", self)
        upload.triggered.connect(lambda: self.upload_requested.emit(self._cwd))
        menu.addAction(upload)
        refresh = QAction("Refresh", self)
        refresh.triggered.connect(self.refresh)
        menu.addAction(refresh)
        menu.exec(self.list_widget.mapToGlobal(point))


class TransferQueuePanel(QWidget):
    """Mini-table showing in-flight transfers (Feature 10 progress widget)."""

    def __init__(self, queue: SFTPTransferQueue, parent: QWidget | None = None) -> None:
        """Bind to a transfer queue and start a refresh timer."""
        super().__init__(parent)
        self._queue = queue

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(QLabel("Transfers:"))
        self._list = QListWidget(self)
        layout.addWidget(self._list, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        """Re-render the transfer list."""
        self._list.clear()
        for tid, prog in sorted(self._queue.jobs.items()):
            status = "DONE" if prog.finished and not prog.error else (
                f"ERR ({prog.error})" if prog.error else f"{prog.percent:5.1f}%"
            )
            speed_kb = prog.speed_bps / 1024.0
            label = f"#{tid} {prog.direction:8s} {status:>10s}  {speed_kb:7.1f} KB/s  {prog.src} → {prog.dst}"
            self._list.addItem(label)


class SFTPPanel(QWidget):
    """Composed SFTP file manager — local pane + remote pane + transfers."""

    def __init__(self, sftp: SFTPClient, queue: SFTPTransferQueue, parent: QWidget | None = None) -> None:
        """Build the composite panel."""
        super().__init__(parent)
        self._sftp = sftp
        self._queue = queue

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.local = _LocalPane(self)
        self.remote = _RemotePane(sftp, self)
        splitter.addWidget(self.local)
        splitter.addWidget(self.remote)
        splitter.setSizes([300, 300])
        layout.addWidget(splitter, 3)

        self.transfers = TransferQueuePanel(queue, self)
        layout.addWidget(self.transfers, 1)
