"""SQLite connection management.

Romulus stores everything in a single SQLite database under `~/.romulus/`.
The connection is configured with WAL mode (for safer concurrent reads while
background workers are writing) and foreign keys (off by default in sqlite3).

The DB also stores ScreenScraper credentials in plaintext (no key management
in the app), so on POSIX we restrict the file permissions to 0o600 to keep
other users on the same machine from reading them. NTFS inherits ACLs from
the parent directory; we do not attempt to tighten Windows ACLs here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = Path.home() / ".romulus"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "romulus.db"


def _restrict_db_permissions(db_path: Path) -> None:
    """On POSIX, chmod the DB (and its WAL/SHM siblings) to owner-only.

    No-op on Windows: NTFS ACLs are inherited from the parent directory and
    `os.chmod` only toggles the read-only bit, which would actively prevent
    sqlite from writing. Failures are logged but not raised — restrictive
    permissions are defense-in-depth, not a hard precondition.
    """
    if sys.platform == "win32":
        return
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path.with_name(db_path.name + suffix)
        if not candidate.exists():
            continue
        try:
            os.chmod(candidate, 0o600)
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            logger.debug("could not chmod %s: %s", candidate, exc)


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and configure) a SQLite connection.

    If `db_path` is omitted, uses `~/.romulus/romulus.db`, creating `~/.romulus/`
    if it does not exist. Always enables WAL mode and foreign-key enforcement.
    `row_factory` is set to `sqlite3.Row` so callers can access columns by name.
    On POSIX systems the DB file is also chmod'd to 0o600 (owner-only) because
    it stores ScreenScraper credentials in plaintext.
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
    _restrict_db_permissions(Path(db_path))
    return conn
