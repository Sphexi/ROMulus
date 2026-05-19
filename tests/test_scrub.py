"""Tests for the reverse-direction library scrub (``core/scrub.py``).

The scrub walks every row in ``roms`` and classifies mismatches against
disk into four buckets:

* ``missing_unflagged`` — file gone, missing=0
* ``outside_root`` — row.library_root != current root
* ``flagged_but_present`` — file present, missing=1
* ``drift`` — size/mtime drift between stored and disk

Apply runs per-bucket SAVEPOINTs so a failure in one bucket doesn't
roll back the others. These tests cover the analyse classification +
the apply behaviour for each bucket, plus the unreadable-stat
no-action guard and the bucket-isolation property of the SAVEPOINTs.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from romulus.core.scanner import scan_library
from romulus.core.scrub import (
    ScrubPlan,
    analyse,
    apply_plan,
)
from romulus.db import queries as q

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rom(tmp_path: Path, system: str, name: str, size: int = 1024) -> Path:
    system_dir = tmp_path / system
    system_dir.mkdir(parents=True, exist_ok=True)
    rom_path = system_dir / name
    rom_path.write_bytes(b"X" * size)
    return rom_path


def _seed_row(conn, *, path: str, size: int = 1024, mtime: float | None = None,
              library_root: str | None = None, missing: int = 0) -> int:
    """Insert a roms row directly so we can pre-configure mismatch states."""
    rom_id = q.upsert_rom(
        conn,
        {
            "path": path,
            "filename": Path(path).name,
            "extension": Path(path).suffix,
            "size_bytes": size,
            "mtime": mtime if mtime is not None else time.time(),
            "system_id": "snes",
            "library_root": library_root,
        },
    )
    if missing:
        conn.execute("UPDATE roms SET missing = 1 WHERE id = ?", (rom_id,))
    conn.commit()
    return rom_id


# ---------------------------------------------------------------------------
# analyse() classification
# ---------------------------------------------------------------------------


class TestAnalyseClassification:
    def test_all_present_and_matching_yields_empty_plan(self, seeded_db, tmp_path):
        """Happy path — no mismatches, no actions."""
        _make_rom(tmp_path, "snes", "Mario.sfc")
        scan_library(seeded_db, tmp_path)

        plan = analyse(seeded_db, str(tmp_path.resolve()))

        assert isinstance(plan, ScrubPlan)
        assert plan.rows_scanned == 1
        assert plan.rows_unreadable == 0
        assert plan.actions == []

    def test_missing_unflagged_bucket(self, seeded_db, tmp_path):
        """File on disk gone but row still says missing=0."""
        rom = _make_rom(tmp_path, "snes", "Vanished.sfc")
        scan_library(seeded_db, tmp_path)
        # Delete the file but DON'T re-scan, so the row stays missing=0.
        rom.unlink()

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        assert len(plan.actions) == 1
        assert plan.actions[0].status == "missing_unflagged"
        assert plan.actions[0].filename == "Vanished.sfc"

    def test_outside_root_bucket(self, seeded_db, tmp_path):
        """A row whose library_root != current root is outside_root regardless of disk state."""
        other_root = tmp_path / "other_library"
        other_root.mkdir()
        current_root = tmp_path / "current_library"
        current_root.mkdir()
        # Seed a row that claims to belong to "other_library".
        _seed_row(
            seeded_db,
            path=str(other_root / "Foreign.sfc"),
            library_root=str(other_root.resolve()),
        )

        plan = analyse(seeded_db, str(current_root.resolve()))
        statuses = [a.status for a in plan.actions]
        assert "outside_root" in statuses

    def test_flagged_but_present_bucket(self, seeded_db, tmp_path):
        """Row with missing=1 but file is actually on disk."""
        rom = _make_rom(tmp_path, "snes", "BackFromTheDead.sfc")
        rom_id = _seed_row(
            seeded_db,
            path=str(rom),
            size=rom.stat().st_size,
            mtime=rom.stat().st_mtime,
            library_root=str(tmp_path.resolve()),
            missing=1,
        )

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        # Should classify as flagged_but_present.
        matching = [
            a
            for a in plan.actions
            if a.rom_id == rom_id and a.status == "flagged_but_present"
        ]
        assert len(matching) == 1

    def test_drift_bucket_size_change(self, seeded_db, tmp_path):
        """File present at the stored path but size has changed."""
        rom = _make_rom(tmp_path, "snes", "Drift.sfc", size=1024)
        rom_id = _seed_row(
            seeded_db,
            path=str(rom),
            size=512,  # stored size is smaller than disk
            mtime=rom.stat().st_mtime,
            library_root=str(tmp_path.resolve()),
        )

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        drift_actions = [a for a in plan.actions if a.status == "drift"]
        assert len(drift_actions) == 1
        assert drift_actions[0].rom_id == rom_id
        assert drift_actions[0].current_size == 1024
        assert drift_actions[0].stored_size == 512

    def test_drift_tolerance_for_close_mtimes(self, seeded_db, tmp_path):
        """Mtime within the 2s tolerance must NOT be classified as drift."""
        rom = _make_rom(tmp_path, "snes", "Steady.sfc", size=1024)
        disk_mtime = rom.stat().st_mtime
        _seed_row(
            seeded_db,
            path=str(rom),
            size=1024,
            mtime=disk_mtime + 1.0,  # within tolerance
            library_root=str(tmp_path.resolve()),
        )

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        assert all(a.status != "drift" for a in plan.actions)

    def test_unreadable_row_not_classified_missing(self, seeded_db, tmp_path):
        """Stat that raises PermissionError must NOT auto-flag missing.

        This is the SMB-share-offline footgun: a transient network
        hiccup would wrongly tombstone every row on the share. The
        scrub records the unreadable count but emits no action.
        """
        # Seed a row pointing at a path that exists on disk but mock the
        # stat to raise PermissionError.
        rom = _make_rom(tmp_path, "snes", "Locked.sfc")
        _seed_row(
            seeded_db,
            path=str(rom),
            library_root=str(tmp_path.resolve()),
        )

        original_stat = Path.stat

        def _raising_stat(self, *args, **kwargs):
            if self.name == "Locked.sfc":
                raise PermissionError("simulated ACL denial")
            return original_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _raising_stat):
            plan = analyse(seeded_db, str(tmp_path.resolve()))

        assert plan.rows_unreadable >= 1
        # No action should mention Locked.sfc.
        assert not any(a.filename == "Locked.sfc" for a in plan.actions)


# ---------------------------------------------------------------------------
# apply_plan() per-bucket behaviour
# ---------------------------------------------------------------------------


class TestApplyPerBucket:
    def test_missing_unflagged_sets_missing_to_one(self, seeded_db, tmp_path):
        rom = _make_rom(tmp_path, "snes", "Goner.sfc")
        scan_library(seeded_db, tmp_path)
        rom.unlink()

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        summary = apply_plan(seeded_db, plan.actions)

        assert summary.flagged_missing == 1
        assert q.count_missing_roms(seeded_db) == 1

    def test_flagged_but_present_clears_missing(self, seeded_db, tmp_path):
        rom = _make_rom(tmp_path, "snes", "Returned.sfc")
        rom_id = _seed_row(
            seeded_db,
            path=str(rom),
            size=rom.stat().st_size,
            mtime=rom.stat().st_mtime,
            library_root=str(tmp_path.resolve()),
            missing=1,
        )

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        approved = [a for a in plan.actions if a.rom_id == rom_id]
        summary = apply_plan(seeded_db, approved)

        assert summary.untombstoned == 1
        row = seeded_db.execute(
            "SELECT missing FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["missing"] == 0

    def test_outside_root_deletes_row_and_dependents(self, seeded_db, tmp_path):
        """An outside-root delete must drop the row + its hashes + dest_inventory."""
        other_root = tmp_path / "other"
        other_root.mkdir()
        rom_id = _seed_row(
            seeded_db,
            path=str(other_root / "Stale.sfc"),
            library_root=str(other_root.resolve()),
        )
        seeded_db.execute(
            "INSERT INTO hashes (rom_id, sha1, crc32, hashed_at) "
            "VALUES (?, ?, ?, ?)",
            (rom_id, "0" * 40, "deadbeef", 0.0),
        )
        seeded_db.commit()

        current_root = tmp_path / "current"
        current_root.mkdir()
        plan = analyse(seeded_db, str(current_root.resolve()))
        summary = apply_plan(seeded_db, plan.actions)

        assert summary.deleted_outside_root == 1
        assert seeded_db.execute(
            "SELECT 1 FROM roms WHERE id = ?", (rom_id,)
        ).fetchone() is None
        assert seeded_db.execute(
            "SELECT 1 FROM hashes WHERE rom_id = ?", (rom_id,)
        ).fetchone() is None

    def test_drift_clears_hash_and_updates_stat(self, seeded_db, tmp_path):
        """Drift fix-up must invalidate the cached hash + update size/mtime."""
        rom = _make_rom(tmp_path, "snes", "Drifted.sfc", size=2048)
        rom_id = _seed_row(
            seeded_db,
            path=str(rom),
            size=1024,  # stored size != disk size
            mtime=rom.stat().st_mtime - 10.0,
            library_root=str(tmp_path.resolve()),
        )
        # Pre-existing cached hash that's no longer valid for the new contents.
        seeded_db.execute(
            "INSERT INTO hashes (rom_id, sha1, crc32, hashed_at) "
            "VALUES (?, ?, ?, ?)",
            (rom_id, "1" * 40, "cafef00d", 0.0),
        )
        seeded_db.commit()

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        drift_actions = [a for a in plan.actions if a.status == "drift"]
        summary = apply_plan(seeded_db, drift_actions)

        assert summary.drift_fixed == 1
        row = seeded_db.execute(
            "SELECT size_bytes FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["size_bytes"] == 2048
        assert seeded_db.execute(
            "SELECT 1 FROM hashes WHERE rom_id = ?", (rom_id,)
        ).fetchone() is None

    def test_apply_empty_actions_is_noop(self, seeded_db):
        """Calling apply_plan with no actions must not touch the DB."""
        summary = apply_plan(seeded_db, [])
        assert summary.flagged_missing == 0
        assert summary.deleted_outside_root == 0
        assert summary.untombstoned == 0
        assert summary.drift_fixed == 0
        assert summary.errors == []


# ---------------------------------------------------------------------------
# Per-bucket SAVEPOINT isolation
# ---------------------------------------------------------------------------


class TestBucketIsolation:
    def test_failure_in_one_bucket_does_not_block_others(
        self, seeded_db, tmp_path, monkeypatch
    ):
        """A raise inside the drift bucket must not roll back missing/un-tombstone work.

        Per-bucket SAVEPOINTs are the whole point of the design: each
        bucket commits independently, so a single misbehaving fix-up
        doesn't undo every other approved action.
        """
        # Set up one missing_unflagged row + one drift row.
        gone = _make_rom(tmp_path, "snes", "Gone.sfc")
        _make_rom(tmp_path, "snes", "WillDrift.sfc", size=2048)
        scan_library(seeded_db, tmp_path)
        gone.unlink()
        # Make the drift row drift by overwriting stored size.
        seeded_db.execute(
            "UPDATE roms SET size_bytes = 999 WHERE filename = 'WillDrift.sfc'"
        )
        seeded_db.commit()

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        # Sanity: both buckets are populated.
        statuses = {a.status for a in plan.actions}
        assert "missing_unflagged" in statuses
        assert "drift" in statuses

        # Force the drift apply to raise.
        import romulus.core.scrub as scrub_mod

        def _explode(_conn, _action):
            raise RuntimeError("simulated drift failure")

        monkeypatch.setattr(scrub_mod, "_apply_drift", _explode)

        summary = apply_plan(seeded_db, plan.actions)

        # missing_unflagged must have committed despite the drift failure.
        assert summary.flagged_missing == 1
        assert q.count_missing_roms(seeded_db) >= 1
        # drift bucket failed.
        assert summary.drift_fixed == 0
        assert summary.errors  # at least one bucket reported an error
        # Connection clean (no open txn).
        assert seeded_db.in_transaction is False


# ---------------------------------------------------------------------------
# Progress callback contract
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_analyse_emits_per_row_ticks(self, seeded_db, tmp_path):
        for n in range(5):
            _make_rom(tmp_path, "snes", f"Game{n}.sfc")
        scan_library(seeded_db, tmp_path)

        events: list[tuple[int, int, str]] = []
        analyse(
            seeded_db,
            str(tmp_path.resolve()),
            progress_callback=lambda c, t, n: events.append((c, t, n)),
        )

        assert len(events) == 5
        assert events[-1][0] == 5
        assert events[-1][1] == 5


# ---------------------------------------------------------------------------
# Connection state safety
# ---------------------------------------------------------------------------


class TestConnectionState:
    def test_apply_leaves_no_open_transaction(self, seeded_db, tmp_path):
        rom = _make_rom(tmp_path, "snes", "TxnCheck.sfc")
        scan_library(seeded_db, tmp_path)
        rom.unlink()

        plan = analyse(seeded_db, str(tmp_path.resolve()))
        apply_plan(seeded_db, plan.actions)

        assert seeded_db.in_transaction is False

    def test_apply_failure_leaves_no_open_transaction(
        self, seeded_db, tmp_path, monkeypatch
    ):
        """Even if every bucket raised, the connection must be clean afterwards."""
        # Build a missing_unflagged action.
        rom = _make_rom(tmp_path, "snes", "Boom.sfc")
        scan_library(seeded_db, tmp_path)
        rom.unlink()

        plan = analyse(seeded_db, str(tmp_path.resolve()))

        import romulus.core.scrub as scrub_mod

        def _explode(_conn, _action):
            raise RuntimeError("simulated apply failure")

        monkeypatch.setattr(scrub_mod, "_apply_missing_unflagged", _explode)
        apply_plan(seeded_db, plan.actions)

        assert seeded_db.in_transaction is False


# ---------------------------------------------------------------------------
# delete_roms_by_ids helper
# ---------------------------------------------------------------------------


class TestDeleteRomsByIds:
    """The public helper extracted for the outside_root bucket apply."""

    def test_drops_rows_and_dependents(self, seeded_db, tmp_path):
        rom_id = _seed_row(
            seeded_db,
            path=str(tmp_path / "x.sfc"),
            library_root=str(tmp_path.resolve()),
        )
        seeded_db.execute(
            "INSERT INTO hashes (rom_id, sha1, crc32, hashed_at) "
            "VALUES (?, ?, ?, ?)",
            (rom_id, "0" * 40, "deadbeef", 0.0),
        )
        seeded_db.commit()

        deleted = q.delete_roms_by_ids(seeded_db, [rom_id])
        seeded_db.commit()

        assert deleted == 1
        assert seeded_db.execute(
            "SELECT 1 FROM roms WHERE id = ?", (rom_id,)
        ).fetchone() is None
        assert seeded_db.execute(
            "SELECT 1 FROM hashes WHERE rom_id = ?", (rom_id,)
        ).fetchone() is None

    def test_empty_list_is_noop(self, seeded_db):
        assert q.delete_roms_by_ids(seeded_db, []) == 0


# Silence unused-var lint when tests use a fixture path only for the
# Path() instantiation:
pytestmark = pytest.mark.usefixtures("seeded_db")
