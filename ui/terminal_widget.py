"""Terminal display widget + inline find/highlight bar (Feature 9).

NovaTerm's terminal display is a ``QPlainTextEdit`` with an inline
ANSI-stripping layer applied to remote output. The original spec called
for the C++ ``QTermWidget``, but no usable PyQt6 binding for that
library is available on PyPI for Linux Fedora 44 (see CLAUDE.md
sections 1 and 10 for the design note). Until a native backend can be
slotted in we strip CSI / OSC / single-character escape sequences from
the byte stream so raw SSH output renders as readable text instead of
garbled escape codes; full ANSI emulation (colour, cursor positioning,
etc.) is a known follow-up.

The public API exposed by :class:`TerminalWidget` is unchanged from the
spec so a future native backend can be slotted in without touching
callers.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeyEvent,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# Match any ANSI escape sequence we know how to throw away:
#   * CSI sequences:   ESC [ <params> <final-byte 0x40-0x7E>
#   * OSC sequences:   ESC ]  ... BEL  or  ESC ] ... ESC \
#   * Single-char esc: ESC <char in 0x40..0x5F>
# Plus the DEL byte (0x7F) and the bare BEL (0x07) which some shells
# emit on tab-completion failures.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-_]"  # 2-byte ESC + final
    r"|[\x07\x7f]"
)


def _strip_ansi(text: str) -> str:
    """Return ``text`` with ANSI escape sequences removed."""
    return _ANSI_RE.sub("", text)

# ---------------------------------------------------------------------------
# Find bar
# ---------------------------------------------------------------------------


class FindBar(QWidget):
    """Slide-in find bar overlaid on top of the terminal area."""

    closed = pyqtSignal()
    next_match = pyqtSignal()
    prev_match = pyqtSignal()
    query_changed = pyqtSignal(str, bool, bool)  # query, case_sensitive, regex

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the bar widgets."""
        super().__init__(parent)
        self.setObjectName("FindBar")
        self.setAutoFillBackground(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self.query = QLineEdit(self)
        self.query.setPlaceholderText("\U0001F50D  Find …")
        self.query.returnPressed.connect(self.next_match.emit)
        self.query.textChanged.connect(self._emit_query)
        layout.addWidget(self.query, 1)

        self.prev_btn = QPushButton("◀", self)
        self.prev_btn.setFixedWidth(28)
        self.prev_btn.clicked.connect(self.prev_match.emit)
        layout.addWidget(self.prev_btn)

        self.next_btn = QPushButton("▶", self)
        self.next_btn.setFixedWidth(28)
        self.next_btn.clicked.connect(self.next_match.emit)
        layout.addWidget(self.next_btn)

        self.case_cb = QCheckBox("Match case", self)
        self.case_cb.toggled.connect(self._emit_query)
        layout.addWidget(self.case_cb)

        self.regex_cb = QCheckBox("Regex", self)
        self.regex_cb.toggled.connect(self._emit_query)
        layout.addWidget(self.regex_cb)

        self.counter = QLabel("0 / 0 matches", self)
        layout.addWidget(self.counter)

        close_btn = QPushButton("✕", self)
        close_btn.setFixedWidth(28)
        close_btn.clicked.connect(self.closed.emit)
        layout.addWidget(close_btn)

        self.hide()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt API
        """Handle Escape and Shift+Enter inside the bar."""
        if event.key() == Qt.Key.Key_Escape:
            self.closed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.prev_match.emit()
            return
        super().keyPressEvent(event)

    def open(self) -> None:
        """Show the bar and focus the query field."""
        self.show()
        self.query.setFocus()
        self.query.selectAll()

    def set_match_count(self, current: int, total: int) -> None:
        """Update the ``N / M matches`` label."""
        self.counter.setText(f"{current} / {total} matches")

    def set_invalid_regex(self, is_invalid: bool) -> None:
        """Visually mark the query field when regex parsing fails."""
        self.query.setStyleSheet(
            "QLineEdit { border: 1px solid #e74c3c; }" if is_invalid else ""
        )

    def _emit_query(self) -> None:
        """Forward ``query_changed`` whenever the user edits the input."""
        self.query_changed.emit(
            self.query.text(),
            self.case_cb.isChecked(),
            self.regex_cb.isChecked(),
        )


# ---------------------------------------------------------------------------
# Terminal widget
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Match:
    """A single find-bar match (start/end offsets within the document)."""

    start: int
    end: int


class ConfirmPasteDialog(QDialog):
    """Modal preview dialog for multi-line right-click pastes.

    The user can edit the staged text before clicking *OK*; *Cancel*
    aborts and nothing is sent to the remote session.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        """Build the dialog seeded with ``text``."""
        super().__init__(parent)
        self.setWindowTitle("Confirm Paste")
        self.setModal(True)
        self.resize(540, 320)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Review the text before pasting:", self))

        self._editor = QPlainTextEdit(self)
        self._editor.setPlainText(text)
        # Monospaced font so columns line up like in the terminal.
        font = self._editor.font()
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("Monospace")
        self._editor.setFont(font)
        layout.addWidget(self._editor, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def text(self) -> str:
        """Return the (possibly edited) text the user confirmed."""
        return self._editor.toPlainText()


class TerminalWidget(QWidget):
    """Public terminal widget used everywhere else in the app.

    Emits :pyattr:`text_input` whenever the user types — the SSH/Telnet
    backend listens to this signal to forward keystrokes upstream.
    """

    text_input = pyqtSignal(str)
    bell = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the terminal display, find bar and right-click menu."""
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._find_bar = FindBar(self)
        layout.addWidget(self._find_bar)

        self._display = QPlainTextEdit(self)
        self._display.setReadOnly(False)
        self._display.setMaximumBlockCount(10_000)  # default scrollback
        font = QFont("Monospace", 11)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._display.setFont(font)
        self._display.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._display.customContextMenuRequested.connect(self._show_context_menu)
        self._display.installEventFilter(self)
        layout.addWidget(self._display, 1)

        self._matches: list[_Match] = []
        self._current_match = -1
        self._find_bar.query_changed.connect(self._on_find_query)
        self._find_bar.next_match.connect(lambda: self._advance_match(+1))
        self._find_bar.prev_match.connect(lambda: self._advance_match(-1))
        self._find_bar.closed.connect(self.close_find_bar)

        # Hooks injected by the controller.
        self._broadcast_callback: Callable[[str], None] | None = None
        self._on_clear_scrollback: Callable[[], None] | None = None

        self.apply_theme(
            background="#1e1e1e",
            foreground="#d4d4d4",
            cursor="#ffffff",
            selection="#264f78",
        )

    # -- public API --------------------------------------------------------

    def append_output(self, text: str) -> None:
        """Append output received from the remote side.

        Raw SSH output frequently contains ANSI escape sequences (e.g.
        ``\x1b[31m`` colour codes, ``\x1b]0;title\x07`` OSC titles,
        cursor moves, etc.). The current backend has no terminal
        emulator wired up, so we strip those sequences here — otherwise
        they show up as visible gibberish in the QPlainTextEdit.
        """
        clean = _strip_ansi(text)
        if not clean:
            return
        cursor = self._display.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(clean)
        self._display.setTextCursor(cursor)
        self._display.ensureCursorVisible()
        if self._matches:
            self._refresh_highlights()

    def set_scrollback(self, lines: int) -> None:
        """Change the maximum number of retained lines."""
        self._display.setMaximumBlockCount(max(100, lines))

    def set_font(self, family: str, size: int) -> None:
        """Set the terminal font family and size."""
        font = QFont(family, size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._display.setFont(font)

    def apply_theme(
        self,
        *,
        background: str,
        foreground: str,
        cursor: str,
        selection: str,
    ) -> None:
        """Apply background / foreground / cursor / selection colours."""
        self._display.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background-color: {background};
                color: {foreground};
                selection-background-color: {selection};
                border: none;
            }}
            """
        )
        # cursor is set indirectly via stylesheet limitations on QPlainTextEdit
        # — kept here for API parity even when unused.
        _ = cursor

    def open_find_bar(self) -> None:
        """Show the find bar (Ctrl+F)."""
        self._find_bar.open()

    def close_find_bar(self) -> None:
        """Hide the find bar and clear all highlights."""
        self._find_bar.hide()
        self._matches.clear()
        self._current_match = -1
        self._clear_highlights()

    def set_broadcast_callback(self, fn: Callable[[str], None] | None) -> None:
        """Set the function called by ``Send to All Sessions``."""
        self._broadcast_callback = fn

    def set_clear_scrollback_callback(self, fn: Callable[[], None] | None) -> None:
        """Override what *Clear Scrollback Buffer* does (defaults to local)."""
        self._on_clear_scrollback = fn

    # -- right-click menu -------------------------------------------------

    def _show_context_menu(self, point) -> None:
        """Right-click handler.

        With a selection active the right-click *pastes* the selected
        text into the remote session: a single-line selection is sent
        immediately, a multi-line selection first opens a *Confirm
        Paste* dialog so the user can review/edit it before sending.
        With no selection we fall back to the standard Copy / Paste / …
        menu so those actions are still reachable.
        """
        cursor = self._display.textCursor()
        # ``QPlainTextEdit`` uses U+2028 as the line separator inside a
        # selection; normalise that to ``\n`` so callers don't have to.
        selected = (
            cursor.selectedText().replace("\u2028", "\n")
            if cursor.hasSelection()
            else ""
        )

        if selected:
            if "\n" in selected:
                dlg = ConfirmPasteDialog(selected, self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    final = dlg.text()
                    if final:
                        self.text_input.emit(final)
            else:
                self.text_input.emit(selected)
            return

        menu = QMenu(self)
        menu.addAction(self._make_action("Copy", self._display.copy))
        menu.addAction(self._make_action("Paste", self._display.paste))
        menu.addAction(
            self._make_action(
                "Copy & Paste",
                lambda: (self._display.copy(), self._display.paste()),
            )
        )
        menu.addAction(self._make_action("Paste Selection", self._paste_selection))
        menu.addSeparator()
        menu.addAction(self._make_action("Send to All Sessions", self._broadcast_selection))
        menu.addSeparator()
        menu.addAction(self._make_action("Clear Scrollback Buffer", self._clear_scrollback))
        menu.addAction(self._make_action("Find …", self.open_find_bar))
        menu.exec(self._display.mapToGlobal(point))

    def _make_action(self, label: str, slot: Callable[[], None]) -> QAction:
        """Build a QAction wired to ``slot``."""
        action = QAction(label, self)
        action.triggered.connect(slot)
        return action

    def _paste_selection(self) -> None:
        """Paste the current selection (X11 primary selection equivalent)."""
        cursor = self._display.textCursor()
        text = cursor.selectedText()
        if text:
            self.text_input.emit(text)

    def _broadcast_selection(self) -> None:
        """Broadcast the current selection to all other sessions."""
        cursor = self._display.textCursor()
        text = cursor.selectedText()
        if text and self._broadcast_callback:
            self._broadcast_callback(text)

    def _clear_scrollback(self) -> None:
        """Clear the scrollback buffer (overridable per-session)."""
        if self._on_clear_scrollback is not None:
            self._on_clear_scrollback()
        self._display.clear()

    # -- find / highlight --------------------------------------------------

    def _on_find_query(self, query: str, case_sensitive: bool, regex: bool) -> None:
        """Recompute matches whenever the query / flags change."""
        self._matches.clear()
        self._current_match = -1
        self._clear_highlights()

        if not query:
            self._find_bar.set_match_count(0, 0)
            self._find_bar.set_invalid_regex(False)
            return

        text = self._display.toPlainText()
        try:
            if regex:
                pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
                self._find_bar.set_invalid_regex(False)
            else:
                pattern = re.compile(
                    re.escape(query), 0 if case_sensitive else re.IGNORECASE
                )
                self._find_bar.set_invalid_regex(False)
        except re.error:
            self._find_bar.set_invalid_regex(True)
            self._find_bar.set_match_count(0, 0)
            return

        for m in pattern.finditer(text):
            self._matches.append(_Match(start=m.start(), end=m.end()))

        if self._matches:
            self._current_match = 0
        self._refresh_highlights()

    def _refresh_highlights(self) -> None:
        """Re-apply the highlight overlays."""
        extra: list[QPlainTextEdit.ExtraSelection] = []
        match_fmt = QTextCharFormat()
        match_fmt.setBackground(QColor("#f1c40f"))
        current_fmt = QTextCharFormat()
        current_fmt.setBackground(QColor("#e67e22"))

        for i, match in enumerate(self._matches):
            cursor = QTextCursor(self._display.document())
            cursor.setPosition(match.start)
            cursor.setPosition(match.end, QTextCursor.MoveMode.KeepAnchor)
            sel = QPlainTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = current_fmt if i == self._current_match else match_fmt
            extra.append(sel)

        self._display.setExtraSelections(extra)
        self._find_bar.set_match_count(
            self._current_match + 1 if self._matches else 0, len(self._matches)
        )

        if self._current_match >= 0:
            cursor = QTextCursor(self._display.document())
            cursor.setPosition(self._matches[self._current_match].start)
            self._display.setTextCursor(cursor)
            self._display.ensureCursorVisible()

    def _clear_highlights(self) -> None:
        """Drop all extra selections."""
        self._display.setExtraSelections([])

    def _advance_match(self, delta: int) -> None:
        """Move the *current match* pointer by ``delta`` (wrap-around)."""
        if not self._matches:
            return
        self._current_match = (self._current_match + delta) % len(self._matches)
        self._refresh_highlights()

    # -- key forwarding ----------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 — Qt API
        """Forward typing on the QPlainTextEdit upstream as ``text_input``.

        We *consume* any printable key event after emitting ``text_input`` so
        the underlying ``QPlainTextEdit`` does not also insert the character
        locally — otherwise every keystroke would appear twice once the
        remote echo arrives via :meth:`append_output`. Non-printable keys
        (arrow keys, modifiers, etc.) fall through to default handling so
        the user can still navigate the scrollback with the keyboard.
        """
        if obj is self._display and event.type() == event.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            if event.key() == Qt.Key.Key_F and (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self.open_find_bar()
                return True
            text = event.text()
            if text:
                self.text_input.emit(text)
                return True
        return super().eventFilter(obj, event)
