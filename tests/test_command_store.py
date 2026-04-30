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


def test_update_group_renames(store: SessionStore) -> None:
    cs = CommandStore(store)
    gid = cs.create_group("Old")
    cs.update_group(gid, name="New")
    assert [g.name for g in cs.list_groups()] == ["New"]


def test_reorder_command_swaps_neighbours(store: SessionStore) -> None:
    cs = CommandStore(store)
    gid = cs.create_group("ops")
    cs.create_command(name="a", command_text="a", group_id=gid)
    cs.create_command(name="b", command_text="b", group_id=gid)
    cs.create_command(name="c", command_text="c", group_id=gid)

    cmds = cs.list_commands(group_id=gid)
    assert [c.name for c in cmds] == ["a", "b", "c"]

    cs.reorder_command(cmds[0].id, delta=+1)
    assert [c.name for c in cs.list_commands(group_id=gid)] == ["b", "a", "c"]

    cs.reorder_command(cmds[2].id, delta=-1)
    # ``c`` now in position 1: [b, c, a]
    assert [c.name for c in cs.list_commands(group_id=gid)] == ["b", "c", "a"]

    # Edge-of-list moves are no-ops.
    top = cs.list_commands(group_id=gid)[0]
    cs.reorder_command(top.id, delta=-1)
    assert [c.name for c in cs.list_commands(group_id=gid)] == ["b", "c", "a"]


def test_command_import_export_roundtrip(store: SessionStore) -> None:
    cs = CommandStore(store)
    gid = cs.create_group("Net")
    cs.create_command(name="ping", command_text="ping -c1 %HOST%", group_id=gid, tags=["net"])
    payload = cs.export_json()

    other = CommandStore(SessionStore(":memory:"))
    n = other.import_json(payload)
    assert n == 1
    assert any(c.name == "ping" for c in other.list_commands())
