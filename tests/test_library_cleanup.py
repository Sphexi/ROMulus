"""Tests for the library-cleanup feature — missing tombstones, library-root
change detection, and the orphan-game prune.

Covers:

* Scanner sweeps deleted-on-disk files to ``missing=1`` rather than dropping
  the row.
* Re-scanning after a reconnect flips ``missing`` back to 0 via the
  path-keyed UPSERT (no duplicates).
* ``count_roms_with_other_library_root`` correctly reports cross-library
  rows so the UI can prompt the user before wiping them.
* ``delete_missing_roms`` + ``prune_orphan_games`` only touch the rows
  they're meant to.
* The schema migration adds the new columns without breaking legacy DBs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from romulus.core.scanner import scan_library
from romulus.db import queries as q
from romulus.db.schema import _migrate_roms_add_library_root_and_missing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rom(tmp_path: Path, system: str, name: str, size: int = 1024) -> Path:
    """Create a fake ROM file under ``tmp_path/<system>/<name>``."""
    system_dir = tmp_path / system
    system_dir.mkdir(parents=True, exist_ok=True)
    rom_path = system_dir / name
    rom_path.write_bytes(b"X" * size)
    return rom_path


# ---------------------------------------------------------------------------
# Scanner missing-sweep behavior
# ---------------------------------------------------------------------------


class TestScannerMissingSweep:
    def test_unchanged_files_stay_present(self, seeded_db, tmp_path):
        """A file scanned twice with no changes should not be flagged missing."""
        _make_rom(tmp_path, "snes", "Game.sfc")
        scan_library(seeded_db, tmp_path)
        scan_library(seeded_db, tmp_path)
        assert q.count_missing_roms(seeded_db) == 0

    def test_deleted_file_flagged_missing(self, seeded_db, tmp_path):
        """A file present in scan 1 and absent in scan 2 should be tombstoned."""
        rom = _make_rom(tmp_path, "snes", "Vanished.sfc")
        scan_library(seeded_db, tmp_path)
        rom.unlink()
        result = scan_library(seeded_db, tmp_path)

        assert result.files_newly_missing == 1
        assert q.count_missing_roms(seeded_db) == 1
        # The row is still there, just tombstoned.
        row = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = ?", ("Vanished.sfc",)
        ).fetchone()
        assert row["missing"] == 1

    def test_reconnect_flips_missing_back_to_false(self, seeded_db, tmp_path):
        """Re-creating the deleted file at the same path should un-tombstone it."""
        rom = _make_rom(tmp_path, "snes", "Roundtrip.sfc")
        scan_library(seeded_db, tmp_path)
        original_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = ?", ("Roundtrip.sfc",)
        ).fetchone()["id"]

        rom.unlink()
        scan_library(seeded_db, tmp_path)
        assert q.count_missing_roms(seeded_db) == 1

        rom.write_bytes(b"X" * 1024)
        scan_library(seeded_db, tmp_path)

        # Same row, just un-tombstoned — no duplicate created.
        rows = seeded_db.execute(
            "SELECT id, missing FROM roms WHERE filename = ?", ("Roundtrip.sfc",)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == original_id
        assert rows[0]["missing"] == 0
        assert q.count_missing_roms(seeded_db) == 0

    def test_library_root_stamped_on_scan(self, seeded_db, tmp_path):
        """Every row scanned gets ``library_root`` populated with the scan root."""
        _make_rom(tmp_path, "snes", "A.sfc")
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT library_root FROM roms WHERE filename = 'A.sfc'"
        ).fetchone()
        # Resolved (absolute) path on both sides.
        assert row["library_root"] == str(tmp_path.resolve())

    def test_missing_files_from_other_root_untouched(
        self, seeded_db, tmp_path
    ):
        """Scanning library A should never flag library B's rows as missing."""
        lib_a = tmp_path / "library_a"
        lib_b = tmp_path / "library_b"
        _make_rom(lib_a, "snes", "InA.sfc")
        _make_rom(lib_b, "snes", "InB.sfc")

        scan_library(seeded_db, lib_a)
        scan_library(seeded_db, lib_b)
        # B's row exists and is healthy.
        assert q.count_missing_roms(seeded_db) == 0

        # Delete A's file and rescan A. B's row must remain present.
        (lib_a / "snes" / "InA.sfc").unlink()
        scan_library(seeded_db, lib_a)
        b_row = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = 'InB.sfc'"
        ).fetchone()
        assert b_row["missing"] == 0


# ---------------------------------------------------------------------------
# Library-root change detection / wipe
# ---------------------------------------------------------------------------


class TestLibraryRootChange:
    def test_count_roms_with_other_root(self, seeded_db, tmp_path):
        lib_a = tmp_path / "library_a"
        lib_b = tmp_path / "library_b"
        _make_rom(lib_a, "snes", "A.sfc")
        _make_rom(lib_b, "snes", "B.sfc")
        scan_library(seeded_db, lib_a)
        scan_library(seeded_db, lib_b)

        # From A's perspective, B is the "other" library and has one row.
        assert (
            q.count_roms_with_other_library_root(
                seeded_db, str(lib_a.resolve())
            )
            == 1
        )
        # NULL library_root rows (legacy / pre-migration) are not counted.
        seeded_db.execute(
            "INSERT INTO roms (path, filename, extension, size_bytes, mtime, "
            "library_root) VALUES (?, ?, ?, ?, ?, NULL)",
            ("/legacy/x.sfc", "x.sfc", ".sfc", 1024, 0.0),
        )
        assert (
            q.count_roms_with_other_library_root(
                seeded_db, str(lib_a.resolve())
            )
            == 1
        )

    def test_delete_roms_with_other_library_root(self, seeded_db, tmp_path):
        lib_a = tmp_path / "library_a"
        lib_b = tmp_path / "library_b"
        _make_rom(lib_a, "snes", "Keeper.sfc")
        _make_rom(lib_b, "snes", "Dropped.sfc")
        scan_library(seeded_db, lib_a)
        scan_library(seeded_db, lib_b)

        deleted = q.delete_roms_with_other_library_root(
            seeded_db, str(lib_a.resolve())
        )
        assert deleted == 1
        remaining = seeded_db.execute(
            "SELECT filename FROM roms"
        ).fetchall()
        assert {r["filename"] for r in remaining} == {"Keeper.sfc"}

    def test_delete_other_root_leaves_null_root_rows_alone(
        self, seeded_db, tmp_path
    ):
        """Legacy rows with library_root=NULL survive a library-switch wipe.

        Those rows haven't been scanned under the new layout yet — they're
        either pre-migration entries or rows from a path the user has since
        unmounted. The user cleans them via a regular scan + Clean Missing,
        not by switching libraries.
        """
        seeded_db.execute(
            "INSERT INTO roms (path, filename, extension, size_bytes, mtime, "
            "library_root) VALUES (?, ?, ?, ?, ?, NULL)",
            ("/legacy/x.sfc", "x.sfc", ".sfc", 1024, 0.0),
        )
        seeded_db.commit()
        q.delete_roms_with_other_library_root(seeded_db, "/some/new/root")
        row = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM roms WHERE library_root IS NULL"
        ).fetchone()
        assert row["n"] == 1


# ---------------------------------------------------------------------------
# Clean Missing + orphan prune
# ---------------------------------------------------------------------------


class TestCleanMissing:
    def test_delete_missing_drops_only_tombstoned_rows(
        self, seeded_db, tmp_path
    ):
        _make_rom(tmp_path, "snes", "Alive.sfc")
        gone = _make_rom(tmp_path, "snes", "Gone.sfc")
        scan_library(seeded_db, tmp_path)
        gone.unlink()
        scan_library(seeded_db, tmp_path)

        deleted = q.delete_missing_roms(seeded_db)
        seeded_db.commit()

        assert deleted == 1
        remaining = seeded_db.execute(
            "SELECT filename FROM roms"
        ).fetchall()
        assert {r["filename"] for r in remaining} == {"Alive.sfc"}

    def test_prune_orphan_games_drops_games_with_no_roms(
        self, seeded_db, tmp_path
    ):
        """A game whose only ROM is deleted should be pruned."""
        rom = _make_rom(tmp_path, "snes", "Solo.sfc")
        scan_library(seeded_db, tmp_path)
        games_before = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM games"
        ).fetchone()["n"]
        assert games_before == 1

        rom.unlink()
        scan_library(seeded_db, tmp_path)
        q.delete_missing_roms(seeded_db)
        pruned = q.prune_orphan_games(seeded_db)
        seeded_db.commit()

        assert pruned == 1
        games_after = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM games"
        ).fetchone()["n"]
        assert games_after == 0

    def test_prune_orphan_games_leaves_referenced_games(
        self, seeded_db, tmp_path
    ):
        _make_rom(tmp_path, "snes", "Alive.sfc")
        scan_library(seeded_db, tmp_path)
        pruned = q.prune_orphan_games(seeded_db)
        assert pruned == 0


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    def test_migration_adds_columns_to_legacy_db(self, tmp_path):
        """A fresh v0.1.0-shaped DB should pick up the new columns on migrate."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Build a legacy roms table — no library_root / missing columns.
        conn.execute(
            """
            CREATE TABLE roms (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                path             TEXT NOT NULL UNIQUE,
                filename         TEXT NOT NULL,
                extension        TEXT NOT NULL,
                size_bytes       INTEGER NOT NULL,
                mtime            REAL NOT NULL,
                system_id        TEXT,
                game_id          INTEGER,
                scan_id          INTEGER,
                fuzzy_key        TEXT,
                header_title     TEXT,
                dat_match        TEXT,
                match_confidence TEXT DEFAULT 'unmatched'
            )
            """
        )
        conn.execute(
            "INSERT INTO roms (path, filename, extension, size_bytes, mtime) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/foo/bar.sfc", "bar.sfc", ".sfc", 1024, 0.0),
        )
        conn.commit()

        _migrate_roms_add_library_root_and_missing(conn)

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(roms)")}
        assert "library_root" in cols
        assert "missing" in cols
        # Existing row backfill: NULL library_root + missing=0.
        row = conn.execute("SELECT library_root, missing FROM roms").fetchone()
        assert row["library_root"] is None
        assert row["missing"] == 0
        conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Running the migration twice on a current-shaped DB should be safe."""
        from romulus.db import create_tables

        db_path = tmp_path / "current.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        create_tables(conn)
        # Run the migration helper again explicitly.
        _migrate_roms_add_library_root_and_missing(conn)
        # No exception raised — columns are still there.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(roms)")}
        assert "library_root" in cols
        assert "missing" in cols
        conn.close()

    def test_create_tables_handles_legacy_db_without_new_columns(
        self, tmp_path
    ):
        """``create_tables`` must NOT crash when running against a pre-v0.3.0 DB.

        Regression test for a real user crash: ``CREATE INDEX ... ON
        roms(library_root)`` in SCHEMA_STATEMENTS ran before the migration
        helper added the column, so existing-DB bootstrap died with
        ``no such column: library_root``. Indexes for the new columns must
        live inside the migration helper, not the schema list.
        """
        from romulus.db import create_tables

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Build a pre-v0.3.0 roms table — no library_root / missing.
        conn.execute(
            """
            CREATE TABLE roms (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                path             TEXT NOT NULL UNIQUE,
                filename         TEXT NOT NULL,
                extension        TEXT NOT NULL,
                size_bytes       INTEGER NOT NULL,
                mtime            REAL NOT NULL,
                system_id        TEXT,
                game_id          INTEGER,
                scan_id          INTEGER,
                fuzzy_key        TEXT,
                header_title     TEXT,
                dat_match        TEXT,
                match_confidence TEXT DEFAULT 'unmatched'
            )
            """
        )
        conn.commit()

        # Must not raise — this is the path that was crashing for the user.
        create_tables(conn)

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(roms)")}
        assert "library_root" in cols
        assert "missing" in cols
        # Both new indexes must exist after the migration ran.
        index_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_roms_library_root" in index_names
        assert "idx_roms_missing" in index_names
        conn.close()


# ---------------------------------------------------------------------------
# upsert_rom resets missing on re-scan
# ---------------------------------------------------------------------------


class TestUpsertResetsMissing:
    def test_upsert_resets_missing_to_zero(self, seeded_db):
        """A previously-tombstoned row gets ``missing=0`` on next upsert."""
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/foo/x.sfc",
                "filename": "x.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": 0.0,
                "system_id": "snes",
                "library_root": "/foo",
            },
        )
        seeded_db.commit()
        # Manually tombstone the row (simulate the scanner sweep).
        seeded_db.execute("UPDATE roms SET missing = 1 WHERE id = ?", (rom_id,))
        seeded_db.commit()
        assert q.count_missing_roms(seeded_db) == 1

        # Re-upsert with the same path — should flip missing back to 0.
        q.upsert_rom(
            seeded_db,
            {
                "path": "/foo/x.sfc",
                "filename": "x.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": 0.0,
                "system_id": "snes",
                "library_root": "/foo",
            },
        )
        seeded_db.commit()
        assert q.count_missing_roms(seeded_db) == 0


# ---------------------------------------------------------------------------
# Logging env-var precedence
# ---------------------------------------------------------------------------


class TestLoggingPrecedence:
    def test_env_var_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``ROMULUS_LOG_LEVEL=DEBUG`` should be picked up by ``setup_logging``."""
        import logging

        from romulus.app import setup_logging

        monkeypatch.setenv("ROMULUS_LOG_LEVEL", "DEBUG")
        log_file = tmp_path / "test.log"
        setup_logging(log_file)
        assert logging.getLogger().level == logging.DEBUG

    def test_explicit_arg_beats_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Caller's explicit ``level_name`` should win over env var."""
        import logging

        from romulus.app import setup_logging

        monkeypatch.setenv("ROMULUS_LOG_LEVEL", "DEBUG")
        log_file = tmp_path / "test.log"
        setup_logging(log_file, level_name="WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_default_when_nothing_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import logging

        from romulus.app import setup_logging

        monkeypatch.delenv("ROMULUS_LOG_LEVEL", raising=False)
        log_file = tmp_path / "test.log"
        setup_logging(log_file)
        assert logging.getLogger().level == logging.INFO
