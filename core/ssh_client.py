"""Asynchronous SSH client built on Paramiko + qasync.

Supports up to 3 hops of jump-host chaining (Feature 6) implemented via
Paramiko's transport ``open_channel("direct-tcpip", ...)`` rather than relying
on the system ``ssh`` binary.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import paramiko

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None] | None]


@dataclass(slots=True)
class HopConfig:
    """Connection parameters for a single SSH hop."""

    hostname: str
    port: int = 22
    username: str = ""
    password: str | None = None
    key_path: str | None = None
    key_passphrase: str | None = None


@dataclass(slots=True)
class SSHConnectConfig:
    """Top-level connect config: a target plus an ordered list of jump hops."""

    target: HopConfig
    jumps: list[HopConfig] = field(default_factory=list)
    keepalive_interval: int = 30
    connect_timeout: int = 15
    auto_reconnect_retries: int = 0


class SSHConnectionError(RuntimeError):
    """Raised when an SSH connection attempt fails."""


class AsyncSSHClient:
    """Non-blocking wrapper over a single Paramiko ``SSHClient`` + transport.

    The actual ``connect`` and read/write calls are blocking under the hood;
    we run them in the default executor so the Qt event loop stays responsive.
    """

    MAX_HOPS = 3

    def __init__(self, config: SSHConnectConfig) -> None:
        """Bind the client to a connection config (no I/O yet)."""
        if len(config.jumps) > self.MAX_HOPS:
            raise ValueError(f"At most {self.MAX_HOPS} jump hops are supported")
        self.config = config
        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None
        self._jump_clients: list[paramiko.SSHClient] = []
        self._connected = False

    @property
    def connected(self) -> bool:
        """``True`` when the channel is open and active."""
        return self._connected and self._channel is not None and not self._channel.closed

    # -- public API --------------------------------------------------------

    async def connect(self, progress: ProgressCallback | None = None) -> None:
        """Connect to the target through any configured jump hops.

        :param progress: Optional callable invoked (on the asyncio/Qt main
            loop) with human-readable status strings — used by the UI to
            render the hop-by-hop status dialog. The callback is dispatched
            via ``loop.call_soon_threadsafe`` so it is always run on the
            main thread, even though the SSH handshake itself happens on
            a worker thread.
        """
        loop = asyncio.get_event_loop()
        # Wrap the user-supplied progress callback so it runs on the main loop.
        if progress is None:
            wrapped: ProgressCallback | None = None
        else:
            def wrapped(msg: str, _loop: asyncio.AbstractEventLoop = loop, _cb: ProgressCallback = progress) -> None:
                _loop.call_soon_threadsafe(self._dispatch_progress, _cb, msg)

        try:
            await loop.run_in_executor(None, self._connect_sync, wrapped)
        except Exception as exc:
            logger.exception("SSH connection failed")
            # Tear down any jump-host transports / target client that
            # _connect_sync had managed to bring up before the failure;
            # otherwise their TCP sockets leak until the GC runs (and
            # _disconnect_sync would never be called for a connection
            # that never reached _connected=True).
            try:
                await loop.run_in_executor(None, self._disconnect_sync)
            except Exception:
                logger.exception("Cleanup after failed SSH connect raised")
            raise SSHConnectionError(str(exc)) from exc
        self._connected = True

    @staticmethod
    def _dispatch_progress(cb: ProgressCallback, msg: str) -> None:
        """Run ``cb`` on the main loop and forward awaitable results."""
        result = cb(msg)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)

    async def write(self, data: bytes) -> None:
        """Send raw bytes to the remote shell."""
        if not self.connected or self._channel is None:
            raise SSHConnectionError("Not connected")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._channel.sendall, data)

    async def read(self, max_bytes: int = 4096) -> bytes:
        """Read up to ``max_bytes`` from the remote shell (blocking call)."""
        if not self.connected or self._channel is None:
            raise SSHConnectionError("Not connected")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._channel.recv, max_bytes)

    async def disconnect(self) -> None:
        """Tear down the channel and all hop transports."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._disconnect_sync)
        self._connected = False

    def get_transport(self) -> paramiko.Transport | None:
        """Return the active Paramiko ``Transport`` (used for SFTP)."""
        return self._client.get_transport() if self._client else None

    # -- internals ---------------------------------------------------------

    def _emit(self, progress: ProgressCallback | None, msg: str) -> None:
        """Helper: invoke the (already main-loop-marshalled) progress callback.

        ``progress`` here is the wrapped callback set up in :meth:`connect`,
        which is safe to invoke from any thread (it forwards to the loop).
        """
        logger.info(msg)
        if progress is None:
            return
        progress(msg)

    def _build_kwargs(self, hop: HopConfig) -> dict[str, Any]:
        """Assemble paramiko.connect kwargs for a hop."""
        kwargs: dict[str, Any] = {
            "hostname": hop.hostname,
            "port": hop.port,
            "username": hop.username,
            "timeout": self.config.connect_timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if hop.password:
            kwargs["password"] = hop.password
        if hop.key_path:
            kwargs["key_filename"] = hop.key_path
            if hop.key_passphrase:
                kwargs["passphrase"] = hop.key_passphrase
        return kwargs

    def _connect_sync(self, progress: ProgressCallback | None) -> None:
        """Synchronous body of :meth:`connect` — runs in the executor."""
        previous_transport: paramiko.Transport | None = None
        previous_hop: HopConfig | None = None

        for i, hop in enumerate(self.config.jumps, start=1):
            self._emit(progress, f"Connecting to jump host {i}/{len(self.config.jumps)}: {hop.hostname}")
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            sock = None
            if previous_transport is not None and previous_hop is not None:
                sock = previous_transport.open_channel(
                    "direct-tcpip",
                    (hop.hostname, hop.port),
                    (previous_hop.hostname, 0),
                )

            client.connect(sock=sock, **self._build_kwargs(hop))
            self._jump_clients.append(client)
            previous_transport = client.get_transport()
            previous_hop = hop

        # Final hop = target.
        self._emit(progress, f"Connecting to target: {self.config.target.hostname}")
        target = paramiko.SSHClient()
        target.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        sock = None
        if previous_transport is not None and previous_hop is not None:
            sock = previous_transport.open_channel(
                "direct-tcpip",
                (self.config.target.hostname, self.config.target.port),
                (previous_hop.hostname, 0),
            )

        target.connect(sock=sock, **self._build_kwargs(self.config.target))
        self._client = target

        transport = target.get_transport()
        if transport is None:
            raise SSHConnectionError("No transport after connect")
        transport.set_keepalive(self.config.keepalive_interval)

        self._channel = transport.open_session()
        self._channel.get_pty(term="xterm-256color")
        self._channel.invoke_shell()
        self._emit(progress, "Connected")

    def _disconnect_sync(self) -> None:
        """Synchronous body of :meth:`disconnect`."""
        try:
            if self._channel is not None:
                self._channel.close()
        except Exception:
            pass
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        for jc in reversed(self._jump_clients):
            try:
                jc.close()
            except Exception:
                pass
        self._jump_clients.clear()
        self._client = None
        self._channel = None
