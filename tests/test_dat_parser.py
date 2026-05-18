"""Tests for DAT parser — Logiqx XML parsing, hash matching."""

from __future__ import annotations

import time
from pathlib import Path

from romulus.core.dat_parser import (
    DatEntry,
    load_all_dats,
    match_hashes,
    parse_dat_file,
    parse_region_from_name,
)
from romulus.db import queries

_DAT_SNES_TEMPLATE = """<?xml version="1.0"?>
<datafile>
    <header>
        <name>Nintendo - Super Nintendo Entertainment System</name>
        <description>Test DAT</description>
        <version>1</version>
    </header>
    <game name="{game_name}">
        <description>{game_name}</description>
        <rom name="{rom_name}" size="{size}" crc="{crc}" md5="{md5}" sha1="{sha1}"/>
    </game>
</datafile>
"""

_DAT_MULTI_GAME = """<?xml version="1.0"?>
<datafile>
    <header>
        <name>Nintendo - Game Boy</name>
        <version>1</version>
    </header>
    <game name="Tetris (World) (Rev 1)">
        <rom name="Tetris (World) (Rev 1).gb" size="32768" crc="aaaaaaaa"
             md5="00000000000000000000000000000000"
             sha1="1111111111111111111111111111111111111111"/>
    </game>
    <game name="Pokemon Red (USA, Europe)">
        <rom name="Pokemon Red (USA, Europe).gb" size="1048576" crc="bbbbbbbb"
             md5="11111111111111111111111111111111"
             sha1="2222222222222222222222222222222222222222"/>
    </game>
</datafile>
"""


def _write_dat(
    path: Path,
    *,
    game_name: str = "Sample Game (USA)",
    rom_name: str = "Sample Game (USA).sfc",
    size: int = 1024,
    crc: str = "deadbeef",
    md5: str = "0" * 32,
    sha1: str = "a" * 40,
) -> Path:
    path.write_text(
        _DAT_SNES_TEMPLATE.format(
            game_name=game_name,
            rom_name=rom_name,
            size=size,
            crc=crc,
            md5=md5,
            sha1=sha1,
        ),
        encoding="utf-8",
    )
    return path


def _enroll_rom(
    conn, path: Path, system_id: str, size_bytes: int, mtime: float
) -> int:
    return queries.upsert_rom(
        conn,
        {
            "path": str(path),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "size_bytes": size_bytes,
            "mtime": mtime,
            "system_id": system_id,
        },
    )


# ---------------------------------------------------------------------------
# parse_region_from_name
# ---------------------------------------------------------------------------


class TestParseRegion:
    def test_single_region(self):
        assert parse_region_from_name("Game (USA)") == "USA"

    def test_multi_region_csv(self):
        assert parse_region_from_name("Game (USA, Europe)") == "USA, Europe"

    def test_case_insensitive_token_match(self):
        assert parse_region_from_name("Game (Japan)") == "Japan"

    def test_skips_non_region_groups(self):
        assert parse_region_from_name("Game (Rev 1)") is None

    def test_returns_region_even_when_other_groups_present(self):
        assert parse_region_from_name("Game (Europe) (Rev 1)") == "Europe"

    def test_no_groups_returns_none(self):
        assert parse_region_from_name("Just a Name") is None

    def test_mixed_group_with_unknown_token_skipped(self):
        # "USA, Beta" — "Beta" is not a region token, so this group is skipped.
        assert parse_region_from_name("Game (USA, Beta)") is None


# ---------------------------------------------------------------------------
# parse_dat_file
# ---------------------------------------------------------------------------


class TestParseDatFile:
    def test_parses_single_entry(self, tmp_path):
        dat = _write_dat(tmp_path / "snes.dat")
        entries = parse_dat_file(dat)
        assert len(entries) == 1
        e = entries[0]
        assert isinstance(e, DatEntry)
        assert e.game_name == "Sample Game (USA)"
        assert e.rom_name == "Sample Game (USA).sfc"
        assert e.size_bytes == 1024
        assert e.crc32 == "deadbeef"
        assert e.sha1 == "a" * 40
        assert e.region == "USA"

    def test_resolves_system_id_from_header_name(self, tmp_path):
        dat = _write_dat(tmp_path / "snes.dat")
        entries = parse_dat_file(dat)
        assert entries[0].system_id == "snes"

    def test_multi_game_dat(self, tmp_path):
        dat = tmp_path / "gb.dat"
        dat.write_text(_DAT_MULTI_GAME, encoding="utf-8")
        entries = parse_dat_file(dat)
        assert len(entries) == 2
        names = {e.game_name for e in entries}
        assert "Tetris (World) (Rev 1)" in names
        assert "Pokemon Red (USA, Europe)" in names
        tetris = next(e for e in entries if "Tetris" in e.game_name)
        assert tetris.revision == "Rev 1"
        assert tetris.region == "World"
        pokemon = next(e for e in entries if "Pokemon" in e.game_name)
        assert pokemon.region == "USA, Europe"
        assert all(e.system_id == "gb" for e in entries)

    def test_lowercases_hashes(self, tmp_path):
        dat = _write_dat(
            tmp_path / "snes.dat",
            crc="DEADBEEF",
            sha1="AABBCCDD" * 5,
            md5="FF" * 16,
        )
        entries = parse_dat_file(dat)
        assert entries[0].crc32 == "deadbeef"
        assert entries[0].sha1 == "aabbccdd" * 5
        assert entries[0].md5 == "ff" * 16

    def test_malformed_xml_returns_empty(self, tmp_path):
        dat = tmp_path / "bad.dat"
        dat.write_text("<datafile><not closed", encoding="utf-8")
        assert parse_dat_file(dat) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_dat_file(tmp_path / "missing.dat") == []

    def test_unknown_system_header_yields_none_system_id(self, tmp_path):
        dat = tmp_path / "weird.dat"
        dat.write_text(
            """<?xml version="1.0"?>
            <datafile>
                <header><name>Some Obscure Platform</name></header>
                <game name="X"><rom name="X.bin" size="1" crc="0" sha1="0"/></game>
            </datafile>""",
            encoding="utf-8",
        )
        entries = parse_dat_file(dat)
        assert len(entries) == 1
        assert entries[0].system_id is None


# ---------------------------------------------------------------------------
# load_all_dats
# ---------------------------------------------------------------------------


class TestLoadAllDats:
    def test_loads_directory_of_dats(self, seeded_db, tmp_path):
        dat_dir = tmp_path / "dats"
        dat_dir.mkdir()
        _write_dat(dat_dir / "a.dat", game_name="A (USA)", sha1="a" * 40)
        _write_dat(dat_dir / "b.dat", game_name="B (USA)", sha1="b" * 40)
        inserted = load_all_dats(seeded_db, [dat_dir])
        assert inserted == 2
        rows = seeded_db.execute("SELECT game_name FROM dat_entries").fetchall()
        assert {r["game_name"] for r in rows} == {"A (USA)", "B (USA)"}

    def test_mixed_files_and_directories(self, seeded_db, tmp_path):
        f1 = _write_dat(tmp_path / "single.dat", game_name="One (USA)", sha1="1" * 40)
        sub = tmp_path / "sub"
        sub.mkdir()
        _write_dat(sub / "two.dat", game_name="Two (USA)", sha1="2" * 40)
        inserted = load_all_dats(seeded_db, [f1, sub])
        assert inserted == 2

    def test_loads_bundled_data_dats(self, seeded_db):
        bundled = Path(__file__).resolve().parent.parent / "data" / "dats"
        if not bundled.exists():
            return
        inserted = load_all_dats(seeded_db, [bundled])
        assert inserted >= 1

    def test_ignores_missing_paths(self, seeded_db, tmp_path):
        inserted = load_all_dats(seeded_db, [tmp_path / "nope"])
        assert inserted == 0


# ---------------------------------------------------------------------------
# DAT match queries
# ---------------------------------------------------------------------------


class TestDatQueries:
    def test_insert_and_lookup_by_sha1(self, seeded_db):
        entry = DatEntry(
            dat_file="x.dat",
            system_id="snes",
            game_name="Game (USA)",
            rom_name="Game (USA).sfc",
            size_bytes=1024,
            crc32="deadbeef",
            md5="0" * 32,
            sha1="a" * 40,
            region="USA",
            revision=None,
            is_bios=False,
        )
        queries.insert_dat_entry(seeded_db, entry)
        row = queries.get_dat_by_sha1(seeded_db, "a" * 40)
        assert row is not None
        assert row["game_name"] == "Game (USA)"

    def test_sha1_lookup_is_case_insensitive(self, seeded_db):
        entry = DatEntry(
            dat_file="x.dat", system_id="snes",
            game_name="G", rom_name="g.sfc",
            size_bytes=10, crc32="11", md5=None, sha1="ab" * 20,
            region=None, revision=None, is_bios=False,
        )
        queries.insert_dat_entry(seeded_db, entry)
        row = queries.get_dat_by_sha1(seeded_db, ("AB" * 20).upper())
        assert row is not None

    def test_crc_size_unique_returns_row(self, seeded_db):
        entry = DatEntry(
            dat_file="x.dat", system_id="snes",
            game_name="G", rom_name="g.sfc",
            size_bytes=2048, crc32="cafebabe", md5=None, sha1="z" * 40,
            region=None, revision=None, is_bios=False,
        )
        queries.insert_dat_entry(seeded_db, entry)
        row = queries.get_dat_by_crc_size(seeded_db, "cafebabe", 2048)
        assert row is not None
        assert row["game_name"] == "G"

    def test_crc_size_ambiguous_returns_none(self, seeded_db):
        # Two entries sharing CRC32+size collide — fallback must refuse to choose.
        base = dict(
            dat_file="x.dat", system_id="snes",
            rom_name="g.sfc", size_bytes=99, crc32="cccccccc",
            md5=None, region=None, revision=None, is_bios=False,
        )
        queries.insert_dat_entry(
            seeded_db, DatEntry(**base, game_name="Game A", sha1="a" * 40)
        )
        queries.insert_dat_entry(
            seeded_db, DatEntry(**base, game_name="Game B", sha1="b" * 40)
        )
        row = queries.get_dat_by_crc_size(seeded_db, "cccccccc", 99)
        assert row is None

    def test_update_rom_match(self, seeded_db, tmp_path):
        rom_path = tmp_path / "r.gb"
        rom_path.write_bytes(b"\x00" * 16)
        rom_id = _enroll_rom(seeded_db, rom_path, "gb", 16, time.time())
        queries.update_rom_match(seeded_db, rom_id, "Some Game (USA)", "dat_verified")
        row = seeded_db.execute(
            "SELECT dat_match, match_confidence FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["dat_match"] == "Some Game (USA)"
        assert row["match_confidence"] == "dat_verified"


# ---------------------------------------------------------------------------
# match_hashes — end-to-end
# ---------------------------------------------------------------------------


class TestMatchHashes:
    def test_sha1_match_upgrades_confidence(self, seeded_db, tmp_path):
        rom = tmp_path / "g.sfc"
        rom.write_bytes(b"\x00" * 1024)
        rom_id = _enroll_rom(seeded_db, rom, "snes", 1024, time.time())
        queries.upsert_hash(seeded_db, rom_id, "deadbeef", "a" * 40, None)
        _write_dat(tmp_path / "snes.dat", sha1="a" * 40, size=1024)
        load_all_dats(seeded_db, [tmp_path / "snes.dat"])
        matched = match_hashes(seeded_db)
        assert matched == 1
        row = seeded_db.execute(
            "SELECT dat_match, match_confidence FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["match_confidence"] == "dat_verified"
        assert row["dat_match"] == "Sample Game (USA)"

    def test_crc_size_fallback_when_sha_misses(self, seeded_db, tmp_path):
        rom = tmp_path / "g.sfc"
        rom.write_bytes(b"\x00" * 1024)
        rom_id = _enroll_rom(seeded_db, rom, "snes", 1024, time.time())
        # ROM has a SHA-1 the DAT doesn't carry, but CRC32+size match a unique row.
        queries.upsert_hash(seeded_db, rom_id, "cafebabe", "f" * 40, None)
        _write_dat(
            tmp_path / "snes.dat",
            crc="cafebabe",
            sha1="0" * 40,
            size=1024,
            game_name="Crc Fallback Game (USA)",
        )
        load_all_dats(seeded_db, [tmp_path / "snes.dat"])
        matched = match_hashes(seeded_db)
        assert matched == 1
        row = seeded_db.execute(
            "SELECT dat_match FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["dat_match"] == "Crc Fallback Game (USA)"

    def test_no_match_leaves_confidence_alone(self, seeded_db, tmp_path):
        rom = tmp_path / "g.sfc"
        rom.write_bytes(b"\x00" * 1024)
        rom_id = _enroll_rom(seeded_db, rom, "snes", 1024, time.time())
        queries.upsert_hash(seeded_db, rom_id, "11111111", "1" * 40, None)
        # DAT has different hashes.
        _write_dat(tmp_path / "snes.dat", sha1="9" * 40, crc="99999999")
        load_all_dats(seeded_db, [tmp_path / "snes.dat"])
        matched = match_hashes(seeded_db)
        assert matched == 0
        row = seeded_db.execute(
            "SELECT match_confidence, dat_match FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["match_confidence"] != "dat_verified"
        assert row["dat_match"] is None

    def test_already_dat_verified_is_skipped(self, seeded_db, tmp_path):
        rom = tmp_path / "g.sfc"
        rom.write_bytes(b"\x00" * 1024)
        rom_id = _enroll_rom(seeded_db, rom, "snes", 1024, time.time())
        queries.upsert_hash(seeded_db, rom_id, "deadbeef", "a" * 40, None)
        queries.update_rom_match(seeded_db, rom_id, "Existing Name", "dat_verified")
        _write_dat(tmp_path / "snes.dat", sha1="a" * 40)
        load_all_dats(seeded_db, [tmp_path / "snes.dat"])
        matched = match_hashes(seeded_db)
        assert matched == 0
        row = seeded_db.execute(
            "SELECT dat_match FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        # Should still be the original name (no re-stamp).
        assert row["dat_match"] == "Existing Name"

    def test_scope_rom_ids_filters_to_subset(self, seeded_db, tmp_path):
        # Two ROMs, both hashed, both would otherwise match the same DAT row.
        # Scoping to only the first must leave the second's confidence alone —
        # this is the right-click "Heavy Scan this game" guarantee.
        rom_a = tmp_path / "a.sfc"
        rom_b = tmp_path / "b.sfc"
        rom_a.write_bytes(b"\x00" * 1024)
        rom_b.write_bytes(b"\x00" * 1024)
        id_a = _enroll_rom(seeded_db, rom_a, "snes", 1024, time.time())
        id_b = _enroll_rom(seeded_db, rom_b, "snes", 1024, time.time())
        queries.upsert_hash(seeded_db, id_a, "deadbeef", "a" * 40, None)
        queries.upsert_hash(seeded_db, id_b, "deadbeef", "a" * 40, None)
        _write_dat(tmp_path / "snes.dat", sha1="a" * 40, size=1024)
        load_all_dats(seeded_db, [tmp_path / "snes.dat"])

        matched = match_hashes(seeded_db, scope_rom_ids=[id_a])

        assert matched == 1
        row_a = seeded_db.execute(
            "SELECT match_confidence FROM roms WHERE id = ?", (id_a,)
        ).fetchone()
        row_b = seeded_db.execute(
            "SELECT match_confidence FROM roms WHERE id = ?", (id_b,)
        ).fetchone()
        assert row_a["match_confidence"] == "dat_verified"
        assert row_b["match_confidence"] != "dat_verified"

    def test_empty_scope_matches_nothing(self, seeded_db, tmp_path):
        rom = tmp_path / "g.sfc"
        rom.write_bytes(b"\x00" * 1024)
        rom_id = _enroll_rom(seeded_db, rom, "snes", 1024, time.time())
        queries.upsert_hash(seeded_db, rom_id, "deadbeef", "a" * 40, None)
        _write_dat(tmp_path / "snes.dat", sha1="a" * 40, size=1024)
        load_all_dats(seeded_db, [tmp_path / "snes.dat"])

        matched = match_hashes(seeded_db, scope_rom_ids=[])

        assert matched == 0
        row = seeded_db.execute(
            "SELECT match_confidence FROM roms WHERE id = ?", (rom_id,)
        ).fetchone()
        assert row["match_confidence"] != "dat_verified"


# ---------------------------------------------------------------------------
# Security regression — XML entity-expansion DoS (audit v0.1.0 finding #3)
# ---------------------------------------------------------------------------


_BILLION_LAUGHS_DAT = """<?xml version="1.0"?>
<!DOCTYPE datafile [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<datafile>
    <header><name>Evil</name></header>
    <game name="&lol4;"></game>
</datafile>
"""


class TestXmlEntityExpansionGuard:
    """A malicious DAT XML must not be parsable into expanded entities.

    With stdlib ``xml.etree.ElementTree`` this XML would inflate ``&lol4;`` to
    10^4 = 10,000 ``lol`` strings on parse — and at depth-6 (which a
    real-world bomb uses) it's a billion. ``defusedxml`` blocks the entity
    expansion before any inflation happens, raising a defusedxml-specific
    exception or returning a parse error.
    """

    def test_billion_laughs_dat_is_rejected(self, tmp_path: Path) -> None:
        # defusedxml raises ``EntitiesForbidden`` (a subclass of ParseError /
        # the stdlib XML errors). ``parse_dat_file`` catches parse errors and
        # returns an empty list — so the assertion is that the parser DID
        # refuse rather than expand the entities into the canonical game name.
        bomb = tmp_path / "bomb.dat"
        bomb.write_text(_BILLION_LAUGHS_DAT, encoding="utf-8")
        entries = parse_dat_file(bomb)
        # Either empty (caught + skipped) or, if defusedxml raised an
        # exception subclass we don't catch yet, that exception would have
        # bubbled — we want the catch to remain comprehensive.
        assert entries == [], (
            f"defusedxml must block entity expansion, got {len(entries)} "
            f"entries"
        )

    def test_billion_laughs_via_load_all_dats(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """Bulk loader must skip the bomb without consuming gigabytes of RAM."""
        bomb = tmp_path / "bomb.dat"
        bomb.write_text(_BILLION_LAUGHS_DAT, encoding="utf-8")
        # Should complete near-instantly and insert zero rows.
        inserted = load_all_dats(seeded_db, [tmp_path])
        assert inserted == 0


_DAT_HEADER_VARIANT_TEMPLATE = """<?xml version="1.0"?>
<datafile>
    <header><name>{header}</name></header>
    <game name="X"><rom name="X.x" size="1" crc="00000000"/></game>
</datafile>
"""


class TestDatNameAliases:
    """Real No-Intro headers carry suffixes the registry's primary `dat_name`
    can't match exactly (Combined, BigEndian, Decrypted, etc.). The
    `dat_name_aliases` field on SystemDef is supposed to cover them.
    """

    def _entry_system(self, tmp_path: Path, header: str) -> str | None:
        dat = tmp_path / f"{header}.dat"
        dat.write_text(
            _DAT_HEADER_VARIANT_TEMPLATE.format(header=header), encoding="utf-8"
        )
        entries = parse_dat_file(dat)
        assert entries, f"expected at least one entry from {header!r}"
        return entries[0].system_id

    def test_snes_combined_maps_to_snes(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path,
                "Nintendo - Super Nintendo Entertainment System (Combined)",
            )
            == "snes"
        )

    def test_n64_bigendian_maps_to_n64(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Nintendo 64 (BigEndian)")
            == "n64"
        )

    def test_nds_decrypted_maps_to_nds(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Nintendo DS (Decrypted)")
            == "nds"
        )

    def test_dsi_decrypted_maps_to_nds(self, tmp_path: Path) -> None:
        # DSi cart dumps are treated as the same logical system as DS for v0.1.0.
        assert (
            self._entry_system(tmp_path, "Nintendo - Nintendo DSi (Decrypted)")
            == "nds"
        )

    def test_fds_maps_to_nes(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Nintendo - Family Computer Disk System"
            )
            == "nes"
        )

    def test_msx2_maps_to_msx(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Microsoft - MSX2") == "msx"

    def test_zxspectrum_plus3_maps_to_zxspectrum(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Sinclair - ZX Spectrum +3")
            == "zxspectrum"
        )

    def test_canonical_dat_name_still_matches(self, tmp_path: Path) -> None:
        # Sanity — adding aliases must not regress the primary mapping.
        assert (
            self._entry_system(
                tmp_path, "Nintendo - Super Nintendo Entertainment System"
            )
            == "snes"
        )

    def test_unknown_header_returns_none(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Acme - Made Up Console") is None
        )

    # ---- Atari ----

    def test_atari5200_maps_to_atari5200(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Atari - 5200") == "atari5200"

    def test_jaguar_j64_maps_to_jaguar(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Atari - Jaguar (J64)") == "jaguar"

    # ---- Bandai ----

    def test_wonderswan_maps_to_wonderswan(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Bandai - WonderSwan") == "wonderswan"

    def test_wonderswan_color_maps_to_wonderswancolor(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Bandai - WonderSwan Color")
            == "wonderswancolor"
        )

    # ---- Coleco ----

    def test_colecovision_maps_to_colecovision(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Coleco - ColecoVision") == "colecovision"
        )

    # ---- Commodore (extended) ----

    def test_plus4_maps_to_c64plus4(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Commodore - Plus-4") == "c64plus4"

    def test_vic20_maps_to_vic20(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Commodore - VIC-20") == "vic20"

    # ---- Other classic / mini consoles ----

    def test_arcadia2001_maps_to_arcadia2001(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Emerson - Arcadia 2001") == "arcadia2001"
        )

    def test_adventure_vision_maps_to_adventurevision(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Entex - Adventure Vision")
            == "adventurevision"
        )

    def test_super_cassette_vision_maps_to_scv(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Epoch - Super Cassette Vision") == "scv"
        )

    def test_channel_f_maps_to_channelf(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Fairchild - Channel F") == "channelf"
        )

    def test_super_acan_maps_to_superacan(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Funtech - Super Acan") == "superacan"
        )

    def test_vectrex_maps_to_vectrex(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "GCE - Vectrex") == "vectrex"

    def test_game_master_maps_to_gamemaster(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Hartung - Game Master") == "gamemaster"
        )

    def test_odyssey2_maps_to_odyssey2(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Magnavox - Odyssey2") == "odyssey2"

    def test_videopac_plus_maps_to_odyssey2(self, tmp_path: Path) -> None:
        # Same hardware as Odyssey 2 — aliased onto the single SystemDef.
        assert self._entry_system(tmp_path, "Philips - Videopac+") == "odyssey2"

    def test_intellivision_maps_to_intellivision(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Mattel - Intellivision")
            == "intellivision"
        )

    def test_studio_ii_maps_to_studio2(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "RCA - Studio II") == "studio2"

    def test_gamecom_maps_to_gamecom(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Tiger - Game.com") == "gamecom"

    def test_creativision_maps_to_creativision(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "VTech - CreatiVision") == "creativision"
        )

    def test_vsmile_maps_to_vsmile(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "VTech - V.Smile") == "vsmile"

    def test_supervision_maps_to_supervision(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Watara - Supervision") == "supervision"
        )

    # ---- NEC (extended) ----

    def test_pcengine_supergrafx_maps_to_supergrafx(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "NEC - PC Engine SuperGrafx")
            == "supergrafx"
        )

    # ---- Sega (extended) ----

    def test_sg1000_maps_to_sg1000(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Sega - SG-1000") == "sg1000"

    def test_sega_pico_maps_to_segapico(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Sega - PICO") == "segapico"

    # ---- Nintendo extensions / accessories ----

    def test_n64dd_maps_to_n64dd(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Nintendo - Nintendo 64DD") == "n64dd"

    def test_pokemon_mini_maps_to_pokemini(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Pokemon Mini") == "pokemini"
        )

    def test_satellaview_maps_to_satellaview(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Satellaview") == "satellaview"
        )

    def test_sufami_turbo_maps_to_sufami(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Sufami Turbo") == "sufami"
        )

    def test_ereader_maps_to_ereader(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Nintendo - e-Reader") == "ereader"

    # ---- Korean / Japanese niche ----

    def test_gp32_maps_to_gp32(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "GamePark - GP32") == "gp32"

    def test_casio_loopy_maps_to_casioloopy(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Casio - Loopy") == "casioloopy"

    def test_casio_pv1000_maps_to_pv1000(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Casio - PV-1000") == "pv1000"

    # ---- Digital-distribution / install-package era (now mapped) ----
    # These were "intentionally unmapped" in v0.1.0 first-pass; the
    # v0.1.0 final-pass routes them onto real SystemDefs so the bundled
    # CDN / PSN / Xbox Live DATs stop dropping to None.

    def test_wii_wad_maps_to_wii(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Wii (Digital) (WAD)") == "wii"
        )

    def test_wii_cdn_maps_to_wii(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Wii (Digital) (CDN)") == "wii"
        )

    def test_wiiu_digital_maps_to_wiiu(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Wii U (Digital)") == "wiiu"
        )

    def test_wiiu_cdn_maps_to_wiiu(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Wii U (Digital) (CDN)")
            == "wiiu"
        )

    def test_3ds_digital_maps_to_n3ds(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Nintendo 3DS (Digital)")
            == "n3ds"
        )

    def test_3ds_cdn_double_typo_maps_to_n3ds(self, tmp_path: Path) -> None:
        # No-Intro's file has a literal "(CDN) (CDN)" double-suffix typo;
        # preserved verbatim in the alias list.
        assert (
            self._entry_system(
                tmp_path, "Nintendo - Nintendo 3DS (Digital) (CDN) (CDN)"
            )
            == "n3ds"
        )

    def test_3ds_encrypted_maps_to_n3ds(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Nintendo - Nintendo 3DS (Encrypted)")
            == "n3ds"
        )

    def test_new_3ds_digital_maps_to_n3ds(self, tmp_path: Path) -> None:
        # New 3DS is hardware-distinct but the ROM catalogue lives under the
        # same logical "n3ds" id; Citra / Lime3DS handle both.
        assert (
            self._entry_system(tmp_path, "Nintendo - New Nintendo 3DS (Digital)")
            == "n3ds"
        )

    def test_new_3ds_encrypted_maps_to_n3ds(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Nintendo - New Nintendo 3DS (Encrypted)"
            )
            == "n3ds"
        )

    def test_dsiware_maps_to_dsiware(self, tmp_path: Path) -> None:
        # DSiWare (digital eShop titles) is a separate SystemDef from the
        # DS cartridge ``nds`` system — different content, different size
        # bucket, but both run on melonDS / DeSmuME.
        assert (
            self._entry_system(tmp_path, "Nintendo - Nintendo DSi (Digital)")
            == "dsiware"
        )

    def test_psvita_vpk_maps_to_psvita(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Sony - PlayStation Vita (VPK)")
            == "psvita"
        )

    def test_psvita_psn_decrypted_maps_to_psvita(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Vita (PSN) (Decrypted)"
            )
            == "psvita"
        )

    def test_psvita_psn_encrypted_maps_to_psvita(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Vita (PSN) (Encrypted)"
            )
            == "psvita"
        )

    def test_ps3_psn_decrypted_maps_to_ps3(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation 3 (PSN) (Decrypted)"
            )
            == "ps3"
        )

    def test_ps3_psn_encrypted_maps_to_ps3(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation 3 (PSN) (Encrypted)"
            )
            == "ps3"
        )

    def test_xbox360_digital_maps_to_xbox360(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(tmp_path, "Microsoft - XBOX 360 (Digital)")
            == "xbox360"
        )

    def test_xbox360_title_updates_maps_to_xbox360(
        self, tmp_path: Path
    ) -> None:
        assert (
            self._entry_system(
                tmp_path,
                "Microsoft - XBOX 360 (Title Updates) (Discontinued)",
            )
            == "xbox360"
        )

    def test_psp_psn_decrypted_maps_to_psp(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Portable (PSN) (Decrypted)"
            )
            == "psp"
        )

    def test_psp_psn_encrypted_maps_to_psp(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Portable (PSN) (Encrypted)"
            )
            == "psp"
        )

    def test_psp_psx2psp_maps_to_psp(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Portable (PSX2PSP)"
            )
            == "psp"
        )

    def test_j2me_maps_to_j2me(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Mobile - J2ME") == "j2me"

    def test_palm_os_maps_to_palmos(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "Mobile - Palm OS") == "palmos"

    # ---- Intentionally unmapped (still no playable path) ----

    def test_ibm_pc_is_intentionally_unmapped(self, tmp_path: Path) -> None:
        assert self._entry_system(tmp_path, "IBM - PC and Compatibles") is None

    def test_mobile_symbian_is_intentionally_unmapped(
        self, tmp_path: Path
    ) -> None:
        assert self._entry_system(tmp_path, "Mobile - Symbian") is None

    def test_mobile_zeebo_is_intentionally_unmapped(
        self, tmp_path: Path
    ) -> None:
        assert self._entry_system(tmp_path, "Mobile - Zeebo") is None

    def test_ps4_psn_is_intentionally_unmapped(self, tmp_path: Path) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation 4 (PSN) (Encrypted)"
            )
            is None
        )

    def test_psp_umd_music_is_intentionally_unmapped(
        self, tmp_path: Path
    ) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Portable (UMD Music)"
            )
            is None
        )

    def test_psp_umd_video_is_intentionally_unmapped(
        self, tmp_path: Path
    ) -> None:
        assert (
            self._entry_system(
                tmp_path, "Sony - PlayStation Portable (UMD Video)"
            )
            is None
        )
