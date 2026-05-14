"""Tests for database layer — schema, config, connection."""

from __future__ import annotations

import sqlite3
import stat
import sys

import pytest

from romulus.db import (
    DEFAULT_CONFIG,
    create_tables,
    get_all_config,
    get_config,
    get_connection,
    seed_defaults,
    set_config,
)


def test_get_connection_creates_db_file(tmp_path):
    db_path = tmp_path / "subdir" / "romulus.db"
    conn = get_connection(db_path)
    try:
        assert db_path.exists()
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_get_connection_enables_foreign_keys(tmp_path):
    conn = get_connection(tmp_path / "romulus.db")
    try:
        result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert result == 1
    finally:
        conn.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: NTFS ACLs are inherited, not set via chmod",
)
def test_get_connection_restricts_db_file_permissions(tmp_path):
    """The DB stores ScreenScraper credentials — keep it owner-only on POSIX."""
    db_path = tmp_path / "romulus.db"
    conn = get_connection(db_path)
    try:
        mode = stat.S_IMODE(db_path.stat().st_mode)
        # Owner-only — no group or world read/write/execute bits.
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
        assert mode & stat.S_IRUSR
    finally:
        conn.close()


EXPECTED_TABLES = {
    "config",
    "systems",
    "roms",
    "hashes",
    "dat_entries",
    "games",
    "metadata",
    "covers",
    "collections",
    "collection_games",
    "scan_history",
    "organize_plans",
}


def test_create_tables_creates_all_tables(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {row[0] for row in rows}
    assert EXPECTED_TABLES.issubset(names)


def test_create_tables_is_idempotent(db):
    # Calling a second time on the same connection must not raise.
    create_tables(db)
    create_tables(db)


def test_seed_defaults_populates_every_key(db):
    inserted = seed_defaults(db)
    assert inserted == len(DEFAULT_CONFIG)
    stored = get_all_config(db)
    assert set(stored.keys()) == set(DEFAULT_CONFIG.keys())


def test_seed_defaults_is_idempotent(db):
    seed_defaults(db)
    second = seed_defaults(db)
    assert second == 0


def test_seed_defaults_does_not_overwrite_user_values(db):
    set_config(db, "theme", "dark")
    seed_defaults(db)
    assert get_config(db, "theme") == "dark"


def test_set_and_get_config(db):
    set_config(db, "library_path", "/roms")
    assert get_config(db, "library_path") == "/roms"


def test_set_config_upserts(db):
    set_config(db, "theme", "light")
    set_config(db, "theme", "dark")
    assert get_config(db, "theme") == "dark"


def test_get_config_returns_none_for_missing_key(db):
    assert get_config(db, "no_such_key") is None


def test_foreign_keys_enforced_for_roms(db):
    # Inserting a ROM with a non-existent system_id should raise.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO roms (path, filename, extension, size_bytes, mtime, system_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("/x/y.sfc", "y.sfc", ".sfc", 1024, 0.0, "nonexistent_system"),
        )
        db.commit()
