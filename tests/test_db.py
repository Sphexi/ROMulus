"""Tests for database layer — schema, config, connection."""

from __future__ import annotations

import sqlite3
import stat
import sys
import time

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
from romulus.db import queries as q


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


# v0.4.0: games table is gone; collection_games renamed to collection_roms.
EXPECTED_TABLES = {
    "config",
    "systems",
    "roms",
    "hashes",
    "dat_entries",
    "metadata",
    "covers",
    "collections",
    "collection_roms",
    "scan_history",
    "organize_plans",
}

ABSENT_TABLES = {"games", "collection_games"}


def test_create_tables_creates_all_tables(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {row[0] for row in rows}
    assert EXPECTED_TABLES.issubset(names)


def test_create_tables_no_games_table(db):
    """The games table must NOT exist after create_tables() in v0.4.0."""
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {row[0] for row in rows}
    for absent in ABSENT_TABLES:
        assert absent not in names, f"Table '{absent}' should not exist in v0.4.0 schema"


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
        seeded_db, rom_id, "Named_Boxarts", "http://x", "/tmp/c.png"
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


# ---------------------------------------------------------------------------
# Session 13 acceptance-criteria tests
# ---------------------------------------------------------------------------


def test_pragma_foreign_keys_on(tmp_path):
    """Every connection returned by get_connection must have PRAGMA foreign_keys = 1.

    FK cascades on roms.id only fire when foreign_keys is ON. This test
    verifies the pragma is always enabled — a regression here would silently
    break cascade deletes of metadata / covers / collection_roms rows.
    """
    conn = get_connection(tmp_path / "fk_test.db")
    try:
        result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert result == 1, "PRAGMA foreign_keys must be 1 on every connection"
    finally:
        conn.close()


def test_upsert_rom_identity_fields_round_trip(seeded_db):
    """upsert_rom accepts all identity fields; fetching by id returns the same values."""
    rom_id = q.upsert_rom(
        seeded_db,
        {
            "path": "/lib/snes/Zelda.sfc",
            "filename": "Zelda.sfc",
            "extension": ".sfc",
            "size_bytes": 1024,
            "mtime": time.time(),
            "system_id": "snes",
            "title": "The Legend of Zelda: A Link to the Past",
            "canonical_name": "Legend of Zelda, The - A Link to the Past (USA)",
            "region": "USA",
            "revision": "Rev A",
            "is_hack": False,
            "is_homebrew": False,
            "is_bios": False,
        },
    )
    seeded_db.commit()

    row = q.get_rom_by_id(seeded_db, rom_id)
    assert row is not None
    assert row["title"] == "The Legend of Zelda: A Link to the Past"
    assert row["canonical_name"] == "Legend of Zelda, The - A Link to the Past (USA)"
    assert row["region"] == "USA"
    assert row["revision"] == "Rev A"
    assert row["is_hack"] == 0
    assert row["is_homebrew"] == 0
    assert row["is_bios"] == 0


def test_upsert_rom_omitted_identity_fields_preserve_existing(seeded_db):
    """Re-upserting without identity fields must not clobber previously stored values.

    Scenario:
    1. First upsert — supply all identity fields (simulates Heavy Scan result).
    2. Second upsert — supply identity fields again with new values.
    3. Third upsert — omit ALL identity fields (simulates a plain path-refresh
       rescan that only touches mtime/size). The second upsert's values must
       survive unchanged.
    """
    path = "/lib/nes/SMB.nes"
    base: q.RomUpsertData = {
        "path": path,
        "filename": "SMB.nes",
        "extension": ".nes",
        "size_bytes": 40960,
        "mtime": time.time(),
        "system_id": "nes",
    }

    # First upsert — no identity fields yet.
    rom_id = q.upsert_rom(seeded_db, {**base})
    seeded_db.commit()

    # Second upsert — add identity fields.
    q.upsert_rom(
        seeded_db,
        {
            **base,
            "title": "Super Mario Bros.",
            "canonical_name": "Super Mario Bros. (World)",
            "region": "World",
            "revision": None,
            "is_hack": False,
            "is_homebrew": False,
            "is_bios": False,
        },
    )
    seeded_db.commit()

    # Third upsert — omit identity fields entirely (plain rescan).
    q.upsert_rom(
        seeded_db,
        {
            **base,
            "mtime": time.time() + 1,  # simulate file touch
        },
    )
    seeded_db.commit()

    row = q.get_rom_by_id(seeded_db, rom_id)
    assert row is not None, "ROM row must still exist after third upsert"
    assert row["title"] == "Super Mario Bros.", (
        "title must survive a re-upsert that omits identity fields"
    )
    assert row["canonical_name"] == "Super Mario Bros. (World)"
    assert row["region"] == "World"
    assert row["missing"] == 0, "missing flag must be reset to 0 on every upsert"


def test_cascade_delete_clears_dependents(seeded_db):
    """Deleting a roms row must cascade to metadata, covers, and collection_roms."""
    # Insert ROM
    rom_id = q.upsert_rom(
        seeded_db,
        {
            "path": "/lib/gb/Tetris.gb",
            "filename": "Tetris.gb",
            "extension": ".gb",
            "size_bytes": 32768,
            "mtime": time.time(),
            "system_id": "gb",
        },
    )
    seeded_db.commit()

    # Attach metadata
    q.upsert_metadata(
        seeded_db,
        rom_id,
        {"description": "The classic puzzle game", "genre": "Puzzle"},
        source="test",
    )

    # Attach cover
    q.insert_cover(seeded_db, rom_id, "Named_Boxarts", None, "/cache/gb/Tetris.png")

    # Attach collection membership
    coll_id = q.create_collection(seeded_db, "Classics")
    q.add_rom_to_collection(seeded_db, coll_id, rom_id)

    seeded_db.commit()

    # Verify all three dependents exist before deletion
    assert q.get_metadata(seeded_db, rom_id) is not None, "metadata row should exist"
    assert len(q.get_covers(seeded_db, rom_id)) == 1, "cover row should exist"
    assert q.is_rom_in_collection(seeded_db, coll_id, rom_id), "collection link should exist"

    # Delete the ROM row — CASCADE should clean up dependents
    seeded_db.execute("DELETE FROM roms WHERE id = ?", (rom_id,))
    seeded_db.commit()

    # All dependents must be gone
    assert q.get_metadata(seeded_db, rom_id) is None, (
        "metadata row must be CASCADE-deleted when roms row is deleted"
    )
    assert q.get_covers(seeded_db, rom_id) == [], (
        "covers rows must be CASCADE-deleted when roms row is deleted"
    )
    assert not q.is_rom_in_collection(seeded_db, coll_id, rom_id), (
        "collection_roms row must be CASCADE-deleted when roms row is deleted"
    )
