"""Application initialization — database setup and main window launch."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog

from romulus.db import (
    DEFAULT_DB_PATH,
    create_tables,
    get_config,
    get_connection,
    seed_defaults,
    set_config,
)
from romulus.db import queries as q
from romulus.models import seed_systems

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_VALID_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


def _resolve_install_dir() -> Path:
    """Best-effort: find the directory where Romulus is installed.

    Three lookup strategies, tried in order:

    1. **PyInstaller-frozen exe** — ``sys.executable``'s parent.
    2. **Editable install / dev clone** — walk up from this module looking
       for ``pyproject.toml``.
    3. **Fallback** — ``~/.romulus`` so logs still land somewhere writable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    cursor = Path(__file__).resolve()
    for candidate in cursor.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.home() / ".romulus"


INSTALL_DIR = _resolve_install_dir()
DEFAULT_LOG_DIR = INSTALL_DIR / "logs"
DEFAULT_LOG_PATH = DEFAULT_LOG_DIR / "romulus.log"

# Third-party loggers whose DEBUG output is rarely useful even when we want
# verbose app logs. ``httpcore`` emits 10+ lines per HTTP request describing
# TCP/TLS internals; ``httpx`` itself stays at INFO and reports the request
# verb + URL + status, which IS useful. Capped at INFO so DEBUG-level ROMULUS
# stays focused on our own code.
_NOISY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "httpcore",
    "urllib3",
    "asyncio",
    "PIL",
)


def setup_logging(log_path: Path | str | None = None) -> Path:
    """Configure root-logger handlers for the desktop app.

    Routes every ``logging.getLogger(...)`` call in the codebase to:

    1. A rotating file at ``~/.romulus/romulus.log`` (5 MB × 3 backups), and
    2. ``stderr`` so a developer running ``python -m romulus`` sees output too.

    Level defaults to ``INFO``; override with the ``ROMULUS_LOG_LEVEL`` env var
    (one of ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``). Idempotent — safe
    to call more than once (existing handlers are removed first).

    Returns the resolved log path so callers can surface it in error dialogs.
    """
    resolved = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)

    level_name = os.environ.get("ROMULUS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    file_handler = RotatingFileHandler(
        str(resolved),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    for noisy in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.INFO)

    return resolved


def set_log_level(level_name: str) -> None:
    """Adjust the root logger's level at runtime.

    Used by Settings → Diagnostics so the user can switch verbosity without
    restarting the app. Unknown level names silently fall back to INFO. The
    noisy third-party loggers remain capped at INFO regardless.
    """
    normalized = (level_name or "").strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        normalized = "INFO"
    logging.getLogger().setLevel(getattr(logging, normalized))
    for noisy in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.INFO)


def initialize_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the app DB, create tables, and seed systems + defaults + favorites."""
    conn = get_connection(db_path)
    create_tables(conn)
    seed_systems(conn)
    seed_defaults(conn)
    q.ensure_favorites_collection(conn)
    return conn


def prompt_for_library_path(parent=None) -> str:
    """Open a folder picker so the user can choose their ROM library."""
    return QFileDialog.getExistingDirectory(
        parent,
        "Select your ROM library folder",
        str(Path.home()),
    )


def ensure_library_path(conn: sqlite3.Connection, parent=None) -> str:
    """Return the saved library_path, or prompt for one and save it.

    Empty string is returned if the user cancels the dialog.
    """
    current = get_config(conn, "library_path") or ""
    if current:
        return current
    chosen = prompt_for_library_path(parent)
    if chosen:
        set_config(conn, "library_path", chosen)
    return chosen


def run() -> int:
    """Bootstrap QApplication, init the DB, and show the main window."""
    log_path = setup_logging()
    logger = logging.getLogger("romulus")
    logger.info("Romulus starting up (log file: %s)", log_path)
    app = QApplication.instance() or QApplication(sys.argv)
    conn = initialize_database(DEFAULT_DB_PATH)
    # Re-apply the user's configured log level now that the DB is up.
    configured = get_config(conn, "log_level") or "INFO"
    set_log_level(configured)
    # Late import is INTENTIONAL: ``MainWindow`` (and the chain of Qt widget
    # classes it pulls in) requires a live QApplication to exist before any
    # QWidget subclass is even imported on some Qt builds. Moving this back to
    # the top of the file regresses headless startup. Do not "clean up".
    from romulus.ui.main_window import MainWindow

    window = MainWindow(conn)
    ensure_library_path(conn, window)
    window.refresh_all()
    window.show()
    return app.exec()
