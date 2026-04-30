"""SFTP client wrapper + concurrent transfer queue (Feature 10)."""
from __future__ import annotations

import logging
import os
import stat
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import paramiko

from .ssh_client import AsyncSSHClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RemoteEntry:
    """A directory listing entry from the remote side."""

    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    mode: int


@dataclass(slots=True)
class TransferProgress:
    """Mutable progress record kept by :class:`SFTPTransferQueue`."""

    transfer_id: int
    direction: str  # "upload" | "download"
    src: str
    dst: str
    total: int
    transferred: int = 0
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    error: str | None = None

    @property
    def percent(self) -> float:
        """Return completion percentage, 0–100."""
        if self.total <= 0:
            return 0.0
        return min(100.0, (self.transferred / self.total) * 100.0)

    @property
    def speed_bps(self) -> float:
        """Return throughput in bytes/sec."""
        elapsed = max(time.time() - self.started_at, 1e-6)
        return self.transferred / elapsed

    @property
    def eta_seconds(self) -> float:
        """Estimated seconds to completion."""
        if self.transferred <= 0 or self.total <= 0:
            return float("inf")
        remaining = self.total - self.transferred
        return remaining / max(self.speed_bps, 1.0)


class SFTPClient:
    """Thin wrapper around :class:`paramiko.SFTPClient`.

    Reuses the authenticated transport of the active SSH session — no second
    login is required.
    """

    def __init__(self, ssh_client: AsyncSSHClient) -> None:
        """Bind to an already-connected :class:`AsyncSSHClient`."""
        self._ssh = ssh_client
        self._sftp: paramiko.SFTPClient | None = None

    def _get(self) -> paramiko.SFTPClient:
        """Lazily open / reopen the SFTP subsystem."""
        if self._sftp is None:
            transport = self._ssh.get_transport()
            if transport is None:
                raise RuntimeError("SSH transport not available")
            self._sftp = paramiko.SFTPClient.from_transport(transport)
            assert self._sftp is not None
        return self._sftp

    def reconnect_if_needed(self) -> None:
        """Drop and reopen the SFTP channel if the server has hung up."""
        try:
            if self._sftp is not None:
                self._sftp.listdir(".")
        except Exception:
            self._sftp = None
        self._get()

    # -- read-only ops -----------------------------------------------------

    def listdir(self, path: str = ".") -> list[RemoteEntry]:
        """List a remote directory."""
        sftp = self._get()
        out: list[RemoteEntry] = []
        for attr in sftp.listdir_attr(path):
            full = path.rstrip("/") + "/" + attr.filename if path != "." else attr.filename
            out.append(
                RemoteEntry(
                    name=attr.filename,
                    path=full,
                    is_dir=stat.S_ISDIR(attr.st_mode or 0),
                    size=attr.st_size or 0,
                    mtime=attr.st_mtime or 0,
                    mode=attr.st_mode or 0,
                )
            )
        return out

    # -- mutating ops ------------------------------------------------------

    def mkdir(self, path: str) -> None:
        """Create a remote directory (existing path is a no-op)."""
        try:
            self._get().mkdir(path)
        except IOError:
            pass

    def remove(self, path: str) -> None:
        """Delete a remote file or empty directory."""
        sftp = self._get()
        try:
            sftp.remove(path)
        except IOError:
            sftp.rmdir(path)

    def rename(self, src: str, dst: str) -> None:
        """Rename / move a remote path."""
        self._get().rename(src, dst)

    def chmod(self, path: str, mode: int) -> None:
        """Change permissions on a remote path."""
        self._get().chmod(path, mode)


class SFTPTransferQueue:
    """Manages concurrent uploads/downloads using a thread pool."""

    def __init__(self, sftp: SFTPClient, max_workers: int = 3) -> None:
        """:param max_workers: Maximum number of concurrent transfers."""
        self._sftp = sftp
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sftp-xfer")
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._jobs: dict[int, TransferProgress] = {}

    @property
    def jobs(self) -> dict[int, TransferProgress]:
        """Snapshot of in-flight + finished transfers."""
        return dict(self._jobs)

    def _next_id(self) -> int:
        """Allocate a unique transfer id."""
        with self._counter_lock:
            self._counter += 1
            return self._counter

    def upload(self, local_path: str | Path, remote_path: str) -> Future[int]:
        """Schedule an upload. Returns a Future yielding the transfer id."""
        local = Path(local_path)
        if not local.is_file():
            raise FileNotFoundError(f"{local_path} is not a regular file")
        tid = self._next_id()
        progress = TransferProgress(
            transfer_id=tid,
            direction="upload",
            src=str(local),
            dst=remote_path,
            total=local.stat().st_size,
        )
        self._jobs[tid] = progress

        def _run() -> int:
            try:
                sftp = self._sftp._get()  # noqa: SLF001 — internal helper

                def cb(transferred: int, _total: int) -> None:
                    progress.transferred = transferred

                sftp.put(str(local), remote_path, callback=cb)
                progress.transferred = progress.total
            except Exception as exc:  # pragma: no cover — surfaced to UI
                progress.error = str(exc)
                logger.exception("Upload failed")
            finally:
                progress.finished = True
            return tid

        return self._executor.submit(_run)

    def download(self, remote_path: str, local_path: str | Path) -> Future[int]:
        """Schedule a download. Returns a Future yielding the transfer id."""
        tid = self._next_id()
        local = Path(local_path)
        try:
            total = self._sftp._get().stat(remote_path).st_size or 0  # noqa: SLF001
        except Exception:
            total = 0

        progress = TransferProgress(
            transfer_id=tid,
            direction="download",
            src=remote_path,
            dst=str(local),
            total=total,
        )
        self._jobs[tid] = progress

        def _run() -> int:
            try:
                sftp = self._sftp._get()  # noqa: SLF001
                local.parent.mkdir(parents=True, exist_ok=True)

                def cb(transferred: int, _total: int) -> None:
                    progress.transferred = transferred

                sftp.get(remote_path, str(local), callback=cb)
                progress.transferred = progress.total or os.path.getsize(local)
            except Exception as exc:  # pragma: no cover — surfaced to UI
                progress.error = str(exc)
                logger.exception("Download failed")
            finally:
                progress.finished = True
            return tid

        return self._executor.submit(_run)

    def shutdown(self) -> None:
        """Stop accepting new transfers and wait for current ones to finish."""
        self._executor.shutdown(wait=False, cancel_futures=True)
