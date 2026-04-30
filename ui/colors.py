"""Shared colour-tag palette helpers (Feature 8)."""
from __future__ import annotations

from PyQt6.QtGui import QColor

# Canonical palette (see CLAUDE.md §7).
PALETTE: dict[str, str] = {
    "red": "#e74c3c",
    "orange": "#e67e22",
    "yellow": "#f1c40f",
    "green": "#27ae60",
    "teal": "#1abc9c",
    "blue": "#3498db",
    "purple": "#9b59b6",
    "pink": "#e91e63",
    "gray": "#95a5a6",
}

GROUP_PRESETS: dict[str, str] = {
    "Production": "red",
    "Staging": "yellow",
    "Dev": "green",
    "Local": "teal",
}


def color_for(tag: str | None) -> QColor | None:
    """Return a :class:`QColor` for ``tag``, or ``None`` if unknown."""
    if tag is None:
        return None
    hex_value = PALETTE.get(tag.lower())
    return QColor(hex_value) if hex_value else None


def hex_for(tag: str | None) -> str | None:
    """Return a hex string for ``tag``, or ``None`` if unknown."""
    if tag is None:
        return None
    return PALETTE.get(tag.lower())
