"""Tests for the runtime logging setup."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from romulus.app import setup_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Restore the root logger between tests so handlers don't leak across."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(original_level)
    for h in original_handlers:
        root.addHandler(h)


def test_setup_logging_creates_log_file(tmp_path: Path) -> None:
    log_path = tmp_path / "romulus.log"
    resolved = setup_logging(log_path)
    logging.getLogger("romulus.test").info("hello world")
    assert resolved == log_path
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "hello world" in content
    assert "romulus.test" in content


def test_setup_logging_creates_parent_dir(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "deeper" / "romulus.log"
    setup_logging(log_path)
    assert log_path.parent.is_dir()
    logging.getLogger("romulus").warning("path test")
    assert log_path.exists()


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    setup_logging(tmp_path / "romulus.log")
    handlers_first = list(logging.getLogger().handlers)
    setup_logging(tmp_path / "romulus.log")
    handlers_second = list(logging.getLogger().handlers)
    assert len(handlers_first) == len(handlers_second) == 2


def test_setup_logging_respects_env_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROMULUS_LOG_LEVEL", "DEBUG")
    setup_logging(tmp_path / "romulus.log")
    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_falls_back_to_info_for_garbage_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROMULUS_LOG_LEVEL", "NONSENSE")
    setup_logging(tmp_path / "romulus.log")
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_uses_rotating_file_handler(tmp_path: Path) -> None:
    setup_logging(tmp_path / "romulus.log")
    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 5 * 1024 * 1024
    assert file_handlers[0].backupCount == 3


def test_setup_logging_silences_httpcore_even_in_debug_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even at DEBUG level, httpcore connection internals must stay at INFO.

    Otherwise every HTTP request floods the log with TCP/TLS handshake noise.
    """
    monkeypatch.setenv("ROMULUS_LOG_LEVEL", "DEBUG")
    setup_logging(tmp_path / "romulus.log")
    assert logging.getLogger().level == logging.DEBUG
    for noisy in ("httpcore", "urllib3", "asyncio", "PIL"):
        assert logging.getLogger(noisy).level == logging.INFO, (
            f"Expected {noisy} logger to be capped at INFO, "
            f"got {logging.getLogger(noisy).level}"
        )
