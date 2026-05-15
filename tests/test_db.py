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


# ---------------------------------------------------------------------------
# INSERT helpers always return a real lastrowid (type-safety contract).
# ---------------------------------------------------------------------------


def test_insert_helpers_return_nonnull_int(seeded_db):
    """Every public ``upsert_*`` / ``insert_*`` / ``create_*`` helper that is
    annotated ``-> int`` must actually return a positive rowid. The body of
    each helper asserts ``cursor.lastrowid is not None`` — this test exercises
    the happy path so a future refactor that drops the assertion still has a
    safety net against the ``None`` slipping through to the caller.
    """
    import time

    from romulus.db import queries as q

    rom_id = q.upsert_rom(
        seeded_db,
        {
            "path": "/lib/snes/Mario.sfc",
            "filename": "Mario.sfc",
            "extension": ".sfc",
            "size_bytes": 512,
            "mtime": time.time(),
            "system_id": "snes",
        },
    )
    assert isinstance(rom_id, int) and rom_id > 0

    game_id = q.upsert_game(
        seeded_db, {"title": "Mario", "system_id": "snes"}
    )
    assert isinstance(game_id, int) and game_id > 0

    scan_id = q.insert_scan_history(
        seeded_db,
        {"scan_type": "quick", "started_at": "2026-01-01T00:00:00", "root_path": "/lib"},
    )
    assert isinstance(scan_id, int) and scan_id > 0

    fav_id = q.ensure_favorites_collection(seeded_db)
    assert isinstance(fav_id, int) and fav_id > 0
    # Second call must still return an int (existing-row branch).
    assert q.ensure_favorites_collection(seeded_db) == fav_id

    coll_id = q.create_collection(seeded_db, "Test Collection")
    assert isinstance(coll_id, int) and coll_id > 0

    cover_id = q.insert_cover(
        seeded_db, game_id, "Named_Boxarts", "http://x", "/tmp/c.png"
    )
    assert isinstance(cover_id, int) and cover_id > 0

    plan_id = q.insert_organize_plan(seeded_db, '{"actions": []}')
    assert isinstance(plan_id, int) and plan_id > 0


# ---------------------------------------------------------------------------
# CONFIDENCE_RANK single source of truth.
# ---------------------------------------------------------------------------


def test_confidence_rank_is_single_source_of_truth():
    """The Python dict and the SQL CASE used in ``upsert_rom`` must come from
    the same constant — :data:`romulus.db.queries.CONFIDENCE_RANK`.

    The SQL CASE is built from the dict at module-import time via
    :func:`_sql_confidence_case`. This test verifies the build mirrors the
    dict so a future addition like ``"hash_only": 4`` only requires a single
    edit and the SQL stays in sync automatically.
    """
    from romulus.db.queries import (
        _CONFIDENCE_RANK,
        _UPSERT_ROM_CONFIDENCE_CASE,
        CONFIDENCE_RANK,
        _sql_confidence_case,
    )

    # The legacy private name aliases the new public one.
    assert _CONFIDENCE_RANK is CONFIDENCE_RANK

    # Every (name, rank) pair in the dict appears in the generated SQL CASE.
    for name, rank in CONFIDENCE_RANK.items():
        assert f"WHEN '{name}' THEN {rank}" in _UPSERT_ROM_CONFIDENCE_CASE

    # The helper rebuilds against any column expression.
    sample = _sql_confidence_case("roms.match_confidence")
    for name, rank in CONFIDENCE_RANK.items():
        assert f"WHEN '{name}' THEN {rank}" in sample
    assert "ELSE 0" in sample

    # The UI detail panel imports the SAME constant — not a private copy.
    from romulus.ui import detail_panel

    assert detail_panel.CONFIDENCE_RANK is CONFIDENCE_RANK
