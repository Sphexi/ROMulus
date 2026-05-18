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
  they're meant to, and cascade FK-dependent rows in ``hashes`` and
  ``dest_inventory`` so the delete doesn't fail on integrity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romulus.core.scanner import scan_library
from romulus.db import queries as q

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

    def test_scanning_library_a_flags_library_b_rows_missing(
        self, seeded_db, tmp_path
    ):
        """Single-library design: scanning one library marks ALL other rows missing.

        ROMulus treats one library folder at a time as the source of truth.
        After a Quick Scan, every row not visited during that scan — including
        rows from a previous library the user has switched away from — is
        flagged missing so the user can prune them via Clean Missing Entries.
        Reconnecting / re-scanning the other library un-tombstones its rows
        via the path-keyed UPSERT.
        """
        lib_a = tmp_path / "library_a"
        lib_b = tmp_path / "library_b"
        _make_rom(lib_a, "snes", "InA.sfc")
        _make_rom(lib_b, "snes", "InB.sfc")

        scan_library(seeded_db, lib_a)
        # After scanning A, B doesn't exist in the DB yet — no missing rows.
        assert q.count_missing_roms(seeded_db) == 0

        scan_library(seeded_db, lib_b)
        # Now A's row is missing (single-library: scanning B implicitly
        # tombstones everything else).
        a_row = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = 'InA.sfc'"
        ).fetchone()
        assert a_row["missing"] == 1

        # Re-scanning A un-tombstones it.
        scan_library(seeded_db, lib_a)
        a_row = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = 'InA.sfc'"
        ).fetchone()
        assert a_row["missing"] == 0
        # And now B is missing.
        b_row = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = 'InB.sfc'"
        ).fetchone()
        assert b_row["missing"] == 1


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

    def test_delete_roms_rejects_empty_keep_root(self, seeded_db):
        """Safety: passing ``""`` would delete everything — must raise instead."""
        with pytest.raises(ValueError, match="non-empty"):
            q.delete_roms_with_other_library_root(seeded_db, "")


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

    def test_delete_missing_drops_dependent_hashes(
        self, seeded_db, tmp_path
    ):
        """Regression: deleting a missing rom with a hashes row must NOT crash
        on the FK constraint. ``hashes.rom_id REFERENCES roms(id)`` is enforced
        connection-wide via ``PRAGMA foreign_keys = ON``.
        """
        rom = _make_rom(tmp_path, "snes", "Hashed.sfc")
        scan_library(seeded_db, tmp_path)
        rom_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Hashed.sfc'"
        ).fetchone()["id"]
        # Simulate a Heavy Scan having hashed this file.
        seeded_db.execute(
            "INSERT INTO hashes (rom_id, sha1, crc32, hashed_at) "
            "VALUES (?, ?, ?, ?)",
            (rom_id, "0" * 40, "deadbeef", 0.0),
        )
        seeded_db.commit()

        rom.unlink()
        scan_library(seeded_db, tmp_path)
        deleted = q.delete_missing_roms(seeded_db)
        seeded_db.commit()

        assert deleted == 1
        remaining_hash = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM hashes WHERE rom_id = ?", (rom_id,)
        ).fetchone()
        assert remaining_hash["n"] == 0

    def test_delete_missing_drops_dependent_dest_inventory(
        self, seeded_db, tmp_path
    ):
        """Same FK guard, but for ``dest_inventory.rom_id``.

        Triggered when a user has synced a library to a destination, deleted
        ROM files from disk, scanned (tombstoning the rows), and then runs
        Clean Missing — the dest_inventory rows reference the missing roms
        and would block the delete without the dependent-cleanup step.
        """
        rom = _make_rom(tmp_path, "snes", "Synced.sfc")
        scan_library(seeded_db, tmp_path)
        rom_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Synced.sfc'"
        ).fetchone()["id"]
        # Create a sync destination and an inventory entry pointing at this rom.
        seeded_db.execute(
            "INSERT INTO sync_destinations "
            "(name, target_path, profile_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test-dest", "/some/dest", "batocera", "2026-01-01"),
        )
        dest_id = seeded_db.execute(
            "SELECT id FROM sync_destinations WHERE name = 'test-dest'"
        ).fetchone()["id"]
        seeded_db.execute(
            "INSERT INTO dest_inventory "
            "(dest_id, rel_path, size_bytes, mtime, rom_id, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dest_id, "snes/Synced.sfc", 1024, 0.0, rom_id, "2026-01-01"),
        )
        seeded_db.commit()

        rom.unlink()
        scan_library(seeded_db, tmp_path)
        # Pre-fix this would raise sqlite3.IntegrityError: FOREIGN KEY
        # constraint failed.
        deleted = q.delete_missing_roms(seeded_db)
        seeded_db.commit()

        assert deleted == 1
        remaining_inv = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM dest_inventory WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()
        assert remaining_inv["n"] == 0

    def test_delete_roms_with_other_root_drops_dependents(
        self, seeded_db, tmp_path
    ):
        """The library-switch wipe must also clear FK-dependent rows."""
        lib_a = tmp_path / "library_a"
        lib_b = tmp_path / "library_b"
        _make_rom(lib_a, "snes", "Keeper.sfc")
        _make_rom(lib_b, "snes", "Dropped.sfc")
        scan_library(seeded_db, lib_a)
        scan_library(seeded_db, lib_b)
        dropped_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Dropped.sfc'"
        ).fetchone()["id"]
        seeded_db.execute(
            "INSERT INTO hashes (rom_id, sha1, crc32, hashed_at) "
            "VALUES (?, ?, ?, ?)",
            (dropped_id, "1" * 40, "cafef00d", 0.0),
        )
        seeded_db.commit()

        # Re-scan A so the active library_root is A's path.
        scan_library(seeded_db, lib_a)
        q.delete_roms_with_other_library_root(
            seeded_db, str(lib_a.resolve())
        )
        seeded_db.commit()

        # Dropped row and its hash both gone.
        assert (
            seeded_db.execute(
                "SELECT COUNT(*) AS n FROM roms WHERE filename = 'Dropped.sfc'"
            ).fetchone()["n"]
            == 0
        )
        assert (
            seeded_db.execute(
                "SELECT COUNT(*) AS n FROM hashes WHERE rom_id = ?",
                (dropped_id,),
            ).fetchone()["n"]
            == 0
        )


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


# ---------------------------------------------------------------------------
# mark_missing_under_root — temp-table strategy avoids SQLite param limit
# ---------------------------------------------------------------------------


class TestMarkMissingScalesPastVariableLimit:
    """Quick Scan against >999 ROMs used to trip ``too many SQL variables``.

    The stock Windows Python build pins ``SQLITE_MAX_VARIABLE_NUMBER`` at
    999. The naive ``id NOT IN (?, ?, ?, ...)`` binding allocated one
    placeholder per visited ROM and crashed once libraries crossed that
    threshold. The fixed implementation streams the IDs through a temp
    table so the variable count is bounded by the executemany call.
    """

    def test_visiting_two_thousand_roms_does_not_raise(
        self, seeded_db
    ) -> None:
        """Push 2000 rows in, mark them all as visited, no exception."""
        import time

        # Seed 2000 ROMs spanning two systems so we cross the 999 limit
        # comfortably AND have rows on multiple system ids to make sure
        # the temp-table strategy doesn't accidentally narrow by system.
        rom_ids: list[int] = []
        for i in range(2000):
            system_id = "snes" if i % 2 == 0 else "gb"
            rom_id = q.upsert_rom(
                seeded_db,
                {
                    "path": f"/lib/{system_id}/rom_{i:05d}.bin",
                    "filename": f"rom_{i:05d}.bin",
                    "extension": ".bin",
                    "size_bytes": 1024,
                    "mtime": time.time(),
                    "system_id": system_id,
                },
            )
            rom_ids.append(rom_id)
        seeded_db.commit()

        # Visit every single ROM — nothing should be marked missing.
        flagged = q.mark_missing_under_root(
            seeded_db, library_root="/lib", excluded_rom_ids=set(rom_ids)
        )
        assert flagged == 0
        assert q.count_missing_roms(seeded_db) == 0

    def test_excluded_set_correctly_tombstones_others(
        self, seeded_db
    ) -> None:
        """Half-visited sweep must mark only the unvisited half as missing."""
        import time

        rom_ids: list[int] = []
        for i in range(1500):
            rom_id = q.upsert_rom(
                seeded_db,
                {
                    "path": f"/lib/snes/rom_{i:05d}.bin",
                    "filename": f"rom_{i:05d}.bin",
                    "extension": ".bin",
                    "size_bytes": 1024,
                    "mtime": time.time(),
                    "system_id": "snes",
                },
            )
            rom_ids.append(rom_id)
        seeded_db.commit()

        visited = set(rom_ids[:1000])
        flagged = q.mark_missing_under_root(
            seeded_db, library_root="/lib", excluded_rom_ids=visited
        )
        assert flagged == 500
        assert q.count_missing_roms(seeded_db) == 500
        # The temp table must be dropped at the end of the call so a
        # second invocation can recreate it cleanly.
        flagged_again = q.mark_missing_under_root(
            seeded_db, library_root="/lib", excluded_rom_ids=visited
        )
        # Second pass: the previously-visited 1000 are still missing=0,
        # the previously-flagged 500 are missing=1 so they're filtered
        # out by the ``WHERE missing = 0`` guard.
        assert flagged_again == 0
