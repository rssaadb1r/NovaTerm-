"""Terminal display widget + inline find/highlight bar (Feature 9).

NovaTerm's terminal display is a ``QPlainTextEdit`` driven by a
:mod:`pyte` :class:`~pyte.HistoryScreen` emulator (CLAUDE.md §1, §10).
Remote output is fed through :class:`pyte.Stream`, which interprets
CSI / OSC / SGR / cursor-positioning escape sequences against a fixed
character grid; the grid is then re-rendered into the QPlainTextEdit
with :class:`QTextCharFormat` runs carrying ANSI fore/back colours,
bold, italic, underline and reverse-video attributes. Lines that scroll
off the top of the screen accumulate in :attr:`HistoryScreen.history`
and are prefixed to every redraw so the user keeps a configurable
scrollback buffer.

The original spec called for the C++ ``QTermWidget`` binding, but no
usable PyQt6 wheel is published for Linux Fedora 44. ``QPlainTextEdit``
is used as the rendering surface so the public API exposed by
:class:`TerminalWidget` (``append_output``, ``apply_theme``,
``set_scrollback``, ``set_font``) stays stable in the event we ever
slot in a native backend.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import pyte

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
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

# ---------------------------------------------------------------------------
# Pyte palette
# ---------------------------------------------------------------------------
#
# pyte stores cell foreground / background as a string — either the name of
# one of the 16 ANSI colours ("red", "brightblue", "default", …) or a 6-digit
# hex code ("ff8800") for 256-colour / truecolour SGRs. The map below is the
# VS Code dark palette — close enough to the SecureCRT defaults that the
# common shell prompts read correctly.
_PYTE_PALETTE: dict[str, str] = {
    "black":         "#000000",
    "red":           "#cd3131",
    "green":         "#0dbc79",
    "brown":         "#e5e510",   # pyte calls yellow "brown"
    "blue":          "#2472c8",
    "magenta":       "#bc3fbc",
    "cyan":          "#11a8cd",
    "white":         "#e5e5e5",
    "brightblack":   "#666666",
    "brightred":     "#f14c4c",
    "brightgreen":   "#23d18b",
    "brightbrown":   "#f5f543",
    "brightblue":    "#3b8eea",
    "brightmagenta": "#d670d6",
    "brightcyan":    "#29b8db",
    "brightwhite":   "#ffffff",
}


def _pyte_qcolor(name: str | None, fallback: QColor) -> QColor:
    """Translate a pyte colour token into a :class:`QColor`.

    pyte uses the literal string ``"default"`` for SGR 0 (reset to the
    user's chosen foreground / background); we map that back to the
    theme defaults. Anything else is either a palette name or a 6-digit
    hex code emitted by the 256-colour / truecolour SGR handler.
    """
    if not name or name == "default":
        return fallback
    if name in _PYTE_PALETTE:
        return QColor(_PYTE_PALETTE[name])
    if len(name) == 6 and all(c in "0123456789abcdef" for c in name):
        return QColor("#" + name)
    return fallback


class _PyteEmulator:
    """Thin :class:`pyte.HistoryScreen` + :class:`pyte.Stream` wrapper.

    SSH and Telnet servers stream raw VT-style byte sequences; pyte
    interprets them against a fixed-size character grid and exposes the
    resulting cells via :attr:`pyte.Screen.buffer`. ``LNM`` mode is set
    so a bare ``\\n`` is treated as ``\\r\\n`` — some servers (and our
    own injected log-banner output) emit only line-feeds, and without
    LNM the cursor would walk diagonally instead of returning to col 0.
    """

    def __init__(self, cols: int, rows: int, history: int) -> None:
        """Allocate a screen of ``cols x rows`` plus ``history`` scrollback."""
        self.screen = pyte.HistoryScreen(
            cols, rows, history=history, ratio=0.5
        )
        self.screen.set_mode(pyte.modes.LNM)
        self.stream = pyte.Stream(self.screen)

    def feed(self, text: str) -> None:
        """Process a chunk of remote output through the emulator."""
        try:
            self.stream.feed(text)
        except Exception:  # pragma: no cover — defensive, pyte rarely raises
            logger.exception("pyte stream rejected output chunk")

    def resize(self, cols: int, rows: int) -> None:
        """Resize the underlying grid (rows / cols swap, per pyte API)."""
        if cols <= 0 or rows <= 0:
            return
        if cols == self.screen.columns and rows == self.screen.lines:
            return
        try:
            self.screen.resize(rows, cols)
        except Exception:  # pragma: no cover
            logger.exception("pyte resize failed (%dx%d)", cols, rows)

    def reset(self) -> None:
        """Clear screen + scrollback."""
        self.screen.reset()
        self.screen.set_mode(pyte.modes.LNM)


# ---------------------------------------------------------------------------
# Special-key VT escape table
# ---------------------------------------------------------------------------
#
# ``QKeyEvent.text()`` is the empty string for arrow keys, Home / End,
# Delete, Insert, Page Up / Down, and F1..F12 — Qt only fills ``text``
# in for keys that produce a printable character. A real terminal
# expects the application to send the matching VT (xterm-style) escape
# sequence; without this table those keys would never reach the remote
# shell, making line editing, history scroll, and TUIs unusable.
#
# Sequences below match xterm-256color (the TERM string we'll advertise
# at PTY-allocation time) so common shells (bash, zsh, fish), readline
# editors, vim, less, htop, and tmux all interpret them correctly.
_VT_KEYS: dict[int, str] = {
    int(Qt.Key.Key_Up):       "\x1b[A",
    int(Qt.Key.Key_Down):     "\x1b[B",
    int(Qt.Key.Key_Right):    "\x1b[C",
    int(Qt.Key.Key_Left):     "\x1b[D",
    int(Qt.Key.Key_Home):     "\x1b[H",
    int(Qt.Key.Key_End):      "\x1b[F",
    int(Qt.Key.Key_Insert):   "\x1b[2~",
    int(Qt.Key.Key_Delete):   "\x1b[3~",
    int(Qt.Key.Key_PageUp):   "\x1b[5~",
    int(Qt.Key.Key_PageDown): "\x1b[6~",
    int(Qt.Key.Key_F1):       "\x1bOP",
    int(Qt.Key.Key_F2):       "\x1bOQ",
    int(Qt.Key.Key_F3):       "\x1bOR",
    int(Qt.Key.Key_F4):       "\x1bOS",
    int(Qt.Key.Key_F5):       "\x1b[15~",
    int(Qt.Key.Key_F6):       "\x1b[17~",
    int(Qt.Key.Key_F7):       "\x1b[18~",
    int(Qt.Key.Key_F8):       "\x1b[19~",
    int(Qt.Key.Key_F9):       "\x1b[20~",
    int(Qt.Key.Key_F10):      "\x1b[21~",
    int(Qt.Key.Key_F11):      "\x1b[23~",
    int(Qt.Key.Key_F12):      "\x1b[24~",
    int(Qt.Key.Key_Backspace): "\x7f",     # DEL — matches stty erase ^?
    int(Qt.Key.Key_Tab):      "\t",
    int(Qt.Key.Key_Backtab):  "\x1b[Z",
    int(Qt.Key.Key_Escape):   "\x1b",
    int(Qt.Key.Key_Return):   "\r",
    int(Qt.Key.Key_Enter):    "\r",
}


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
        self._display.selectionChanged.connect(self._auto_copy_selection)
        layout.addWidget(self._display, 1)

        self._matches: list[_Match] = []
        self._current_match = -1
        self._find_bar.query_changed.connect(self._on_find_query)
        self._find_bar.next_match.connect(lambda: self._advance_match(+1))
        self._find_bar.prev_match.connect(lambda: self._advance_match(-1))
        self._find_bar.closed.connect(self.close_find_bar)

        self._broadcast_callback: Callable[[str], None] | None = None
        self._on_clear_scrollback: Callable[[], None] | None = None

        # Theme colours — stashed so the pyte renderer can resolve
        # ``default`` cells back to the active theme's fg / bg pair.
        self._theme_fg = QColor("#d4d4d4")
        self._theme_bg = QColor("#1e1e1e")

        # Pyte emulator + debounced redraw timer. Coalescing redraws to
        # ~60Hz keeps high-volume output (``yes``, ``cat /var/log``)
        # from rebuilding the QTextDocument on every byte.
        self._emulator = _PyteEmulator(
            cols=80, rows=24, history=self._display.maximumBlockCount()
        )
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(16)
        self._redraw_timer.timeout.connect(self._redraw_from_emulator)

        self.apply_theme(
            background="#1e1e1e",
            foreground="#d4d4d4",
            cursor="#ffffff",
            selection="#264f78",
        )
        self._sync_emulator_size()

    # -- public API --------------------------------------------------------

    def append_output(self, text: str) -> None:
        """Feed remote output to the pyte emulator.

        The actual repaint of the :class:`QPlainTextEdit` happens via
        :meth:`_redraw_from_emulator`, debounced through a 16ms timer
        so a high-rate stream (e.g. ``yes`` / ``tail -f``) doesn't
        rebuild the QTextDocument on every chunk.
        """
        if not text:
            return
        self._emulator.feed(text)
        if not self._redraw_timer.isActive():
            self._redraw_timer.start()

    def set_scrollback(self, lines: int) -> None:
        """Change the maximum number of retained lines.

        Sets both the QPlainTextEdit's block cap *and* the pyte history
        budget so they stay in sync — otherwise pyte would either keep
        more lines than the widget can render, or fewer than the user
        configured in *Settings → Terminal*.
        """
        capped = max(100, lines)
        self._display.setMaximumBlockCount(capped)
        # pyte exposes the history limit as a writable namedtuple field;
        # rebuild it preserving the existing top / bottom buffers.
        h = self._emulator.screen.history
        self._emulator.screen.history = h._replace(size=capped)

    def set_font(self, family: str, size: int) -> None:
        """Set the terminal font family and size."""
        font = QFont(family, size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._display.setFont(font)
        self._sync_emulator_size()

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
        # Stash defaults so the pyte renderer can resolve ``default``
        # cells to the right colour.
        self._theme_bg = QColor(background)
        self._theme_fg = QColor(foreground)
        _ = cursor  # QPlainTextEdit caret colour is platform-controlled.
        if hasattr(self, "_emulator"):
            self._redraw_from_emulator()

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
        """Right-click handler — always pastes, never shows a menu.

        Three cases (per UX spec):

        * **Single-line selection**: send the selected text to the
          remote session immediately.
        * **Multi-line selection**: open the *Confirm Paste* dialog so
          the user can review / edit the text before sending.
        * **No selection**: paste whatever is currently on the system
          clipboard.

        ``point`` is unused but kept for the
        :pysignal:`customContextMenuRequested` signature.
        """
        del point
        cursor = self._display.textCursor()
        # ``QPlainTextEdit`` selections use U+2029 (PARAGRAPH SEPARATOR)
        # between blocks and U+2028 (LINE SEPARATOR) for soft breaks.
        # Normalise both to ``\n`` so downstream callers and the
        # multi-line check below see real newlines.
        selected = (
            cursor.selectedText()
            .replace("\u2029", "\n")
            .replace("\u2028", "\n")
            if cursor.hasSelection()
            else ""
        )

        if not selected:
            # No selection → paste the system clipboard contents.
            clipboard = QApplication.clipboard()
            text = clipboard.text() if clipboard is not None else ""
            if not text:
                return
            if "\n" in text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n"):
                # Multi-line clipboard contents → confirm before sending.
                dlg = ConfirmPasteDialog(text, self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    final = dlg.text()
                    if final:
                        self.text_input.emit(final)
            else:
                self.text_input.emit(text)
            return

        if "\n" in selected:
            dlg = ConfirmPasteDialog(selected, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                final = dlg.text()
                if final:
                    self.text_input.emit(final)
        else:
            self.text_input.emit(selected)

    def _make_action(self, label: str, slot: Callable[[], None]) -> QAction:
        """Build a QAction wired to ``slot`` (kept for the find-bar menu)."""
        action = QAction(label, self)
        action.triggered.connect(slot)
        return action

    def _auto_copy_selection(self) -> None:
        """Copy the active selection to the clipboard automatically.

        Wired to :pysignal:`QPlainTextEdit.selectionChanged` so any
        mouse-drag or keyboard-driven selection ends up on the clipboard
        without the user having to press *Ctrl+C* or right-click *Copy*.
        Empty selections are ignored so we don't blow away whatever the
        user copied previously when they just click around the buffer.
        """
        cursor = self._display.textCursor()
        if not cursor.hasSelection():
            return
        text = (
            cursor.selectedText()
            .replace("\u2029", "\n")
            .replace("\u2028", "\n")
        )
        if not text:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:  # pragma: no cover — headless edge case
            return
        clipboard.setText(text)

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
        self._emulator.reset()
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

    # -- pyte rendering ----------------------------------------------------

    def _sync_emulator_size(self) -> None:
        """Match the pyte grid to the QPlainTextEdit's pixel viewport.

        Run on construction, font change, and every resize event. Cols
        and rows are derived from the monospace font's advance width
        and line spacing; both are clamped so a tiny / zero-sized
        widget never crashes pyte (which rejects ``cols=0``).
        """
        viewport = self._display.viewport()
        if viewport is None:
            return
        metrics = QFontMetricsF(self._display.font())
        advance = metrics.horizontalAdvance("M") or 8.0
        line_h = metrics.lineSpacing() or 14.0
        cols = max(20, int(viewport.width() / advance))
        rows = max(5, int(viewport.height() / line_h))
        self._emulator.resize(cols, rows)
        if not self._redraw_timer.isActive():
            self._redraw_timer.start()

    def resizeEvent(self, event):  # noqa: N802 — Qt API
        """Re-tile the pyte grid to match the new viewport size."""
        super().resizeEvent(event)
        self._sync_emulator_size()

    def _redraw_from_emulator(self) -> None:
        """Rebuild the :class:`QPlainTextEdit` from the pyte buffer.

        Walks ``screen.history.top`` (lines that have scrolled off) and
        then the current ``screen.buffer`` rows; each row is grouped
        into runs of consecutive cells with identical attributes and
        emitted as :class:`QTextCharFormat`-styled
        :py:meth:`insertText` calls. The selection range and scrollbar
        position are preserved so streaming output doesn't blow away
        the user's text selection or scrollback position.
        """
        document = self._display.document()
        old_cursor = self._display.textCursor()
        sel_anchor = old_cursor.anchor() if old_cursor.hasSelection() else -1
        sel_pos = old_cursor.position() if old_cursor.hasSelection() else -1
        sb = self._display.verticalScrollBar()
        scroll_value = sb.value()
        at_bottom = scroll_value >= sb.maximum() - 2

        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()

        first_block = True
        for line in self._iter_render_lines():
            if not first_block:
                cursor.insertBlock()
            first_block = False
            self._render_line(cursor, line)
        cursor.endEditBlock()

        doc_len = document.characterCount() - 1
        if sel_anchor >= 0 and sel_pos >= 0 and doc_len > 0:
            new_cursor = QTextCursor(document)
            new_cursor.setPosition(min(sel_anchor, doc_len))
            new_cursor.setPosition(
                min(sel_pos, doc_len), QTextCursor.MoveMode.KeepAnchor
            )
            self._display.setTextCursor(new_cursor)

        if at_bottom:
            sb.setValue(sb.maximum())
        else:
            sb.setValue(min(scroll_value, sb.maximum()))

        if self._matches:
            # Match offsets shift around as the buffer redraws — re-run
            # the search against the new document.
            self._on_find_query(
                self._find_bar.query.text(),
                self._find_bar.case_cb.isChecked(),
                self._find_bar.regex_cb.isChecked(),
            )

    def _iter_render_lines(self):
        """Yield every visible row — history first, then current screen."""
        screen = self._emulator.screen
        for line in screen.history.top:
            yield line
        for y in range(screen.lines):
            yield screen.buffer[y]

    def _render_line(self, cursor: QTextCursor, line) -> None:
        """Emit one screen row into ``cursor`` as styled runs.

        Adjacent cells with identical (fg, bg, bold, italic, underscore,
        reverse, strike) tuples are coalesced into a single
        :py:meth:`insertText` call so the QTextDocument doesn't
        fragment into thousands of tiny runs.
        """
        screen = self._emulator.screen
        cols = screen.columns

        # Trim trailing default-coloured spaces so each line looks like
        # ``"prompt$ "`` instead of being padded out to N columns of
        # blanks (which would make selection sweep highlight the empty
        # tail and visually ruin the layout).
        last = -1
        for x in range(cols - 1, -1, -1):
            cell = line[x]
            if cell.data != " " or cell.fg != "default" or cell.bg != "default":
                last = x
                break
        if last < 0:
            return

        run_chars: list[str] = []
        run_key: tuple | None = None
        for x in range(last + 1):
            cell = line[x]
            key = (
                cell.fg, cell.bg, cell.bold, cell.italics,
                cell.underscore, cell.reverse, cell.strikethrough,
            )
            if run_key is not None and key != run_key:
                cursor.insertText("".join(run_chars), self._format_for_key(run_key))
                run_chars = []
            run_key = key
            run_chars.append(cell.data)
        if run_chars and run_key is not None:
            cursor.insertText("".join(run_chars), self._format_for_key(run_key))

    def _format_for_key(self, key: tuple) -> QTextCharFormat:
        """Build a :class:`QTextCharFormat` from a packed cell key."""
        fg_name, bg_name, bold, italics, underscore, reverse, strike = key
        fg = _pyte_qcolor(fg_name, self._theme_fg)
        bg = _pyte_qcolor(bg_name, self._theme_bg)
        if reverse:
            fg, bg = bg, fg
        fmt = QTextCharFormat()
        fmt.setForeground(fg)
        if bg_name != "default" or reverse:
            fmt.setBackground(bg)
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if italics:
            fmt.setFontItalic(True)
        if underscore:
            fmt.setFontUnderline(True)
        if strike:
            fmt.setFontStrikeOut(True)
        return fmt

    # -- key forwarding ----------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 — Qt API
        """Forward keypresses on the QPlainTextEdit upstream as ``text_input``.

        Three cases:

        1. *Special keys* (arrow / Home / End / Delete / Insert / Page
           Up / Down / F1..F12 / Backspace / Tab / Esc / Enter) are
           translated through :data:`_VT_KEYS` into the matching
           xterm-style escape sequence and emitted; the QPlainTextEdit's
           own cursor-movement handlers are bypassed because the next
           pyte redraw would clobber any local edits anyway.
        2. *Printable keys* go out via ``event.text()`` (Qt fills this
           in with the right character for the current keyboard layout
           and modifiers, including dead keys / IME composition).
        3. *Ctrl+letter* combos that don't have a text payload (Ctrl+A,
           Ctrl+C, Ctrl+Z, etc. on layouts where Qt eats the text)
           are converted to their ASCII control byte (Ctrl+A → 0x01,
           …, Ctrl+_ → 0x1f) so they reach the remote shell.

        All forwarded events return ``True`` so the underlying
        QPlainTextEdit does not also process them locally — otherwise
        each keystroke would appear twice once the remote echo arrived
        via :meth:`append_output`.
        """
        if obj is self._display and event.type() == event.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            mods = event.modifiers()
            ctrl_only = (
                mods & Qt.KeyboardModifier.ControlModifier
                and not (mods & Qt.KeyboardModifier.AltModifier)
                and not (mods & Qt.KeyboardModifier.MetaModifier)
            )
            # Ctrl+F opens the find bar — keep this as the only local
            # shortcut so it's reachable even mid-session.
            if event.key() == Qt.Key.Key_F and ctrl_only:
                self.open_find_bar()
                return True

            # 1. Special keys (arrows, Home/End, Delete, F-keys, …).
            seq = _VT_KEYS.get(int(event.key()))
            if seq is not None:
                self.text_input.emit(seq)
                return True

            # 2. Printable keys.
            text = event.text()
            if text:
                self.text_input.emit(text)
                return True

            # 3. Ctrl+<letter> combos with empty text payload — convert
            #    Key_A..Key_Z (0x41..0x5A) to the corresponding ASCII
            #    control byte 0x01..0x1A.
            key_val = int(event.key())
            if ctrl_only and Qt.Key.Key_A <= event.key() <= Qt.Key.Key_Underscore:
                self.text_input.emit(chr(key_val & 0x1f))
                return True
        return super().eventFilter(obj, event)
