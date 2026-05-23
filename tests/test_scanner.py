"""Tests for scanner — folder detection, filename parsing, fuzzy keys, walk."""

from __future__ import annotations

import pytest

from romulus.core.scanner import (
    SIDE_FILE_EXTENSIONS,
    detect_system,
    generate_fuzzy_key,
    is_rom_file,
    is_side_file,
    scan_library,
)
from romulus.models.system import get_systems_by_alias

# ---------------------------------------------------------------------------
# Folder-to-system detection
# ---------------------------------------------------------------------------


class TestFolderDetection:
    def test_exact_match(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("snes", alias_map) == "snes"

    def test_case_insensitive(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("SNES", alias_map) == "snes"
        assert detect_system("SnEs", alias_map) == "snes"

    def test_genesis_alias_resolves_to_megadrive(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("genesis", alias_map) == "megadrive"

    def test_megadrive_alias_resolves_to_megadrive(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("megadrive", alias_map) == "megadrive"

    def test_md_alias_resolves_to_megadrive(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("md", alias_map) == "megadrive"

    def test_superfamicom_alias_resolves_to_snes(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("superfamicom", alias_map) == "snes"

    def test_unknown_folder_returns_none(self, seeded_db):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system("notarealsystem", alias_map) is None

    @pytest.mark.parametrize(
        "folder,expected",
        [
            ("nes", "nes"),
            ("famicom", "nes"),
            ("gbc", "gbc"),
            ("gameboycolor", "gbc"),
            ("psx", "psx"),
            ("playstation", "psx"),
            ("psone", "psx"),
            ("atari2600", "atari2600"),
            ("a2600", "atari2600"),
            ("tg16", "pcengine"),
            ("pce", "pcengine"),
        ],
    )
    def test_common_aliases(self, seeded_db, folder, expected):
        alias_map = get_systems_by_alias(seeded_db)
        assert detect_system(folder, alias_map) == expected


# ---------------------------------------------------------------------------
# Side-file filtering
# ---------------------------------------------------------------------------


class TestSideFiles:
    @pytest.mark.parametrize(
        "filename",
        [
            "Game.cue",
            "Game.m3u",
            "Game.txt",
            "Game.nfo",
            "Game.jpg",
            "Game.png",
            "Game.xml",
            "Game.sav",
            "Game.srm",
            "Game.state",
            "Game.oops",
        ],
    )
    def test_side_file_extensions_filtered(self, filename):
        assert is_side_file(filename) is True

    @pytest.mark.parametrize(
        "filename",
        ["Game.sfc", "Game.gba", "Game.iso", "Game.bin", "Game.md"],
    )
    def test_rom_files_not_side_files(self, filename):
        assert is_side_file(filename) is False

    def test_side_files_match_spec_list(self):
        # Sanity: the published Session 2 spec lists these explicitly.
        required = {
            ".cue", ".m3u", ".sub", ".txt", ".nfo", ".jpg", ".png",
            ".xml", ".dat", ".sav", ".srm", ".state", ".oops",
        }
        assert required.issubset(SIDE_FILE_EXTENSIONS)


# ---------------------------------------------------------------------------
# Extension acceptance per system
# ---------------------------------------------------------------------------


class TestIsRomFile:
    def test_sfc_accepted_for_snes(self):
        assert is_rom_file("Game.sfc", [".sfc", ".smc"]) is True

    def test_uppercase_extension_accepted(self):
        assert is_rom_file("Game.SFC", [".sfc"]) is True

    def test_unknown_extension_rejected(self):
        assert is_rom_file("Game.gba", [".sfc", ".smc"]) is False

    def test_zip_accepted_even_when_not_in_system_extensions(self):
        """Genesis/MD ROMs are typically stored zipped, but megadrive's
        accepted_extensions list only carries .md/.gen/.bin/.smd. The scanner
        accepts .zip universally so those files don't get silently dropped.
        """
        assert is_rom_file("Sonic.zip", [".md", ".gen", ".bin"]) is True

    def test_7z_accepted_even_when_not_in_system_extensions(self):
        assert is_rom_file("Sonic.7z", [".md", ".gen", ".bin"]) is True

    def test_uppercase_zip_accepted(self):
        assert is_rom_file("Game.ZIP", [".sfc"]) is True


# ---------------------------------------------------------------------------
# Fuzzy key generation
# ---------------------------------------------------------------------------


class TestFuzzyKey:
    def test_simple_lowercase(self):
        assert generate_fuzzy_key("Sonic") == "sonic"

    def test_strips_spaces(self):
        assert generate_fuzzy_key("Sonic the Hedgehog") == "sonicthehedgehog"

    def test_strips_separators(self):
        assert generate_fuzzy_key("Aero-the-Acro_Bat") == "aerotheacrobat"

    def test_strips_punctuation(self):
        assert generate_fuzzy_key("Castlevania: Symphony of the Night") == (
            "castlevaniasymphonyofthenight"
        )

    def test_trailing_article_to_front(self):
        # "Addams Family, The" → "The Addams Family" → strip "the" → "addamsfamily"
        assert generate_fuzzy_key("Addams Family, The") == "addamsfamily"

    def test_leading_article_stripped(self):
        assert generate_fuzzy_key("The Addams Family") == "addamsfamily"

    def test_no_article_unchanged(self):
        assert generate_fuzzy_key("Addams Family") == "addamsfamily"

    def test_french_article(self):
        # "Le Petit" → "petit"
        assert generate_fuzzy_key("Le Petit Prince") == "petitprince"

    def test_three_forms_of_addams_family_match(self):
        keys = {
            generate_fuzzy_key("Addams Family, The"),
            generate_fuzzy_key("The Addams Family"),
            generate_fuzzy_key("Addams Family"),
        }
        assert len(keys) == 1

    def test_roman_numeral_six(self):
        assert generate_fuzzy_key("Final Fantasy VI") == "finalfantasy6"

    def test_roman_numeral_three(self):
        assert generate_fuzzy_key("Final Fantasy III") == "finalfantasy3"

    def test_single_letter_roman_not_converted(self):
        # Single-letter "I" is ambiguous (word "I", first sequel). Per spec, skip.
        assert generate_fuzzy_key("Final Fantasy I") == "finalfantasyi"

    def test_sequel_number_preserved(self):
        # Bare integers at end are sequel numbers, NOT version suffixes.
        assert generate_fuzzy_key("Aero the Acro-Bat 2") == "aerotheacrobat2"

    def test_version_suffix_stripped(self):
        # v1.1 IS a version suffix and should be stripped.
        assert generate_fuzzy_key("Game v1.1") == "game"

    def test_rev_suffix_stripped(self):
        assert generate_fuzzy_key("Game Rev 1") == "game"

    def test_decimal_version_stripped(self):
        assert generate_fuzzy_key("Game 1.1") == "game"

    def test_pure_sequel_number_kept(self):
        # "Game 2" — bare integer is a sequel, keep it.
        assert generate_fuzzy_key("Game 2") == "game2"


class TestFuzzyKeyCollapseEquivalents:
    """The acceptance criterion for Session 2 lives here."""

    def test_addams_family_three_forms(self):
        a = generate_fuzzy_key("Addams Family, The")
        b = generate_fuzzy_key("The Addams Family")
        c = generate_fuzzy_key("Addams Family")
        assert a == b == c

    def test_acro_bat_punctuation_variants(self):
        names = [
            "Aero the Acro-Bat 2",
            "Aero The Acro-bat 2",
            "Aero-the-Acro_Bat_2",
            "Aero the AcroBat 2",
        ]
        keys = {generate_fuzzy_key(n) for n in names}
        assert len(keys) == 1


# ---------------------------------------------------------------------------
# Full scan against a mock directory tree
# ---------------------------------------------------------------------------


def _make_tree(root):
    """Create a small ROM library tree under `root`."""
    (root / "snes").mkdir()
    (root / "snes" / "Super Mario World (USA).sfc").write_bytes(b"\x00" * 1024)
    (root / "snes" / "Addams Family, The (USA).sfc").write_bytes(b"\x00" * 1024)
    (root / "snes" / "The Addams Family (Europe).sfc").write_bytes(b"\x00" * 1024)
    (root / "snes" / "screenshot.png").write_bytes(b"fake")
    (root / "snes" / "save.srm").write_bytes(b"fake")

    (root / "genesis").mkdir()  # alias for megadrive
    (root / "genesis" / "Sonic the Hedgehog (USA, Europe).md").write_bytes(b"\x00" * 1024)
    (root / "genesis" / "Streets of Rage 2 (USA).md").write_bytes(b"\x00" * 1024)

    (root / "nes").mkdir()
    (root / "nes" / "subdir").mkdir()
    (root / "nes" / "subdir" / "Castlevania (USA).nes").write_bytes(b"\x00" * 1024)

    (root / "unknown_console").mkdir()
    (root / "unknown_console" / "Mystery.bin").write_bytes(b"\x00" * 1024)


class TestFullScan:
    def test_scan_finds_rom_files(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        result = scan_library(seeded_db, tmp_path)
        # 2 SNES + 1 Europe SNES + 2 Genesis + 1 NES nested = 6
        # Note: Addams Family (USA) and (Europe) are 2 separate ROMs.
        assert result.files_found == 6

    def test_scan_skips_side_files(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        scan_library(seeded_db, tmp_path)
        # No ROM with .png or .srm extension should have been enrolled.
        rows = seeded_db.execute(
            "SELECT extension FROM roms"
        ).fetchall()
        extensions = {row[0] for row in rows}
        assert ".png" not in extensions
        assert ".srm" not in extensions

    def test_scan_skips_unknown_systems(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        scan_library(seeded_db, tmp_path)
        # Mystery.bin under unknown_console should NOT have been enrolled.
        rows = seeded_db.execute(
            "SELECT path FROM roms WHERE filename = 'Mystery.bin'"
        ).fetchall()
        assert rows == []

    def test_scan_recognizes_genesis_as_megadrive(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        scan_library(seeded_db, tmp_path)
        rows = seeded_db.execute(
            "SELECT system_id FROM roms WHERE filename LIKE '%Sonic%'"
        ).fetchall()
        assert all(row[0] == "megadrive" for row in rows)

    def test_scan_enrolls_zipped_genesis_roms(self, seeded_db, tmp_path):
        """Regression for user-reported bug: a genesis folder of .zip + .srm
        files was silently skipped because megadrive's accepted_extensions
        didn't include .zip. Archives are now universally accepted.
        """
        genesis = tmp_path / "genesis"
        genesis.mkdir()
        (genesis / "Sonic the Hedgehog (USA).zip").write_bytes(b"PK\x03\x04" * 16)
        (genesis / "Streets of Rage 2 (USA).zip").write_bytes(b"PK\x03\x04" * 16)
        (genesis / "Sonic the Hedgehog (USA).srm").write_bytes(b"save data")
        result = scan_library(seeded_db, tmp_path)
        assert result.files_found == 2  # both zips enrolled
        rows = seeded_db.execute(
            "SELECT filename, system_id, extension FROM roms ORDER BY filename"
        ).fetchall()
        assert len(rows) == 2
        assert all(r["system_id"] == "megadrive" for r in rows)
        assert all(r["extension"] == ".zip" for r in rows)

    def test_scan_enrolls_7z_archives(self, seeded_db, tmp_path):
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Game.7z").write_bytes(b"7z\xbc\xaf\x27\x1c")
        result = scan_library(seeded_db, tmp_path)
        assert result.files_found == 1

    def test_release_type_keeps_cartridge_and_vc_as_distinct_roms(
        self, seeded_db, tmp_path
    ):
        """Regression: a cartridge dump and a Virtual Console dump must NOT
        collapse into the same fuzzy_key.  The release_type tag is appended
        to fuzzy_key so the two ROMs are distinguishable.
        """
        md = tmp_path / "genesis"
        md.mkdir()
        (md / "Alien Soldier.zip").write_bytes(b"PK\x03\x04")
        (md / "Alien Soldier (USA) (Virtual Console).zip").write_bytes(b"PK\x03\x04")
        scan_library(seeded_db, tmp_path)

        rows = seeded_db.execute(
            "SELECT fuzzy_key FROM roms WHERE system_id = 'megadrive' "
            "ORDER BY fuzzy_key"
        ).fetchall()
        assert len(rows) == 2
        keys = {r["fuzzy_key"] for r in rows}
        # The VC rom must have a distinct fuzzy_key from the plain one.
        assert len(keys) == 2, "cartridge and VC roms must have distinct fuzzy_keys"

    def test_scan_walks_subdirectories(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        scan_library(seeded_db, tmp_path)
        # Nested Castlevania under nes/subdir should be enrolled as nes.
        row = seeded_db.execute(
            "SELECT system_id FROM roms WHERE filename LIKE 'Castlevania%'"
        ).fetchone()
        assert row is not None
        assert row[0] == "nes"

    def test_scan_populates_fuzzy_key(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        scan_library(seeded_db, tmp_path)
        rows = seeded_db.execute(
            "SELECT fuzzy_key FROM roms WHERE filename LIKE 'Super Mario%'"
        ).fetchall()
        assert rows[0][0] == "supermarioworld"

    def test_scan_creates_scan_history_row(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        result = scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT scan_type, files_found, finished_at FROM scan_history WHERE id = ?",
            (result.scan_id,),
        ).fetchone()
        assert row["scan_type"] == "quick"
        assert row["files_found"] == 6
        assert row["finished_at"] is not None

    def test_scan_progress_callback_invoked(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        seen: list[tuple[int, str]] = []
        scan_library(
            seeded_db,
            tmp_path,
            progress_callback=lambda n, name: seen.append((n, name)),
        )
        # The scanner emits phase-transition labels during the post-walk DB
        # phases ("Marking missing entries…", "Finalising scan history…") so
        # the UI dialog can show activity instead of a frozen Cancel button.
        # Filter those out — they all end with the literal Unicode ellipsis —
        # and check only the per-file ticks.
        file_events = [
            (n, name) for n, name in seen if not name.endswith("…")
        ]
        assert len(file_events) == 6
        # Each per-file call should report a strictly increasing count.
        assert [n for n, _ in file_events] == list(range(1, 7))

    def test_rescan_is_idempotent(self, seeded_db, tmp_path):
        _make_tree(tmp_path)
        first = scan_library(seeded_db, tmp_path)
        second = scan_library(seeded_db, tmp_path)
        # File count stays the same — no duplicate rows.
        count = seeded_db.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
        assert count == 6
        assert first.files_found == second.files_found

    def test_library_root_named_as_system(self, seeded_db, tmp_path):
        # User points the library at /tmp/.../snes directly.
        snes_root = tmp_path / "snes"
        snes_root.mkdir()
        (snes_root / "Game.sfc").write_bytes(b"\x00" * 1024)
        result = scan_library(seeded_db, snes_root)
        assert result.files_found == 1
        row = seeded_db.execute(
            "SELECT system_id FROM roms WHERE filename = 'Game.sfc'"
        ).fetchone()
        assert row[0] == "snes"


# ---------------------------------------------------------------------------
# match_confidence is monotonic across rescans
# ---------------------------------------------------------------------------


class TestRescanPreservesMatchConfidence:
    """A Quick rescan must never downgrade a stronger prior match.

    The upsert in `queries.upsert_rom` re-runs with `match_confidence='fuzzy'`,
    so without protection a previous Heavy Scan result of `dat_verified` would
    regress. The CASE expression in `upsert_rom` enforces monotonic upgrades.
    """

    def test_rescan_does_not_downgrade_dat_verified(self, seeded_db, tmp_path):
        from romulus.db import queries

        snes = tmp_path / "snes"
        snes.mkdir()
        rom = snes / "Game.sfc"
        rom.write_bytes(b"\x00" * 1024)

        scan_library(seeded_db, tmp_path)
        rom_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Game.sfc'"
        ).fetchone()[0]

        # Simulate the Heavy Scan stamping the ROM as DAT-verified.
        queries.update_rom_match(seeded_db, rom_id, "Game (USA)", "dat_verified")
        seeded_db.commit()

        # User runs Quick Scan again — confidence must stay dat_verified.
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT match_confidence, dat_match FROM roms WHERE id = ?",
            (rom_id,),
        ).fetchone()
        assert row["match_confidence"] == "dat_verified"
        assert row["dat_match"] == "Game (USA)"

    def test_rescan_does_not_downgrade_header_to_fuzzy(self, seeded_db, tmp_path):
        from romulus.db import queries

        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Game.sfc").write_bytes(b"\x00" * 1024)

        scan_library(seeded_db, tmp_path)
        rom_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Game.sfc'"
        ).fetchone()[0]
        queries.update_rom_match(seeded_db, rom_id, "HeaderTitle", "header")
        seeded_db.commit()

        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT match_confidence FROM roms WHERE id = ?",
            (rom_id,),
        ).fetchone()
        assert row["match_confidence"] == "header"

    def test_upgrades_still_work(self, seeded_db):
        # Direct unit test of upsert_rom — going fuzzy -> dat_verified must apply.
        from romulus.db import queries

        rom_id = queries.upsert_rom(
            seeded_db,
            {
                "path": "/fake/Game.sfc",
                "filename": "Game.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": 1000.0,
                "system_id": "snes",
                "fuzzy_key": "game",
                "match_confidence": "fuzzy",
            },
        )
        # Same path, now claiming dat_verified — should upgrade.
        queries.upsert_rom(
            seeded_db,
            {
                "path": "/fake/Game.sfc",
                "filename": "Game.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": 1000.0,
                "system_id": "snes",
                "fuzzy_key": "game",
                "match_confidence": "dat_verified",
            },
        )
        row = seeded_db.execute(
            "SELECT match_confidence FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["match_confidence"] == "dat_verified"


# ---------------------------------------------------------------------------
# Identity fields written directly to roms rows (v0.4.0 — no games table)
# ---------------------------------------------------------------------------


class TestIdentityFieldsOnRoms:
    """Quick Scan must populate title / region / revision / is_hack on roms rows."""

    def test_title_populated_from_filename(self, seeded_db, tmp_path):
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Super Mario World (USA).sfc").write_bytes(b"\x00" * 1024)
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT title FROM roms WHERE filename = 'Super Mario World (USA).sfc'"
        ).fetchone()
        assert row is not None
        assert row["title"] == "Super Mario World"

    def test_region_populated_from_filename(self, seeded_db, tmp_path):
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Chrono Trigger (USA).sfc").write_bytes(b"\x00" * 1024)
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT region FROM roms WHERE filename = 'Chrono Trigger (USA).sfc'"
        ).fetchone()
        assert row is not None
        assert row["region"] == "USA"

    def test_revision_populated_from_filename(self, seeded_db, tmp_path):
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Street Fighter II (USA) (Rev 1).sfc").write_bytes(b"\x00" * 1024)
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT revision FROM roms WHERE filename LIKE 'Street Fighter%'"
        ).fetchone()
        assert row is not None
        assert row["revision"] == "Rev 1"

    def test_is_hack_set_for_bracket_h_tag(self, seeded_db, tmp_path):
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Super Mario World [h1].sfc").write_bytes(b"\x00" * 1024)
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT is_hack FROM roms WHERE filename LIKE '%[h1]%'"
        ).fetchone()
        assert row is not None
        assert row["is_hack"] == 1

    def test_no_region_when_not_present(self, seeded_db, tmp_path):
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Mystery Game.sfc").write_bytes(b"\x00" * 1024)
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT title, region FROM roms WHERE filename = 'Mystery Game.sfc'"
        ).fetchone()
        assert row is not None
        assert row["title"] == "Mystery Game"
        assert row["region"] is None

    def test_two_regions_same_title_have_distinct_regions(self, seeded_db, tmp_path):
        """USA and Japan regional variants must each keep their own region value."""
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Super Metroid (USA).sfc").write_bytes(b"\x00" * 1024)
        (snes / "Super Metroid (Japan).sfc").write_bytes(b"\x00" * 1024)
        scan_library(seeded_db, tmp_path)
        rows = seeded_db.execute(
            "SELECT region FROM roms WHERE filename LIKE 'Super Metroid%' "
            "ORDER BY filename"
        ).fetchall()
        assert len(rows) == 2
        regions = {r["region"] for r in rows}
        assert "USA" in regions
        assert "Japan" in regions

    def test_rescan_does_not_overwrite_dat_derived_title(self, seeded_db, tmp_path):
        """A Quick Scan rescan must not clobber a DAT-derived canonical_name."""
        snes = tmp_path / "snes"
        snes.mkdir()
        (snes / "Game.sfc").write_bytes(b"\x00" * 1024)

        scan_library(seeded_db, tmp_path)
        rom_id = seeded_db.execute(
            "SELECT id FROM roms WHERE filename = 'Game.sfc'"
        ).fetchone()[0]

        # Simulate Heavy Scan stamping a canonical_name.
        seeded_db.execute(
            "UPDATE roms SET canonical_name = 'Canonical Title (USA)' WHERE id = ?",
            (rom_id,),
        )
        seeded_db.commit()

        # Rescan — canonical_name must survive via COALESCE.
        scan_library(seeded_db, tmp_path)
        row = seeded_db.execute(
            "SELECT canonical_name FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["canonical_name"] == "Canonical Title (USA)"
