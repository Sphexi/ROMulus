"""Tests for the library-cleanup feature — missing tombstones, library-root
change detection, and cascade deletes.

Covers:

* Scanner sweeps deleted-on-disk files to ``missing=1`` rather than dropping
  the row.
* Re-scanning after a reconnect flips ``missing`` back to 0 via the
  path-keyed UPSERT (no duplicates).
* ``count_roms_with_other_library_root`` correctly reports cross-library
  rows so the UI can prompt the user before wiping them.
* ``delete_missing_roms`` only touches the rows it should; ON DELETE CASCADE
  on ``metadata``, ``covers``, and ``collection_roms`` cleans up dependents
  automatically in the strict 1:1 model.
* ``delete_rom_by_id`` is the user-initiated single-ROM delete action.

Note: The ``prune_orphan_games`` helper and the ``games`` table were removed
in v0.4.0.  Tests that previously exercised those mechanics are replaced by
cascade-delete tests that verify the same invariants via ON DELETE CASCADE.
Tests in ``test_db.py::TestCascadeDelete`` provide additional coverage.
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
# Clean Missing + cascade deletes (v0.4.0: no orphan-games prune needed)
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

    def test_cascade_clears_metadata_covers_collection_roms(
        self, seeded_db, tmp_path
    ):
        """Deleting a rom row cascades to metadata, covers, and collection_roms.

        In the strict 1:1 model there is no separate games table. The ON
        DELETE CASCADE constraints on metadata, covers, and collection_roms
        replace the old prune_orphan_games helper.
        """
        rom = _make_rom(tmp_path, "snes", "Mario.sfc")
        scan_library(seeded_db, tmp_path)
        rom_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Mario.sfc'"
        ).fetchone()["id"]

        # Attach metadata + cover + collection membership.
        q.upsert_metadata(
            seeded_db,
            rom_id,
            {
                "description": "test desc",
                "genre": "platformer",
                "publisher": "Nintendo",
                "developer": "Nintendo",
                "release_date": "1985-09-13",
                "release_year": 1985,
                "players": "1",
                "rating": "E",
            },
            source="test",
        )
        q.insert_cover(
            seeded_db,
            rom_id,
            cover_type="Named_Boxarts",
            source_url=None,
            local_path="/lib/snes/Mario.png",
        )
        seeded_db.execute(
            "INSERT INTO collections (name) VALUES (?)", ("Test Coll",)
        )
        collection_id = seeded_db.execute(
            "SELECT id FROM collections WHERE name = 'Test Coll'"
        ).fetchone()["id"]
        q.add_rom_to_collection(seeded_db, collection_id, rom_id)
        seeded_db.commit()

        # Sanity: dependents exist.
        assert q.get_metadata(seeded_db, rom_id) is not None
        assert q.get_covers(seeded_db, rom_id)
        assert seeded_db.execute(
            "SELECT 1 FROM collection_roms WHERE rom_id = ?", (rom_id,)
        ).fetchone() is not None

        # Tombstone + delete the rom.
        rom.unlink()
        scan_library(seeded_db, tmp_path)
        q.delete_missing_roms(seeded_db)
        seeded_db.commit()

        # Every dependent row gone via CASCADE.
        assert q.get_metadata(seeded_db, rom_id) is None
        assert q.get_covers(seeded_db, rom_id) == []
        assert seeded_db.execute(
            "SELECT 1 FROM collection_roms WHERE rom_id = ?", (rom_id,)
        ).fetchone() is None

    def test_delete_missing_drops_dependent_hashes(
        self, seeded_db, tmp_path
    ):
        """Deleting a missing rom with a hashes row must NOT crash on the FK.

        ``hashes.rom_id REFERENCES roms(id)`` is enforced connection-wide via
        ``PRAGMA foreign_keys = ON``.
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


# ---------------------------------------------------------------------------
# delete_rom_by_id — user-initiated "Delete this ROM" right-click action
# ---------------------------------------------------------------------------


class TestDeleteRomById:
    """Direct single-rom delete used by the game-table right-click action.

    In the strict 1:1 model each ROM is its own "game" — deleting the
    rom row via delete_rom_by_id cascades metadata/covers/collection_roms
    automatically. hashes and dest_inventory need explicit cleanup first
    (they predate the CASCADE migration).
    """

    def test_delete_rom_removes_row(self, seeded_db) -> None:
        """delete_rom_by_id removes the roms row."""
        import time

        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Solo.sfc",
                "filename": "Solo.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
            },
        )
        seeded_db.commit()

        assert q.delete_rom_by_id(seeded_db, rom_id) is True
        assert seeded_db.execute(
            "SELECT 1 FROM roms WHERE id = ?", (rom_id,)
        ).fetchone() is None

    def test_delete_rom_cascades_metadata_and_covers(self, seeded_db) -> None:
        """ON DELETE CASCADE must clean metadata and covers rows."""
        import time

        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/WithMeta.sfc",
                "filename": "WithMeta.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
            },
        )
        q.upsert_metadata(
            seeded_db,
            rom_id,
            {"description": "test", "genre": "action"},
            source="test",
        )
        q.insert_cover(
            seeded_db,
            rom_id,
            cover_type="Named_Boxarts",
            source_url=None,
            local_path="/lib/snes/WithMeta.png",
        )
        seeded_db.commit()

        q.delete_rom_by_id(seeded_db, rom_id)
        seeded_db.commit()

        assert q.get_metadata(seeded_db, rom_id) is None
        assert q.get_covers(seeded_db, rom_id) == []

    def test_drops_hash_dependent(self, seeded_db) -> None:
        """FK-dependent rows in ``hashes`` must be cleared first.

        Without ``_delete_rom_dependents`` the DELETE on ``roms`` raises
        ``IntegrityError: FOREIGN KEY constraint failed`` because
        ``PRAGMA foreign_keys = ON`` is enabled connection-wide.
        """
        import time

        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Hashed.sfc",
                "filename": "Hashed.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
            },
        )
        q.upsert_hash(
            seeded_db,
            rom_id,
            crc32="deadbeef",
            sha1="a" * 40,
            md5=None,
        )
        seeded_db.commit()

        assert q.delete_rom_by_id(seeded_db, rom_id) is True
        assert seeded_db.execute(
            "SELECT 1 FROM hashes WHERE rom_id = ?", (rom_id,)
        ).fetchone() is None

    def test_returns_false_when_id_unknown(self, seeded_db) -> None:
        """A no-op delete on a nonexistent id reports it cleanly."""
        assert q.delete_rom_by_id(seeded_db, 999_999) is False


class TestGetRomPath:
    """The lookup ``Reveal in Explorer`` / ``Delete this ROM`` use to
    resolve a rom id to its on-disk path before any FS action.
    """

    def test_returns_stored_path(self, seeded_db) -> None:
        import time

        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Mario.sfc",
                "filename": "Mario.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
            },
        )
        assert q.get_rom_path(seeded_db, rom_id) == "/lib/snes/Mario.sfc"

    def test_returns_none_for_unknown_id(self, seeded_db) -> None:
        assert q.get_rom_path(seeded_db, 999_999) is None


# ---------------------------------------------------------------------------
# Scoped scan — sidebar right-click "Quick Scan <system>"
# ---------------------------------------------------------------------------


class TestScopedQuickScan:
    """``scan_library(scope_system_id=...)`` walks the same library but only
    enrols / tombstones within the chosen system. Other systems' rows must
    be left strictly alone.
    """

    def test_scope_only_enrols_matching_system(
        self, seeded_db, tmp_path
    ) -> None:
        """A scoped scan must not insert roms for other systems."""
        _make_rom(tmp_path, "snes", "Mario.sfc")
        _make_rom(tmp_path, "megadrive", "Sonic.md")

        scan_library(seeded_db, tmp_path, scope_system_id="snes")

        rows = seeded_db.execute(
            "SELECT filename, system_id FROM roms"
        ).fetchall()
        assert {r["system_id"] for r in rows} == {"snes"}
        assert {r["filename"] for r in rows} == {"Mario.sfc"}

    def test_scope_does_not_tombstone_other_systems(
        self, seeded_db, tmp_path
    ) -> None:
        """A scoped rescan that finds nothing must NOT mark NES roms missing.

        Regression guard: the old library-wide sweep would tombstone
        every row not visited by the current walk, so a sidebar
        right-click "Quick Scan: atari7800" rescan against a library
        with no 7800 ROMs would silently wipe every other system.
        """
        import time

        # Seed an NES row directly so it predates the scan we're about
        # to run; the scoped scan shouldn't touch it.
        nes_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/nes/Zelda.nes",
                "filename": "Zelda.nes",
                "extension": ".nes",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "nes",
            },
        )
        seeded_db.commit()

        # Empty library on disk under a *different* system scope.
        scan_library(seeded_db, tmp_path, scope_system_id="atari7800")

        nes_row = seeded_db.execute(
            "SELECT missing FROM roms WHERE id = ?", (nes_id,)
        ).fetchone()
        assert nes_row["missing"] == 0, (
            "Scoped scan must not tombstone rows outside its scope"
        )

    def test_scope_tombstones_only_missing_rows_within_scope(
        self, seeded_db, tmp_path
    ) -> None:
        """When a scoped rescan misses a row IN-scope, that row tombstones.

        Two SNES roms exist; one is removed from disk; the scoped scan
        should tombstone the missing one and leave the present one.
        """
        rom1 = _make_rom(tmp_path, "snes", "Present.sfc")
        rom2 = _make_rom(tmp_path, "snes", "Gone.sfc")
        scan_library(seeded_db, tmp_path)
        # Confirm both enrolled.
        assert seeded_db.execute(
            "SELECT COUNT(*) FROM roms WHERE missing = 0"
        ).fetchone()[0] == 2

        rom2.unlink()  # noqa: F841
        scan_library(seeded_db, tmp_path, scope_system_id="snes")

        present = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = ?", ("Present.sfc",)
        ).fetchone()
        gone = seeded_db.execute(
            "SELECT missing FROM roms WHERE filename = ?", ("Gone.sfc",)
        ).fetchone()
        assert present["missing"] == 0
        assert gone["missing"] == 1
        assert str(rom1)  # silence unused-var lint


class TestScannerPostWalkProgressMessages:
    """The scanner emits progress events at phase transitions so the UI
    dialog can show ``Marking missing entries...`` / ``Finalising scan
    history...`` instead of a frozen Cancel button while the post-walk DB
    work runs.

    Note: "Linking ROMs to games" was removed in v0.4.0 (no games table).
    """

    def test_emits_phase_labels_after_walk(
        self, seeded_db, tmp_path
    ) -> None:
        _make_rom(tmp_path, "snes", "Mario.sfc")

        events: list[tuple[int, str]] = []
        scan_library(
            seeded_db,
            tmp_path,
            progress_callback=lambda c, name: events.append((c, name)),
        )

        labels = [name for _, name in events]
        assert "Marking missing entries…" in labels
        assert "Finalising scan history…" in labels
        # v0.4.0: no "Grouping ROMs into games" / "Linking ROMs to games" phase.
        assert not any("games" in lbl.lower() for lbl in labels)


# ---------------------------------------------------------------------------
# Clean Missing — rollback safety + worker
# ---------------------------------------------------------------------------


class TestCleanMissingRollbackOnException:
    """Regression for ``KNOWN-ISSUES.md`` 2026-05-18: an exception raised
    during the cleanup chain must not leave an open transaction behind.

    In the strict 1:1 model the worker no longer calls prune_orphan_games.
    These tests verify the rollback contract still holds if any step raises.
    """

    def _seed_missing_rows(self, conn, count: int) -> None:
        """Insert ``count`` rows already flagged ``missing = 1``."""
        import time

        for i in range(count):
            rom_id = q.upsert_rom(
                conn,
                {
                    "path": f"/lib/snes/Ghost_{i:04d}.sfc",
                    "filename": f"Ghost_{i:04d}.sfc",
                    "extension": ".sfc",
                    "size_bytes": 1024,
                    "mtime": time.time(),
                    "system_id": "snes",
                },
            )
            conn.execute("UPDATE roms SET missing = 1 WHERE id = ?", (rom_id,))
        conn.commit()

    def test_rollback_clears_open_transaction(
        self, seeded_db, monkeypatch
    ) -> None:
        """Failure inside delete_missing_roms must leave conn.in_transaction = False.

        Forces delete_missing_roms to raise after the dependent-row cleanup
        but before commit. Verifies the worker's rollback guard fires and the
        seeded missing rows are still present (delete rolled back).
        """
        self._seed_missing_rows(seeded_db, count=10)

        from romulus.ui.workers import CleanMissingWorker

        # Force a raise inside the worker's transaction window.
        def _boom(*_a, **_kw):  # noqa: ANN001
            raise RuntimeError("simulated delete failure")

        monkeypatch.setattr(
            "romulus.ui.workers.q.delete_missing_roms", _boom
        )

        worker = CleanMissingWorker(":memory:")
        with pytest.raises(RuntimeError, match="simulated"):
            worker._run_work(seeded_db)

        assert seeded_db.in_transaction is False, (
            "rollback should have closed the implicit transaction"
        )
        # Deletes must have been rolled back — every seeded missing row still present.
        remaining = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM roms WHERE missing = 1"
        ).fetchone()["n"]
        assert remaining == 10, (
            "delete should have been rolled back — caller saw an exception"
        )

    def test_success_path_commits(self, seeded_db) -> None:
        """Happy path: no exception → conn commits → in_transaction False."""
        self._seed_missing_rows(seeded_db, count=5)

        from romulus.ui.workers import CleanMissingWorker

        worker = CleanMissingWorker(":memory:")
        worker._run_work(seeded_db)

        assert seeded_db.in_transaction is False
        assert q.count_missing_roms(seeded_db) == 0


class TestCleanMissingWorkerSmoke:
    """End-to-end QThread smoke test for CleanMissingWorker."""

    def test_worker_finishes_with_correct_counts(
        self, qapp, tmp_path
    ) -> None:
        """A real CleanMissingWorker thread should emit finished_ok with
        the deleted-rom count, then terminate cleanly.

        In the strict 1:1 model finished_ok(deleted_roms, 0) — there is no
        separate games table to prune.
        """
        import time

        from PySide6.QtCore import QEventLoop

        from romulus.db import create_tables, get_connection
        from romulus.models import seed_systems
        from romulus.ui.workers import CleanMissingWorker

        db_path = tmp_path / "clean.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)

        # Seed a missing rom.
        rom_id = q.upsert_rom(
            conn,
            {
                "path": "/lib/snes/Ghost.sfc",
                "filename": "Ghost.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
            },
        )
        conn.execute("UPDATE roms SET missing = 1 WHERE id = ?", (rom_id,))
        conn.commit()
        conn.close()

        worker = CleanMissingWorker(db_path)
        finished: list[tuple[int, int]] = []
        failed: list[str] = []
        worker.finished_ok.connect(lambda d, p: finished.append((d, p)))
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()
        assert worker.wait(5000), "CleanMissingWorker did not finish in 5s"

        assert not failed, f"unexpected failure: {failed}"
        # In the 1:1 model pruned_games is always 0.
        assert finished == [(1, 0)], (
            f"expected one deleted rom + zero pruned games, got {finished}"
        )

        # Verify the deletes actually committed on disk.
        verify = get_connection(db_path)
        try:
            roms_left = verify.execute(
                "SELECT COUNT(*) AS n FROM roms WHERE missing = 1"
            ).fetchone()["n"]
        finally:
            verify.close()
        assert roms_left == 0

    def test_worker_zero_missing_emits_zero_counts(
        self, qapp, tmp_path
    ) -> None:
        """An empty cleanup (no missing rows) should still emit finished_ok(0, 0).

        Guards against a regression where an early-exit returned without
        signalling — the dialog would then hang on the determinate bar
        forever.
        """
        from PySide6.QtCore import QEventLoop

        from romulus.db import create_tables, get_connection
        from romulus.models import seed_systems
        from romulus.ui.workers import CleanMissingWorker

        db_path = tmp_path / "empty.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        conn.close()

        worker = CleanMissingWorker(db_path)
        finished: list[tuple[int, int]] = []
        worker.finished_ok.connect(lambda d, p: finished.append((d, p)))

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()
        assert worker.wait(5000)

        assert finished == [(0, 0)]
