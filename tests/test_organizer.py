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

from romulus.core import atomic
from romulus.core.organizer import (
    ACTION_COLLISION,
    ACTION_DELETE_DUPLICATE,
    ACTION_DELETE_FILE,
    ACTION_MERGE_FOLDER,
    ACTION_RENAME,
    RESOLUTION_DELETE_SOURCE,
    RESOLUTION_DO_NOTHING,
    RESOLUTION_REPLACE_TARGET,
    OrganizeAction,
    OrganizePlan,
    analyze_library,
    available_resolutions,
    detect_collisions,
    execute_plan,
    find_alias_merges,
    find_duplicates,
    find_renameable_roms,
    resolve_collision,
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
    is_hack: bool = False,
) -> int:
    """Insert a ROM row and return its id.

    Identity fields (``is_hack``) are stored directly on the ``roms`` row in
    the strict 1:1 schema (Session 13). There is no ``games`` table or
    ``game_id`` foreign key.
    """
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
            "is_hack": is_hack,
        },
    )
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
        # Insert the original and a hack ROM with the same SHA-1.
        # Session 13: is_hack lives directly on roms; there is no games table.
        rom_orig = _insert_rom(
            seeded_db,
            path="/lib/snes/Mario.sfc",
            system_id="snes",
            is_hack=False,
        )
        rom_hack = _insert_rom(
            seeded_db,
            path="/lib/snes/Mario (Hack).sfc",
            system_id="snes",
            is_hack=True,
        )
        _insert_hash(seeded_db, rom_orig, sha1="c" * 40)
        _insert_hash(seeded_db, rom_hack, sha1="c" * 40)
        # The hack is filtered out by get_duplicate_groups (is_hack=1 WHERE
        # clause), leaving only the original in the SHA-1 group — no dupe
        # pair, so no delete action is produced.
        assert find_duplicates(seeded_db) == []



# ---------------------------------------------------------------------------
# Detection: collisions
# ---------------------------------------------------------------------------


class TestDetectCollisions:
    def test_two_renames_to_same_target_become_collision(self, seeded_db) -> None:
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
        result = detect_collisions(seeded_db, [a, b])
        kinds = [r.kind for r in result]
        assert ACTION_COLLISION in kinds
        # The two renames were filtered out.
        assert all(r.kind != ACTION_RENAME for r in result)

    def test_no_collision_when_targets_unique(self, seeded_db) -> None:
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
        result = detect_collisions(seeded_db, [a, b])
        assert all(r.kind == ACTION_RENAME for r in result)

    def test_rename_target_occupied_by_existing_rom_becomes_collision(
        self, seeded_db
    ) -> None:
        """Case 3 — rename target matches an un-renamed DB row.

        ROM A at path ``/lib/snes/657 Igo.nes`` is DAT-verified and wants to
        rename to ``/lib/snes/Igo - Kyuu Roban Taikyoku (Japan).nes``.
        ROM B already exists at that exact path with a different SHA-1 and is
        NOT itself being renamed in this plan.

        ``detect_collisions`` must surface an ``ACTION_COLLISION`` for A's
        rename and filter the conflicting rename out of the result.
        """
        # Insert ROM B at the target path of A's rename. Different SHA-1.
        _insert_rom(
            seeded_db,
            path="/lib/snes/Igo - Kyuu Roban Taikyoku (Japan).nes",
            system_id="snes",
            match_confidence="fuzzy",
        )
        # Insert ROM A (the one wanting to rename).
        rom_a_id = _insert_rom(
            seeded_db,
            path="/lib/snes/657 Igo.nes",
            system_id="snes",
            match_confidence="dat_verified",
            dat_match="Igo - Kyuu Roban Taikyoku (Japan)",
        )
        rename_a = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=rom_a_id,
            source_path="/lib/snes/657 Igo.nes",
            target_path="/lib/snes/Igo - Kyuu Roban Taikyoku (Japan).nes",
            reason="DAT-verified name: Igo - Kyuu Roban Taikyoku (Japan)",
        )
        result = detect_collisions(seeded_db, [rename_a])
        # The rename is replaced by a collision.
        assert len(result) == 1
        assert result[0].kind == ACTION_COLLISION
        assert result[0].target_path == "/lib/snes/Igo - Kyuu Roban Taikyoku (Japan).nes"
        assert "occupied" in result[0].reason

    def test_rename_to_own_existing_path_not_a_collision(self, seeded_db) -> None:
        """A ROM renaming to its own current path is already filtered by
        ``find_renameable_roms`` (target_path == source_path guard), but
        even if such an action somehow reaches ``detect_collisions``, it
        should not be flagged — the existing DB row IS the same rom being
        renamed.
        """
        rom_id = _insert_rom(
            seeded_db,
            path="/lib/snes/Mario.sfc",
            system_id="snes",
            match_confidence="dat_verified",
            dat_match="Mario",
        )
        action = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=rom_id,
            source_path="/lib/snes/Mario.sfc",
            target_path="/lib/snes/Mario.sfc",
        )
        result = detect_collisions(seeded_db, [action])
        # target == source: case 2 guard passes (target IN rename_sources and
        # target == group[0].source_path), case 3 guard skips because the
        # existing row's id IS in renamed_rom_ids. No collision.
        assert all(r.kind == ACTION_RENAME for r in result)


class TestDetectCollisionsContentAware:
    """Tier-3 logic: when a rename's target path is occupied by an existing
    un-renamed rom, compare SHA-1s to decide collision vs upgraded dedup."""

    def test_3a_matching_sha1_neither_hack_upgrades_to_delete_duplicate(
        self, seeded_db
    ) -> None:
        """Both sides have the same SHA-1 and neither is a hack. The
        canonical-named target is the keeper; the rename source becomes a
        delete_duplicate. No collision surfaces."""
        target_id = _insert_rom(
            seeded_db,
            path="/lib/nes/Wagyan Land 2 (Japan).nes",
            system_id="nes",
            match_confidence="fuzzy",
        )
        _insert_hash(seeded_db, target_id, sha1="a" * 40)

        source_id = _insert_rom(
            seeded_db,
            path="/lib/nes/108 Wagan Land 2.nes",
            system_id="nes",
            match_confidence="dat_verified",
            dat_match="Wagyan Land 2 (Japan)",
        )
        _insert_hash(seeded_db, source_id, sha1="a" * 40)

        rename = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=source_id,
            source_path="/lib/nes/108 Wagan Land 2.nes",
            target_path="/lib/nes/Wagyan Land 2 (Japan).nes",
        )
        result = detect_collisions(seeded_db, [rename])
        # Rename filtered, no collision row, one delete_duplicate added.
        assert len(result) == 1
        assert result[0].kind == ACTION_DELETE_DUPLICATE
        assert result[0].rom_id == source_id
        assert result[0].source_path == "/lib/nes/108 Wagan Land 2.nes"
        assert result[0].target_path == "/lib/nes/Wagyan Land 2 (Japan).nes"
        assert "SHA-1 matches" in result[0].reason

    def test_3b_different_sha1_neither_hack_is_real_collision(
        self, seeded_db
    ) -> None:
        """Both sides have SHA-1s but they differ. Two different ROMs want
        the same canonical name. Real collision, no auto-upgrade."""
        target_id = _insert_rom(
            seeded_db,
            path="/lib/nes/Mario.nes",
            system_id="nes",
            match_confidence="fuzzy",
        )
        _insert_hash(seeded_db, target_id, sha1="a" * 40)

        source_id = _insert_rom(
            seeded_db,
            path="/lib/nes/123 Mario.nes",
            system_id="nes",
            match_confidence="dat_verified",
            dat_match="Mario",
        )
        _insert_hash(seeded_db, source_id, sha1="b" * 40)

        rename = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=source_id,
            source_path="/lib/nes/123 Mario.nes",
            target_path="/lib/nes/Mario.nes",
        )
        result = detect_collisions(seeded_db, [rename])
        assert len(result) == 1
        assert result[0].kind == ACTION_COLLISION
        assert "different file" in result[0].reason

    def test_3c_target_missing_sha1_is_real_collision(self, seeded_db) -> None:
        """The existing target hasn't been Heavy-Scanned (no SHA-1). We can't
        prove equality, so it's a real collision with a hint to Heavy Scan."""
        _insert_rom(
            seeded_db,
            path="/lib/nes/Mario.nes",
            system_id="nes",
            match_confidence="fuzzy",
        )  # no hash row
        source_id = _insert_rom(
            seeded_db,
            path="/lib/nes/123 Mario.nes",
            system_id="nes",
            match_confidence="dat_verified",
            dat_match="Mario",
        )
        _insert_hash(seeded_db, source_id, sha1="a" * 40)
        rename = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=source_id,
            source_path="/lib/nes/123 Mario.nes",
            target_path="/lib/nes/Mario.nes",
        )
        result = detect_collisions(seeded_db, [rename])
        assert len(result) == 1
        assert result[0].kind == ACTION_COLLISION
        assert "Heavy Scan" in result[0].reason

    def test_3d_hack_on_target_does_not_auto_upgrade(self, seeded_db) -> None:
        """Even with matching SHA-1, a hack on either side must never be
        auto-converted to delete_duplicate (design rule #8: hacks are
        first-class artifacts). Surface as a collision instead."""
        target_id = _insert_rom(
            seeded_db,
            path="/lib/nes/Mario.nes",
            system_id="nes",
            match_confidence="fuzzy",
            is_hack=True,
        )
        _insert_hash(seeded_db, target_id, sha1="a" * 40)
        source_id = _insert_rom(
            seeded_db,
            path="/lib/nes/123 Mario.nes",
            system_id="nes",
            match_confidence="dat_verified",
            dat_match="Mario",
        )
        _insert_hash(seeded_db, source_id, sha1="a" * 40)

        rename = OrganizeAction(
            kind=ACTION_RENAME,
            rom_id=source_id,
            source_path="/lib/nes/123 Mario.nes",
            target_path="/lib/nes/Mario.nes",
        )
        result = detect_collisions(seeded_db, [rename])
        assert len(result) == 1
        assert result[0].kind == ACTION_COLLISION
        assert "hack" in result[0].reason.lower()


class TestAnalyzeLibraryTieredOrdering:
    """Verify dupes run before renames and that roms scheduled for deletion
    don't also get a competing rename action."""

    def test_rom_scheduled_for_dedup_skips_rename(self, seeded_db, tmp_path) -> None:
        """Two DAT-verified roms with identical SHA-1 in the same folder.
        find_duplicates picks one as keeper, the other as dup-to-delete.
        find_renameable_roms must NOT propose a rename for the dup. The
        keeper may still get a rename if its filename differs from its
        dat_match; that's correct."""
        # Keeper: filename matches canonical, so no rename will be proposed.
        keeper_id = _insert_rom(
            seeded_db,
            path=str(tmp_path / "snes" / "Super Mario World.sfc"),
            system_id="snes",
            match_confidence="dat_verified",
            dat_match="Super Mario World",
        )
        _insert_hash(seeded_db, keeper_id, sha1="a" * 40)

        # Dup: longer filename loses the keeper tiebreak; would normally
        # also be a rename candidate (filename != dat_match) but should be
        # suppressed because it's about to be deleted.
        dup_id = _insert_rom(
            seeded_db,
            path=str(tmp_path / "snes" / "Super Mario World (USA) (Rev 1).sfc"),
            system_id="snes",
            match_confidence="dat_verified",
            dat_match="Super Mario World",
        )
        _insert_hash(seeded_db, dup_id, sha1="a" * 40)

        plan = analyze_library(seeded_db)
        # Exactly one delete_duplicate for the dup_id.
        dupe_actions = [a for a in plan.actions if a.kind == ACTION_DELETE_DUPLICATE]
        assert len(dupe_actions) == 1
        assert dupe_actions[0].rom_id == dup_id
        # No rename action targets the dup_id.
        rename_actions = [a for a in plan.actions if a.kind == ACTION_RENAME]
        assert all(a.rom_id != dup_id for a in rename_actions), (
            f"rom scheduled for deletion got a competing rename: {rename_actions}"
        )


# ---------------------------------------------------------------------------
# Collision resolution (user-chosen actions per collision row)
# ---------------------------------------------------------------------------


class TestAvailableResolutions:
    """``available_resolutions`` decides which dropdown options apply to a
    given collision based on what rom IDs the detector was able to capture."""

    def test_case_3_full_options(self) -> None:
        """Case 3 collisions populate both rom_id and target_rom_id, so all
        three resolutions are offered."""
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=1,
            target_rom_id=2,
            source_path="/lib/nes/108 X.nes",
            target_path="/lib/nes/X (Japan).nes",
        )
        opts = available_resolutions(action)
        values = [v for v, _ in opts]
        assert values == [
            RESOLUTION_DO_NOTHING,
            RESOLUTION_DELETE_SOURCE,
            RESOLUTION_REPLACE_TARGET,
        ]

    def test_case_1_or_2_only_do_nothing(self) -> None:
        """Cases 1 and 2 (rename-vs-rename, rename chain) don't have a
        target rom row, so only Do nothing is offered."""
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=1,
            target_rom_id=None,  # case 1 / 2 path
            source_path="/lib/nes/a.nes",
            target_path="/lib/nes/X.nes",
        )
        opts = available_resolutions(action)
        values = [v for v, _ in opts]
        # rom_id present but no target -> delete source IS offered,
        # but replace_target is NOT.
        assert RESOLUTION_DO_NOTHING in values
        assert RESOLUTION_DELETE_SOURCE in values
        assert RESOLUTION_REPLACE_TARGET not in values

    def test_no_source_rom_id_only_do_nothing(self) -> None:
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=None,
            target_rom_id=None,
            source_path="/lib/nes/a.nes",
            target_path="/lib/nes/b.nes",
        )
        opts = available_resolutions(action)
        assert [v for v, _ in opts] == [RESOLUTION_DO_NOTHING]


class TestResolveCollision:
    """``resolve_collision`` maps a user-chosen resolution to concrete
    actions ready for the execute pipeline."""

    def test_do_nothing_returns_empty(self) -> None:
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=1,
            target_rom_id=2,
            source_path="/lib/nes/a.nes",
            target_path="/lib/nes/b.nes",
        )
        assert resolve_collision(action, RESOLUTION_DO_NOTHING) == []

    def test_delete_source_returns_delete_file(self) -> None:
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=42,
            target_rom_id=99,
            source_path="/lib/nes/108 X.nes",
            target_path="/lib/nes/X (Japan).nes",
        )
        result = resolve_collision(action, RESOLUTION_DELETE_SOURCE)
        assert len(result) == 1
        assert result[0].kind == ACTION_DELETE_FILE
        assert result[0].rom_id == 42
        assert result[0].source_path == "/lib/nes/108 X.nes"

    def test_replace_target_returns_delete_then_rename(self) -> None:
        """Delete-target-and-rename-source yields two actions in order:
        the target delete first, then the source rename. The execute loop
        runs sequentially under per-action SAVEPOINTs so the rename's
        dest.exists() check will see the path is free."""
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=42,
            target_rom_id=99,
            source_path="/lib/nes/108 X.nes",
            target_path="/lib/nes/X (Japan).nes",
        )
        result = resolve_collision(action, RESOLUTION_REPLACE_TARGET)
        assert len(result) == 2
        assert result[0].kind == ACTION_DELETE_FILE
        assert result[0].rom_id == 99
        assert result[0].source_path == "/lib/nes/X (Japan).nes"
        assert result[1].kind == ACTION_RENAME
        assert result[1].rom_id == 42
        assert result[1].source_path == "/lib/nes/108 X.nes"
        assert result[1].target_path == "/lib/nes/X (Japan).nes"

    def test_replace_target_without_target_rom_returns_empty(self) -> None:
        """If the collision lacks target_rom_id we can't safely produce a
        delete action; the resolver returns empty so the user is required
        to choose a different option."""
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=42,
            target_rom_id=None,
            source_path="/lib/nes/108 X.nes",
            target_path="/lib/nes/X (Japan).nes",
        )
        assert resolve_collision(action, RESOLUTION_REPLACE_TARGET) == []

    def test_unknown_resolution_treated_as_do_nothing(self) -> None:
        action = OrganizeAction(
            kind=ACTION_COLLISION,
            rom_id=42,
            target_rom_id=99,
            source_path="/lib/nes/108 X.nes",
            target_path="/lib/nes/X (Japan).nes",
        )
        assert resolve_collision(action, "garbage_value") == []


class TestExecuteDeleteFile:
    """``ACTION_DELETE_FILE`` runs an unconditional delete — no TOCTOU."""

    def test_unlinks_file_and_drops_row(self, seeded_db, tmp_path) -> None:
        target_dir = tmp_path / "nes"
        target_dir.mkdir()
        path = target_dir / "Doomed.nes"
        path.write_bytes(b"doomed bytes")
        rom_id = _insert_rom(
            seeded_db,
            path=str(path),
            system_id="nes",
            match_confidence="fuzzy",
        )
        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_DELETE_FILE,
                    rom_id=rom_id,
                    source_path=str(path),
                    reason="user resolution: delete source",
                )
            ]
        )
        summary = execute_plan(seeded_db, plan.actions)
        assert summary.applied == 1
        assert summary.failed == 0
        assert not path.exists()
        # DB row gone.
        row = seeded_db.execute(
            "SELECT 1 FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row is None


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
        assert summary.applied == 1
        assert summary.failed == 0
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
        assert summary.failed == 1
        assert summary.applied == 0
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
        assert summary.applied == 1
        assert not dup_path.exists()
        assert keeper_path.exists()
        assert q.get_rom_by_id(seeded_db, dup_id) is None


# ---------------------------------------------------------------------------
# Execution: delete_duplicate — normalized-hash TOCTOU guard (Bug 2)
# ---------------------------------------------------------------------------


class TestExecuteDeleteDuplicateNormalizedHash:
    """Bug 2 fix: the TOCTOU guard must use ``hash_rom`` (normalized) not
    ``_digest_stream`` (raw).

    Before the fix, a ``.sfc`` plain file and a ``.zip`` containing the same
    bytes would always fail the guard because raw digests differ. After the
    fix, ``hash_rom`` extracts the zip and compares normalized SHA-1s, so the
    action succeeds when content is genuinely identical.
    """

    def _write_zip(self, zip_path: Path, inner_name: str, content: bytes) -> None:
        """Write a single-file zip archive at ``zip_path``."""
        import zipfile

        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr(inner_name, content)

    def test_sfc_and_zip_same_content_succeeds(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """A .sfc file and a .zip of the same bytes should apply cleanly.

        Both files hash to the same SHA-1 under ``hash_rom`` (zip is extracted
        before hashing; snes has no header_rule so no stripping is applied).
        The TOCTOU guard must pass and the duplicate must be removed.
        """
        rom_bytes = b"fake-snes-rom-payload-" + b"\xab\xcd" * 128
        sfc_path = tmp_path / "snes" / "Mario.sfc"
        zip_path = tmp_path / "snes" / "Mario.zip"
        sfc_path.parent.mkdir(parents=True, exist_ok=True)
        sfc_path.write_bytes(rom_bytes)
        self._write_zip(zip_path, "Mario.sfc", rom_bytes)

        # Both roms enrolled in DB for the snes system (no header_rule).
        keeper_id = _insert_rom(
            seeded_db,
            path=str(sfc_path).replace("\\", "/"),
            system_id="snes",
        )
        dup_id = _insert_rom(
            seeded_db,
            path=str(zip_path).replace("\\", "/"),
            system_id="snes",
        )
        action = OrganizeAction(
            kind=ACTION_DELETE_DUPLICATE,
            rom_id=dup_id,
            source_path=str(zip_path).replace("\\", "/"),
            target_path=str(sfc_path).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary.applied == 1, summary.errors
        assert not zip_path.exists()
        assert sfc_path.exists()
        assert q.get_rom_by_id(seeded_db, dup_id) is None
        # Keeper row still intact.
        assert q.get_rom_by_id(seeded_db, keeper_id) is not None

    def test_sfc_and_zip_different_content_refuses(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """A .sfc and a .zip with DIFFERENT inner content must be refused.

        The guard detects the SHA-1 mismatch after normalization and raises
        ``ValueError``, leaving both files intact on disk.
        """
        sfc_bytes = b"original-rom-" + b"\x11" * 64
        zip_inner_bytes = b"completely-different-payload-" + b"\x22" * 64
        sfc_path = tmp_path / "snes" / "Mario.sfc"
        zip_path = tmp_path / "snes" / "Mario_dup.zip"
        sfc_path.parent.mkdir(parents=True, exist_ok=True)
        sfc_path.write_bytes(sfc_bytes)
        self._write_zip(zip_path, "Mario.sfc", zip_inner_bytes)

        keeper_id = _insert_rom(
            seeded_db,
            path=str(sfc_path).replace("\\", "/"),
            system_id="snes",
        )
        dup_id = _insert_rom(
            seeded_db,
            path=str(zip_path).replace("\\", "/"),
            system_id="snes",
        )
        action = OrganizeAction(
            kind=ACTION_DELETE_DUPLICATE,
            rom_id=dup_id,
            source_path=str(zip_path).replace("\\", "/"),
            target_path=str(sfc_path).replace("\\", "/"),
        )
        summary = execute_plan(seeded_db, [action])
        assert summary.failed == 1
        assert summary.applied == 0
        # Both files survive untouched.
        assert zip_path.exists()
        assert sfc_path.exists()
        # Error message must mention "post-normalization".
        assert any("post-normalization" in e for e in summary.errors), summary.errors
        # Both DB rows survive.
        assert q.get_rom_by_id(seeded_db, dup_id) is not None
        assert q.get_rom_by_id(seeded_db, keeper_id) is not None


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
        assert summary.applied == 1
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
        assert summary.skipped == 1
        assert summary.applied == 0
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

        monkeypatch.setattr(atomic.os, "replace", _always_raise)

        summary = execute_plan(seeded_db, [action])
        assert summary.failed == 1
        assert summary.applied == 0
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
        # Capture the exact source/dest paths for action B so the flaky_replace
        # check is path-equality based rather than substring-based. A future
        # rename of the test fixtures (e.g. "b.sfc" -> "sample-b.sfc") would
        # otherwise silently stop exercising the rollback path.
        b_src_path, b_dest_path = files["b"]
        b_src_str = str(b_src_path)
        b_dest_str = str(b_dest_path)

        def flaky_replace(s, d):
            # Fail every replace involving action B specifically. The fallback
            # copy-via-tempfile path also routes through os.replace, but the
            # ``s`` (source) or ``d`` (final destination) always traces back to
            # action B's exact paths.
            if str(s) == b_src_str or str(d) == b_dest_str:
                raise OSError("simulated second-action failure")
            return real_replace(s, d)

        monkeypatch.setattr(atomic.os, "replace", flaky_replace)

        summary = execute_plan(seeded_db, actions)
        assert summary.applied == 2
        assert summary.failed == 1
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

    def test_collision_combo_default_emits_nothing(self, qapp) -> None:
        """Default selection ('Do nothing') means the collision contributes
        no actions to approved_actions(). The user has to pick a real
        resolution explicitly."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    rom_id=10,
                    target_rom_id=20,
                    source_path="/lib/nes/108 X.nes",
                    target_path="/lib/nes/X (Japan).nes",
                    reason="target path already occupied by a different file",
                )
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            # One combo was installed for the one collision row.
            combos = dialog._iter_collision_combos()
            assert len(combos) == 1
            _, combo = combos[0]
            assert combo.count() == 3  # Do nothing / Delete source / Replace target
            assert combo.currentData() == RESOLUTION_DO_NOTHING
            assert dialog.approved_actions() == []
        finally:
            dialog.close()

    def test_collision_combo_replace_target_emits_two_actions(self, qapp) -> None:
        """When the user picks 'Delete target and rename source',
        approved_actions() yields a DELETE_FILE for the target followed
        by a RENAME for the source — in that order."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    rom_id=10,
                    target_rom_id=20,
                    source_path="/lib/nes/108 X.nes",
                    target_path="/lib/nes/X (Japan).nes",
                    reason="target path already occupied",
                )
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            _, combo = dialog._iter_collision_combos()[0]
            # Find and select the replace_target option.
            for i in range(combo.count()):
                if combo.itemData(i) == RESOLUTION_REPLACE_TARGET:
                    combo.setCurrentIndex(i)
                    break
            else:
                raise AssertionError("RESOLUTION_REPLACE_TARGET not in combo")
            approved = dialog.approved_actions()
            assert len(approved) == 2
            assert approved[0].kind == ACTION_DELETE_FILE
            assert approved[0].rom_id == 20
            assert approved[0].source_path == "/lib/nes/X (Japan).nes"
            assert approved[1].kind == ACTION_RENAME
            assert approved[1].rom_id == 10
            assert approved[1].source_path == "/lib/nes/108 X.nes"
            assert approved[1].target_path == "/lib/nes/X (Japan).nes"
        finally:
            dialog.close()

    def test_collision_combo_options_for_case_1_collision(self, qapp) -> None:
        """A case-1 collision (no target_rom_id captured) should still get
        a combo, but with fewer options — only Do nothing and Delete source."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    rom_id=5,
                    target_rom_id=None,
                    source_path="/lib/nes/a.nes",
                    target_path="/lib/nes/X.nes",
                    reason="2 rename(s) target this path",
                )
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            _, combo = dialog._iter_collision_combos()[0]
            values = {combo.itemData(i) for i in range(combo.count())}
            assert RESOLUTION_DO_NOTHING in values
            assert RESOLUTION_DELETE_SOURCE in values
            assert RESOLUTION_REPLACE_TARGET not in values
        finally:
            dialog.close()

    def test_apply_disables_both_buttons_during_run(self, qapp) -> None:
        """Apply, Cancel, Select/Deselect All, checkboxes, and collision
        combos all get locked when the user clicks Apply — the work is
        in-flight to the worker and the dialog snapshot is frozen."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="DAT-verified",
                ),
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    rom_id=10,
                    target_rom_id=20,
                    source_path="/lib/nes/108 X.nes",
                    target_path="/lib/nes/X (Japan).nes",
                    reason="case 3",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            dialog._on_apply_clicked()
            assert dialog._apply_btn.isEnabled() is False
            assert dialog._cancel_btn.isEnabled() is False
            assert dialog._select_all_btn.isEnabled() is False
            assert dialog._deselect_all_btn.isEnabled() is False
            for _, combo in dialog._iter_collision_combos():
                assert combo.isEnabled() is False
            for item in dialog._iter_action_items():
                assert item.isEnabled() is False
        finally:
            dialog.close()

    def test_on_finished_swaps_to_close_button(self, qapp) -> None:
        """After the worker reports back, the Apply button is hidden and
        Cancel is renamed to Close + re-enabled + wired to accept()."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="DAT-verified",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            dialog._on_apply_clicked()
            dialog.on_finished(applied=1, skipped=0, failed=0)
            assert dialog._apply_btn.isVisible() is False
            assert dialog._cancel_btn.isEnabled() is True
            assert dialog._cancel_btn.text() == "Close"
        finally:
            dialog.close()

    def test_on_finished_success_removes_submitted_rows(self, qapp) -> None:
        """On a clean apply (failed=0) every row that was acted upon is
        removed from the model. Rows that were unchecked / Do-nothing
        stay visible so the user can see what remains to address."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                # Will be checked + submitted.
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="DAT-verified",
                ),
                # Will be submitted via collision combo Delete source.
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    rom_id=10,
                    target_rom_id=20,
                    source_path="/lib/nes/108 X.nes",
                    target_path="/lib/nes/X (Japan).nes",
                    reason="case 3",
                ),
                # Stays — user leaves this collision on Do nothing.
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    rom_id=30,
                    target_rom_id=40,
                    source_path="/lib/nes/200 Y.nes",
                    target_path="/lib/nes/Y (Japan).nes",
                    reason="case 3",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            # Pick "Delete source" on the first collision combo, leave
            # the second on Do nothing.
            combos = dialog._iter_collision_combos()
            assert len(combos) == 2
            first_action, first_combo = combos[0]
            assert first_action.rom_id == 10
            for i in range(first_combo.count()):
                if first_combo.itemData(i) == RESOLUTION_DELETE_SOURCE:
                    first_combo.setCurrentIndex(i)
                    break
            dialog._on_apply_clicked()
            dialog.on_finished(applied=2, skipped=0, failed=0)
            # Two rows should be gone (the rename + the resolved collision).
            # The second collision (Do nothing) should remain.
            from romulus.ui.organize_preview import _ACTION_ROLE

            remaining_actions: list[OrganizeAction] = []
            root = dialog._model.invisibleRootItem()
            for i in range(root.rowCount()):
                header = root.child(i, 0)
                for j in range(header.rowCount()):
                    child = header.child(j, 0)
                    if child is not None:
                        a = child.data(_ACTION_ROLE)
                        if isinstance(a, OrganizeAction):
                            remaining_actions.append(a)
            assert len(remaining_actions) == 1
            assert remaining_actions[0].rom_id == 30
        finally:
            dialog.close()

    def test_on_finished_with_failures_keeps_rows(self, qapp) -> None:
        """If any action failed, submitted rows STAY visible so the
        user can review what was involved when investigating."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="DAT-verified",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            dialog._on_apply_clicked()
            dialog.on_finished(applied=0, skipped=0, failed=1)
            # Row should still be there.
            root = dialog._model.invisibleRootItem()
            total_children = 0
            for i in range(root.rowCount()):
                header = root.child(i, 0)
                total_children += header.rowCount()
            assert total_children == 1
        finally:
            dialog.close()

    def test_on_failed_also_swaps_to_close_button(self, qapp) -> None:
        """A worker exception (on_failed path) must also reach the
        done-state swap so the user isn't left with a disabled Cancel
        button after a fatal error."""
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="DAT-verified",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            dialog._on_apply_clicked()
            dialog.on_failed("disk full")
            assert dialog._cancel_btn.isEnabled() is True
            assert dialog._cancel_btn.text() == "Close"
            assert dialog._apply_btn.isVisible() is False
        finally:
            dialog.close()

    def test_group_header_toggle_cascades_to_children(self, qapp) -> None:
        """Tri-state header toggle from GroupedCheckboxTreeMixin must
        flip only that group's children, leaving sibling groups alone.

        Regression guard for KNOWN-ISSUES.md 2026-05-18 "Per-group
        select/deselect in preview dialogs" — a multi-thousand-row
        plan with two groups of three actions, toggling group A's
        header should ONLY flip group A's actions.
        """
        from PySide6.QtCore import Qt

        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                # Group A: three renames.
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="rename A",
                ),
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=2,
                    source_path="/lib/snes/b.sfc",
                    target_path="/lib/snes/B.sfc",
                    reason="rename B",
                ),
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=3,
                    source_path="/lib/snes/c.sfc",
                    target_path="/lib/snes/C.sfc",
                    reason="rename C",
                ),
                # Group B: three duplicate removals.
                OrganizeAction(
                    kind=ACTION_DELETE_DUPLICATE,
                    rom_id=4,
                    source_path="/lib/snes/dup1.sfc",
                    target_path="/lib/snes/dup1.sfc",
                    reason="dup of A",
                ),
                OrganizeAction(
                    kind=ACTION_DELETE_DUPLICATE,
                    rom_id=5,
                    source_path="/lib/snes/dup2.sfc",
                    target_path="/lib/snes/dup2.sfc",
                    reason="dup of B",
                ),
                OrganizeAction(
                    kind=ACTION_DELETE_DUPLICATE,
                    rom_id=6,
                    source_path="/lib/snes/dup3.sfc",
                    target_path="/lib/snes/dup3.sfc",
                    reason="dup of C",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            root = dialog._model.invisibleRootItem()
            # Find the rename header + the duplicate header.
            headers: dict[str, object] = {}
            for i in range(root.rowCount()):
                header = root.child(i, 0)
                if header is None:
                    continue
                # Header label is "Renames (3)" or "Duplicate removals (3)" — index by row count.
                if header.rowCount() == 3:
                    label = header.text().lower()
                    if "rename" in label:
                        headers["renames"] = header
                    elif "duplicate" in label:
                        headers["dupes"] = header
            assert "renames" in headers and "dupes" in headers, (
                f"could not find both group headers: {list(headers)}"
            )

            renames_hdr = headers["renames"]
            dupes_hdr = headers["dupes"]

            # Initial state: all six children checked, both headers Checked.
            assert renames_hdr.checkState() == Qt.CheckState.Checked
            assert dupes_hdr.checkState() == Qt.CheckState.Checked
            assert len(dialog.approved_actions()) == 6

            # Toggle the renames header to Unchecked.
            renames_hdr.setCheckState(Qt.CheckState.Unchecked)

            # All three renames should be unchecked, all three dupes still checked.
            for j in range(renames_hdr.rowCount()):
                child = renames_hdr.child(j, 0)
                assert child.checkState() == Qt.CheckState.Unchecked, (
                    f"renames child {j} should be unchecked after header toggle"
                )
            for j in range(dupes_hdr.rowCount()):
                child = dupes_hdr.child(j, 0)
                assert child.checkState() == Qt.CheckState.Checked, (
                    f"dupes child {j} should still be checked"
                )

            # approved_actions should be just the 3 dupes now.
            approved = dialog.approved_actions()
            assert len(approved) == 3
            assert all(a.kind == ACTION_DELETE_DUPLICATE for a in approved)
        finally:
            dialog.close()

    def test_partial_child_check_puts_header_in_tristate(self, qapp) -> None:
        """Unchecking one child of a fully-checked group should leave the
        header in PartiallyChecked state — surfacing the mixed selection.
        """
        from PySide6.QtCore import Qt

        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=i,
                    source_path=f"/lib/snes/{i}.sfc",
                    target_path=f"/lib/snes/R{i}.sfc",
                    reason=f"rename {i}",
                )
                for i in range(3)
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            root = dialog._model.invisibleRootItem()
            header = root.child(0, 0)
            # Sanity: starts fully checked.
            assert header.checkState() == Qt.CheckState.Checked

            # Uncheck just one child.
            header.child(0, 0).setCheckState(Qt.CheckState.Unchecked)

            # Header now reflects partial selection.
            assert header.checkState() == Qt.CheckState.PartiallyChecked

            # Uncheck the other two — header drops to Unchecked.
            header.child(1, 0).setCheckState(Qt.CheckState.Unchecked)
            header.child(2, 0).setCheckState(Qt.CheckState.Unchecked)
            assert header.checkState() == Qt.CheckState.Unchecked
        finally:
            dialog.close()

    def test_collision_only_group_has_non_checkable_header(self, qapp) -> None:
        """A group whose every child is non-checkable (Collisions) must NOT
        get a tri-state checkbox — showing one would be misleading since
        clicking it has no effect.
        """
        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    source_path="/lib/snes/x.sfc",
                    target_path="/lib/snes/Conflict.sfc",
                    reason="two renames target this path",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            root = dialog._model.invisibleRootItem()
            header = root.child(0, 0)
            assert header.isCheckable() is False
        finally:
            dialog.close()

    def test_right_click_select_all_in_group(self, qapp) -> None:
        """Calling the mixin's group-cascade helper directly (the
        right-click 'Select all in this group' action) must flip the
        group's children without touching other groups.

        We exercise the helper, not the menu popup itself, because
        QMenu.exec is hard to drive headlessly. The right-click handler
        is a thin wrapper around the same cascade helper.
        """
        from PySide6.QtCore import Qt

        from romulus.ui.organize_preview import OrganizePreviewDialog

        plan = OrganizePlan(
            actions=[
                OrganizeAction(
                    kind=ACTION_RENAME,
                    rom_id=1,
                    source_path="/lib/snes/a.sfc",
                    target_path="/lib/snes/A.sfc",
                    reason="rename A",
                ),
                OrganizeAction(
                    kind=ACTION_DELETE_DUPLICATE,
                    rom_id=2,
                    source_path="/lib/snes/dup.sfc",
                    target_path="/lib/snes/dup.sfc",
                    reason="dup",
                ),
            ]
        )
        dialog = OrganizePreviewDialog(plan)
        try:
            root = dialog._model.invisibleRootItem()
            renames_hdr = None
            dupes_hdr = None
            for i in range(root.rowCount()):
                header = root.child(i, 0)
                if "rename" in header.text().lower():
                    renames_hdr = header
                elif "duplicate" in header.text().lower():
                    dupes_hdr = header
            assert renames_hdr is not None
            assert dupes_hdr is not None

            # Deselect everything to start.
            dialog._on_deselect_all()
            assert dialog.approved_actions() == []

            # Cascade-select just the renames group.
            dialog._gct_cascade_header(renames_hdr, Qt.CheckState.Checked)

            approved = dialog.approved_actions()
            assert len(approved) == 1
            assert approved[0].kind == ACTION_RENAME
        finally:
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
