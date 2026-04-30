"""Tab manager: QTabWidget + colour stripes + split view + detach (Feature 2/8)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QSplitter,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .colors import color_for
from .terminal_widget import TerminalWidget

CONNECTED = "connected"
DISCONNECTED = "disconnected"
ERROR = "error"

_DOT_COLOURS = {
    CONNECTED: QColor("#27ae60"),
    DISCONNECTED: QColor("#7f8c8d"),
    ERROR: QColor("#e74c3c"),
}


class TabContent(QWidget):
    """Container for a single tab's content (terminal or split layout)."""

    def __init__(
        self,
        terminal: TerminalWidget,
        *,
        session_id: int | None = None,
        session_name: str = "Untitled",
        color_tag: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Wrap ``terminal`` plus metadata (session id, color, name)."""
        super().__init__(parent)
        self.session_id = session_id
        self.session_name = session_name
        self.color_tag = color_tag
        self.broadcasting = False
        self.status = DISCONNECTED

        self._splitter: QSplitter | None = None
        self._terminal = terminal

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Coloured left stripe.
        self._stripe = QWidget(self)
        self._stripe.setFixedWidth(3)
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        body_row.addWidget(self._stripe)
        body_row.addWidget(terminal, 1)
        outer.addLayout(body_row)
        self._apply_color()

    def terminal(self) -> TerminalWidget:
        """Return the (active) terminal widget for this tab."""
        return self._terminal

    def split(self, orientation: Qt.Orientation, second_terminal: TerminalWidget) -> None:
        """Add ``second_terminal`` next-to/below the current one."""
        if self._splitter is None:
            current = self._terminal
            self._splitter = QSplitter(orientation, self)
            self._splitter.addWidget(current)
            self._splitter.addWidget(second_terminal)
            # Replace the row layout's terminal with the splitter.
            old_layout = self.layout()
            assert old_layout is not None
            QWidget().setLayout(old_layout)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.addWidget(self._stripe)
            row.addWidget(self._splitter, 1)
            outer.addLayout(row)
        else:
            self._splitter.setOrientation(orientation)
            self._splitter.addWidget(second_terminal)

    def set_color_tag(self, tag: str | None) -> None:
        """Update the coloured left stripe."""
        self.color_tag = tag
        self._apply_color()

    def _apply_color(self) -> None:
        """Re-paint the left stripe and very-subtle background tint."""
        color = color_for(self.color_tag)
        if color is None:
            self._stripe.setStyleSheet("background: transparent;")
            self.setStyleSheet("")
            return
        self._stripe.setStyleSheet(f"background:{color.name()};")
        self.setStyleSheet(
            f"TabContent {{ background-color: rgba({color.red()},{color.green()},{color.blue()},20); }}"
        )


def _make_dot(color: QColor, broadcast: bool = False) -> QIcon:
    """Build a small coloured dot icon — optionally with a broadcast ring."""
    pix = QPixmap(14, 14)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 10, 10)
    if broadcast:
        painter.setBrush(Qt.GlobalColor.transparent)
        painter.setPen(QColor("#3498db"))
        painter.drawEllipse(0, 0, 13, 13)
    painter.end()
    return QIcon(pix)


class NovaTabWidget(QTabWidget):
    """``QTabWidget`` subclass with NovaTerm-specific tab decoration & menu."""

    new_tab_requested = pyqtSignal()
    detach_requested = pyqtSignal(int)
    close_requested = pyqtSignal(int)
    reconnect_requested = pyqtSignal(int)
    disconnect_requested = pyqtSignal(int)
    clone_requested = pyqtSignal(int)
    rename_requested = pyqtSignal(int)
    split_horizontal_requested = pyqtSignal(int)
    split_vertical_requested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialise tabs with movable bar, custom context menu and shortcuts."""
        super().__init__(parent)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setDocumentMode(True)
        self.tabCloseRequested.connect(self.close_requested.emit)

        bar = self.tabBar()
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        bar.customContextMenuRequested.connect(self._show_tab_menu)

    def add_tab(self, content: TabContent) -> int:
        """Add ``content`` as a new tab and return its index."""
        idx = self.addTab(content, content.session_name)
        self.refresh_tab(idx)
        return idx

    def refresh_tab(self, index: int) -> None:
        """Re-apply the indicator dot + colour tinting to tab ``index``."""
        widget = self.widget(index)
        if not isinstance(widget, TabContent):
            return
        color = _DOT_COLOURS.get(widget.status, _DOT_COLOURS[DISCONNECTED])
        # Tint the dot with the session colour when connected.
        if widget.status == CONNECTED and widget.color_tag is not None:
            tinted = color_for(widget.color_tag) or color
            color = tinted
        self.setTabIcon(index, _make_dot(color, broadcast=widget.broadcasting))
        self.setTabText(index, widget.session_name)

    def set_tab_status(self, index: int, status: str) -> None:
        """Update the connection-status dot on a tab."""
        widget = self.widget(index)
        if isinstance(widget, TabContent):
            widget.status = status
            self.refresh_tab(index)

    def set_broadcast_indicator(self, index: int, broadcasting: bool) -> None:
        """Add/remove the small broadcast ring on a tab icon."""
        widget = self.widget(index)
        if isinstance(widget, TabContent):
            widget.broadcasting = broadcasting
            self.refresh_tab(index)

    def _show_tab_menu(self, point) -> None:
        """Right-click context menu for tabs."""
        bar: QTabBar = self.tabBar()
        idx = bar.tabAt(point)
        if idx < 0:
            return
        menu = QMenu(self)
        for label, sig in [
            ("Rename", self.rename_requested),
            ("Disconnect", self.disconnect_requested),
            ("Reconnect", self.reconnect_requested),
            ("Clone Tab", self.clone_requested),
            ("Move to New Window", self.detach_requested),
            ("Split Horizontally", self.split_horizontal_requested),
            ("Split Vertically", self.split_vertical_requested),
            ("Close", self.close_requested),
        ]:
            act = QAction(label, self)
            act.triggered.connect(lambda _checked=False, i=idx, s=sig: s.emit(i))
            menu.addAction(act)
        menu.addSeparator()
        close_all = QAction("Close All", self)
        close_all.triggered.connect(self._close_all)
        menu.addAction(close_all)
        close_others = QAction("Close Others", self)
        close_others.triggered.connect(lambda: self._close_others(idx))
        menu.addAction(close_others)
        menu.exec(bar.mapToGlobal(point))

    def _close_all(self) -> None:
        """Emit close-requested for each tab from right to left."""
        for i in reversed(range(self.count())):
            self.close_requested.emit(i)

    def _close_others(self, keep: int) -> None:
        """Close all tabs except ``keep``."""
        for i in reversed(range(self.count())):
            if i != keep:
                self.close_requested.emit(i)
