"""Shared test fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo root is on sys.path for `core.*` and `ui.*` imports.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from core.session_store import SessionStore  # noqa: E402


@pytest.fixture
def store() -> SessionStore:
    """Return an in-memory :class:`SessionStore` for unit tests."""
    return SessionStore(db_path=":memory:")
