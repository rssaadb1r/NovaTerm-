"""Filesystem paths used by NovaTerm.

All paths are derived from :mod:`platformdirs`; no hardcoded paths are used
anywhere in the application.
"""
from __future__ import annotations

from pathlib import Path

from platformdirs import PlatformDirs

_dirs = PlatformDirs(appname="NovaTerm", appauthor=False, ensure_exists=False)


def config_dir() -> Path:
    """Return the user configuration directory (created on demand)."""
    p = Path(_dirs.user_config_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Return the user data directory (created on demand)."""
    p = Path(_dirs.user_data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    """Return the user log directory (created on demand)."""
    p = Path(_dirs.user_log_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def database_path() -> Path:
    """Return the SQLite database file path."""
    return data_dir() / "novaterm.sqlite3"


def settings_file() -> Path:
    """Return the path to the global TOML settings file."""
    return config_dir() / "settings.toml"


def themes_dir() -> Path:
    """Return the directory bundled themes are loaded from.

    Falls back to the project-relative ``themes/`` directory when running
    from a source checkout (where the package is not installed).
    """
    here = Path(__file__).resolve().parent.parent / "themes"
    return here
