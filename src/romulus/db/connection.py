"""SQLite connection management.

Romulus stores everything in a single SQLite database under `~/.romulus/`.
The connection is configured with WAL mode (for safer concurrent reads while
background workers are writing) and foreign keys (off by default in sqlite3).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_DIR = Path.home() / ".romulus"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "romulus.db"


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and configure) a SQLite connection.

    If `db_path` is omitted, uses `~/.romulus/romulus.db`, creating `~/.romulus/`
    if it does not exist. Always enables WAL mode and foreign-key enforcement.
    `row_factory` is set to `sqlite3.Row` so callers can access columns by name.
    """
    if db_path is None:
        DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DEFAULT_DB_PATH
    else:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
