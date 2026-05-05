"""Asynchronous Telnet client based on :mod:`telnetlib3`.

Python 3.12 removed the stdlib :mod:`telnetlib`, so ``telnetlib3`` is used
instead — it speaks the same protocol but is asyncio-native.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import telnetlib3

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelnetConfig:
    """Connection parameters for a single Telnet session."""

    hostname: str
    port: int = 23
    username: str | None = None
    password: str | None = None
    encoding: str = "utf-8"
    connect_timeout: int = 15


class TelnetConnectionError(RuntimeError):
    """Raised when a Telnet connection attempt fails."""


class AsyncTelnetClient:
    """Lightweight wrapper around ``telnetlib3.open_connection``."""

    def __init__(self, config: TelnetConfig) -> None:
        """Bind the client to a config (no I/O yet)."""
        self.config = config
        self._reader: telnetlib3.TelnetReader | None = None
        self._writer: telnetlib3.TelnetWriter | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """``True`` while the underlying StreamWriter is open."""
        return self._connected and self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the TCP connection and negotiate the Telnet options."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                telnetlib3.open_connection(
                    host=self.config.hostname,
                    port=self.config.port,
                    encoding=self.config.encoding,
                ),
                timeout=self.config.connect_timeout,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise TelnetConnectionError(str(exc)) from exc
        self._connected = True

    async def write(self, data: str) -> None:
        """Send a string to the remote side."""
        if not self.connected or self._writer is None:
            raise TelnetConnectionError("Not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def read(self, max_bytes: int = 4096) -> str:
        """Read up to ``max_bytes`` characters."""
        if not self.connected or self._reader is None:
            raise TelnetConnectionError("Not connected")
        return await self._reader.read(max_bytes)

    async def disconnect(self) -> None:
        """Close the connection."""
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._connected = False
