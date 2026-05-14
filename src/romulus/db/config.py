"""Application configuration manager.

The `config` table stores all user-facing settings as key-value pairs. Complex
values (lists, paths) are JSON-encoded; callers that need structured data
should decode the returned string themselves, or use a helper layer.

Defaults are seeded once on first run via `seed_defaults`. Subsequent runs
preserve whatever the user has set via the Settings dialog.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_CONFIG: dict[str, str] = {
    "library_path": "",
    "dat_paths": json.dumps(["data/dats"]),
    "cover_cache_path": str(Path.home() / ".romulus" / "covers"),
    "screenscraper_username": "",
    "screenscraper_password": "",
    "theme": "system",
    "default_view": "table",
    "scan_threads": "8",
    "last_scan_type": "",
    "last_scan_time": "",
}


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the stored value for `key`, or None if unset."""
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a config entry. Always commits."""
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_all_config(conn: sqlite3.Connection) -> dict[str, str]:
    """Return every config entry as a plain dict."""
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {row[0]: row[1] for row in rows}


def seed_defaults(conn: sqlite3.Connection) -> int:
    """Insert any missing default config entries; return rows added.

    Existing values are preserved — this is intended to backfill new defaults
    after an upgrade, not to overwrite user settings.
    """
    cursor = conn.cursor()
    inserted = 0
    for key, value in DEFAULT_CONFIG.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted
