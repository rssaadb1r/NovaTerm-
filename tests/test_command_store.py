"""Unit tests for ``core.command_store``."""
from __future__ import annotations

from datetime import datetime

from core.command_store import CommandStore, substitute_variables
from core.session_store import SessionStore


def test_variable_substitution() -> None:
    out = substitute_variables(
        "echo %USER%@%HOST% %SESSION% %DATE% %TIME% %UNKNOWN%",
        hostname="example.com",
        username="alice",
        session_name="my session",
        when=datetime(2024, 7, 4, 13, 5, 1),
    )
    assert "alice@example.com" in out
    assert "my_session" in out
    assert "2024-07-04" in out
    assert "13-05-01" in out
    # Unknown tokens are left alone.
    assert "%UNKNOWN%" in out


def test_command_crud_and_search(store: SessionStore) -> None:
    cs = CommandStore(store)
    gid = cs.create_group("Diagnostics")
    cs.create_command(name="uptime", command_text="uptime", group_id=gid, tags=["sys"])
    cs.create_command(name="disks", command_text="df -h", group_id=gid, tags=["sys"])
    assert {c.name for c in cs.list_commands()} == {"uptime", "disks"}
    assert {c.name for c in cs.search_commands("up")} == {"uptime"}


def test_command_import_export_roundtrip(store: SessionStore) -> None:
    cs = CommandStore(store)
    gid = cs.create_group("Net")
    cs.create_command(name="ping", command_text="ping -c1 %HOST%", group_id=gid, tags=["net"])
    payload = cs.export_json()

    other = CommandStore(SessionStore(":memory:"))
    n = other.import_json(payload)
    assert n == 1
    assert any(c.name == "ping" for c in other.list_commands())
