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
