"""Tests for the export engine and destination profile loader.

The exporter never writes outside ``tmp_path`` — every test stages a small
on-disk ROM library, exports it into ``tmp_path/out``, and inspects the
resulting tree. Atomic-write behaviour is verified the same way the
organizer's test does: monkeypatch ``atomic.os.replace`` to raise and assert
no partial files leak into the target.
"""

from __future__ import annotations

import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from romulus.core import atomic
from romulus.core.exporter import (
    BUILTIN_PROFILES_DIR,
    ExportFilters,
    ExportOptions,
    export_collection,
    generate_gamelist_xml,
    generate_m3u_playlists,
    load_all_profiles,
    load_profile,
    preview_export,
)
from romulus.db import queries as q
from romulus.models.profile import DestinationProfile, SystemMapping
from romulus.models.system import SYSTEM_REGISTRY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rom_file(path: Path, content: bytes = b"rom-bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _insert_rom_with_game(
    conn: sqlite3.Connection,
    *,
    path: str,
    system_id: str,
    extension: str,
    filename: str,
    size_bytes: int,
    title: str,
    region: str | None = None,
    canonical_name: str | None = None,
) -> tuple[int, int]:
    """Insert a games row and the matching roms row; return both ids."""
    game_id = q.upsert_game(
        conn,
        {
            "title": title,
            "system_id": system_id,
            "canonical_name": canonical_name,
            "region": region,
        },
    )
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
    q.link_rom_to_game(conn, rom_id, game_id)
    conn.commit()
    return rom_id, game_id


def _build_minimal_profile(
    *, gamelist: str | None = "emulationstation_xml"
) -> DestinationProfile:
    """A tiny profile mapping snes + nes + gba; everything else unsupported."""
    systems: dict[str, SystemMapping] = {}
    for sys_def in SYSTEM_REGISTRY:
        if sys_def.id == "snes":
            systems[sys_def.id] = SystemMapping(
                folder="snes", extensions=[".sfc"], supported=True
            )
        elif sys_def.id == "nes":
            systems[sys_def.id] = SystemMapping(
                folder="nes", extensions=[".nes"], supported=True
            )
        elif sys_def.id == "gba":
            systems[sys_def.id] = SystemMapping(
                folder="gba", extensions=[".gba"], supported=True
            )
        else:
            systems[sys_def.id] = SystemMapping(folder="", supported=False)
    return DestinationProfile(
        id="test",
        name="Test Profile",
        base_path="roms",
        gamelist_format=gamelist,
        artwork_subdir="downloaded_media",
        multi_disc="m3u",
        systems=systems,
    )


# ---------------------------------------------------------------------------
# Profile YAML loading
# ---------------------------------------------------------------------------


class TestProfileLoading:
    def test_load_profile_round_trips(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "x.yaml"
        yaml_path.write_text(
            """
id: example
name: Example
base_path: roms
gamelist_format: emulationstation_xml
systems:
  snes:
    folder: snes
    extensions: [".sfc"]
  nes:
    folder: ""
    supported: false
""",
            encoding="utf-8",
        )
        profile = load_profile(yaml_path)
        assert profile.id == "example"
        assert profile.systems["snes"].is_supported is True
        assert profile.systems["nes"].is_supported is False

    def test_load_all_profiles_skips_invalid_yaml(self, tmp_path: Path) -> None:
        good = tmp_path / "good.yaml"
        good.write_text(
            "id: ok\nname: OK\nbase_path: r\nsystems: {}\n", encoding="utf-8"
        )
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: a: valid yaml: : :", encoding="utf-8")
        profiles = load_all_profiles(builtin_dir=tmp_path)
        assert "ok" in profiles
        assert "bad" not in profiles

    def test_user_dir_overrides_builtin(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        builtin.mkdir()
        user.mkdir()
        (builtin / "p.yaml").write_text(
            "id: p\nname: BuiltIn\nbase_path: r\nsystems: {}\n",
            encoding="utf-8",
        )
        (user / "p.yaml").write_text(
            "id: p\nname: UserCustom\nbase_path: r\nsystems: {}\n",
            encoding="utf-8",
        )
        profiles = load_all_profiles(builtin_dir=builtin, user_dir=user)
        assert profiles["p"].name == "UserCustom"


# ---------------------------------------------------------------------------
# Built-in profile coverage — every registry system has an explicit decision.
# ---------------------------------------------------------------------------


class TestBuiltInProfileCoverage:
    """Every built-in profile must list a folder mapping for every registry
    system (either ``supported: true`` with a folder, or ``supported: false``).
    Silently omitting a system would let a target ship without a clear
    decision for, e.g., GBA support — the carry-forward rule for session 10.
    """

    EXPECTED_PROFILE_IDS = {
        "batocera",
        "retropie",
        "onionos",
        "muos",
        "mister",
        "analogue-pocket",
    }

    def test_all_six_built_in_profiles_load(self) -> None:
        profiles = load_all_profiles(builtin_dir=BUILTIN_PROFILES_DIR)
        assert set(profiles) >= self.EXPECTED_PROFILE_IDS

    def test_every_system_has_an_explicit_decision(self) -> None:
        profiles = load_all_profiles(builtin_dir=BUILTIN_PROFILES_DIR)
        system_ids = {s.id for s in SYSTEM_REGISTRY}
        for profile_id in self.EXPECTED_PROFILE_IDS:
            profile = profiles[profile_id]
            missing = system_ids - set(profile.systems.keys())
            assert missing == set(), (
                f"profile {profile_id!r} is missing explicit decisions for: "
                f"{sorted(missing)}"
            )

    def test_supported_systems_have_a_folder(self) -> None:
        profiles = load_all_profiles(builtin_dir=BUILTIN_PROFILES_DIR)
        for profile_id in self.EXPECTED_PROFILE_IDS:
            profile = profiles[profile_id]
            for system_id, mapping in profile.systems.items():
                if mapping.supported:
                    assert mapping.folder, (
                        f"profile {profile_id!r}: system {system_id!r} is "
                        f"marked supported but has no folder"
                    )


# ---------------------------------------------------------------------------
# preview_export
# ---------------------------------------------------------------------------


class TestPreviewExport:
    def test_counts_files_and_size(self, seeded_db, tmp_path: Path) -> None:
        rom_path = tmp_path / "lib" / "snes" / "mario.sfc"
        _make_rom_file(rom_path, content=b"x" * 1234)
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="mario.sfc",
            size_bytes=1234,
            title="Super Mario World",
        )
        profile = _build_minimal_profile()
        preview = preview_export(
            seeded_db, profile, tmp_path / "out", ExportFilters()
        )
        assert preview.file_count == 1
        assert preview.total_size_bytes == 1234
        assert preview.by_system["snes"] == 1
        folder_keys = list(preview.folder_tree.keys())
        assert len(folder_keys) == 1
        assert folder_keys[0].endswith("out/roms/snes")

    def test_unsupported_systems_are_reported(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "gc" / "wind.iso"
        _make_rom_file(rom_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="gamecube",
            extension=".iso",
            filename="wind.iso",
            size_bytes=12,
            title="Wind Waker",
        )
        profile = _build_minimal_profile()
        preview = preview_export(
            seeded_db, profile, tmp_path / "out", ExportFilters()
        )
        assert preview.file_count == 0
        assert "gamecube" in preview.unsupported_systems

    def test_system_filter(self, seeded_db, tmp_path: Path) -> None:
        snes_path = tmp_path / "lib" / "snes" / "a.sfc"
        nes_path = tmp_path / "lib" / "nes" / "b.nes"
        _make_rom_file(snes_path)
        _make_rom_file(nes_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(snes_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="a.sfc",
            size_bytes=10,
            title="SNES Game",
        )
        _insert_rom_with_game(
            seeded_db,
            path=str(nes_path).replace("\\", "/"),
            system_id="nes",
            extension=".nes",
            filename="b.nes",
            size_bytes=20,
            title="NES Game",
        )
        profile = _build_minimal_profile()
        preview = preview_export(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(systems=["nes"]),
        )
        assert preview.file_count == 1
        assert preview.by_system == {"nes": 1}


# ---------------------------------------------------------------------------
# export_collection — file copies
# ---------------------------------------------------------------------------


class TestExportCopies:
    def test_files_copied_to_correct_folder_structure(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path, content=b"snes-bytes")
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Super Mario World",
        )
        profile = _build_minimal_profile()
        target = tmp_path / "out"
        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        expected = target / "roms" / "snes" / "Mario.sfc"
        assert expected.exists()
        assert expected.read_bytes() == b"snes-bytes"
        assert summary.files_copied == 1
        assert summary.bytes_copied == 10
        assert summary.systems == ["snes"]

    def test_idempotent_when_destination_exists(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path, content=b"bytes-1234")
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Super Mario World",
        )
        profile = _build_minimal_profile()
        target = tmp_path / "out"
        export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        # Second pass — destination exists with same size; we expect a skip.
        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        assert summary.files_copied == 0
        assert summary.files_skipped == 1

    def test_unsupported_system_skipped(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "gc" / "wind.iso"
        _make_rom_file(rom_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="gamecube",
            extension=".iso",
            filename="wind.iso",
            size_bytes=10,
            title="Wind Waker",
        )
        profile = _build_minimal_profile()
        target = tmp_path / "out"
        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        assert summary.files_copied == 0
        assert summary.files_skipped == 1
        assert not (target / "roms" / "gamecube").exists()

    def test_progress_callback_fires_per_file(
        self, seeded_db, tmp_path: Path
    ) -> None:
        for stem in ("a", "b", "c"):
            rom_path = tmp_path / "lib" / "snes" / f"{stem}.sfc"
            _make_rom_file(rom_path)
            _insert_rom_with_game(
                seeded_db,
                path=str(rom_path).replace("\\", "/"),
                system_id="snes",
                extension=".sfc",
                filename=f"{stem}.sfc",
                size_bytes=9,
                title=f"Game {stem}",
            )
        profile = _build_minimal_profile()
        ticks: list[tuple[int, int, str]] = []
        export_collection(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
            progress_callback=lambda i, total, name: ticks.append((i, total, name)),
        )
        assert [t[0] for t in ticks] == [1, 2, 3]
        assert all(t[1] == 3 for t in ticks)

    def test_progress_callback_can_cancel(
        self, seeded_db, tmp_path: Path
    ) -> None:
        for stem in ("a", "b", "c"):
            rom_path = tmp_path / "lib" / "snes" / f"{stem}.sfc"
            _make_rom_file(rom_path)
            _insert_rom_with_game(
                seeded_db,
                path=str(rom_path).replace("\\", "/"),
                system_id="snes",
                extension=".sfc",
                filename=f"{stem}.sfc",
                size_bytes=9,
                title=f"Game {stem}",
            )
        profile = _build_minimal_profile()

        class CancelMarkerError(Exception):
            pass

        def _progress(i: int, total: int, name: str) -> None:
            if i == 2:
                raise CancelMarkerError

        with pytest.raises(CancelMarkerError):
            export_collection(
                seeded_db,
                profile,
                tmp_path / "out",
                ExportFilters(),
                ExportOptions(generate_gamelist=False, generate_m3u=False),
                progress_callback=_progress,
            )


# ---------------------------------------------------------------------------
# gamelist.xml generation
# ---------------------------------------------------------------------------


class TestGamelistXml:
    def test_gamelist_xml_contains_entries(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path)
        _rom_id, game_id = _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Super Mario World (Parsed)",
        )
        q.upsert_metadata(
            seeded_db,
            game_id,
            {
                "description": "A platformer.",
                "genre": "Platform",
                "developer": "Nintendo EAD",
                "publisher": "Nintendo",
                "release_date": "19901121",
                "players": "1-2",
                "rating": "0.9",
            },
            source="test",
        )
        seeded_db.commit()

        profile = _build_minimal_profile()
        export_collection(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=True, generate_m3u=False),
        )
        gamelist_path = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        assert gamelist_path.exists()

        tree = ET.parse(gamelist_path)
        root = tree.getroot()
        assert root.tag == "gameList"
        games = root.findall("game")
        assert len(games) == 1
        game_node = games[0]
        assert game_node.find("path").text == "./Mario.sfc"
        assert game_node.find("name").text == "Super Mario World (Parsed)"
        assert game_node.find("desc").text == "A platformer."
        assert game_node.find("genre").text == "Platform"
        assert game_node.find("developer").text == "Nintendo EAD"
        assert game_node.find("releasedate").text == "19901121"

    def test_gamelist_falls_back_to_title_when_canonical_null(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "snes" / "X.sfc"
        _make_rom_file(rom_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="X.sfc",
            size_bytes=5,
            title="Parsed Title",
            canonical_name=None,
        )
        profile = _build_minimal_profile()
        export_collection(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=True, generate_m3u=False),
        )
        gamelist_path = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        root = ET.parse(gamelist_path).getroot()
        assert root.find("game/name").text == "Parsed Title"

    def test_generate_gamelist_xml_directly(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "snes" / "Y.sfc"
        _make_rom_file(rom_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Y.sfc",
            size_bytes=5,
            title="Direct",
        )
        rows = list(
            seeded_db.execute(
                "SELECT r.id, r.filename, r.game_id, "
                "g.title AS title, g.canonical_name AS canonical_name "
                "FROM roms r LEFT JOIN games g ON g.id = r.game_id"
            ).fetchall()
        )
        out_dir = tmp_path / "direct"
        out_dir.mkdir()
        path = generate_gamelist_xml(seeded_db, "snes", out_dir, rows)
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith(
            '<?xml version="1.0" encoding="UTF-8"?>'
        )


# ---------------------------------------------------------------------------
# .m3u playlist generation
# ---------------------------------------------------------------------------


class TestM3uGeneration:
    def test_multi_disc_playlist_written(
        self, seeded_db, tmp_path: Path
    ) -> None:
        disc1 = tmp_path / "lib" / "psx" / "Game (Disc 1).cue"
        disc2 = tmp_path / "lib" / "psx" / "Game (Disc 2).cue"
        _make_rom_file(disc1)
        _make_rom_file(disc2)
        for name, src in (
            ("Game (Disc 1).cue", disc1),
            ("Game (Disc 2).cue", disc2),
        ):
            _insert_rom_with_game(
                seeded_db,
                path=str(src).replace("\\", "/"),
                # snes used so the minimal profile copies the file.
                system_id="snes",
                extension=".cue",
                filename=name,
                size_bytes=4,
                title=name,
            )
        profile = _build_minimal_profile()
        export_collection(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=True),
        )
        m3u = tmp_path / "out" / "roms" / "snes" / "Game.m3u"
        assert m3u.exists()
        contents = m3u.read_text(encoding="utf-8").strip().splitlines()
        assert contents == ["Game (Disc 1).cue", "Game (Disc 2).cue"]

    def test_single_disc_does_not_write_m3u(self, tmp_path: Path) -> None:
        class FakeRow(dict):
            def keys(self):
                return list(super().keys())

        row = FakeRow(filename="Lonely (Disc 1).cue")
        out_dir = tmp_path / "snes"
        out_dir.mkdir()
        written = generate_m3u_playlists(out_dir, [row])
        assert written == 0
        assert list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Artwork copy
# ---------------------------------------------------------------------------


class TestArtworkCopy:
    def test_artwork_copied_alongside_gamelist(
        self, seeded_db, tmp_path: Path
    ) -> None:
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        cover_path = tmp_path / "covers" / "snes" / "Mario.png"
        _make_rom_file(rom_path)
        _make_rom_file(cover_path, content=b"\x89PNG")
        _rom_id, game_id = _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Mario",
        )
        q.insert_cover(
            seeded_db,
            game_id,
            "boxart",
            source_url=None,
            local_path=str(cover_path).replace("\\", "/"),
        )
        seeded_db.commit()
        profile = _build_minimal_profile()
        export_collection(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(
                generate_gamelist=False,
                generate_m3u=False,
                include_artwork=True,
            ),
        )
        expected_art = (
            tmp_path
            / "out"
            / "roms"
            / "snes"
            / "downloaded_media"
            / "Mario-image.png"
        )
        assert expected_art.exists()
        assert expected_art.read_bytes() == b"\x89PNG"


# ---------------------------------------------------------------------------
# Atomic-write contract — partial files don't leak on failure.
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_failed_replace_leaves_no_partial_file(
        self, seeded_db, tmp_path: Path, monkeypatch
    ) -> None:
        """If ``os.replace`` raises mid-copy, the destination must not exist.

        We force every ``os.replace`` to fail, run the export, and verify the
        target folder is empty (no ``.part`` leftover, no half-written ROM).
        """
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path, content=b"src-bytes")
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=9,
            title="Mario",
        )

        def _always_raise(*_a, **_kw) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(atomic.os, "replace", _always_raise)
        profile = _build_minimal_profile()
        summary = export_collection(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        assert summary.files_copied == 0
        assert summary.errors
        target_dir = tmp_path / "out" / "roms" / "snes"
        if target_dir.exists():
            leftovers = list(target_dir.iterdir())
            assert leftovers == [], f"unexpected leftovers: {leftovers}"
        # Source is untouched.
        assert rom_path.read_bytes() == b"src-bytes"
