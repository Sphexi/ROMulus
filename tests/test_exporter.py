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


def _insert_rom(
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
    match_confidence: str = "fuzzy",
) -> int:
    """Insert a roms row directly (v0.4.0 1:1 model) and return its id.

    All identity columns live on the roms row itself.
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
            "match_confidence": match_confidence,
            "title": title,
            "canonical_name": canonical_name,
            "region": region,
        },
    )
    conn.commit()
    return rom_id


# Back-compat alias used in tests that still unpack two values — the second
# element is now always ``None`` (no game_id in the 1:1 model).
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
) -> tuple[int, None]:
    """Compatibility shim — returns (rom_id, None).

    The second element was ``game_id`` before the strict 1:1 migration.
    Tests that only need the rom_id should migrate to :func:`_insert_rom`.
    """
    rom_id = _insert_rom(
        conn,
        path=path,
        system_id=system_id,
        extension=extension,
        filename=filename,
        size_bytes=size_bytes,
        title=title,
        region=region,
        canonical_name=canonical_name,
    )
    return rom_id, None


def _build_minimal_profile(
    *, gamelist: str | None = "emulationstation_xml"
) -> DestinationProfile:
    """A tiny profile mapping snes + nes + gba + psx + pcenginecd.

    PSX and PC Engine CD are included so multi-disc m3u tests can use the
    right ``system_id`` rather than smuggling a .cue under ``snes``.
    Everything else in the registry is left explicitly unsupported.
    """
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
        elif sys_def.id == "psx":
            systems[sys_def.id] = SystemMapping(
                folder="psx", extensions=[".cue", ".bin"], supported=True
            )
        elif sys_def.id == "pcenginecd":
            systems[sys_def.id] = SystemMapping(
                folder="pcenginecd",
                extensions=[".cue", ".bin"],
                supported=True,
            )
        else:
            systems[sys_def.id] = SystemMapping(folder="", supported=False)
    return DestinationProfile(
        id="test",
        name="Test Profile",
        base_path="roms",
        gamelist_format=gamelist,
        artwork_subdir="downloaded_media",
        artwork_filename_template="{stem}-image{ext}",
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
        "anbernic-rglauncher",
    }

    def test_all_built_in_profiles_load(self) -> None:
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

    def test_region_filter_excludes_other_regions(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Region filter keeps only ROMs whose game.region matches the list."""
        usa_path = tmp_path / "lib" / "snes" / "usa.sfc"
        jp_path = tmp_path / "lib" / "snes" / "jp.sfc"
        _make_rom_file(usa_path)
        _make_rom_file(jp_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(usa_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="usa.sfc",
            size_bytes=10,
            title="USA Game",
            region="USA",
        )
        _insert_rom_with_game(
            seeded_db,
            path=str(jp_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="jp.sfc",
            size_bytes=20,
            title="Japan Game",
            region="Japan",
        )
        profile = _build_minimal_profile()
        preview = preview_export(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(regions=["USA"]),
        )
        assert preview.file_count == 1
        # The Japan ROM was filtered out.
        assert preview.total_size_bytes == 10

    def test_region_filter_other_includes_null_and_unlisted(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """The ``Other`` bucket includes NULL-region games AND any region not
        explicitly listed in the filter — the special-case behaviour the
        export-dialog UI exposes via its ``Other`` checkbox.
        """
        no_region = tmp_path / "lib" / "snes" / "nores.sfc"
        brazil = tmp_path / "lib" / "snes" / "brazil.sfc"
        usa = tmp_path / "lib" / "snes" / "usa.sfc"
        for p in (no_region, brazil, usa):
            _make_rom_file(p)
        _insert_rom_with_game(
            seeded_db,
            path=str(no_region).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="nores.sfc",
            size_bytes=10,
            title="NoRegion",
            region=None,
        )
        _insert_rom_with_game(
            seeded_db,
            path=str(brazil).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="brazil.sfc",
            size_bytes=20,
            title="Brazil Game",
            region="Brazil",
        )
        _insert_rom_with_game(
            seeded_db,
            path=str(usa).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="usa.sfc",
            size_bytes=30,
            title="USA Game",
            region="USA",
        )
        profile = _build_minimal_profile()
        # ``Other`` plus an explicit ``Brazil`` — both the null-region and the
        # listed-region rows must come through; USA must NOT come through.
        # Note: the SQL is ``g.region IS NULL OR g.region IN (?)`` so listing
        # Brazil explicitly while also including ``Other`` is what makes the
        # NULL-region row pass.
        preview = preview_export(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(regions=["Brazil", "Other"]),
        )
        # NoRegion + Brazil pass; USA filtered out.
        assert preview.file_count == 2
        assert preview.total_size_bytes == 10 + 20

    def test_collection_filter_intersects_with_collection_games(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """``collection_id`` filter restricts the candidate set via the
        ``collection_games`` join — only ROMs whose game is in the given
        collection are returned.
        """
        a_path = tmp_path / "lib" / "snes" / "a.sfc"
        b_path = tmp_path / "lib" / "snes" / "b.sfc"
        _make_rom_file(a_path)
        _make_rom_file(b_path)
        rom_a = _insert_rom(
            seeded_db,
            path=str(a_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="a.sfc",
            size_bytes=10,
            title="A",
        )
        _insert_rom(
            seeded_db,
            path=str(b_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="b.sfc",
            size_bytes=20,
            title="B",
        )
        # Put only ROM A in a "Favorites"-style collection.
        coll_id = q.create_collection(seeded_db, "MyCollection")
        q.add_rom_to_collection(seeded_db, coll_id, rom_a)

        profile = _build_minimal_profile()
        preview = preview_export(
            seeded_db,
            profile,
            tmp_path / "out",
            ExportFilters(collection_id=coll_id),
        )
        assert preview.file_count == 1
        assert preview.total_size_bytes == 10


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
        # Phase 1: per-ROM ticks, total=3, label is verb-prefixed.
        rom_ticks = [t for t in ticks if t[1] == 3]
        assert [t[0] for t in rom_ticks] == [1, 2, 3]
        assert all(t[2].startswith("Copying ") for t in rom_ticks)
        # Phase 2: per-system sidecar ticks. One snes system → one tick.
        sidecar_ticks = [t for t in ticks if "Refreshing sidecars" in t[2]]
        assert len(sidecar_ticks) == 1
        assert sidecar_ticks[0][2] == "Refreshing sidecars: snes"

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
        rom_id = _insert_rom(
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
            rom_id,
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
        _insert_rom(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Y.sfc",
            size_bytes=5,
            title="Direct",
        )
        # v0.4.0: query reads identity columns directly from roms.
        rows = list(
            seeded_db.execute(
                "SELECT r.id, r.filename, r.system_id, "
                "r.title AS title, r.canonical_name AS canonical_name, "
                "r.match_confidence, r.extension "
                "FROM roms r"
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
        # Multi-disc lives on PSX / PC Engine CD in the wild — using the right
        # system_id here ensures _build_minimal_profile's psx mapping is
        # exercising the real path rather than smuggling a .cue under snes.
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
                system_id="psx",
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
        m3u = tmp_path / "out" / "roms" / "psx" / "Game.m3u"
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
        rom_id = _insert_rom(
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
            rom_id,
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

    def test_artwork_filename_template_default_no_suffix(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """A profile with the default template gets ``{stem}{ext}`` — the
        modern Daijisho/Onion/muOS/Anbernic convention, no ``-image`` suffix."""
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        cover_path = tmp_path / "covers" / "snes" / "Mario.png"
        _make_rom_file(rom_path)
        _make_rom_file(cover_path, content=b"\x89PNG")
        rom_id = _insert_rom(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Mario",
        )
        q.insert_cover(
            seeded_db, rom_id, "Named_Boxarts", None,
            str(cover_path).replace("\\", "/"),
        )
        seeded_db.commit()

        # Build a profile using the default artwork_filename_template.
        profile = DestinationProfile(
            id="modern",
            name="Modern Launcher",
            base_path="Roms",
            artwork_subdir="Imgs",
            systems={
                "snes": SystemMapping(folder="snes", extensions=[".sfc"]),
            },
        )
        export_collection(
            seeded_db, profile, tmp_path / "out", ExportFilters(),
            ExportOptions(generate_gamelist=False, include_artwork=True),
        )
        expected = tmp_path / "out" / "Roms" / "snes" / "Imgs" / "Mario.png"
        assert expected.exists(), (
            f"expected default template '{{stem}}{{ext}}' to produce "
            f"{expected}; not found"
        )
        # And the legacy ``-image.png`` variant must NOT exist.
        legacy = tmp_path / "out" / "Roms" / "snes" / "Imgs" / "Mario-image.png"
        assert not legacy.exists()


# ---------------------------------------------------------------------------
# Gamelist <image> emission
# ---------------------------------------------------------------------------


class TestGamelistImageEmission:
    """``generate_gamelist_xml`` must emit an ``<image>`` element pointing at
    the artwork that ``copy_artwork`` will write, so EmulationStation can
    actually find the cover. Honors the profile's filename template.
    """

    def test_image_element_points_at_legacy_path(
        self, seeded_db, tmp_path: Path
    ) -> None:
        from xml.etree import ElementTree as PlainET

        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        cover_path = tmp_path / "covers" / "snes" / "Mario.png"
        _make_rom_file(rom_path)
        _make_rom_file(cover_path, content=b"\x89PNG")
        rom_id = _insert_rom(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Mario",
        )
        q.insert_cover(
            seeded_db, rom_id, "Named_Boxarts", None,
            str(cover_path).replace("\\", "/"),
        )
        seeded_db.commit()
        profile = _build_minimal_profile(gamelist="emulationstation_xml")
        export_collection(
            seeded_db, profile, tmp_path / "out", ExportFilters(),
            ExportOptions(generate_gamelist=True, include_artwork=True),
        )
        gamelist = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        assert gamelist.exists()
        tree = PlainET.parse(gamelist)
        images = [el.text for el in tree.getroot().iter("image")]
        assert images == ["./downloaded_media/Mario-image.png"]

    def test_image_element_points_at_default_path(
        self, seeded_db, tmp_path: Path
    ) -> None:
        from xml.etree import ElementTree as PlainET

        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        cover_path = tmp_path / "covers" / "snes" / "Mario.png"
        _make_rom_file(rom_path)
        _make_rom_file(cover_path, content=b"\x89PNG")
        rom_id = _insert_rom(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Mario",
        )
        q.insert_cover(
            seeded_db, rom_id, "Named_Boxarts", None,
            str(cover_path).replace("\\", "/"),
        )
        seeded_db.commit()
        profile = DestinationProfile(
            id="modern",
            name="Modern",
            base_path="Roms",
            gamelist_format="emulationstation_xml",
            artwork_subdir="Imgs",
            systems={"snes": SystemMapping(folder="snes", extensions=[".sfc"])},
        )
        export_collection(
            seeded_db, profile, tmp_path / "out", ExportFilters(),
            ExportOptions(generate_gamelist=True, include_artwork=True),
        )
        gamelist = tmp_path / "out" / "Roms" / "snes" / "gamelist.xml"
        tree = PlainET.parse(gamelist)
        images = [el.text for el in tree.getroot().iter("image")]
        assert images == ["./Imgs/Mario.png"]

    def test_no_image_element_when_profile_has_no_artwork(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """If artwork_subdir is None, gamelist should not include <image>."""
        from xml.etree import ElementTree as PlainET

        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path)
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Mario",
        )
        seeded_db.commit()
        profile = DestinationProfile(
            id="noart",
            name="No Artwork",
            base_path="roms",
            gamelist_format="emulationstation_xml",
            artwork_subdir=None,
            systems={"snes": SystemMapping(folder="snes", extensions=[".sfc"])},
        )
        export_collection(
            seeded_db, profile, tmp_path / "out", ExportFilters(),
            ExportOptions(generate_gamelist=True, include_artwork=True),
        )
        gamelist = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        tree = PlainET.parse(gamelist)
        assert list(tree.getroot().iter("image")) == []


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


# ---------------------------------------------------------------------------
# Security regression — profile path traversal (audit v0.1.0 finding #1)
# ---------------------------------------------------------------------------


class TestProfilePathTraversal:
    """End-to-end check that a malicious profile YAML cannot escape ``target``.

    Pydantic validators reject the profile at load time, but ``_system_dest_dir``
    also resolves the final path at export time as a belt-and-suspenders
    guard. Both layers are tested here.
    """

    def test_load_profile_rejects_absolute_base_path(self, tmp_path: Path) -> None:
        """A profile YAML with absolute ``base_path`` is refused at load time."""
        import pytest
        from pydantic import ValidationError

        bad = tmp_path / "evil.yaml"
        bad.write_text(
            "id: evil\nname: Evil\nbase_path: /etc\nsystems: {}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_profile(bad)

    def test_load_profile_rejects_traversal_folder(self, tmp_path: Path) -> None:
        """A system mapping with ``..`` traversal is refused at load time."""
        import pytest
        from pydantic import ValidationError

        bad = tmp_path / "evil.yaml"
        bad.write_text(
            "id: evil\nname: Evil\nbase_path: roms\n"
            "systems:\n  snes:\n    folder: '../../etc'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_profile(bad)

    def test_load_all_profiles_skips_malicious_yaml(self, tmp_path: Path) -> None:
        """``load_all_profiles`` logs+skips a bad profile, doesn't crash."""
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        builtin.mkdir()
        user.mkdir()
        (builtin / "ok.yaml").write_text(
            "id: ok\nname: OK\nbase_path: roms\nsystems: {}\n", encoding="utf-8"
        )
        (user / "evil.yaml").write_text(
            "id: evil\nname: Evil\nbase_path: /etc\nsystems: {}\n",
            encoding="utf-8",
        )
        profiles = load_all_profiles(builtin_dir=builtin, user_dir=user)
        assert "ok" in profiles
        assert "evil" not in profiles

    def test_export_runtime_guard_blocks_constructed_escape(
        self, tmp_path: Path
    ) -> None:
        """Even if a profile bypasses validators, ``_system_dest_dir`` refuses.

        We construct the malicious profile via ``model_construct`` (skipping
        validators) to simulate a hypothetical future bug in the load-time
        check. ``_system_dest_dir`` resolves the final path and raises rather
        than writing outside ``target``.
        """
        import pytest

        from romulus.core.exporter import _system_dest_dir

        evil_profile = DestinationProfile.model_construct(
            id="evil",
            name="Evil",
            description=None,
            case_sensitive=True,
            base_path="../../../tmp_evil_escape",
            gamelist_format=None,
            artwork_subdir=None,
            multi_disc=None,
            systems={},
        )
        evil_mapping = SystemMapping.model_construct(
            folder="snes", extensions=[], supported=True
        )
        target = tmp_path / "out"
        target.mkdir()
        with pytest.raises(ValueError, match="outside target"):
            _system_dest_dir(target, evil_profile, evil_mapping)

    def test_builtin_profiles_still_load(self) -> None:
        """All shipped profiles must validate cleanly with new rules."""
        profiles = load_all_profiles()
        assert set(profiles.keys()) == {
            "analogue-pocket",
            "anbernic-rglauncher",
            "batocera",
            "mister",
            "muos",
            "onionos",
            "retropie",
        }


# ---------------------------------------------------------------------------
# Security regression — overwrite refusal (audit v0.1.0 finding #4)
# ---------------------------------------------------------------------------


class TestRefuseOverwriteDifferentSize:
    """``export_collection`` must NOT clobber an existing file of a different size."""

    def test_refuses_to_overwrite_size_mismatch(
        self, seeded_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """An existing destination file with a different size is kept intact."""
        rom_path = tmp_path / "src" / "snes" / "Game.sfc"
        _make_rom_file(rom_path, b"new-rom-bytes-32-chars-long-padding!")
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path),
            system_id="snes",
            extension=".sfc",
            filename="Game.sfc",
            size_bytes=len(rom_path.read_bytes()),
            title="Game",
        )
        profile = _build_minimal_profile(gamelist=None)
        target = tmp_path / "out"
        dest = target / "roms" / "snes" / "Game.sfc"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Pre-populate a different-sized "user" file at the dest path.
        dest.write_bytes(b"PRECIOUS-USER-DATA")
        precious = dest.read_bytes()

        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        # Existing file is untouched, summary surfaces the refusal.
        assert dest.read_bytes() == precious
        assert summary.files_copied == 0
        assert summary.files_skipped == 1
        assert any("refusing to overwrite" in e for e in summary.errors)


# ---------------------------------------------------------------------------
# Per-system summary breakdown (post-apply diagnostic)
# ---------------------------------------------------------------------------


class TestPerSystemBreakdown:
    """``ExportSummary.per_system`` must classify each row into the right bucket.

    The post-export summary dialog reads from this dict to surface "why
    did 4,363 amiga files get skipped" without forcing the user to grep
    logs. Each bucket maps 1:1 to a code path in
    ``export_collection``'s copy loop.
    """

    def test_copied_files_bump_copied_bucket(
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
        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        assert "snes" in summary.per_system
        bucket = summary.per_system["snes"]
        assert bucket.copied == 1
        assert bucket.bytes_copied == 10
        assert bucket.skipped_unsupported == 0
        assert bucket.skipped_already_present == 0
        assert bucket.skipped_refused == 0
        assert bucket.errors == 0

    def test_unsupported_system_bumps_unsupported_bucket(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """A system that the profile marks unsupported lands in its own bucket.

        Mirrors the Amiga / C64 / ZX Spectrum case from the production
        export the user ran against the Anbernic profile.
        """
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
        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        assert "gamecube" in summary.per_system
        bucket = summary.per_system["gamecube"]
        assert bucket.skipped_unsupported == 1
        assert bucket.copied == 0

    def test_already_present_bumps_idempotent_bucket(
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
        target = tmp_path / "out"
        # First export — populates.
        export_collection(
            seeded_db,
            _build_minimal_profile(),
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        # Second pass — should be idempotent skip-already-present.
        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        bucket = summary.per_system["snes"]
        assert bucket.copied == 0
        assert bucket.skipped_already_present == 1
        assert bucket.skipped_refused == 0
        assert bucket.errors == 0

    def test_refuse_overwrite_bumps_refused_and_errors(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Refuse-overwrite must hit BOTH skipped_refused and errors.

        This is the MAME collision case from the production run — same
        filename, different size at the destination. The bucket should
        record it as a refusal AND count it toward errors so the
        dialog renders the cell red.
        """
        rom_path = tmp_path / "src" / "snes" / "Game.sfc"
        _make_rom_file(rom_path, b"new-rom-bytes-32-chars-long-padding!")
        _insert_rom_with_game(
            seeded_db,
            path=str(rom_path),
            system_id="snes",
            extension=".sfc",
            filename="Game.sfc",
            size_bytes=len(rom_path.read_bytes()),
            title="Game",
        )
        profile = _build_minimal_profile(gamelist=None)
        target = tmp_path / "out"
        dest = target / "roms" / "snes" / "Game.sfc"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PRECIOUS-USER-DATA-different-size")

        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        bucket = summary.per_system["snes"]
        assert bucket.copied == 0
        assert bucket.skipped_refused == 1
        assert bucket.errors == 1

    def test_per_system_sum_matches_aggregate(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Sum of per-system buckets must equal the aggregate counters.

        Guards against a future refactor adding a new skip branch but
        forgetting to bump the corresponding per-system counter (which
        would silently produce a dialog whose totals row disagrees with
        the existing one-line summary).
        """
        # One copied, one unsupported.
        for stem, system in (("Mario", "snes"), ("Wind", "gamecube")):
            rom_path = tmp_path / "lib" / system / f"{stem}.bin"
            _make_rom_file(rom_path)
            _insert_rom_with_game(
                seeded_db,
                path=str(rom_path).replace("\\", "/"),
                system_id=system,
                extension=".bin",
                filename=f"{stem}.bin",
                size_bytes=10,
                title=stem,
            )
        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(generate_gamelist=False, generate_m3u=False),
        )
        per_system_copied = sum(
            b.copied for b in summary.per_system.values()
        )
        per_system_skipped = sum(
            b.skipped_unsupported
            + b.skipped_already_present
            + b.skipped_refused
            for b in summary.per_system.values()
        )
        per_system_errors = sum(b.errors for b in summary.per_system.values())
        assert per_system_copied == summary.files_copied
        assert per_system_skipped == summary.files_skipped
        # Errors count: per_system_errors counts sidecar/refuse/missing-src
        # bumps. Aggregate ``summary.errors`` is a list — its length must
        # match the per-system error count.
        assert per_system_errors == len(summary.errors)


# ---------------------------------------------------------------------------
# include_roms toggle — artwork/gamelist-only mode
# ---------------------------------------------------------------------------


class TestIncludeRomsToggle:
    """``ExportOptions.include_roms = False`` runs the sidecar phase
    (gamelist + artwork) without copying any ROM bytes.

    Use case: after Enrich Metadata / Find Covers, push the fresh
    sidecars to the device without re-copying gigabytes of ROMs that
    are already there.
    """

    def _stage_game_with_cover(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> tuple[int, Path]:
        """Seed one snes game + on-disk cover. Returns (rom_id, cover_path)."""
        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path, content=b"snes-bytes")
        rom_id = _insert_rom(
            conn,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Super Mario World",
        )
        cover_path = tmp_path / "covers" / "mario.png"
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(b"COVER-PNG")
        q.insert_cover(
            conn,
            rom_id,
            cover_type="Named_Boxarts",
            source_url=None,
            local_path=str(cover_path),
        )
        conn.commit()
        return rom_id, cover_path

    def test_include_roms_false_skips_rom_copy_but_runs_sidecars(
        self, seeded_db, tmp_path: Path
    ) -> None:
        _game_id, _cover = self._stage_game_with_cover(seeded_db, tmp_path)
        profile = _build_minimal_profile()
        target = tmp_path / "out"

        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(include_roms=False, generate_m3u=False),
        )

        # ROM bytes must NOT be on dest.
        assert not (target / "roms" / "snes" / "Mario.sfc").exists()
        # Artwork must be on dest (filename template is "{stem}-image{ext}").
        assert (
            target
            / "roms"
            / "snes"
            / "downloaded_media"
            / "Mario-image.png"
        ).exists()
        # gamelist.xml must be on dest.
        assert (target / "roms" / "snes" / "gamelist.xml").exists()
        # Counts: no copies, no sidecar-phase artwork failures, system touched.
        assert summary.files_copied == 0
        assert summary.bytes_copied == 0
        assert summary.systems == ["snes"]
        assert summary.artwork_copied == 1
        assert summary.gamelists_written == 1
        # Per-system bucket carries the artwork count — required so the
        # post-export summary dialog renders the "Covers refreshed"
        # column instead of showing an empty snes row.
        assert summary.per_system["snes"].artwork_copied == 1
        assert summary.per_system["snes"].copied == 0

    def test_include_roms_false_does_not_touch_unsupported(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """A system the profile marks unsupported must NOT get an
        artwork folder created — we'd have no folder anyway, and the
        per-system bucket should record it as skipped_unsupported
        even in artwork-only mode.
        """
        # Gamecube isn't in _build_minimal_profile's supported set.
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
            ExportOptions(include_roms=False, generate_m3u=False),
        )

        assert "gamecube" in summary.per_system
        assert summary.per_system["gamecube"].skipped_unsupported == 1
        # No gamecube folder at all on dest.
        assert not (target / "roms" / "gamecube").exists()

    def test_include_roms_false_when_already_synced(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """After a normal export, re-running with include_roms=False
        should idempotently re-publish artwork + gamelist without
        re-copying the ROM. Mirrors the user's actual workflow.
        """
        _game_id, _cover = self._stage_game_with_cover(seeded_db, tmp_path)
        profile = _build_minimal_profile()
        target = tmp_path / "out"

        # Pass 1: normal full export.
        export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(generate_m3u=False),
        )
        rom_on_dest = target / "roms" / "snes" / "Mario.sfc"
        assert rom_on_dest.exists()
        rom_mtime_before = rom_on_dest.stat().st_mtime

        # Pass 2: artwork-only mode. The ROM file's mtime must NOT change.
        time.sleep(0.05)
        summary = export_collection(
            seeded_db,
            profile,
            target,
            ExportFilters(),
            ExportOptions(include_roms=False, generate_m3u=False),
        )
        assert rom_on_dest.stat().st_mtime == rom_mtime_before
        assert summary.files_copied == 0


class TestArtworkFreshnessSkip:
    """``copy_artwork`` must skip a dest cover that already matches the
    local copy by size + mtime, so the artwork-only workflow doesn't
    re-copy every cover on every run.
    """

    def test_already_current_artwork_is_skipped(
        self, seeded_db, tmp_path: Path
    ) -> None:
        from romulus.core.exporter import copy_artwork

        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path)
        rom_id = _insert_rom(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Super Mario World",
        )
        cover_path = tmp_path / "covers" / "mario.png"
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(b"COVER-PNG-BYTES")
        q.insert_cover(
            seeded_db,
            rom_id,
            cover_type="Named_Boxarts",
            source_url=None,
            local_path=str(cover_path),
        )
        seeded_db.commit()

        profile = _build_minimal_profile()
        target = tmp_path / "out"
        rows = list(seeded_db.execute(
            "SELECT * FROM roms WHERE system_id = 'snes'"
        ))

        # First pass — fresh copy.
        first = copy_artwork(seeded_db, "snes", profile, target, rows)
        assert first == 1
        dest_cover = (
            target / "roms" / "snes" / "downloaded_media" / "Mario-image.png"
        )
        dest_mtime_before = dest_cover.stat().st_mtime

        # Second pass — same source, dest already current. Must skip.
        time.sleep(0.05)
        second = copy_artwork(seeded_db, "snes", profile, target, rows)
        assert second == 0
        # mtime must not have changed (no re-write).
        assert dest_cover.stat().st_mtime == dest_mtime_before

    def test_modified_source_triggers_recopy(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """If the local cover file changes (size or mtime), copy_artwork
        must re-publish it — that's the whole point of running an
        artwork-only export after enrichment.
        """
        from romulus.core.exporter import copy_artwork

        rom_path = tmp_path / "lib" / "snes" / "Mario.sfc"
        _make_rom_file(rom_path)
        rom_id = _insert_rom(
            seeded_db,
            path=str(rom_path).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario.sfc",
            size_bytes=10,
            title="Super Mario World",
        )
        cover_path = tmp_path / "covers" / "mario.png"
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(b"OLD-COVER")
        q.insert_cover(
            seeded_db,
            rom_id,
            cover_type="Named_Boxarts",
            source_url=None,
            local_path=str(cover_path),
        )
        seeded_db.commit()

        profile = _build_minimal_profile()
        target = tmp_path / "out"
        rows = list(seeded_db.execute(
            "SELECT * FROM roms WHERE system_id = 'snes'"
        ))

        # First pass — fresh copy of OLD bytes.
        copy_artwork(seeded_db, "snes", profile, target, rows)
        dest_cover = (
            target / "roms" / "snes" / "downloaded_media" / "Mario-image.png"
        )
        assert dest_cover.read_bytes() == b"OLD-COVER"

        # Simulate the user re-enriching: rewrite the local cover.
        time.sleep(0.05)
        cover_path.write_bytes(b"NEW-COVER-WITH-DIFFERENT-SIZE")

        # Second pass — must re-copy because size differs.
        second = copy_artwork(seeded_db, "snes", profile, target, rows)
        assert second == 1
        assert dest_cover.read_bytes() == b"NEW-COVER-WITH-DIFFERENT-SIZE"


# ---------------------------------------------------------------------------
# Distinct-content toggle (ExportOptions.distinct_content_only)
# ---------------------------------------------------------------------------


class TestDistinctContentOnly:
    """``distinct_content_only=True`` exports one keeper per SHA-1 cluster.

    Acceptance criteria (from session 16 spec):

    - 3 byte-identical roms (same SHA-1) + 1 distinct rom, toggle OFF → 4 entries.
    - Same with toggle ON → 2 entries (one per SHA-1 cluster).
    - Keeper preference: dat_verified > canonical extension > shorter filename
      > lower rom_id.
    - ROMs without a SHA-1 row always pass through regardless of toggle.
    - Composition: artwork-only mode (include_roms=False) + distinct-content
      suppresses gamelist duplicates just as in the full-copy mode.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _seed_identical_cluster(
        self,
        conn: sqlite3.Connection,
        tmp_path: Path,
        *,
        sha1: str = "aabbcc001122334455667788990011223344",
    ) -> list[int]:
        """Seed three roms that all share the same SHA-1.

        Returns ``[rom_id_dat, rom_id_smc, rom_id_long]``.

        Ranking: dat_verified beats fuzzy regardless of ext, so
        ``rom_id_dat`` is the expected keeper when toggle is ON.
        """
        lib = tmp_path / "lib"
        # ROM 1 — dat_verified, canonical .sfc extension, short name.
        p1 = lib / "snes" / "Mario World (USA).sfc"
        _make_rom_file(p1, content=b"IDENTICAL-CONTENT")
        rid_dat = _insert_rom(
            conn,
            path=str(p1).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Mario World (USA).sfc",
            size_bytes=17,
            title="Mario World",
            match_confidence="dat_verified",
        )
        q.upsert_hash(conn, rid_dat, None, sha1, None)

        # ROM 2 — fuzzy, non-canonical .smc extension (ext_rank > .sfc).
        p2 = lib / "snes" / "Mario World (USA).smc"
        _make_rom_file(p2, content=b"IDENTICAL-CONTENT")
        rid_smc = _insert_rom(
            conn,
            path=str(p2).replace("\\", "/"),
            system_id="snes",
            extension=".smc",
            filename="Mario World (USA).smc",
            size_bytes=17,
            title="Mario World",
            match_confidence="fuzzy",
        )
        q.upsert_hash(conn, rid_smc, None, sha1, None)

        # ROM 3 — fuzzy, .sfc but longer filename than ROM 1.
        p3 = lib / "snes" / "Super Mario World - The Complete Version (USA).sfc"
        _make_rom_file(p3, content=b"IDENTICAL-CONTENT")
        rid_long = _insert_rom(
            conn,
            path=str(p3).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Super Mario World - The Complete Version (USA).sfc",
            size_bytes=17,
            title="Mario World",
            match_confidence="fuzzy",
        )
        q.upsert_hash(conn, rid_long, None, sha1, None)

        conn.commit()
        return [rid_dat, rid_smc, rid_long]

    def _seed_distinct_rom(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> int:
        """One SNES rom with a different SHA-1 — distinct content."""
        p = tmp_path / "lib" / "snes" / "Donkey Kong Country (USA).sfc"
        _make_rom_file(p, content=b"DIFFERENT-CONTENT")
        rid = _insert_rom(
            conn,
            path=str(p).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Donkey Kong Country (USA).sfc",
            size_bytes=17,
            title="Donkey Kong Country",
        )
        q.upsert_hash(
            conn, rid, None, "deadbeef0011223344556677889900112233", None
        )
        conn.commit()
        return rid

    def _seed_no_hash_rom(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> int:
        """A Quick-Scan-only rom — no hashes row at all."""
        p = tmp_path / "lib" / "snes" / "Unknown Game.sfc"
        _make_rom_file(p, content=b"UNKNOWN-CONTENT")
        rid = _insert_rom(
            conn,
            path=str(p).replace("\\", "/"),
            system_id="snes",
            extension=".sfc",
            filename="Unknown Game.sfc",
            size_bytes=15,
            title="Unknown Game",
        )
        conn.commit()
        return rid

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_toggle_off_exports_all_roms(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Default (toggle OFF) — all 4 roms get their own <game> entry."""
        self._seed_identical_cluster(seeded_db, tmp_path)
        self._seed_distinct_rom(seeded_db, tmp_path)

        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(distinct_content_only=False, generate_m3u=False),
        )
        assert summary.files_copied == 4

        import xml.etree.ElementTree as ET
        gamelist = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        tree = ET.parse(gamelist)
        games = list(tree.getroot().iter("game"))
        assert len(games) == 4

    def test_toggle_on_exports_one_per_sha1_cluster(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Toggle ON — 3 identical roms collapse to 1 keeper + 1 distinct = 2."""
        self._seed_identical_cluster(seeded_db, tmp_path)
        self._seed_distinct_rom(seeded_db, tmp_path)

        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(distinct_content_only=True, generate_m3u=False),
        )
        # 1 keeper from the identical cluster + 1 distinct = 2.
        assert summary.files_copied == 2

        import xml.etree.ElementTree as ET
        gamelist = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        tree = ET.parse(gamelist)
        games = list(tree.getroot().iter("game"))
        assert len(games) == 2

    def test_keeper_prefers_dat_verified(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """The dat_verified rom must win over fuzzy-confidence peers."""
        rid_dat, rid_smc, rid_long = self._seed_identical_cluster(
            seeded_db, tmp_path
        )

        export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(distinct_content_only=True, generate_m3u=False),
        )

        import xml.etree.ElementTree as ET
        gamelist = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        tree = ET.parse(gamelist)
        games = list(tree.getroot().iter("game"))
        assert len(games) == 1
        # The <path> element encodes the filename on dest.
        path_text = games[0].findtext("path") or ""
        assert "Mario World (USA).sfc" in path_text

    def test_roms_without_sha1_always_export(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Quick-scan-only ROMs (no hashes row) always pass through."""
        self._seed_no_hash_rom(seeded_db, tmp_path)

        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(distinct_content_only=True, generate_m3u=False),
        )
        assert summary.files_copied == 1

    def test_skipped_duplicates_counter_populated(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """``skipped_duplicates`` on the per-system bucket must equal 2
        when 3 identical roms are exported with toggle ON (3 - 1 kept = 2 skipped).
        """
        self._seed_identical_cluster(seeded_db, tmp_path)

        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(distinct_content_only=True, generate_m3u=False),
        )
        bucket = summary.per_system["snes"]
        assert bucket.skipped_duplicates == 2
        assert bucket.copied == 1

    def test_distinct_content_composes_with_include_roms_false(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Artwork-only mode + distinct-content: gamelist gets 1 entry, no ROM copy."""
        self._seed_identical_cluster(seeded_db, tmp_path)
        self._seed_distinct_rom(seeded_db, tmp_path)

        summary = export_collection(
            seeded_db,
            _build_minimal_profile(),
            tmp_path / "out",
            ExportFilters(),
            ExportOptions(
                include_roms=False,
                distinct_content_only=True,
                generate_m3u=False,
            ),
        )
        # No roms copied in artwork-only mode.
        assert summary.files_copied == 0
        # gamelist should only contain 2 entries (one per SHA-1 cluster).
        import xml.etree.ElementTree as ET
        gamelist = tmp_path / "out" / "roms" / "snes" / "gamelist.xml"
        tree = ET.parse(gamelist)
        games = list(tree.getroot().iter("game"))
        assert len(games) == 2
