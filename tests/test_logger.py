"""Unit test for ``core.logger``."""
from __future__ import annotations

from pathlib import Path

from core.logger import SessionLogger


def test_session_logger_writes(tmp_path: Path) -> None:
    template = str(tmp_path / "%SESSION%-%DATE%.log")
    with SessionLogger(session_name="mybox", path_template=template) as logger:
        logger.write("hello\n")
        logger.write("world\n")
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    text = files[0].read_text()
    assert "hello" in text and "world" in text


def test_session_logger_overwrite_mode(tmp_path: Path) -> None:
    template = str(tmp_path / "x.log")
    with SessionLogger(session_name="m", path_template=template, mode="overwrite") as logger:
        logger.write("first\n")
    with SessionLogger(session_name="m", path_template=template, mode="overwrite") as logger:
        logger.write("second\n")
    text = (tmp_path / "x.log").read_text()
    assert "first" not in text
    assert "second" in text
