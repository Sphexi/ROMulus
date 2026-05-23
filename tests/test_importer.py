"""Tests for the inbound ROM import engine.

Coverage:

* Plan analysis — new files routed to correct system folders.
* Plan analysis — path / filename / hash dupes correctly classified.
* Plan analysis — refuses to analyse a staging folder inside the library.
* Plan analysis — ``created_systems`` populated for previously-unseen
  systems.
* Plan analysis — zip archive with multiple ROM extensions flagged as
  ``multi_rom_archive``.
* Apply — ``copy`` action atomically writes via :func:`atomic.atomic_copy`
  (monkeypatch contract, same shape as the exporter test).
* Apply — ``move`` action unlinks source only after copy succeeds.
* Apply — ``replace`` action overwrites the existing file atomically and
  updates the existing rom row (no duplicate insert).
* Apply — ``keep_both`` action disambiguates filename and inserts new row.
* Apply — progress callback fan-out + cooperative cancel between actions.
* Apply — ``upsert_rom`` re-uses an existing ``missing=1`` row when the
  target path matches (path-keyed UPSERT contract).
* Apply — SAVEPOINT rollback on a mid-plan failure leaves the DB
  consistent with disk for the rolled-back action.
* Save-plan-as-JSON: round-trip through :meth:`ImportPlan.to_json` →
  :meth:`ImportPlan.from_json` without information loss.
"""

from __future__ import annotations

import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from romulus.core import atomic
from romulus.core.importer import (
    ImportAction,
    ImportCancelled,
    ImportOptions,
    ImportPlan,
    analyse_import,
    apply_plan,
)
from romulus.db import queries as q

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rom_file(path: Path, content: bytes = b"rom-bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _enrol_existing_rom(
    conn: sqlite3.Connection,
    *,
    path: str,
    system_id: str,
    filename: str,
    extension: str,
    size_bytes: int,
    sha1: str | None = None,
) -> int:
    """Insert a ``roms`` row (and optionally a ``hashes`` row) directly.

    Used to stand up "already-in-library" state for dupe-detection tests
    without going through the scanner.
    """
    rom_id = q.upsert_rom(
        conn,
        {
            "path": path,
            "filename": filename,
            "extension": extension,
            "size_bytes": size_bytes,
            "mtime": time.time(),
            "system_id": system_id,
            "fuzzy_key": filename.lower(),
            "match_confidence": "fuzzy",
        },
    )
    if sha1 is not None:
        conn.execute(
            "INSERT OR REPLACE INTO hashes (rom_id, crc32, sha1, md5, hashed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rom_id, "00000000", sha1, "", time.time()),
        )
    conn.commit()
    return rom_id


# ---------------------------------------------------------------------------
# Plan analysis
# ---------------------------------------------------------------------------


class TestAnalyseImport:
    def test_new_files_routed_to_correct_system_folder(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A staging file under ``snes/`` lands at ``<library>/snes/<name>``."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(staging / "snes" / "Game.sfc", content=b"game-bytes")

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        assert len(plan.actions) == 1
        action = plan.actions[0]
        assert action.system_id == "snes"
        assert action.status == "new"
        assert action.target_path == library / "snes" / "Game.sfc"

    def test_extension_fallback_when_no_system_folder(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A bare ROM with an unambiguous extension still resolves via extension.

        ``.nes`` is owned only by NES in the registry; ``.sfc`` is shared
        across snes / satellaview / sufami so it would NOT match this path.
        The :func:`_build_extension_to_system` filter is what enforces that
        — shared extensions stay None and fall through to ``_unsorted``.
        """
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()
        _make_rom_file(staging / "Stray.nes", content=b"stray-bytes")

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        assert len(plan.actions) == 1
        assert plan.actions[0].system_id == "nes"

    def test_unresolvable_file_routes_to_unsorted(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A file with an unknown extension and no system folder → _unsorted/."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()
        _make_rom_file(staging / "Mystery.weird", content=b"???")

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        assert len(plan.actions) == 1
        action = plan.actions[0]
        assert action.system_id is None
        assert action.target_path.parent.name == "_unsorted"

    def test_path_dupe_detection(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """An identical file at the target path is flagged dupe_path + skip."""
        library = tmp_path / "library"
        (library / "snes").mkdir(parents=True)
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        # Identical content + same name at both locations.
        _make_rom_file(staging / "snes" / "Game.sfc", content=b"same-bytes")
        _make_rom_file(library / "snes" / "Game.sfc", content=b"same-bytes")

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        assert len(plan.actions) == 1
        assert plan.actions[0].status == "dupe_path"
        assert plan.actions[0].resolution == "skip"

    def test_filename_dupe_detection_different_content(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Same name + different bytes at target → conflict default skip."""
        library = tmp_path / "library"
        (library / "snes").mkdir(parents=True)
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(staging / "snes" / "Game.sfc", content=b"new-bytes")
        _make_rom_file(
            library / "snes" / "Game.sfc", content=b"different-bytes-here"
        )

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        assert len(plan.actions) == 1
        action = plan.actions[0]
        assert action.status == "dupe_filename"
        assert action.resolution == "skip"
        # The reason text mentions both sizes so the user can see the diff.
        assert "byte" in action.reason

    def test_hash_dupe_detection(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """When heavy_identify is on, a SHA-1 already in the library wins."""
        library = tmp_path / "library"
        (library / "snes").mkdir(parents=True)
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        # Stage a file the library doesn't have at the target path or
        # filename — only the SHA-1 is shared. We compute the hash the
        # same way the importer will (no header rule for our fake bytes).
        content = b"hash-dupe-bytes"
        from romulus.core.hasher import hash_rom

        rom_path = staging / "snes" / "Renamed.sfc"
        _make_rom_file(rom_path, content=content)
        digest = hash_rom(rom_path, None)
        assert digest is not None
        # Enrol a separate rom row with the same SHA-1.
        _enrol_existing_rom(
            seeded_db,
            path=str(library / "snes" / "Original.sfc"),
            system_id="snes",
            filename="Original.sfc",
            extension=".sfc",
            size_bytes=len(content),
            sha1=digest.sha1,
        )

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=True)
        )

        assert len(plan.actions) == 1
        action = plan.actions[0]
        assert action.status == "dupe_hash"
        assert action.resolution == "skip"

    def test_created_systems_populated(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Systems missing on the library side surface in created_systems."""
        library = tmp_path / "library"
        library.mkdir()  # Empty library — no snes folder yet.
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(staging / "snes" / "Game.sfc")

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        assert "snes" in plan.created_systems

    def test_refuses_staging_inside_library(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Importing from inside the library would be a self-recursion footgun."""
        library = tmp_path / "library"
        staging = library / "inbox"
        staging.mkdir(parents=True)

        with pytest.raises(ValueError, match="outside the library root"):
            analyse_import(
                seeded_db, staging, library, ImportOptions(heavy_identify=False)
            )

    def test_multi_rom_zip_flagged(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A zip with >1 ROM extension at top level is badged multi_rom_archive."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        # Make a zip with two .sfc entries.
        zip_path = staging / "snes" / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("First.sfc", b"first-bytes")
            zf.writestr("Second.sfc", b"second-bytes")

        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )

        # Exactly one action for the zip, flagged as multi_rom_archive.
        actions = [a for a in plan.actions if a.source_path == zip_path]
        assert len(actions) == 1
        assert actions[0].status == "multi_rom_archive"
        assert actions[0].resolution == "skip"


# ---------------------------------------------------------------------------
# Plan application — atomic copy + move + replace + keep_both
# ---------------------------------------------------------------------------


def _plan_with_single_copy(
    staging: Path, library: Path
) -> ImportPlan:
    source = staging / "snes" / "Game.sfc"
    target = library / "snes" / "Game.sfc"
    return ImportPlan(
        staging_root=staging,
        library_root=library,
        actions=[
            ImportAction(
                source_path=source,
                target_path=target,
                system_id="snes",
                status="new",
                resolution="copy",
                confidence="fuzzy",
                size_bytes=source.stat().st_size,
            )
        ],
    )


class TestApplyAtomic:
    def test_copy_uses_atomic_copy(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every copy action must route through ``atomic.atomic_copy``."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(staging / "snes" / "Game.sfc", content=b"src-bytes")
        plan = _plan_with_single_copy(staging, library)

        calls: list[tuple[Path, Path]] = []
        original = atomic.atomic_copy

        def _record(src: Path, dst: Path) -> None:
            calls.append((src, dst))
            return original(src, dst)

        monkeypatch.setattr("romulus.core.importer.atomic.atomic_copy", _record)
        summary = apply_plan(seeded_db, plan)

        assert summary.files_imported == 1
        assert calls, "atomic_copy was never called"
        assert (library / "snes" / "Game.sfc").read_bytes() == b"src-bytes"
        # Source intact (copy, not move).
        assert (staging / "snes" / "Game.sfc").exists()

    def test_move_unlinks_source_only_after_copy(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """``move`` removes the source only after a successful copy."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        source = staging / "snes" / "Game.sfc"
        _make_rom_file(source, content=b"move-bytes")
        plan = _plan_with_single_copy(staging, library)
        plan.actions[0].resolution = "move"

        summary = apply_plan(seeded_db, plan)

        assert summary.files_imported == 1
        target = library / "snes" / "Game.sfc"
        assert target.read_bytes() == b"move-bytes"
        assert not source.exists()

    def test_move_failure_mid_copy_leaves_source_intact(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A simulated atomic-copy failure must NOT delete the source."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        source = staging / "snes" / "Game.sfc"
        _make_rom_file(source, content=b"src-bytes")
        plan = _plan_with_single_copy(staging, library)
        plan.actions[0].resolution = "move"

        def _boom(_src: Path, _dst: Path) -> None:
            raise OSError("simulated copy failure")

        monkeypatch.setattr("romulus.core.importer.atomic.atomic_copy", _boom)
        summary = apply_plan(seeded_db, plan)

        assert summary.files_imported == 0
        # The failure was rolled back; source must still be there.
        assert source.exists()
        assert source.read_bytes() == b"src-bytes"
        assert summary.errors

    def test_replace_overwrites_atomically_and_reuses_row(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """``replace`` overwrites the file and the existing rom row is reused."""
        library = tmp_path / "library"
        (library / "snes").mkdir(parents=True)
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        target = library / "snes" / "Game.sfc"
        _make_rom_file(target, content=b"old-bytes")
        _make_rom_file(staging / "snes" / "Game.sfc", content=b"new-bytes")
        # Pre-enrol the existing rom so we can verify the row count stays
        # at 1 after replace.
        existing_rom_id = _enrol_existing_rom(
            seeded_db,
            path=str(target),
            system_id="snes",
            filename="Game.sfc",
            extension=".sfc",
            size_bytes=9,
        )

        plan = _plan_with_single_copy(staging, library)
        plan.actions[0].resolution = "replace"
        plan.actions[0].existing_rom_id = existing_rom_id

        summary = apply_plan(seeded_db, plan)

        assert summary.files_replaced == 1
        # The file has the NEW bytes.
        assert target.read_bytes() == b"new-bytes"
        # Still exactly one rom row at this path (UPSERT, not INSERT).
        rows = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM roms WHERE path = ?", (str(target),)
        ).fetchone()
        assert rows["n"] == 1

    def test_keep_both_disambiguates(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """``keep_both`` renames the import to avoid clobbering."""
        library = tmp_path / "library"
        (library / "snes").mkdir(parents=True)
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(library / "snes" / "Game.sfc", content=b"old-bytes")
        _make_rom_file(staging / "snes" / "Game.sfc", content=b"new-bytes")

        plan = _plan_with_single_copy(staging, library)
        plan.actions[0].resolution = "keep_both"

        summary = apply_plan(seeded_db, plan)

        assert summary.files_kept_both == 1
        # Both files exist; the new one has the disambiguating suffix.
        assert (library / "snes" / "Game.sfc").read_bytes() == b"old-bytes"
        assert (library / "snes" / "Game_imported.sfc").read_bytes() == b"new-bytes"


# ---------------------------------------------------------------------------
# Progress + cooperative cancel + path-keyed un-tombstone + SAVEPOINT rollback
# ---------------------------------------------------------------------------


class TestApplyControl:
    def test_progress_callback_fans_out(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """The progress callback fires once per action with (current, total, name)."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        actions = []
        for i in range(3):
            src = staging / "snes" / f"Game{i}.sfc"
            _make_rom_file(src, content=f"bytes-{i}".encode())
            actions.append(
                ImportAction(
                    source_path=src,
                    target_path=library / "snes" / src.name,
                    system_id="snes",
                    status="new",
                    resolution="copy",
                    confidence="fuzzy",
                    size_bytes=src.stat().st_size,
                )
            )
        plan = ImportPlan(
            staging_root=staging, library_root=library, actions=actions
        )

        seen: list[tuple[int, int, str]] = []

        def _capture(current: int, total: int, label: str) -> None:
            seen.append((current, total, label))

        summary = apply_plan(seeded_db, plan, progress_callback=_capture)

        assert summary.files_imported == 3
        assert len(seen) == 3
        assert all(total == 3 for _c, total, _l in seen)
        assert [c for c, _t, _l in seen] == [1, 2, 3]

    def test_cooperative_cancel_between_actions(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Raising :class:`ImportCancelled` from the callback unwinds the loop."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        actions = []
        for i in range(3):
            src = staging / "snes" / f"Game{i}.sfc"
            _make_rom_file(src, content=f"bytes-{i}".encode())
            actions.append(
                ImportAction(
                    source_path=src,
                    target_path=library / "snes" / src.name,
                    system_id="snes",
                    status="new",
                    resolution="copy",
                    confidence="fuzzy",
                    size_bytes=src.stat().st_size,
                )
            )
        plan = ImportPlan(
            staging_root=staging, library_root=library, actions=actions
        )

        def _cancel_after_one(current: int, _total: int, _label: str) -> None:
            if current >= 2:
                raise ImportCancelled

        with pytest.raises(ImportCancelled):
            apply_plan(seeded_db, plan, progress_callback=_cancel_after_one)

        # The first action got committed; the cancellation fired before
        # the second action's copy ran.
        assert (library / "snes" / "Game0.sfc").exists()
        assert not (library / "snes" / "Game1.sfc").exists()

    def test_upsert_reuses_tombstoned_row(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Importing to a path with a ``missing=1`` row un-tombstones it."""
        library = tmp_path / "library"
        (library / "snes").mkdir(parents=True)
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        target = library / "snes" / "Game.sfc"

        # Pre-create a tombstoned rom row at the exact target path. The
        # importer's upsert_rom call should land on this row and flip
        # missing back to 0 — NOT create a second row.
        rom_id = _enrol_existing_rom(
            seeded_db,
            path=str(target),
            system_id="snes",
            filename="Game.sfc",
            extension=".sfc",
            size_bytes=9,
        )
        seeded_db.execute(
            "UPDATE roms SET missing = 1 WHERE id = ?", (rom_id,)
        )
        seeded_db.commit()

        _make_rom_file(staging / "snes" / "Game.sfc", content=b"fresh-bytes")
        plan = _plan_with_single_copy(staging, library)

        apply_plan(seeded_db, plan)

        # Still exactly one row at this path; missing flipped back to 0.
        rows = seeded_db.execute(
            "SELECT id, missing FROM roms WHERE path = ?", (str(target),)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == rom_id
        assert rows[0]["missing"] == 0

    def test_savepoint_rollback_isolates_failure(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed action rolls back only itself; siblings still succeed."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        ok_src = staging / "snes" / "Ok.sfc"
        bad_src = staging / "snes" / "Bad.sfc"
        _make_rom_file(ok_src, content=b"ok-bytes")
        _make_rom_file(bad_src, content=b"bad-bytes")
        plan = ImportPlan(
            staging_root=staging,
            library_root=library,
            actions=[
                ImportAction(
                    source_path=ok_src,
                    target_path=library / "snes" / "Ok.sfc",
                    system_id="snes",
                    status="new",
                    resolution="copy",
                    confidence="fuzzy",
                    size_bytes=ok_src.stat().st_size,
                ),
                ImportAction(
                    source_path=bad_src,
                    target_path=library / "snes" / "Bad.sfc",
                    system_id="snes",
                    status="new",
                    resolution="copy",
                    confidence="fuzzy",
                    size_bytes=bad_src.stat().st_size,
                ),
            ],
        )

        original = atomic.atomic_copy

        def _selective_fail(src: Path, dst: Path) -> None:
            if src.name == "Bad.sfc":
                raise OSError("simulated copy failure")
            return original(src, dst)

        monkeypatch.setattr(
            "romulus.core.importer.atomic.atomic_copy", _selective_fail
        )
        summary = apply_plan(seeded_db, plan)

        assert summary.files_imported == 1
        assert summary.errors  # the bad one surfaced
        # Good file present, bad file absent.
        assert (library / "snes" / "Ok.sfc").exists()
        assert not (library / "snes" / "Bad.sfc").exists()
        # And the DB has exactly one row for the Ok file.
        rows = seeded_db.execute(
            "SELECT path FROM roms WHERE filename LIKE 'Ok%'"
        ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Plan JSON round-trip
# ---------------------------------------------------------------------------


class TestPlanJson:
    def test_round_trip(self, tmp_path: Path) -> None:
        """Saving the plan as JSON and re-loading it preserves every field."""
        staging = tmp_path / "staging"
        library = tmp_path / "library"
        action = ImportAction(
            source_path=staging / "snes" / "Game.sfc",
            target_path=library / "snes" / "Game.sfc",
            system_id="snes",
            status="new",
            resolution="copy",
            confidence="fuzzy",
            size_bytes=512,
            reason="",
        )
        plan = ImportPlan(
            staging_root=staging,
            library_root=library,
            actions=[action],
            created_systems={"snes"},
            heavy_identify=True,
            total_bytes=512,
        )

        payload = plan.to_json()
        restored = ImportPlan.from_json(payload)

        assert restored.staging_root == staging
        assert restored.library_root == library
        assert restored.created_systems == {"snes"}
        assert restored.heavy_identify is True
        assert restored.total_bytes == 512
        assert len(restored.actions) == 1
        r = restored.actions[0]
        assert r.source_path == action.source_path
        assert r.target_path == action.target_path
        assert r.system_id == "snes"
        assert r.status == "new"
        assert r.resolution == "copy"
        assert r.confidence == "fuzzy"
        assert r.size_bytes == 512

    def test_from_json_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValueError):
            ImportPlan.from_json('{"kind": "something-else", "version": 1}')

    def test_from_json_rejects_unknown_version(self) -> None:
        with pytest.raises(ValueError):
            ImportPlan.from_json(
                '{"kind": "romulus.import_plan", "version": 99, '
                '"staging_root": "/", "library_root": "/"}'
            )


# ---------------------------------------------------------------------------
# Identity fields threaded through upsert_rom during import
# ---------------------------------------------------------------------------


class TestImportIdentityFields:
    def test_apply_writes_title_region_to_rom_row(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """``apply_plan`` must populate title / region / revision on the enrolled row."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(
            staging / "snes" / "Chrono Trigger (USA) (Rev 1).sfc",
            content=b"ct-bytes",
        )
        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )
        assert len(plan.actions) == 1
        apply_plan(seeded_db, plan)

        row = seeded_db.execute(
            "SELECT title, region, revision, is_hack FROM roms "
            "WHERE filename = 'Chrono Trigger (USA) (Rev 1).sfc'"
        ).fetchone()
        assert row is not None
        assert row["title"] == "Chrono Trigger"
        assert row["region"] == "USA"
        assert row["revision"] == "Rev 1"
        assert row["is_hack"] == 0

    def test_apply_sets_is_hack_for_bracket_h_file(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A file with ``[h1]`` in its name must land with ``is_hack=1``."""
        library = tmp_path / "library"
        library.mkdir()
        staging = tmp_path / "staging"
        (staging / "snes").mkdir(parents=True)
        _make_rom_file(
            staging / "snes" / "Super Mario World [h1].sfc",
            content=b"hack-bytes",
        )
        plan = analyse_import(
            seeded_db, staging, library, ImportOptions(heavy_identify=False)
        )
        apply_plan(seeded_db, plan)

        row = seeded_db.execute(
            "SELECT is_hack FROM roms WHERE filename LIKE '%[h1]%'"
        ).fetchone()
        assert row is not None
        assert row["is_hack"] == 1


# ---------------------------------------------------------------------------
# find_rom_by_path / find_rom_by_sha1 queries
# ---------------------------------------------------------------------------


class TestImportQueries:
    def test_find_rom_by_path(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "snes" / "Game.sfc")
        _enrol_existing_rom(
            seeded_db,
            path=path,
            system_id="snes",
            filename="Game.sfc",
            extension=".sfc",
            size_bytes=9,
        )
        assert q.find_rom_by_path(seeded_db, path) is not None
        assert q.find_rom_by_path(seeded_db, path + ".missing") is None

    def test_find_rom_by_sha1(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "snes" / "Game.sfc")
        _enrol_existing_rom(
            seeded_db,
            path=path,
            system_id="snes",
            filename="Game.sfc",
            extension=".sfc",
            size_bytes=9,
            sha1="deadbeef" * 5,
        )
        assert q.find_rom_by_sha1(seeded_db, "deadbeef" * 5) is not None
        assert q.find_rom_by_sha1(seeded_db, "0" * 40) is None
        # Empty hash short-circuits to None without hitting the DB.
        assert q.find_rom_by_sha1(seeded_db, "") is None
