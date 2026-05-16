"""Application configuration manager.

The `config` table stores all user-facing settings as key-value pairs. Complex
values (lists, paths) are JSON-encoded; callers that need structured data
should decode the returned string themselves, or use a helper layer.

Defaults are seeded once on first run via `seed_defaults`. Subsequent runs
preserve whatever the user has set via the Settings dialog.

Path defaults (``cover_cache_path``, ``dat_paths``) are computed lazily from
the resolved data and install directories so portable-ZIP launches under
``C:\\Tools\\Romulus\\`` and dev runs from the repo both pick sensible
locations without the user having to edit settings on first launch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from romulus.db.connection import (
    _resolve_data_dir,
    _resolve_install_dir,
)


def _default_cover_cache_dir() -> Path:
    """``<data_dir>/covers/`` — created on first cover fetch."""
    return _resolve_data_dir() / "covers"


def _default_dat_paths() -> list[str]:
    """Prefer ``<install_dir>/dats`` (portable layout) then dev fallback.

    The portable build seeds DATs into ``<install_dir>/dats/`` on first
    launch. Dev clones keep them at the legacy ``data/dats/`` path beside
    the repo, so we list that too — the DAT loader walks every entry.
    """
    install_dir = _resolve_install_dir()
    candidates: list[str] = []
    portable = install_dir / "dats"
    legacy = install_dir / "data" / "dats"
    candidates.append(str(portable))
    if legacy != portable:
        candidates.append(str(legacy))
    return candidates


#: Default on-disk location for cached cover-art images. Single source of
#: truth shared between :data:`DEFAULT_CONFIG` (seeded on first run) and the
#: metadata module's fallback in :func:`romulus.metadata._resolve_cache_dir`.
DEFAULT_COVER_CACHE_DIR: Path = _default_cover_cache_dir()

DEFAULT_CONFIG: dict[str, str] = {
    "library_path": "",
    "dat_paths": json.dumps(_default_dat_paths()),
    "cover_cache_path": str(DEFAULT_COVER_CACHE_DIR),
    "screenscraper_username": "",
    "screenscraper_password": "",
    "theme": "system",
    "default_view": "table",
    "scan_threads": "8",
    "log_level": "INFO",
    "last_scan_type": "",
    "last_scan_time": "",
}


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the stored value for `key`, or None if unset."""
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


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
    return {row["key"]: row["value"] for row in rows}


def seed_defaults(conn: sqlite3.Connection) -> int:
    """Insert any missing default config entries; return rows added.

    Existing values are preserved — this is intended to backfill new defaults
    after an upgrade, not to overwrite user settings.
    """
    inserted = 0
    for key, value in DEFAULT_CONFIG.items():
        inserted += conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        ).rowcount
    conn.commit()
    return inserted
