"""Tests for the runtime logging setup."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from romulus.app import (
    DEFAULT_LOG_DIR,
    DEFAULT_LOG_PATH,
    INSTALL_DIR,
    LogFileLockedError,
    _log_file_is_locked,
    set_log_level,
    setup_logging,
)


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


# ---------------------------------------------------------------------------
# Install-dir-relative log path
# ---------------------------------------------------------------------------


def test_default_log_path_lives_under_install_dir() -> None:
    """DEFAULT_LOG_PATH points at <install_dir>/logs/romulus.log."""
    assert DEFAULT_LOG_PATH == DEFAULT_LOG_DIR / "romulus.log"
    assert DEFAULT_LOG_DIR == INSTALL_DIR / "logs"


def test_install_dir_is_project_root_in_editable_install() -> None:
    """In a dev clone (this test run), install dir must be the repo root.

    Verified by asserting that pyproject.toml lives directly inside it.
    """
    assert (INSTALL_DIR / "pyproject.toml").is_file()


# ---------------------------------------------------------------------------
# set_log_level runtime adjustment
# ---------------------------------------------------------------------------


def test_set_log_level_changes_root_level(tmp_path: Path) -> None:
    setup_logging(tmp_path / "romulus.log")
    set_log_level("WARNING")
    assert logging.getLogger().level == logging.WARNING
    set_log_level("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_set_log_level_keeps_httpcore_quiet(tmp_path: Path) -> None:
    """Switching to DEBUG via Settings must NOT re-enable httpcore noise."""
    setup_logging(tmp_path / "romulus.log")
    set_log_level("DEBUG")
    assert logging.getLogger("httpcore").level == logging.INFO


def test_set_log_level_falls_back_to_info_for_unknown(tmp_path: Path) -> None:
    setup_logging(tmp_path / "romulus.log")
    set_log_level("NONSENSE")
    assert logging.getLogger().level == logging.INFO


def test_set_log_level_falls_back_to_info_for_empty(tmp_path: Path) -> None:
    setup_logging(tmp_path / "romulus.log")
    set_log_level("")
    assert logging.getLogger().level == logging.INFO


# ---------------------------------------------------------------------------
# Log-file lock detection (Windows-only failure mode the helper guards against)
# ---------------------------------------------------------------------------


def test_log_file_is_locked_returns_false_for_missing_file(tmp_path: Path) -> None:
    """Missing files are 'not locked' — RotatingFileHandler creates them."""
    assert _log_file_is_locked(tmp_path / "does-not-exist.log") is False


def test_log_file_is_locked_returns_false_for_writable_file(
    tmp_path: Path,
) -> None:
    """A file we own and nothing else holds open probes as unlocked."""
    target = tmp_path / "romulus.log"
    target.write_text("seed", encoding="utf-8")
    assert _log_file_is_locked(target) is False


def test_log_file_is_locked_returns_true_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate the Windows "file held by another process" condition.

    POSIX renames of open files always succeed, so the real-world
    locking behaviour can't be reproduced cross-platform here; we
    monkeypatch ``os.rename`` to raise the same error the Windows
    kernel would throw.
    """
    import os

    target = tmp_path / "romulus.log"
    target.write_text("seed", encoding="utf-8")

    def fake_rename(src: str, dst: str) -> None:
        raise PermissionError(32, "fake")

    monkeypatch.setattr(os, "rename", fake_rename)
    assert _log_file_is_locked(target) is True


def test_setup_logging_raises_when_log_file_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The custom error must replace the silent PermissionError-on-rotate."""
    import os

    target = tmp_path / "romulus.log"
    target.write_text("seed", encoding="utf-8")

    def fake_rename(src: str, dst: str) -> None:
        raise PermissionError(32, "fake")

    monkeypatch.setattr(os, "rename", fake_rename)
    with pytest.raises(LogFileLockedError):
        setup_logging(target)


def test_setup_logging_passes_through_other_oserrors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-locking OS errors during the rename probe should NOT mask as locked."""
    import os

    target = tmp_path / "romulus.log"
    target.write_text("seed", encoding="utf-8")

    def fake_rename(src: str, dst: str) -> None:
        raise OSError(30, "read-only fs")

    monkeypatch.setattr(os, "rename", fake_rename)
    # Probe returns False; setup_logging proceeds normally (the
    # underlying handler construction would then raise its own error).
    assert _log_file_is_locked(target) is False
