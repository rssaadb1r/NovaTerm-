"""Per-session file logger (Feature 12)."""
from __future__ import annotations

from datetime import datetime
from io import TextIOBase
from pathlib import Path
from typing import Final

from . import paths
from .command_store import substitute_variables

DEFAULT_FILENAME: Final[str] = "%SESSION%-%DATE%.log"


class SessionLogger:
    """Append-or-overwrite log file for a single session.

    The output filename pattern supports ``%SESSION% / %DATE% / %TIME%`` (see
    :func:`core.command_store.substitute_variables`).
    """

    def __init__(
        self,
        session_name: str,
        *,
        path_template: str | None = None,
        mode: str = "append",
        hostname: str | None = None,
        username: str | None = None,
    ) -> None:
        """Open a log file for writing.

        :param session_name: Friendly name of the session (used in ``%SESSION%``).
        :param path_template: Filesystem path or filename. Variables expand at
            open time. ``None`` uses :data:`DEFAULT_FILENAME` under the user
            log dir.
        :param mode: ``"append"`` or ``"overwrite"``.
        :param hostname: Optional, exposed as ``%HOST%`` in the path template.
        :param username: Optional, exposed as ``%USER%`` in the path template.
        """
        if mode not in {"append", "overwrite"}:
            raise ValueError(f"Invalid mode: {mode!r}")

        template = path_template or str(paths.log_dir() / DEFAULT_FILENAME)
        resolved = substitute_variables(
            template,
            hostname=hostname,
            username=username,
            session_name=session_name,
        )
        self.path = Path(resolved).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        flag = "a" if mode == "append" else "w"
        self._fh: TextIOBase | None = self.path.open(flag, encoding="utf-8", buffering=1)
        self._fh.write(f"\n--- NovaTerm session log opened {datetime.now().isoformat()} ---\n")

    def write(self, text: str) -> None:
        """Append text to the log; no-op if the file is closed."""
        if self._fh is None:
            return
        self._fh.write(text)

    def close(self) -> None:
        """Close the underlying file handle."""
        if self._fh is not None:
            self._fh.write(f"\n--- closed {datetime.now().isoformat()} ---\n")
            self._fh.close()
            self._fh = None

    def __enter__(self) -> SessionLogger:
        """Context-manager entry."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on context-manager exit."""
        self.close()
