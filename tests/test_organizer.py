"""Tests for library organizer — merge, rename, dedup, collision detection.

Filesystem actions exercise real directories under ``tmp_path`` and inspect
both the on-disk state and the SQLite rows. Atomic-write behaviour is verified
by monkeypatching ``os.replace`` to raise mid-run; subsequent unrelated
actions must still apply and the DB must stay consistent with the disk.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from romulus.core import organizer
from romulus.core.organizer import (
    ACTION_COLLISION,
    ACTION_DELETE_DUPLICATE,
    ACTION_MERGE_FOLDER,
    ACTION_RENAME,
    OrganizeAction,
    OrganizePlan,
    analyze_library,
    detect_collisions,
    execute_plan,
    find_alias_merges,
    find_cross_extension_dupes,
    find_duplicates,
    find_renameable_roms,
)
from romulus.db import queries as q

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_rom(
    conn: sqlite3.Connection,
    *,
    path: str,
    system_id: str,
    extension: str | None = None,
    size_bytes: int = 1024,
    match_confidence: str = "fuzzy",
    dat_match: str | None = None,
    game_id: int | None = None,
) -> int:
    """Insert a ROM row and return its id."""
    filename = path.rsplit("/", 1)[-1]
    ext = extension or ("." + filename.rsplit(".", 1)[-1])
    rom_id = q.upsert_rom(
        conn,
        {
            "path": path,
            "filename": filename,
            "extension": ext,
            "size_bytes": size_bytes,
            "mtime": time.time(),
            "system_id": system_id,
            "fuzzy_key": filename.lower(),
            "match_confidence": match_confidence,
            "dat_match": dat_match,
        },
    )
    if game_id is not None:
        q.link_rom_to_game(conn, rom_id, game_id)
    conn.commit()
    return rom_id


def _insert_hash(
    conn: sqlite3.Connection, rom_id: int, sha1: str, crc32: str = "abc12345"
) -> None:
    q.upsert_hash(conn, rom_id, crc32=crc32, sha1=sha1, md5=None)
    conn.commit()


def _make_file(path: Path, content: bytes = b"rom-bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------
# Detection: alias merges
# ---------------------------------------------------------------------------


class TestFindAliasMerges:
    def test_detects_genesis_alias_of_megadrive(self, seeded_db) -> None:
        _insert_rom(seeded_db, path="/lib/megadrive/Sonic.md", system_id="megadrive")
        _insert_rom(seeded_db, path="/lib/genesis/Streets.md", system_id="megadrive")
        actions = find_alias_merges(seeded_db)
        assert len(actions) == 1
        assert actions[0].kind == ACTION_MERGE_FOLDER
        assert actions[0].source_path == "/lib/genesis"
        assert actions[0].target_path == "/lib/megadrive"

    def test_no_merge_without_canonical_target(self, seeded_db) -> None:
        # No canonical 'megadrive' folder — just two alias folders.
        _insert_rom(seeded_db, path="/lib/genesis/Sonic.md", system_id="megadrive")
        _insert_rom(seeded_db, path="/lib/gen/Sonic2.md", system_id="megadrive")
        # Neither is canonical, so no merge proposal.
        assert find_alias_merges(seeded_db) == []

    def test_canonical_folder_alone_means_no_action(self, seeded_db) -> None:
        _insert_rom(seeded_db, path="/lib/snes/Mario.sfc", system_id="snes")
        _insert_rom(seeded_db, path="/lib/snes/Zelda.sfc", system_id="snes")
        assert find_alias_merges(seeded_db) == []

    def test_ignores_non_alias_folder(self, seeded_db) -> None:
        # 'totallyrandom' isn't an alias of any system; nothing to merge.
        _insert_rom(seeded_db, path="/lib/snes/Mario.sfc", system_id="snes")
        _insert_rom(seeded_db, path="/lib/totallyrandom/Zelda.sfc", system_id="snes")
        assert find_alias_merges(seeded_db) == []


# ---------------------------------------------------------------------------
# Detection: renames
# ---------------------------------------------------------------------------


class TestFindRenameableRoms:
    def test_dat_verified_rom_proposes_rename(self, seeded_db) -> None:
        _insert_rom(
            seeded_db,
            path="/lib/snes/sm.sfc",
            system_id="snes",
            extension=".sfc",
            match_confidence="dat_verified",
            dat_match="Super Mario World (USA)",
        )
        actions = find_renameable_roms(seeded_db)
        assert len(actions) == 1
        action = actions[0]
        assert action.kind == ACTION_RENAME
        assert action.source_path == "/lib/snes/sm.sfc"
        assert action.target_path == "/lib/snes/Super Mario World (USA).sfc"

    def test_fuzzy_match_is_not_renamed(self, seeded_db) -> None:
        _insert_rom(
            seeded_db,
            path="/lib/snes/sm.sfc",
            system_id="snes",
            match_confidence="fuzzy",
            dat_match=None,
        )
        assert find_renameable_roms(seeded_db) == []

    def test_already_canonical_name_no_action(self, seeded_db) -> None:
        _insert_rom(
            seeded_db,
            path="/lib/snes/Super Mario World (USA).sfc",
            system_id="snes",
            extension=".sfc",
            match_confidence="dat_verified",
            dat_match="Super Mario World (USA)",
        )
        assert find_renameable_roms(seeded_db) == []


# ---------------------------------------------------------------------------
# Detection: duplicates
# ---------------------------------------------------------------------------


class TestFindDuplicates:
    def test_same_sha1_two_roms(self, seeded_db) -> None:
        rom_a = _insert_rom(seeded_db, path="/lib/snes/A.sfc", system_id="snes")
        rom_b = _insert_rom(seeded_db, path="/lib/snes/B.smc", system_id="snes")
        _insert_hash(seeded_db, rom_a, sha1="deadbeef" * 5)
        _insert_hash(seeded_db, rom_b, sha1="deadbeef" * 5)
        actions = find_duplicates(seeded_db)
        # One delete action — keeper is .sfc, dupe is .smc.
        assert len(actions) == 1
        assert actions[0].kind == ACTION_DELETE_DUPLICATE
        assert actions[0].source_path.endswith("B.smc")
        assert actions[0].target_path.endswith("A.sfc")

    def test_no_dupes_when_sha1_differs(self, seeded_db) -> None:
        rom_a = _insert_rom(seeded_db, path="/lib/snes/A.sfc", system_id="snes")
        rom_b = _insert_rom(seeded_db, path="/lib/snes/B.sfc", system_id="snes")
        _insert_hash(seeded_db, rom_a, sha1="a" * 40)
        _insert_hash(seeded_db, rom_b, sha1="b" * 40)
        assert find_duplicates(seeded_db) == []

    def test_hack_never_deduped_against_original(self, seeded_db) -> None:
        # Insert the original and a hack game, both with the same SHA-1.
        original_id = q.upsert_game(
            seeded_db,
            {"title": "Mario", "system_id": "snes", "is_hack": False},
        )
        hack_id = q.upsert_game(
            seeded_db,
            {"title": "Mario Hack", "system_id": "snes", "is_hack": True},
        )
        rom_orig = _insert_rom(
            seeded_db,
            path="/lib/snes/Mario.sfc",
            system_id="snes",
            game_id=original_id,
        )
        rom_hack = _insert_rom(
            seeded_db,
            path="/lib/snes/Mario (Hack).sfc",
            system_id="snes",
            game_id=hack_id,
        )
        _insert_hash(seeded_db, rom_orig, sha1="c" * 40)
        _insert_hash(seeded_db, rom_hack, sha1="c" * 40)
        # The hack is filtered out, leaving only the original — no dupe group.
        assert find_duplicates(seeded_db) == []


# ---------------------------------------------------------------------------
# Detection: cross-extension dupes
# ---------------------------------------------------------------------------


class TestFindCrossExtensionDupes:
    def test_sfc_and_smc_in_same_folder(self, seeded_db) -> None:
        game_id = q.upsert_game(
            seeded_db, {"title": "Mario", "system_id": "snes"}
        )
        _insert_rom(
            seeded_db,
            path="/lib/snes/Mario.sfc",
            system_id="snes",
            extension=".sfc",
            game_id=game_id,
        )
        _insert_rom(
            seeded_db,
            path="/lib/snes/Mario.smc",
            system_id="snes",
            extension=".smc",
            game_id=game_id,
        )
        actions = find_cross_extension_dupes(seeded_db)
        assert len(actions) == 1
        assert actions[0].source_path.endswith("Mario.smc")
        assert actions[0].target_path.endswith("Mario.sfc")

    def test_different_folders_not_cross_ext_dupe(self, seeded_db) -> None:
        game_id = q.upsert_game(
            seeded_db, {"title": "Mario", "system_id": "snes"}
        )
        _insert_rom(
            seeded_db,
            path="/lib/snes/Mario.sfc",
            system_id="snes",
            extension=".sfc",
            game_id=game_id,
        )
        _insert_rom(
            seeded_db,
            path="/lib/snes-hacks/Mario.smc",
            system_id="snes",
            extension=".smc",
            game_id=game_id,
        )
        assert find_cross_extension_dupes(seeded_db) == []


# ---------------------------------------------------------------------------
# Detection: collisions
# ---------------------------------------------------------------------------


class TestDetectCollisions:
    def test_two_renames_to_same_target_become_collision(self) -> None:
        a = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=1,
            source_path="/lib/snes/a.sfc",
            target_path="/lib/snes/Mario.sfc",
        )
        b = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=2,
            source_path="/lib/snes/b.sfc",
            target_path="/lib/snes/Mario.sfc",
        )
        result = detect_collisions([a, b])
        kinds = [r.kind for r in result]
        assert ACTION_COLLISION in kinds
        # The two renames were filtered out.
        assert all(r.kind != ACTION_RENAME for r in result)

    def test_no_collision_when_targets_unique(self) -> None:
        a = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=1,
            source_path="/lib/snes/a.sfc",
            target_path="/lib/snes/Mario.sfc",
        )
        b = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=2,
            source_path="/lib/snes/b.sfc",
            target_path="/lib/snes/Zelda.sfc",
        )
        result = detect_collisions([a, b])
        assert all(r.kind == ACTION_RENAME for r in result)


# ---------------------------------------------------------------------------
# Plan analysis (end-to-end)
# ---------------------------------------------------------------------------


class TestAnalyzeLibrary:
    def test_empty_library_returns_empty_plan(self, seeded_db) -> None:
        plan = analyze_library(seeded_db)
        assert plan.actions == []

    def test_plan_round_trips_json(self, seeded_db) -> None:
        _insert_rom(
            seeded_db,
            path="/lib/snes/sm.sfc",
            system_id="snes",
            extension=".sfc",
            match_confidence="dat_verified",
            dat_match="Super Mario World (USA)",
        )
        plan = analyze_library(seeded_db)
        # JSON round-trip is non-empty and parseable.
        text = plan.to_json()
        assert "Super Mario World" in text


# ---------------------------------------------------------------------------
# Execution: rename
# ---------------------------------------------------------------------------


class TestExecuteRename:
    def test_rename_moves_file_and_updates_db(
        self, seeded_db, tmp_path: Path
    ) -> None:
        src = tmp_path / "snes" / "sm.sfc"
        dest = tmp_path / "snes" / "Super Mario World (USA).sfc"
        _make_file(src)
        rom_id = _insert_rom(
            seeded_db,
            path=str(src).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            match_confidence="dat_verified",
            dat_match="Super Mario World (USA)",
        )
        action = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=rom_id,
            source_path=str(src).replace("\\", "/"),
            target_path=str(dest).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary["applied"] == 1
        assert summary["failed"] == 0
        assert not src.exists()
        assert dest.exists()
        row = q.get_rom_by_id(seeded_db, rom_id)
        assert row is not None
        assert row["filename"] == "Super Mario World (USA).sfc"
        assert row["path"] == str(dest).replace("\\", "/")

    def test_rename_to_existing_target_fails_cleanly(
        self, seeded_db, tmp_path: Path
    ) -> None:
        src = tmp_path / "snes" / "sm.sfc"
        dest = tmp_path / "snes" / "Conflict.sfc"
        _make_file(src, content=b"src")
        _make_file(dest, content=b"existing")  # pre-existing different file
        rom_id = _insert_rom(
            seeded_db, path=str(src).replace("\\", "/"), system_id="snes"
        )
        action = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=rom_id,
            source_path=str(src).replace("\\", "/"),
            target_path=str(dest).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary["failed"] == 1
        assert summary["applied"] == 0
        # Both files should still exist with their original contents.
        assert src.read_bytes() == b"src"
        assert dest.read_bytes() == b"existing"


# ---------------------------------------------------------------------------
# Execution: delete_duplicate
# ---------------------------------------------------------------------------


class TestExecuteDeleteDuplicate:
    def test_delete_removes_file_and_row(
        self, seeded_db, tmp_path: Path
    ) -> None:
        dup_path = tmp_path / "snes" / "Mario.smc"
        keeper_path = tmp_path / "snes" / "Mario.sfc"
        _make_file(dup_path)
        _make_file(keeper_path)
        dup_id = _insert_rom(
            seeded_db,
            path=str(dup_path).replace("\\", "/"),
            system_id="snes",
        )
        action = OrganizeAction(
            kind=ACTION_DELETE_DUPLICATE,
            rom_id=dup_id,
            source_path=str(dup_path).replace("\\", "/"),
            target_path=str(keeper_path).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary["applied"] == 1
        assert not dup_path.exists()
        assert keeper_path.exists()
        assert q.get_rom_by_id(seeded_db, dup_id) is None


# ---------------------------------------------------------------------------
# Execution: merge_folder
# ---------------------------------------------------------------------------


class TestExecuteMergeFolder:
    def test_merge_moves_files_and_updates_paths(
        self, seeded_db, tmp_path: Path
    ) -> None:
        alias = tmp_path / "genesis"
        canonical = tmp_path / "megadrive"
        canonical.mkdir()
        _make_file(alias / "Sonic.md")
        _make_file(alias / "Streets.md")
        rom_a = _insert_rom(
            seeded_db,
            path=str(alias / "Sonic.md").replace("\\", "/"),
            system_id="megadrive",
        )
        rom_b = _insert_rom(
            seeded_db,
            path=str(alias / "Streets.md").replace("\\", "/"),
            system_id="megadrive",
        )
        action = OrganizeAction(
            kind=ACTION_MERGE_FOLDER,
            source_path=str(alias).replace("\\", "/"),
            target_path=str(canonical).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary["applied"] == 1
        assert (canonical / "Sonic.md").exists()
        assert (canonical / "Streets.md").exists()
        for rom_id, fname in ((rom_a, "Sonic.md"), (rom_b, "Streets.md")):
            row = q.get_rom_by_id(seeded_db, rom_id)
            assert row is not None
            assert row["path"].endswith(f"megadrive/{fname}")


# ---------------------------------------------------------------------------
# Execution: collision actions are skipped
# ---------------------------------------------------------------------------


class TestExecuteCollisions:
    def test_collision_actions_are_skipped(
        self, seeded_db, tmp_path: Path
    ) -> None:
        # A collision action should not touch the filesystem.
        target = tmp_path / "snes" / "Conflict.sfc"
        _make_file(target, content=b"existing")
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            source_path=str(tmp_path / "snes" / "src.sfc").replace("\\", "/"),
            target_path=str(target).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary["skipped"] == 1
        assert summary["applied"] == 0
        assert target.read_bytes() == b"existing"


# ---------------------------------------------------------------------------
# Atomic write + per-action rollback
# ---------------------------------------------------------------------------


class TestAtomicAndRollback:
    def test_failed_replace_leaves_no_partial_file(
        self, seeded_db, tmp_path: Path, monkeypatch
    ) -> None:
        """If ``os.replace`` raises mid-action, the destination must not exist.

        We force every ``os.replace`` (both the fast path and the fallback's
        final swap) to fail, then verify the destination directory has no
        leftover ``.part`` tempfile or half-written ROM.
        """
        src = tmp_path / "snes" / "sm.sfc"
        dest = tmp_path / "snes" / "Super Mario World (USA).sfc"
        _make_file(src, content=b"original-bytes")
        rom_id = _insert_rom(
            seeded_db, path=str(src).replace("\\", "/"), system_id="snes"
        )
        action = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=rom_id,
            source_path=str(src).replace("\\", "/"),
            target_path=str(dest).replace("\\", "/"),
        )

        def _always_raise(*_a, **_kw) -> None:
            raise OSError("simulated mid-rename failure")

        monkeypatch.setattr(organizer.os, "replace", _always_raise)

        summary = execute_plan(seeded_db, [action])
        assert summary["failed"] == 1
        assert summary["applied"] == 0
        # Source still intact.
        assert src.exists()
        assert src.read_bytes() == b"original-bytes"
        # Destination was never created.
        assert not dest.exists()
        # No half-written .part tempfile left behind.
        leftovers = list(dest.parent.glob(".*.part")) if dest.parent.exists() else []
        assert leftovers == []
        # DB row still points at the original path.
        row = q.get_rom_by_id(seeded_db, rom_id)
        assert row is not None
        assert row["path"] == str(src).replace("\\", "/")

    def test_failure_on_second_action_does_not_abort_subsequent(
        self, seeded_db, tmp_path: Path, monkeypatch
    ) -> None:
        """A mid-plan failure must NOT abort later, unrelated actions.

        Three rename actions, with ``os.replace`` configured to raise on the
        second call. The first and third must still apply; the DB must reflect
        exactly the surviving on-disk state.
        """
        files = {}
        rom_ids = {}
        for stem in ("a", "b", "c"):
            src = tmp_path / "snes" / f"{stem}.sfc"
            dest = tmp_path / "snes" / f"Game{stem.upper()}.sfc"
            _make_file(src)
            files[stem] = (src, dest)
            rom_ids[stem] = _insert_rom(
                seeded_db,
                path=str(src).replace("\\", "/"),
                system_id="snes",
            )

        actions = [
            OrganizeAction(
                kind=ACTION_RENAME,
                rom_id=rom_ids[stem],
                source_path=str(src).replace("\\", "/"),
                target_path=str(dest).replace("\\", "/"),
            )
            for stem, (src, dest) in files.items()
        ]

        real_replace = os.replace

        def flaky_replace(s, d):
            # Fail every replace touching action B's source/dest. The fallback
            # copy-via-tempfile path also calls os.replace, so we filter on the
            # destination path being the .part tempfile-or-final variant.
            if "b.sfc" in str(s) or "GameB.sfc" in str(d):
                raise OSError("simulated second-action failure")
            return real_replace(s, d)

        monkeypatch.setattr(organizer.os, "replace", flaky_replace)

        summary = execute_plan(seeded_db, actions)
        assert summary["applied"] == 2
        assert summary["failed"] == 1
        # First and third actions applied; second left untouched.
        a_src, a_dest = files["a"]
        b_src, b_dest = files["b"]
        c_src, c_dest = files["c"]
        assert not a_src.exists() and a_dest.exists()
        assert b_src.exists() and not b_dest.exists()
        assert not c_src.exists() and c_dest.exists()
        # DB rows in sync with disk: rom_a + rom_c moved, rom_b still original.
        row_a = q.get_rom_by_id(seeded_db, rom_ids["a"])
        row_b = q.get_rom_by_id(seeded_db, rom_ids["b"])
        row_c = q.get_rom_by_id(seeded_db, rom_ids["c"])
        assert row_a is not None and row_a["path"].endswith("GameA.sfc")
        assert row_b is not None and row_b["path"].endswith("b.sfc")
        assert row_c is not None and row_c["path"].endswith("GameC.sfc")


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_callback_fires_once_per_action(
        self, seeded_db, tmp_path: Path
    ) -> None:
        src = tmp_path / "snes" / "x.sfc"
        dest = tmp_path / "snes" / "Y.sfc"
        _make_file(src)
        rom_id = _insert_rom(
            seeded_db, path=str(src).replace("\\", "/"), system_id="snes"
        )
        ticks: list[tuple[int, int, str]] = []
        execute_plan(
            seeded_db,
            [
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=rom_id,
                    source_path=str(src).replace("\\", "/"),
                    target_path=str(dest).replace("\\", "/"),
                )
            ],
            progress_callback=lambda i, total, name: ticks.append((i, total, name)),
        )
        assert ticks == [(1, 1, str(src).replace("\\", "/"))]


# ---------------------------------------------------------------------------
# Query helpers (DB-only checks)
# ---------------------------------------------------------------------------


class TestQueries:
    def test_insert_and_update_organize_plan(self, seeded_db) -> None:
        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                ),
            ]
        )
        plan_id = q.insert_organize_plan(seeded_db, plan.to_json())
        row = seeded_db.execute(
            "SELECT status FROM organize_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"
        q.update_plan_status(seeded_db, plan_id, "applied")
        row = seeded_db.execute(
            "SELECT status FROM organize_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        assert row["status"] == "applied"

    def test_delete_rom_also_removes_hash(self, seeded_db) -> None:
        rom_id = _insert_rom(
            seeded_db, path="/lib/snes/A.sfc", system_id="snes"
        )
        _insert_hash(seeded_db, rom_id, sha1="a" * 40)
        q.delete_rom(seeded_db, rom_id)
        seeded_db.commit()
        assert q.get_rom_by_id(seeded_db, rom_id) is None
        assert q.get_hash(seeded_db, rom_id) is None

    def test_update_rom_path(self, seeded_db) -> None:
        rom_id = _insert_rom(
            seeded_db, path="/lib/snes/old.sfc", system_id="snes"
        )
        q.update_rom_path(seeded_db, rom_id, "/lib/snes/new.sfc", "new.sfc")
        seeded_db.commit()
        row = q.get_rom_by_id(seeded_db, rom_id)
        assert row is not None
        assert row["path"] == "/lib/snes/new.sfc"
        assert row["filename"] == "new.sfc"


# ---------------------------------------------------------------------------
# UI smoke: OrganizePreviewDialog renders without errors.
# ---------------------------------------------------------------------------


class TestOrganizePreviewDialog:
    def test_dialog_renders_with_actions(self, qapp) -> None:
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="DAT-verified name: A",
                ),
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    source_path="/lib/snes/x.sfc",
                    target_path="/lib/snes/Conflict.sfc",
                    reason="two renames target this path",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        # Initial state: rename is checked, collision is not checkable.
        approved = dialog.approved_actions()
        assert len(approved) == 1
        assert approved[0].kind == ACTION_RENAME
        # Deselect all clears checkboxes.
        dialog._on_deselect_all()
        assert dialog.approved_actions() == []
        dialog._on_select_all()
        assert len(dialog.approved_actions()) == 1
        dialog.close()

    def test_empty_plan_disables_apply(self, qapp) -> None:
        from romulus.ui.organize_preview import OrganizePreviewDialog

        dialog = OrganizePreviewDialog(OrganizePlan())
        assert dialog._apply_btn.isEnabled() is False
        dialog.close()


# ---------------------------------------------------------------------------
# Wiring: MainWindow Organize handler concurrency guard
# ---------------------------------------------------------------------------


class TestMainWindowOrganizeGuard:
    def test_organize_guards_against_concurrent_runs(
        self, qapp, seeded_db, monkeypatch
    ) -> None:
        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)

        class _FakeRunning:
            def isRunning(self) -> bool:  # noqa: N802 - QThread API
                return True

        window._organize_worker = _FakeRunning()  # type: ignore[assignment]

        info_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "romulus.ui.main_window.QMessageBox.information",
            lambda *args, **_kw: info_calls.append((args[1], args[2])),
        )
        window._on_organize()
        assert info_calls, "expected a warning when organize is already running"

    def test_close_event_waits_on_running_organize_worker(
        self, qapp, seeded_db
    ) -> None:
        from PySide6.QtGui import QCloseEvent

        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)

        class _FakeWorker:
            def __init__(self) -> None:
                self.cancel_called = False
                self.wait_args: list[int] = []
                self._running = True

            def isRunning(self) -> bool:  # noqa: N802
                return self._running

            def cancel(self) -> None:
                self.cancel_called = True

            def wait(self, msecs: int) -> bool:
                self.wait_args.append(msecs)
                self._running = False
                return True

        fake = _FakeWorker()
        window._organize_worker = fake  # type: ignore[assignment]
        window.closeEvent(QCloseEvent())
        assert fake.cancel_called is True
        assert fake.wait_args == [5000]
