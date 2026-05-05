"""Quick Host Bar input field with built-in suggestion popup.

The widget looks like a normal :class:`QLineEdit` but knows how to ask
the :class:`SessionStore` for its recent connections + saved sessions
and surface them as a 5-row dropdown:

* clicking into an empty field immediately shows the *5 most recent*
  hostnames the user connected to;
* typing rebuilds the suggestion set against hostname / IP / session
  name, also capped at 5 rows;
* the standard :class:`QCompleter` behaviour gives us arrow-key
  navigation, Enter-to-fill, Escape-to-close, and click-outside-to-close
  for free; the user still has to hit Enter again on the line edit to
  actually connect.
"""
from __future__ import annotations

from PyQt6.QtCore import QStringListModel, Qt
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import QCompleter, QLineEdit, QWidget

from core.session_store import SessionStore

#: Maximum suggestions shown in the dropdown at any time.
MAX_SUGGESTIONS = 5


class HostInputLineEdit(QLineEdit):
    """Hostname line edit with a 5-row suggestion popup.

    The widget is store-aware: pass a :class:`SessionStore` once and it
    will refresh its suggestion list on every focus / text change. It is
    safe to pass ``None`` (e.g. in tests) — in that case the completer
    falls back to an empty model.
    """

    def __init__(
        self,
        store: SessionStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Build the input and wire up the completer."""
        super().__init__(parent)
        self._store = store
        self.setPlaceholderText("hostname\u2026")

        self._model = QStringListModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(MAX_SUGGESTIONS)
        self.setCompleter(self._completer)

        self.textEdited.connect(self._on_text_edited)

    # -- public ------------------------------------------------------------

    def set_store(self, store: SessionStore | None) -> None:
        """Swap the underlying :class:`SessionStore` (or unset)."""
        self._store = store

    # -- Qt overrides ------------------------------------------------------

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802 (Qt API)
        """When the field is empty on focus, immediately show recents."""
        super().focusInEvent(event)
        if not self.text():
            self._populate_recent()
            self._completer.complete()

    # -- internals ---------------------------------------------------------

    def _populate_recent(self) -> None:
        """Push the last ``MAX_SUGGESTIONS`` recent hostnames into the model."""
        suggestions: list[str] = []
        if self._store is not None:
            for row in self._store.list_recent_connections():
                if row.hostname not in suggestions:
                    suggestions.append(row.hostname)
                if len(suggestions) >= MAX_SUGGESTIONS:
                    break
        self._model.setStringList(suggestions)

    def _on_text_edited(self, text: str) -> None:
        """Rebuild the suggestion list as the user types.

        Matches anything in the user's data whose hostname / IP / session
        name *contains* the typed substring (case-insensitive). The
        result is capped at :data:`MAX_SUGGESTIONS` so the popup stays
        compact.
        """
        if self._store is None:
            return
        if not text:
            self._populate_recent()
            return

        needle = text.lower()
        seen: set[str] = set()
        out: list[str] = []

        # Saved sessions \u2014 match against hostname or session name.
        for sess in self._store.list_sessions():
            for haystack in (sess.hostname or "", sess.name or ""):
                if needle in (haystack or "").lower():
                    if sess.hostname and sess.hostname not in seen:
                        seen.add(sess.hostname)
                        out.append(sess.hostname)
                    break
            if len(out) >= MAX_SUGGESTIONS:
                break

        # Recent connections \u2014 match against hostname.
        if len(out) < MAX_SUGGESTIONS:
            for row in self._store.list_recent_connections():
                host = row.hostname or ""
                if needle in host.lower() and host not in seen:
                    seen.add(host)
                    out.append(host)
                    if len(out) >= MAX_SUGGESTIONS:
                        break

        self._model.setStringList(out[:MAX_SUGGESTIONS])
