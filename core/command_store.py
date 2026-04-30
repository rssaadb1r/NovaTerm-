"""CRUD helpers for the Command Manager (Feature 5).

Backed by the same SQLite database as :mod:`core.session_store`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from .session_store import Command, CommandGroup, SessionStore

# Variable substitution pattern (see CLAUDE.md §8).
_VAR_RE = re.compile(r"%(HOST|USER|SESSION|DATE|TIME)%")


def _safe_filename(text: str) -> str:
    """Sanitise text for safe inclusion in a filename."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "session")


def substitute_variables(
    text: str,
    *,
    hostname: str | None = None,
    username: str | None = None,
    session_name: str | None = None,
    when: datetime | None = None,
) -> str:
    """Expand ``%HOST% / %USER% / %SESSION% / %DATE% / %TIME%`` tokens.

    Unknown tokens are left untouched.

    :param text: Source text containing zero or more tokens.
    :param hostname: Replacement for ``%HOST%``.
    :param username: Replacement for ``%USER%``.
    :param session_name: Replacement for ``%SESSION%`` (sanitised).
    :param when: Datetime used for ``%DATE%`` and ``%TIME%``; defaults to now.
    """
    when = when or datetime.now()
    mapping = {
        "HOST": hostname or "",
        "USER": username or "",
        "SESSION": _safe_filename(session_name or ""),
        "DATE": when.strftime("%Y-%m-%d"),
        "TIME": when.strftime("%H-%M-%S"),
    }
    return _VAR_RE.sub(lambda m: mapping[m.group(1)], text)


class CommandStore:
    """High-level CRUD wrapper around :class:`Command` + :class:`CommandGroup`."""

    def __init__(self, store: SessionStore) -> None:
        """Bind to an existing :class:`SessionStore`."""
        self._store = store

    # -- groups ------------------------------------------------------------

    def create_group(self, name: str, sort_order: int = 0) -> int:
        """Create a command group and return its id."""
        with self._store.session() as s:
            g = CommandGroup(name=name, sort_order=sort_order)
            s.add(g)
            s.flush()
            return g.id

    def list_groups(self) -> list[CommandGroup]:
        """Return all command groups ordered by ``sort_order`` then name."""
        with self._store.session() as s:
            return list(
                s.scalars(select(CommandGroup).order_by(CommandGroup.sort_order, CommandGroup.name))
            )

    def delete_group(self, group_id: int) -> None:
        """Delete a command group (commands inside become group-less)."""
        with self._store.session() as s:
            g = s.get(CommandGroup, group_id)
            if g is not None:
                s.delete(g)

    # -- commands ----------------------------------------------------------

    def create_command(self, **fields: Any) -> int:
        """Create a command row and return its id."""
        with self._store.session() as s:
            c = Command(**fields)
            s.add(c)
            s.flush()
            return c.id

    def update_command(self, command_id: int, **fields: Any) -> None:
        """Update fields on an existing command row."""
        with self._store.session() as s:
            row = s.get(Command, command_id)
            if row is None:
                raise KeyError(f"Command id={command_id} not found")
            for k, v in fields.items():
                setattr(row, k, v)

    def delete_command(self, command_id: int) -> None:
        """Delete a command row by id."""
        with self._store.session() as s:
            row = s.get(Command, command_id)
            if row is not None:
                s.delete(row)

    def list_commands(self, group_id: int | None = None) -> list[Command]:
        """Return all commands (optionally filtered by group)."""
        with self._store.session() as s:
            stmt = select(Command).order_by(Command.name)
            if group_id is not None:
                stmt = stmt.where(Command.group_id == group_id)
            return list(s.scalars(stmt))

    def search_commands(self, query: str) -> list[Command]:
        """Naive fuzzy search: match contiguous letters of ``query`` in name/text."""
        from sqlalchemy import func as f

        q = f"%{query.lower()}%"
        with self._store.session() as s:
            stmt = select(Command).where(
                f.lower(Command.name).like(q) | f.lower(Command.command_text).like(q)
            )
            return list(s.scalars(stmt))

    # -- import / export ---------------------------------------------------

    def export_json(self) -> str:
        """Serialise every command + group to a JSON string."""
        groups = [{"id": g.id, "name": g.name, "sort_order": g.sort_order} for g in self.list_groups()]
        commands = [
            {
                "name": c.name,
                "command_text": c.command_text,
                "description": c.description,
                "tags": list(c.tags or []),
                "hotkey": c.hotkey,
                "group_id": c.group_id,
            }
            for c in self.list_commands()
        ]
        return json.dumps({"groups": groups, "commands": commands}, indent=2)

    def import_json(self, payload: str) -> int:
        """Import commands + groups from a JSON document. Returns command count."""
        data = json.loads(payload)
        added = 0
        id_map: dict[int, int] = {}
        with self._store.session() as s:
            for g in data.get("groups", []) or []:
                row = CommandGroup(name=g["name"], sort_order=int(g.get("sort_order", 0)))
                s.add(row)
                s.flush()
                id_map[int(g["id"])] = row.id
            for c in data.get("commands", []) or []:
                gid = c.get("group_id")
                mapped_gid = id_map.get(int(gid)) if gid is not None else None
                s.add(
                    Command(
                        group_id=mapped_gid,
                        name=c["name"],
                        command_text=c["command_text"],
                        description=c.get("description"),
                        tags=list(c.get("tags") or []),
                        hotkey=c.get("hotkey"),
                    )
                )
                added += 1
        return added
