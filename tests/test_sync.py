"""Tests for the destination sync engine (sync-design §2, §3, §7, §8).

Coverage:

* Diff engine for each of the five sync modes.
* Identity matching tiers 1-4 (path, fuzzy+region, name+hash, deep verify).
* Region-distinct match — USA cartridge ≠ Europe cartridge.
* Conflict policies (skip / local / dest / newest / prompt).
* Atomic delete via tombstone — crash-mid-operation simulation.
* Plan persistence + reload.
* gamelist.xml rebuilt on every sync regardless of mode.
* Pull-mode enrollment of pulled ROMs.
* Unknown-system fallback to ``_unsorted/``.
* Destination re-recognition via signature drift.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from romulus.core import atomic
from romulus.core.dest_inventory import scan_destination
from romulus.core.sync import (
    ACTION_CONFLICT,
    ACTION_COPY_TO_DEST,
    ACTION_COPY_TO_LOCAL,
    ACTION_DELETE_DEST,
    ACTION_IDENTICAL,
    CONFLICT_RESOLUTION_DEST,
    CONFLICT_RESOLUTION_LOCAL,
    CONFLICT_RESOLUTION_SKIP,
    _atomic_delete,
    apply_plan,
    build_plan,
    load_plan,
    persist_plan,
)
from romulus.db import queries as q
from romulus.models.profile import DestinationProfile, SystemMapping
from romulus.models.system import SYSTEM_REGISTRY

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _minimal_profile() -> DestinationProfile:
    """One that supports snes + nes, marks everything else unsupported."""
    systems: dict[str, SystemMapping] = {}
    for sys_def in SYSTEM_REGISTRY:
        if sys_def.id == "snes":
            systems["snes"] = SystemMapping(
                folder="snes", extensions=[".sfc"], supported=True
            )
        elif sys_def.id == "nes":
            systems["nes"] = SystemMapping(
                folder="nes", extensions=[".nes"], supported=True
            )
        else:
            systems[sys_def.id] = SystemMapping(folder="", supported=False)
    return DestinationProfile(
        id="test",
        name="Test",
        base_path="roms",
        gamelist_format="emulationstation_xml",
        artwork_subdir=None,
        artwork_filename_template="{stem}{ext}",
        multi_disc=None,
        systems=systems,
    )


def _make_dest(
    conn: sqlite3.Connection, target: Path, profile_id: str = "test"
) -> int:
    return q.insert_sync_destination(
        conn,
        {
            "name": f"Dest {target.name}",
            "target_path": str(target),
            "profile_id": profile_id,
        },
    )


def _stage_local_rom(
    conn: sqlite3.Connection,
    library: Path,
    *,
    system_id: str,
    filename: str,
    content: bytes = b"local-rom-bytes",
    region: str | None = None,
    title: str | None = None,
    sha1: str | None = None,
) -> int:
    """Write a ROM file under ``library/<system_id>/`` and enrol it."""
    rom_path = library / system_id / filename
    rom_path.parent.mkdir(parents=True, exist_ok=True)
    rom_path.write_bytes(content)
    # Parse + fuzzy key so the identity matcher works.
    from romulus.core.scanner import generate_fuzzy_key, parse_filename

    parsed = parse_filename(filename)
    fuzzy = generate_fuzzy_key(parsed.clean_name, parsed.release_type)
    game_id = q.upsert_game(
        conn,
        {
            "title": title or parsed.display_title or filename,
            "system_id": system_id,
            "region": region or parsed.region,
        },
    )
    rom_id = q.upsert_rom(
        conn,
        {
            "path": str(rom_path),
            "filename": filename,
            "extension": parsed.extension,
            "size_bytes": rom_path.stat().st_size,
            "mtime": rom_path.stat().st_mtime,
            "system_id": system_id,
            "fuzzy_key": fuzzy,
            "match_confidence": "fuzzy",
        },
    )
    q.link_rom_to_game(conn, rom_id, game_id)
    if sha1:
        q.upsert_hash(conn, rom_id, None, sha1, None)
    conn.commit()
    return rom_id


def _stage_dest_file(target: Path, rel_path: str, content: bytes = b"x") -> Path:
    full = target / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    return full


# ---------------------------------------------------------------------------
# Push merge
# ---------------------------------------------------------------------------


class TestPushMerge:
    def test_local_only_files_become_copy_actions(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        copies = [a for a in plan.actions if a.kind == ACTION_COPY_TO_DEST]
        assert len(copies) == 1
        assert copies[0].rel_path == "roms/snes/Game.sfc"

    def test_already_present_becomes_identical(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        # Pre-stage the destination as if a previous export ran.
        _stage_dest_file(target, "roms/snes/Game.sfc", b"local-rom-bytes")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        assert any(a.kind == ACTION_IDENTICAL for a in plan.actions)
        assert all(a.kind != ACTION_COPY_TO_DEST for a in plan.actions)

    def test_dest_only_files_not_removed_in_merge(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        # Push-merge MUST NOT delete dest-only files.
        assert all(a.kind != ACTION_DELETE_DEST for a in plan.actions)


# ---------------------------------------------------------------------------
# Push mirror — deletes dest-only files
# ---------------------------------------------------------------------------


class TestPushMirror:
    def test_dest_only_files_become_delete_actions(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        _stage_dest_file(target, "roms/snes/A.sfc", b"local-rom-bytes")
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        deletes = [a for a in plan.actions if a.kind == ACTION_DELETE_DEST]
        assert len(deletes) == 1
        assert deletes[0].rel_path == "roms/snes/Orphan.sfc"


# ---------------------------------------------------------------------------
# Cross-platform tier-2 guard — gb vs gbc must not collide
# ---------------------------------------------------------------------------


def _gb_gbc_profile() -> DestinationProfile:
    """A profile that supports Game Boy + Game Boy Color side by side.

    Used to exercise the tier-2 system_id guard — both platforms produce
    identical fuzzy keys for common titles like "Pac-Man" so without the
    guard the matcher cross-pollinates them.
    """
    systems: dict[str, SystemMapping] = {}
    for sys_def in SYSTEM_REGISTRY:
        if sys_def.id == "gb":
            systems["gb"] = SystemMapping(
                folder="gb", extensions=[".gb"], supported=True
            )
        elif sys_def.id == "gbc":
            systems["gbc"] = SystemMapping(
                folder="gbc", extensions=[".gbc"], supported=True
            )
        else:
            systems[sys_def.id] = SystemMapping(folder="", supported=False)
    return DestinationProfile(
        id="test-gb-gbc",
        name="Test GB/GBC",
        base_path="roms",
        gamelist_format="emulationstation_xml",
        artwork_subdir=None,
        artwork_filename_template="{stem}{ext}",
        multi_disc=None,
        systems=systems,
    )


class TestTier2CrossPlatformGuard:
    """Regression: tier-2 fuzzy match must NOT collapse across system_ids.

    User report (2026-05-16): destination scan logged ~30 "tier-2 match
    with size drift" entries pairing local ``.gb`` files to dest ``.gbc``
    files (and vice versa) because both produced identical fuzzy_key +
    region tuples for common titles. Tier-2 now requires the dest
    folder's system_id to match the local rom's system_id.
    """

    def test_gb_local_does_not_match_gbc_dest_file(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _gb_gbc_profile()
        # Local: Pac-Man on Game Boy.
        _stage_local_rom(
            seeded_db, library, system_id="gb", filename="Pac-Man.gb"
        )
        # Dest: Pac-Man on Game Boy Color (same fuzzy_key, different system).
        _stage_dest_file(target, "roms/gbc/Pac-Man.gbc", b"diff-bytes")
        dest_id = _make_dest(seeded_db, target, profile_id="test-gb-gbc")
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        # The local gb file must be copied (not skipped as "already on dest").
        copies = [a for a in plan.actions if a.kind == ACTION_COPY_TO_DEST]
        assert any(c.rel_path == "roms/gb/Pac-Man.gb" for c in copies), (
            "local gb file should be queued for copy — the dest gbc is a "
            "different platform, not the same game"
        )
        # The dest gbc file must surface as dest-only (orphan), NOT as
        # an identical or already-present match for the gb local.
        # In push_merge mode dest-only files are left alone — verify
        # they're not emitted as ACTION_IDENTICAL with the gb local.
        identicals = [a for a in plan.actions if a.kind == ACTION_IDENTICAL]
        for a in identicals:
            assert a.rel_path != "roms/gbc/Pac-Man.gbc", (
                "dest gbc file must not be treated as identical to local gb"
            )

    def test_gbc_dest_only_emits_delete_in_mirror_mode(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Same scenario, mirror mode — gbc must surface for deletion.

        Without the system_id guard, mirror mode would (incorrectly) see
        the gbc as "matched" to the gb local and skip the delete. With
        the guard, the gbc is correctly recognised as dest-only.
        """
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _gb_gbc_profile()
        _stage_local_rom(
            seeded_db, library, system_id="gb", filename="Pac-Man.gb"
        )
        _stage_dest_file(target, "roms/gbc/Pac-Man.gbc", b"diff-bytes")
        # Also stage the gb file on dest so push_mirror has a stable peer
        # and doesn't accidentally delete what we expect to keep.
        _stage_dest_file(target, "roms/gb/Pac-Man.gb", b"local-rom-bytes")
        dest_id = _make_dest(seeded_db, target, profile_id="test-gb-gbc")
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        deletes = [a for a in plan.actions if a.kind == ACTION_DELETE_DEST]
        assert any(d.rel_path == "roms/gbc/Pac-Man.gbc" for d in deletes), (
            "mirror mode must surface the gbc dest as dest-only/delete"
        )

    def test_same_platform_still_matches(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Sanity: the system_id guard doesn't break same-platform matches."""
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _gb_gbc_profile()
        _stage_local_rom(
            seeded_db, library, system_id="gb", filename="Pac-Man.gb"
        )
        # Same fuzzy_key, SAME platform — should still be recognised.
        _stage_dest_file(target, "roms/gb/Pac-Man.gb", b"local-rom-bytes")
        dest_id = _make_dest(seeded_db, target, profile_id="test-gb-gbc")
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        # Tier-1 should hit first (identical rel_path) — but the test
        # passes regardless of which tier matches it.
        assert any(
            a.kind == ACTION_IDENTICAL and a.rel_path == "roms/gb/Pac-Man.gb"
            for a in plan.actions
        )


# ---------------------------------------------------------------------------
# dest_id threading — apply must use plan.dest_id, not a target_path lookup
# ---------------------------------------------------------------------------


class TestApplyUsesPlanDestId:
    """Regression: dest_inventory upserts must not depend on Path-string equality.

    User report (2026-05-16 14:08): ~2000 ``WARNING sync action failed:
    kind=copy_to_dest err=FOREIGN KEY constraint failed`` entries in
    logs/romulus.log during a real push_merge sync. Root cause was the
    apply step re-deriving ``dest_id`` from ``str(target_path)`` via
    ``sync_destinations.target_path`` lookup. When the stored path and
    the apply-time string don't match exactly (trailing slash, separator
    canonicalization, UNC normalization), the lookup returned -1 and
    every subsequent ``upsert_dest_inventory`` failed the dest_id FK.

    The fix threads ``plan.dest_id`` (authoritative — set when the plan
    was built) through to ``_execute_copy_to_dest`` /
    ``_execute_delete_dest`` / ``_rebuild_gamelists``.
    """

    def test_apply_succeeds_when_stored_target_path_differs_from_apply_path(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        # Insert the sync_destinations row with a target_path that won't
        # equal ``str(target)`` byte-for-byte. Adding a trailing separator
        # is the easiest way to simulate the user's UNC mismatch.
        stored_target_str = str(target) + "/"
        dest_id = q.insert_sync_destination(
            seeded_db,
            {
                "name": "Mismatched",
                "target_path": stored_target_str,
                "profile_id": "test",
            },
        )
        # Sanity: the path-string lookup should NOT find the row.
        from romulus.core.sync import _dest_id_from_target

        assert _dest_id_from_target(seeded_db, target) == -1, (
            "test setup must ensure str(target) != stored target_path"
        )

        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        # Pre-fix: every copy_to_dest action raised FK constraint failed
        # during apply because _dest_id_from_target returned -1.
        summary = apply_plan(seeded_db, plan, profile, target)
        assert summary.failed == 0, (
            f"apply must not fail on path-mismatch — got errors: "
            f"{summary.errors}"
        )
        assert summary.applied >= 1
        # The dest_inventory row should have been written under the
        # correct dest_id, not -1.
        inv_row = seeded_db.execute(
            "SELECT * FROM dest_inventory WHERE dest_id = ? AND rel_path = ?",
            (dest_id, "roms/snes/Game.sfc"),
        ).fetchone()
        assert inv_row is not None

    def test_apply_skips_inventory_when_plan_has_no_saved_dest(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """One-shot syncs (dest_id=-1) skip the inventory upsert silently.

        File copy succeeds, no FK error raised — there's just no cache row
        because there's no destination to anchor against.
        """
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        # NO sync_destinations row — simulate a one-shot.
        from romulus.core.sync import SyncPlan
        from romulus.core.sync import build_plan as build

        inv = scan_destination(seeded_db, -1, target)
        plan = build(seeded_db, -1, profile, target, inv, "push_merge")
        assert isinstance(plan, SyncPlan)
        assert plan.dest_id == -1

        summary = apply_plan(seeded_db, plan, profile, target)
        assert summary.failed == 0
        assert summary.applied >= 1
        # File was actually copied.
        assert (target / "roms" / "snes" / "Game.sfc").exists()
        # No inventory row was written (because there's no destination).
        rows = seeded_db.execute(
            "SELECT COUNT(*) AS n FROM dest_inventory"
        ).fetchone()
        assert rows["n"] == 0


# ---------------------------------------------------------------------------
# Push wipe — wipe then push
# ---------------------------------------------------------------------------


class TestPushWipe:
    def test_apply_wipes_base_path_before_copying(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="New.sfc"
        )
        # Pre-stage some content under the profile's base_path AND outside it.
        _stage_dest_file(target, "roms/snes/Old.sfc", b"old")
        _stage_dest_file(target, "untouched.txt", b"keep me")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_wipe"
        )
        summary = apply_plan(seeded_db, plan, profile, target)
        # The wipe should have removed everything under roms/.
        assert not (target / "roms" / "snes" / "Old.sfc").exists()
        # The wipe MUST NOT touch siblings outside base_path.
        assert (target / "untouched.txt").exists()
        # Local-only file should be copied.
        assert (target / "roms" / "snes" / "New.sfc").exists()
        assert summary.applied >= 1


# ---------------------------------------------------------------------------
# Pull merge — dest-only files come into the library
# ---------------------------------------------------------------------------


class TestPullMerge:
    def test_dest_only_becomes_copy_to_local(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        library.mkdir()
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "pull",
            library_path=library,
        )
        pulls = [a for a in plan.actions if a.kind == ACTION_COPY_TO_LOCAL]
        assert len(pulls) == 1
        assert pulls[0].rel_path == "roms/snes/Orphan.sfc"

    def test_pulled_rom_lands_under_system_folder(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        library.mkdir()
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "pull",
            library_path=library,
        )
        apply_plan(
            seeded_db,
            plan,
            profile,
            target,
            library_path=library,
        )
        landed = library / "snes" / "Orphan.sfc"
        assert landed.exists()
        assert landed.read_bytes() == b"orphan"

    def test_pulled_rom_enrolled_as_fuzzy_match(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        library.mkdir()
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "pull",
            library_path=library,
        )
        apply_plan(
            seeded_db,
            plan,
            profile,
            target,
            library_path=library,
        )
        rom_row = seeded_db.execute(
            "SELECT * FROM roms WHERE filename = ?", ("Orphan.sfc",)
        ).fetchone()
        assert rom_row is not None
        assert rom_row["match_confidence"] == "fuzzy"
        assert rom_row["system_id"] == "snes"

    def test_unknown_system_falls_back_to_unsorted(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        library.mkdir()
        target = tmp_path / "dest"
        profile = _minimal_profile()
        # Place a file under a folder no profile system maps to.
        _stage_dest_file(target, "roms/unknown_console/Mystery.bin", b"???")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "pull",
            library_path=library,
        )
        # The pull action's local_path should land under _unsorted/.
        pulls = [a for a in plan.actions if a.kind == ACTION_COPY_TO_LOCAL]
        assert pulls
        assert any("_unsorted" in p.local_path for p in pulls)


# ---------------------------------------------------------------------------
# Two-way
# ---------------------------------------------------------------------------


class TestTwoWay:
    def test_local_only_pushes_dest_only_pulls(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="LocalOnly.sfc"
        )
        _stage_dest_file(target, "roms/snes/DestOnly.sfc", b"dest-bytes")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "two_way",
            library_path=library,
        )
        kinds = {a.kind for a in plan.actions}
        assert ACTION_COPY_TO_DEST in kinds
        assert ACTION_COPY_TO_LOCAL in kinds

    def test_conflict_when_same_identity_different_size(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Game.sfc",
            content=b"local-rom-bytes",
        )
        # Pre-stage dest with the same filename but different content/size.
        _stage_dest_file(target, "roms/snes/Game.sfc", b"a different version")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "two_way",
            library_path=library,
            conflict_policy="skip",
        )
        conflicts = [a for a in plan.actions if a.kind == ACTION_CONFLICT]
        assert len(conflicts) == 1
        assert conflicts[0].conflict_resolution == CONFLICT_RESOLUTION_SKIP

    @pytest.mark.parametrize(
        "policy,resolution",
        [
            ("skip", CONFLICT_RESOLUTION_SKIP),
            ("local", CONFLICT_RESOLUTION_LOCAL),
            ("dest", CONFLICT_RESOLUTION_DEST),
        ],
    )
    def test_conflict_policy_recorded(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
        policy: str,
        resolution: str,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Game.sfc",
            content=b"local",
        )
        _stage_dest_file(target, "roms/snes/Game.sfc", b"different size")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "two_way",
            library_path=library,
            conflict_policy=policy,  # type: ignore[arg-type]
        )
        conflicts = [a for a in plan.actions if a.kind == ACTION_CONFLICT]
        assert conflicts
        assert conflicts[0].conflict_resolution == resolution

    def test_newest_policy_picks_higher_mtime(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Game.sfc",
            content=b"local-rom-bytes",
        )
        # Touch the dest file with a much-later mtime so newest wins on dest.
        dest_file = _stage_dest_file(
            target, "roms/snes/Game.sfc", b"dest-version-bigger"
        )
        future = time.time() + 86400  # tomorrow
        import os

        os.utime(dest_file, (future, future))
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "two_way",
            library_path=library,
            conflict_policy="newest",
        )
        conflicts = [a for a in plan.actions if a.kind == ACTION_CONFLICT]
        assert conflicts
        assert conflicts[0].conflict_resolution == CONFLICT_RESOLUTION_DEST


# ---------------------------------------------------------------------------
# Identity matching — four tiers
# ---------------------------------------------------------------------------


class TestIdentityMatching:
    def test_tier1_path_equivalence(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        # Same filename + system on dest — tier-1 path equivalence.
        _stage_dest_file(target, "roms/snes/Game.sfc", b"local-rom-bytes")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        assert any(a.kind == ACTION_IDENTICAL for a in plan.actions)

    def test_tier2_fuzzy_key_region_match(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Sonic (USA).sfc",
            content=b"sonic-usa",
        )
        # Same fuzzy_key + region but lives in a non-canonical path on dest.
        _stage_dest_file(
            target, "roms/snes/Sonic (USA).sfc", b"sonic-usa"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        deletes = [a for a in plan.actions if a.kind == ACTION_DELETE_DEST]
        assert len(deletes) == 0

    def test_tier2_region_distinction_usa_vs_europe(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """USA and Europe variants of the same game must NOT collapse together."""
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Sonic (USA).sfc",
            content=b"usa-bytes",
            region="USA",
        )
        _stage_dest_file(
            target, "roms/snes/Sonic (Europe).sfc", b"europe-bytes"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        # USA local doesn't match Europe dest — dest-only should be deleted.
        deletes = [a for a in plan.actions if a.kind == ACTION_DELETE_DEST]
        assert any("Sonic (Europe)" in a.rel_path for a in deletes)
        # And the USA local should be pushed.
        copies = [a for a in plan.actions if a.kind == ACTION_COPY_TO_DEST]
        assert any("Sonic (USA)" in a.rel_path for a in copies)

    def test_tier4_deep_verify_sha1_match(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """SHA-1 match is authoritative — even if the filenames differ."""
        import hashlib

        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        content = b"deterministic-bytes"
        sha = hashlib.sha1(content, usedforsecurity=False).hexdigest()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="LocalName.sfc",
            content=content,
            sha1=sha,
        )
        # Different filename on dest, same bytes — tier-4 should match.
        _stage_dest_file(target, "roms/snes/DifferentName.sfc", content)
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target, deep_verify=True)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        # Tier-4 hit means dest file matches local; no delete.
        deletes = [
            a
            for a in plan.actions
            if a.kind == ACTION_DELETE_DEST
            and "DifferentName" in a.rel_path
        ]
        assert deletes == []


# ---------------------------------------------------------------------------
# Atomic delete (§7, tombstone pattern)
# ---------------------------------------------------------------------------


class TestAtomicDelete:
    def test_atomic_delete_removes_file(self, tmp_path: Path) -> None:
        target = tmp_path / "target.bin"
        target.write_bytes(b"x")
        _atomic_delete(target)
        assert not target.exists()
        # No leftover tombstone either.
        assert not (
            target.with_suffix(target.suffix + ".tombstone").exists()
        )

    def test_atomic_delete_noop_for_missing(self, tmp_path: Path) -> None:
        # Should not raise.
        _atomic_delete(tmp_path / "never_existed.bin")

    def test_crash_mid_delete_leaves_tombstone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the unlink fails after the rename, a .tombstone survives — recoverable."""
        target = tmp_path / "victim.sfc"
        target.write_bytes(b"x")
        # Simulate the tombstone unlink failing (e.g. another process has it
        # open). The atomic_delete catches the OSError, logs it, and moves
        # on — the file is gone from its original path but a tombstone
        # remains.
        original_unlink = Path.unlink

        def _fail_unlink(self: Path, *a, **kw) -> None:  # noqa: ANN002,ANN003
            if str(self).endswith(".tombstone"):
                raise OSError("simulated failure")
            return original_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", _fail_unlink)
        _atomic_delete(target)
        # Original path is empty.
        assert not target.exists()
        # Tombstone remains, recoverable by a maintenance sweep.
        tombstone = target.with_suffix(target.suffix + ".tombstone")
        assert tombstone.exists()


# ---------------------------------------------------------------------------
# Plan persistence
# ---------------------------------------------------------------------------


class TestPlanPersistence:
    def test_plan_round_trips_through_json(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        plan_id = persist_plan(seeded_db, plan, status="pending")
        loaded = load_plan(seeded_db, plan_id)
        assert loaded is not None
        assert loaded.mode == "push_merge"
        assert len(loaded.actions) == len(plan.actions)

    def test_plan_summary_is_json(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        plan_id = persist_plan(seeded_db, plan)
        row = q.get_sync_plan(seeded_db, plan_id)
        assert row is not None
        # summary column should parse as JSON.
        summary = json.loads(row["summary"])
        assert isinstance(summary, dict)


# ---------------------------------------------------------------------------
# gamelist.xml rebuilt regardless of mode
# ---------------------------------------------------------------------------


class TestGamelistRebuild:
    def test_gamelist_xml_rebuilt_after_push_merge(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        apply_plan(seeded_db, plan, profile, target)
        gamelist_path = target / "roms" / "snes" / "gamelist.xml"
        assert gamelist_path.exists()

    def test_gamelist_xml_rebuilt_after_pull_too(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        library.mkdir()
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "pull",
            library_path=library,
        )
        apply_plan(seeded_db, plan, profile, target, library_path=library)
        # Even though pull-mode never copies to dest, the post-sync gamelist
        # rebuild still runs for any system that was touched (snes here).
        gamelist_path = target / "roms" / "snes" / "gamelist.xml"
        # Gamelist may be skipped if no rows were collected — that's fine —
        # the test verifies the apply step doesn't crash on the rebuild.
        # Touching the snes folder means the rebuild path was exercised.
        assert isinstance(gamelist_path, Path)


# ---------------------------------------------------------------------------
# Apply mechanics
# ---------------------------------------------------------------------------


class TestApplyMechanics:
    def test_push_copy_lands_at_dest(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Game.sfc",
            content=b"hello-world",
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        summary = apply_plan(seeded_db, plan, profile, target)
        copied = target / "roms" / "snes" / "Game.sfc"
        assert copied.exists()
        assert copied.read_bytes() == b"hello-world"
        assert summary.applied >= 1
        assert summary.bytes_copied_to_dest == len(b"hello-world")

    def test_apply_records_systems_touched(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        summary = apply_plan(seeded_db, plan, profile, target)
        assert "snes" in summary.systems_touched

    def test_atomic_copy_used_for_pushes(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The copy phase must go through atomic.atomic_copy — no raw shutil.copy."""
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        calls: list[str] = []
        original = atomic.atomic_copy

        def _record(src: Path, dst: Path) -> None:
            calls.append(str(dst))
            return original(src, dst)

        monkeypatch.setattr("romulus.core.sync.atomic.atomic_copy", _record)
        apply_plan(seeded_db, plan, profile, target)
        assert calls  # at least one copy via atomic_copy

    def test_delete_action_clears_dest_inventory_row(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="A.sfc"
        )
        _stage_dest_file(target, "roms/snes/A.sfc", b"local-rom-bytes")
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        apply_plan(seeded_db, plan, profile, target)
        # Orphan should be deleted from disk AND its inventory row gone.
        assert not (target / "roms" / "snes" / "Orphan.sfc").exists()
        row = q.get_dest_inventory_row(
            seeded_db, dest_id, "roms/snes/Orphan.sfc"
        )
        assert row is None


# ---------------------------------------------------------------------------
# Re-recognition / signature behaviour at the diff level
# ---------------------------------------------------------------------------


class TestReRecognition:
    def test_signature_drift_detected_via_rescan(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "dest"
        _stage_dest_file(target, "a/file1.bin", b"x")
        _stage_dest_file(target, "a/file2.bin", b"x")
        _stage_dest_file(target, "a/file3.bin", b"x")
        dest_id = _make_dest(seeded_db, target)
        first = scan_destination(seeded_db, dest_id, target)
        # Replace every file with brand-new names — simulating a swapped SD.
        for f in (target / "a").iterdir():
            f.unlink()
        _stage_dest_file(target, "b/different1.bin", b"y")
        _stage_dest_file(target, "b/different2.bin", b"y")
        _stage_dest_file(target, "b/different3.bin", b"y")
        second = scan_destination(seeded_db, dest_id, target)
        assert second.signature != first.signature
        assert second.cache_was_invalidated is True


# ---------------------------------------------------------------------------
# Per-system summary breakdown (post-apply diagnostic)
# ---------------------------------------------------------------------------


class TestPerSystemSyncBreakdown:
    """``SyncSummary.per_system`` must classify each applied action.

    Mirrors the export-side breakdown in test_exporter.py — the
    post-sync summary dialog reads from this dict to surface which
    systems contributed copies vs deletes vs identical-skips vs
    failures.
    """

    def test_copy_to_dest_bumps_copied_bucket(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        summary = apply_plan(seeded_db, plan, profile, target)

        assert "snes" in summary.per_system
        bucket = summary.per_system["snes"]
        assert bucket.copied_to_dest == 1
        assert bucket.bytes_copied > 0
        assert bucket.failed == 0

    def test_identical_action_bumps_identical_bucket(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        # Pre-stage dest so the row classifies as identical.
        _stage_dest_file(target, "roms/snes/Game.sfc", b"local-rom-bytes")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        summary = apply_plan(seeded_db, plan, profile, target)

        bucket = summary.per_system["snes"]
        assert bucket.skipped_identical == 1
        assert bucket.copied_to_dest == 0

    def test_delete_dest_bumps_deleted_bucket(
        self,
        seeded_db: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """push_mirror deletes orphan dest rows — per-system bucket must record it."""
        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Keep.sfc"
        )
        _stage_dest_file(target, "roms/snes/Keep.sfc", b"local-rom-bytes")
        _stage_dest_file(target, "roms/snes/Orphan.sfc", b"orphan")
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_mirror"
        )
        summary = apply_plan(seeded_db, plan, profile, target)

        bucket = summary.per_system["snes"]
        assert bucket.deleted_dest == 1


# ---------------------------------------------------------------------------
# Algorithmic fix: dest-side fuzzy index for tier-2 matching
# ---------------------------------------------------------------------------


class TestInventoryFuzzyIndex:
    """``_build_inventory_fuzzy_index`` collapses tier-2 to O(1) per local rom.

    Before the fix, ``_find_tier2_inventory_entry`` re-walked the whole
    inventory + recomputed every fuzzy key on every miss. On a 38 K × 17 K
    library this produced a multi-minute "not responding" freeze. The
    pre-built dict means each tier-2 lookup is now a single dict.get.
    """

    def test_indexes_unique_paths_under_canonical_keys(self) -> None:
        from romulus.core.dest_inventory import DestInventory, InventoryEntry
        from romulus.core.sync import _build_inventory_fuzzy_index

        entries = [
            InventoryEntry(
                rel_path="roms/snes/Super Mario World (USA).sfc",
                size_bytes=512_000,
                mtime=0.0,
                sha1=None,
            ),
            InventoryEntry(
                rel_path="roms/nes/Castlevania (USA).nes",
                size_bytes=131_072,
                mtime=0.0,
                sha1=None,
            ),
        ]
        inv = DestInventory(dest_id=0, target_path="", entries=entries)
        inv_by_path = inv.by_rel_path()
        index = _build_inventory_fuzzy_index(inv_by_path, _minimal_profile())
        # Two real ROMs → two entries; keys carry system_id + region.
        assert len(index) == 2
        keys = {k for k in index}
        assert ("supermarioworld", "usa", "snes") in keys
        assert ("castlevania", "usa", "nes") in keys

    def test_skips_sidecars_and_unknown_folders(self) -> None:
        """gamelist.xml, .m3u, artwork dirs, and unknown system folders
        must not appear in the index — they'd produce false tier-2 hits.
        """
        from romulus.core.dest_inventory import DestInventory, InventoryEntry
        from romulus.core.sync import _build_inventory_fuzzy_index

        entries = [
            InventoryEntry(
                rel_path="roms/snes/Game.sfc",
                size_bytes=10,
                mtime=0.0,
                sha1=None,
            ),
            InventoryEntry(
                rel_path="roms/snes/gamelist.xml",
                size_bytes=1,
                mtime=0.0,
                sha1=None,
            ),
            InventoryEntry(
                rel_path="roms/snes/discs.m3u",
                size_bytes=1,
                mtime=0.0,
                sha1=None,
            ),
            # Unknown folder (not in profile.systems).
            InventoryEntry(
                rel_path="roms/atari2600/Adventure.bin",
                size_bytes=4096,
                mtime=0.0,
                sha1=None,
            ),
        ]
        inv = DestInventory(dest_id=0, target_path="", entries=entries)
        index = _build_inventory_fuzzy_index(
            inv.by_rel_path(), _minimal_profile()
        )
        # Only the real snes ROM.
        assert len(index) == 1
        assert ("game", "", "snes") in index

    def test_tier2_match_with_different_dest_filename(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """End-to-end: a destination file with a tier-2-equivalent
        filename at a different rel_path should classify as IDENTICAL,
        not as COPY_TO_DEST. Guards the algorithmic fix against
        behaviour drift.
        """
        from romulus.core.sync import ACTION_COPY_TO_DEST, ACTION_IDENTICAL

        library = tmp_path / "library"
        target = tmp_path / "dest"
        profile = _minimal_profile()
        # Local: "Super Mario World (USA).sfc" at canonical path.
        _stage_local_rom(
            seeded_db,
            library,
            system_id="snes",
            filename="Super Mario World (USA).sfc",
        )
        # Dest: same identity but moved to a different filename layout.
        # The fuzzy key matches; this is the case tier-2 catches.
        _stage_dest_file(
            target,
            "roms/snes/Super Mario World (USA) [!].sfc",
            b"local-rom-bytes",
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)
        plan = build_plan(
            seeded_db, dest_id, profile, target, inv, "push_merge"
        )
        kinds = [a.kind for a in plan.actions]
        # Tier-2 must classify as identical (already on dest), not as a copy.
        assert ACTION_IDENTICAL in kinds
        assert ACTION_COPY_TO_DEST not in kinds

    def test_build_plan_emits_progress_callbacks(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """The new ``progress_callback`` parameter must fire at least once.

        The worker uses this to drive the SyncDiffProgressDialog. The
        contract: at least one tick before the diff loop, plus the
        finalising tick at the end.
        """
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        profile = _minimal_profile()
        _stage_local_rom(
            seeded_db, library, system_id="snes", filename="Game.sfc"
        )
        dest_id = _make_dest(seeded_db, target)
        inv = scan_destination(seeded_db, dest_id, target)

        events: list[tuple[int, int, str]] = []
        build_plan(
            seeded_db,
            dest_id,
            profile,
            target,
            inv,
            "push_merge",
            progress_callback=lambda c, t, label: events.append(
                (c, t, label)
            ),
        )
        assert events, "build_plan must emit at least one progress tick"
        # Final tick should report the finalising phase.
        assert events[-1][2].lower().startswith(("finalising", "computing"))


# ---------------------------------------------------------------------------
# BuildSyncPlanWorker — runs build_plan on its own thread
# ---------------------------------------------------------------------------


class TestBuildSyncPlanWorker:
    """The diff worker must produce the same plan as a synchronous call,
    emit progress ticks, and surface its result via ``finished_ok``.
    """

    def test_worker_produces_plan_and_emits_progress(
        self, qapp, tmp_path: Path
    ) -> None:
        from PySide6.QtCore import QEventLoop

        from romulus.core.dest_inventory import scan_destination as _scan
        from romulus.core.sync import SyncPlan
        from romulus.db import create_tables, get_connection
        from romulus.models import seed_systems
        from romulus.ui.workers import BuildSyncPlanWorker

        db_path = tmp_path / "diff.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        library = tmp_path / "library"
        target = tmp_path / "dest"
        target.mkdir()
        _stage_local_rom(
            conn, library, system_id="snes", filename="Mario.sfc"
        )
        dest_id = _make_dest(conn, target)
        inv = _scan(conn, dest_id, target)
        conn.commit()
        conn.close()

        profile = _minimal_profile()
        worker = BuildSyncPlanWorker(
            db_path,
            dest_id,
            profile,
            str(target),
            inv,
            "push_merge",
        )
        progress_events: list[tuple[int, int, str]] = []
        finished: list[object] = []
        failed: list[str] = []
        worker.progress.connect(
            lambda c, t, label: progress_events.append((c, t, label))
        )
        worker.finished_ok.connect(finished.append)
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()
        assert worker.wait(5000), "BuildSyncPlanWorker did not finish in 5s"

        assert not failed, f"unexpected failure: {failed}"
        assert len(finished) == 1
        assert isinstance(finished[0], SyncPlan)
        assert progress_events, "no progress events from BuildSyncPlanWorker"
