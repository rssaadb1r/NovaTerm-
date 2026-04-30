"""Command Window — bottom dock for ad-hoc / pasted commands.

Layout (top → bottom):

* a multi-line :class:`QPlainTextEdit` for the user to type / paste a
  command
* a row with two ``Send`` buttons and a *Send on Enter* checkbox

When *Send on Enter* is checked, pressing the Enter key while the editor
has focus sends the buffered text immediately (Shift+Enter still inserts
a literal newline). Visibility is toggled from ``View → Command Window``
or ``Ctrl+Shift+C``; the panel itself is mounted inside a vertical
QSplitter in :mod:`ui.main_window` so the user can drag the top edge to
resize it.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

#: Default panel height in pixels (per the spec).
DEFAULT_HEIGHT = 80
#: Minimum panel height in pixels.
MINIMUM_HEIGHT = 60


class _CommandEditor(QPlainTextEdit):
    """``QPlainTextEdit`` that emits a signal on bare Enter when asked.

    A reference to :class:`CommandWindow` is kept so we can consult the
    *Send on Enter* checkbox at the moment of the keypress instead of
    capturing its value at construction time.
    """

    enter_pressed = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt API)
        """Emit ``enter_pressed`` when bare Enter is pressed (and consume it)."""
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self.enter_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CommandWindow(QFrame):
    """A docked text-input panel for one-off commands.

    The window is intentionally signal-only: it knows nothing about
    sessions, vaults, or asyncio, and emits two distinct signals so the
    main window can decide where to dispatch the text.
    """

    #: Emitted when the user clicks *Send to Active Session* (or hits
    #: Enter with *Send on Enter* enabled).
    send_to_active = pyqtSignal(str)

    #: Emitted when the user clicks *Send to All Sessions*.
    send_to_all = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the panel widgets."""
        super().__init__(parent)
        self.setObjectName("CommandWindow")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(MINIMUM_HEIGHT)
        # Allow the host splitter to resize us, but keep a sensible default.
        self.resize(self.width(), DEFAULT_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(4)

        self._editor = _CommandEditor(self)
        self._editor.setPlaceholderText(
            "Type a command \u2013 Send to Active or Send to All\u2026"
        )
        self._editor.enter_pressed.connect(self._on_enter)
        outer.addWidget(self._editor, 1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(6)

        self._active_btn = QPushButton("Send to Active Session", self)
        self._active_btn.clicked.connect(self._dispatch_active)
        button_row.addWidget(self._active_btn)

        self._all_btn = QPushButton("Send to All Sessions", self)
        self._all_btn.clicked.connect(self._dispatch_all)
        button_row.addWidget(self._all_btn)

        button_row.addStretch(1)

        self._enter_cb = QCheckBox("Send on Enter", self)
        button_row.addWidget(self._enter_cb)
        outer.addLayout(button_row)

    # -- public ------------------------------------------------------------

    def sizeHint(self):  # noqa: N802 (Qt API)
        """Default to ``DEFAULT_HEIGHT`` pixels tall regardless of contents."""
        hint = super().sizeHint()
        hint.setHeight(DEFAULT_HEIGHT)
        return hint

    def focus_editor(self) -> None:
        """Move keyboard focus to the editor (used after the user toggles us on)."""
        self._editor.setFocus()
        self._editor.moveCursor(self._editor.textCursor().MoveOperation.End)

    # -- internals ---------------------------------------------------------

    def _current_text(self) -> str:
        """Return the editor contents (trailing whitespace preserved)."""
        return self._editor.toPlainText()

    def _on_enter(self) -> None:
        """Bare-Enter slot: dispatch to active *only* when the checkbox is on.

        The editor consumes the Enter key regardless so the cursor never
        creates a stray blank line in *Send on Enter* mode; with the
        checkbox off we fall back to inserting a newline so the user can
        compose multi-line commands.
        """
        if self._enter_cb.isChecked():
            self._dispatch_active()
        else:
            self._editor.insertPlainText("\n")

    def _dispatch_active(self) -> None:
        text = self._current_text()
        if not text:
            return
        self.send_to_active.emit(text)

    def _dispatch_all(self) -> None:
        text = self._current_text()
        if not text:
            return
        self.send_to_all.emit(text)
