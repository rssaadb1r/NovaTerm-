"""Unit tests for :mod:`core.securecrt_import`."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core.securecrt_import import (
    ImportedCommand,
    ImportedSession,
    parse_config,
)


@pytest.fixture()
def fake_config(tmp_path: Path) -> Path:
    """A minimal SecureCRT *Config* tree with two sessions + button bar."""
    config = tmp_path / "Config"
    sessions = config / "Sessions"
    (sessions / "Production").mkdir(parents=True)

    (sessions / "localhost.ini").write_text(
        textwrap.dedent(
            '''\
            S:"Hostname"=localhost
            D:"[SSH2] Port"=00000016
            S:"Username"=admin
            S:"Protocol Name"=SSH2
            '''
        )
    )
    (sessions / "Production" / "edge1.ini").write_text(
        textwrap.dedent(
            '''\
            S:"Hostname"=10.0.0.1
            D:"[Telnet] Port"=00000017
            S:"Username"=cisco
            S:"Protocol Name"=Telnet
            '''
        )
    )

    (config / "Button Bar.ini").write_text(
        textwrap.dedent(
            '''\
            S:"Button 0 - Caption"=Show users
            S:"Button 0 - Arg"=who
            S:"Button 1 - Caption"=Disk
            S:"Button 1 - Arg"=df -h
            '''
        )
    )
    return config


def test_parse_sessions_decodes_protocol_port_folder(fake_config: Path) -> None:
    """SecureCRT ports are 8-digit hex; protocol normalises to ssh/telnet."""
    result = parse_config(fake_config)
    sessions = {s.name: s for s in result.sessions}
    assert sessions["localhost"] == ImportedSession(
        name="localhost",
        hostname="localhost",
        port=22,
        protocol="ssh",
        username="admin",
        folder_path=(),
    )
    assert sessions["edge1"] == ImportedSession(
        name="edge1",
        hostname="10.0.0.1",
        port=23,
        protocol="telnet",
        username="cisco",
        folder_path=("Production",),
    )


def test_parse_button_bar_pairs_caption_and_arg(fake_config: Path) -> None:
    """Buttons are reassembled by index from Caption + Arg pairs."""
    result = parse_config(fake_config)
    assert result.commands == [
        ImportedCommand(name="Show users", command_text="who"),
        ImportedCommand(name="Disk", command_text="df -h"),
    ]


def test_missing_button_bar_yields_no_commands(tmp_path: Path) -> None:
    """An empty Config (no Button Bar.ini) returns 0 commands cleanly."""
    config = tmp_path / "Config"
    (config / "Sessions").mkdir(parents=True)
    result = parse_config(config)
    assert result.commands == []
    assert result.sessions == []


def test_session_without_hostname_is_skipped(tmp_path: Path) -> None:
    """SecureCRT folder-marker INI files have no Hostname; skip them."""
    config = tmp_path / "Config"
    sessions = config / "Sessions"
    sessions.mkdir(parents=True)
    (sessions / "junk.ini").write_text(
        '''S:"Protocol Name"=SSH2\n'''  # no Hostname
    )
    result = parse_config(config)
    assert result.sessions == []
