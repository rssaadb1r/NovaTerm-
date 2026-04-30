"""Smoke tests for ``core.sftp_client`` (no real network required)."""
from __future__ import annotations

from core.sftp_client import TransferProgress


def test_transfer_progress_arithmetic() -> None:
    p = TransferProgress(transfer_id=1, direction="upload", src="a", dst="b", total=100)
    p.transferred = 25
    assert 24 <= p.percent <= 26
    p.transferred = 100
    p.finished = True
    assert p.percent == 100.0
    # speed_bps is non-negative
    assert p.speed_bps >= 0
