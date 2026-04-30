"""Smoke tests for ``core.ssh_client`` (no real network required)."""
from __future__ import annotations

import asyncio

import pytest

from core.ssh_client import (
    AsyncSSHClient,
    HopConfig,
    SSHConnectConfig,
    SSHConnectionError,
)


def test_too_many_jumps_rejected() -> None:
    cfg = SSHConnectConfig(
        target=HopConfig(hostname="t"),
        jumps=[HopConfig(hostname=f"j{i}") for i in range(4)],
    )
    with pytest.raises(ValueError):
        AsyncSSHClient(cfg)


def test_connect_to_invalid_host_raises() -> None:
    """Connecting to an unreachable address should raise ``SSHConnectionError``."""
    cfg = SSHConnectConfig(
        target=HopConfig(
            hostname="127.0.0.1",
            port=1,  # almost certainly closed
            username="nobody",
            password="nope",
        ),
        connect_timeout=2,
    )
    client = AsyncSSHClient(cfg)

    async def _run() -> None:
        with pytest.raises(SSHConnectionError):
            await client.connect()

    asyncio.run(_run())


def test_initial_state() -> None:
    client = AsyncSSHClient(SSHConnectConfig(target=HopConfig(hostname="x")))
    assert client.connected is False
    assert client.get_transport() is None
