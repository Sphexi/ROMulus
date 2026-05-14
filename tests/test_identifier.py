"""Tests for identifier pipeline — header extraction, internal titles."""

from __future__ import annotations

from pathlib import Path

import pytest

from romulus.core.identifier import extract_header_title


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _pad(data: bytes, target: int) -> bytes:
    if len(data) >= target:
        return data
    return data + b"\x00" * (target - len(data))


# ---------------------------------------------------------------------------
# SNES — LoROM, HiROM, with/without SMC header
# ---------------------------------------------------------------------------


class TestSnesHeader:
    def _make_lorom(self, title: str, with_smc: bool = False) -> bytes:
        title_bytes = title.encode("ascii").ljust(21, b" ")
        # Body must be a multiple of 1024 so size % 1024 == 512 after the SMC header.
        body = bytearray(b"\x00" * (33 * 1024))
        body[0x7FC0 : 0x7FC0 + 21] = title_bytes
        body = bytes(body)
        if with_smc:
            header = b"\xAA" * 512
            return header + body
        return body

    def _make_hirom(self, title: str) -> bytes:
        title_bytes = title.encode("ascii").ljust(21, b" ")
        body = bytearray(b"\x00" * (65 * 1024))
        body[0xFFC0 : 0xFFC0 + 21] = title_bytes
        return bytes(body)

    def test_lorom_title_without_smc_header(self, tmp_path):
        rom = _write(tmp_path / "lorom.sfc", self._make_lorom("SUPER MARIO WORLD"))
        assert extract_header_title(rom, "snes", "smc_512") == "SUPER MARIO WORLD"

    def test_lorom_title_with_smc_header(self, tmp_path):
        rom = _write(tmp_path / "lorom.smc", self._make_lorom("ZELDA III", with_smc=True))
        assert extract_header_title(rom, "snes", "smc_512") == "ZELDA III"

    def test_hirom_title(self, tmp_path):
        rom = _write(tmp_path / "hirom.sfc", self._make_hirom("CHRONO TRIGGER"))
        assert extract_header_title(rom, "snes", "smc_512") == "CHRONO TRIGGER"

    def test_short_file_returns_none(self, tmp_path):
        rom = _write(tmp_path / "tiny.sfc", b"\x00" * 100)
        assert extract_header_title(rom, "snes", "smc_512") is None

    def test_pick_higher_printable_ratio(self, tmp_path):
        title_bytes = b"GOOD GAME TITLE".ljust(21, b" ")
        garbage = b"\xff\xfe\x01\x02" * 8
        garbage = garbage[:21]
        body = bytearray(_pad(b"", 0xFFC0 + 21))
        body[0x7FC0 : 0x7FC0 + 21] = title_bytes
        body[0xFFC0 : 0xFFC0 + 21] = garbage
        rom = _write(tmp_path / "ambiguous.sfc", bytes(body))
        assert extract_header_title(rom, "snes", "smc_512") == "GOOD GAME TITLE"


# ---------------------------------------------------------------------------
# N64 — z64 / v64 / n64 byte orders
# ---------------------------------------------------------------------------


class TestN64Header:
    def _make_z64(self, title: str) -> bytes:
        title_bytes = title.encode("ascii").ljust(20, b" ")
        body = bytearray(_pad(b"\x80\x37\x12\x40", 0x40))
        body[0x20 : 0x20 + 20] = title_bytes
        return bytes(body)

    def _byteswap_v64(self, data: bytes) -> bytes:
        ba = bytearray(data)
        for i in range(0, len(ba), 2):
            ba[i], ba[i + 1] = ba[i + 1], ba[i]
        return bytes(ba)

    def _byteswap_n64(self, data: bytes) -> bytes:
        ba = bytearray(data)
        for i in range(0, len(ba), 4):
            ba[i : i + 4] = ba[i : i + 4][::-1]
        return bytes(ba)

    def test_z64_native(self, tmp_path):
        rom = _write(tmp_path / "g.z64", self._make_z64("SUPER MARIO 64"))
        assert extract_header_title(rom, "n64", "n64_byteswap") == "SUPER MARIO 64"

    def test_v64_byteswapped(self, tmp_path):
        z64 = self._make_z64("MARIO KART 64")
        rom = _write(tmp_path / "g.v64", self._byteswap_v64(z64))
        assert extract_header_title(rom, "n64", "n64_byteswap") == "MARIO KART 64"

    def test_n64_wordswapped(self, tmp_path):
        z64 = self._make_z64("ZELDA OOT")
        rom = _write(tmp_path / "g.n64", self._byteswap_n64(z64))
        assert extract_header_title(rom, "n64", "n64_byteswap") == "ZELDA OOT"

    def test_unrecognized_magic_returns_none(self, tmp_path):
        rom = _write(tmp_path / "bad.n64", b"\xde\xad\xbe\xef" + b"\x00" * 100)
        assert extract_header_title(rom, "n64", "n64_byteswap") is None


# ---------------------------------------------------------------------------
# Mega Drive — overseas vs domestic magic
# ---------------------------------------------------------------------------


class TestMegaDriveHeader:
    def _make_md(self, title: str, overseas: bool = True) -> bytes:
        magic = b"SEGA MEGA DRIVE " if overseas else b"SEGA GENESIS    "
        body = bytearray(_pad(b"", 0x150 + 48))
        body[0x100 : 0x100 + 16] = magic
        title_bytes = title.encode("ascii").ljust(48, b" ")
        if overseas:
            body[0x150 : 0x150 + 48] = title_bytes
        else:
            # Domestic title sits at 0x120.
            body = bytearray(_pad(b"", 0x150 + 48))
            body[0x100 : 0x100 + 16] = magic
            body[0x120 : 0x120 + 48] = title_bytes
        return bytes(body)

    def test_overseas_title(self, tmp_path):
        rom = _write(tmp_path / "overseas.md", self._make_md("SONIC THE HEDGEHOG"))
        assert extract_header_title(rom, "megadrive", None) == "SONIC THE HEDGEHOG"

    def test_domestic_title(self, tmp_path):
        rom = _write(tmp_path / "domestic.md", self._make_md("PHANTASY STAR", overseas=False))
        assert extract_header_title(rom, "megadrive", None) == "PHANTASY STAR"

    def test_missing_sega_magic_returns_none(self, tmp_path):
        body = bytearray(_pad(b"", 0x150 + 48))
        body[0x100 : 0x100 + 16] = b"NOT A SEGA ROM!!"
        body[0x150 : 0x150 + 48] = b"TITLE".ljust(48, b" ")
        rom = _write(tmp_path / "fake.md", bytes(body))
        assert extract_header_title(rom, "megadrive", None) is None


# ---------------------------------------------------------------------------
# Game Boy / GBC / GBA / DS
# ---------------------------------------------------------------------------


class TestHandheldHeaders:
    def test_gb_title_trimmed_at_null(self, tmp_path):
        body = bytearray(_pad(b"", 0x134 + 16))
        body[0x134 : 0x134 + 16] = b"TETRIS\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        rom = _write(tmp_path / "tetris.gb", bytes(body))
        assert extract_header_title(rom, "gb", None) == "TETRIS"

    def test_gbc_uses_same_offset(self, tmp_path):
        body = bytearray(_pad(b"", 0x134 + 16))
        body[0x134 : 0x134 + 16] = b"POKEMON YELLOW\x00\x00"
        rom = _write(tmp_path / "yellow.gbc", bytes(body))
        assert extract_header_title(rom, "gbc", None) == "POKEMON YELLOW"

    def test_gba_title(self, tmp_path):
        body = bytearray(_pad(b"", 0xA0 + 12))
        body[0xA0 : 0xA0 + 12] = b"METROID ZER0"
        rom = _write(tmp_path / "metroid.gba", bytes(body))
        assert extract_header_title(rom, "gba", None) == "METROID ZER0"

    def test_ds_title(self, tmp_path):
        body = bytearray(_pad(b"", 0x40))
        body[0x00 : 0x00 + 12] = b"NSMB\x00\x00\x00\x00\x00\x00\x00\x00"
        rom = _write(tmp_path / "nsmb.nds", bytes(body))
        assert extract_header_title(rom, "nds", None) == "NSMB"


# ---------------------------------------------------------------------------
# Systems without internal titles fall through to None
# ---------------------------------------------------------------------------


class TestSystemsWithoutHeaders:
    @pytest.mark.parametrize("system_id", ["nes", "pcengine", "atari2600", "mastersystem"])
    def test_no_title_for_headerless_systems(self, tmp_path, system_id):
        rom = _write(tmp_path / "any.bin", b"\x00" * 1024)
        assert extract_header_title(rom, system_id, None) is None

    def test_unknown_system_returns_none(self, tmp_path):
        rom = _write(tmp_path / "any.bin", b"\x00" * 1024)
        assert extract_header_title(rom, "nonexistent", None) is None


# ---------------------------------------------------------------------------
# Robustness — empty/missing files
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_missing_file_returns_none(self, tmp_path):
        assert extract_header_title(tmp_path / "nope.sfc", "snes", "smc_512") is None

    def test_empty_file_returns_none(self, tmp_path):
        rom = _write(tmp_path / "empty.sfc", b"")
        assert extract_header_title(rom, "snes", "smc_512") is None
