"""Tests for hasher — SHA-1/CRC32 with header stripping, ZIP extraction."""

from __future__ import annotations

import hashlib
import time
import zipfile
import zlib
from pathlib import Path

from romulus.core.hasher import (
    hash_library,
    hash_rom,
    normalize_rom_content,
)
from romulus.db import queries


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _crc32(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def _enroll_rom(conn, path: Path, system_id: str) -> int:
    stat = path.stat()
    return queries.upsert_rom(
        conn,
        {
            "path": str(path),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "system_id": system_id,
        },
    )


# ---------------------------------------------------------------------------
# normalize_rom_content
# ---------------------------------------------------------------------------


class TestNormalizeRomContent:
    def test_no_rule_passes_through(self):
        data = b"hello world"
        assert normalize_rom_content(data, None) is data

    def test_smc_strips_when_size_mod_1024_is_512(self):
        payload = b"\xAB" * 1024
        headered = b"\x00" * 512 + payload
        assert normalize_rom_content(headered, "smc_512") == payload

    def test_smc_passes_through_when_no_extra_512(self):
        clean = b"\xAB" * 1024
        assert normalize_rom_content(clean, "smc_512") == clean

    def test_ines_strips_only_when_magic_present(self):
        payload = b"\xAA" * 256
        headered = b"NES\x1a" + b"\x00" * 12 + payload
        assert normalize_rom_content(headered, "ines_16") == payload

    def test_ines_leaves_unheadered_alone(self):
        clean = b"\xAA" * 256
        assert normalize_rom_content(clean, "ines_16") == clean

    def test_lynx_strips_only_when_magic_present(self):
        payload = b"\xCD" * 256
        headered = b"LYNX\x00" + b"\x00" * 59 + payload
        assert normalize_rom_content(headered, "lynx_64") == payload

    def test_lynx_leaves_unheadered_alone(self):
        clean = b"\xCD" * 256
        assert normalize_rom_content(clean, "lynx_64") == clean

    def test_n64_z64_passes_through(self):
        data = b"\x80\x37\x12\x40" + b"\xAB" * 60
        assert normalize_rom_content(data, "n64_byteswap") == data

    def test_n64_v64_halfword_swapped(self):
        z64 = b"\x80\x37\x12\x40" + b"\x11\x22\x33\x44" * 2
        v64 = bytearray(z64)
        for i in range(0, len(v64), 2):
            v64[i], v64[i + 1] = v64[i + 1], v64[i]
        assert normalize_rom_content(bytes(v64), "n64_byteswap") == z64

    def test_n64_n64_wordswapped(self):
        z64 = b"\x80\x37\x12\x40" + b"\x11\x22\x33\x44" * 2
        n64 = bytearray(z64)
        for i in range(0, len(n64), 4):
            n64[i : i + 4] = n64[i : i + 4][::-1]
        assert normalize_rom_content(bytes(n64), "n64_byteswap") == z64

    def test_unknown_rule_passes_through(self):
        data = b"anything"
        assert normalize_rom_content(data, "no_such_rule") == data


# ---------------------------------------------------------------------------
# hash_rom — file-level
# ---------------------------------------------------------------------------


class TestHashRom:
    def test_plain_file_hashes_full_content(self, tmp_path):
        payload = b"ABCDEFGHIJKLMNOP" * 1024
        rom = tmp_path / "plain.bin"
        rom.write_bytes(payload)
        result = hash_rom(rom, None)
        assert result is not None
        assert result.sha1 == _sha1(payload)
        assert result.crc32 == _crc32(payload)
        assert result.size == len(payload)

    def test_smc_header_stripped_before_hashing(self, tmp_path):
        payload = b"\xAB" * 1024
        headered = b"\xFF" * 512 + payload
        rom = tmp_path / "headered.smc"
        rom.write_bytes(headered)
        result = hash_rom(rom, "smc_512")
        assert result is not None
        assert result.sha1 == _sha1(payload)
        assert result.size == len(payload)

    def test_smc_file_without_extra_512_hashes_full(self, tmp_path):
        payload = b"\xAB" * 1024
        rom = tmp_path / "clean.sfc"
        rom.write_bytes(payload)
        result = hash_rom(rom, "smc_512")
        assert result is not None
        assert result.sha1 == _sha1(payload)

    def test_ines_header_stripped(self, tmp_path):
        payload = b"\xAA" * 1024
        headered = b"NES\x1a" + b"\x00" * 12 + payload
        rom = tmp_path / "game.nes"
        rom.write_bytes(headered)
        result = hash_rom(rom, "ines_16")
        assert result is not None
        assert result.sha1 == _sha1(payload)

    def test_n64_byteswap_normalizes_to_z64(self, tmp_path):
        z64 = b"\x80\x37\x12\x40" + b"\x11\x22\x33\x44" * 256
        v64 = bytearray(z64)
        for i in range(0, len(v64), 2):
            v64[i], v64[i + 1] = v64[i + 1], v64[i]
        z64_rom = tmp_path / "g.z64"
        v64_rom = tmp_path / "g.v64"
        z64_rom.write_bytes(z64)
        v64_rom.write_bytes(bytes(v64))
        a = hash_rom(z64_rom, "n64_byteswap")
        b = hash_rom(v64_rom, "n64_byteswap")
        assert a is not None and b is not None
        assert a.sha1 == b.sha1

    def test_zip_single_inner_file_extracted(self, tmp_path):
        payload = b"hello-zip-world" * 100
        zip_path = tmp_path / "single.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("inside.bin", payload)
        result = hash_rom(zip_path, None)
        assert result is not None
        assert result.sha1 == _sha1(payload)
        assert result.size == len(payload)

    def test_zip_multi_inner_files_hashes_largest(self, tmp_path):
        small = b"small" * 10
        large = b"L" * 4096
        zip_path = tmp_path / "multi.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("small.bin", small)
            zf.writestr("large.bin", large)
        result = hash_rom(zip_path, None)
        assert result is not None
        assert result.sha1 == _sha1(large)

    def test_zip_applies_header_rule_to_inner_payload(self, tmp_path):
        payload = b"\xAA" * 1024
        headered = b"NES\x1a" + b"\x00" * 12 + payload
        zip_path = tmp_path / "headered.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("game.nes", headered)
        result = hash_rom(zip_path, "ines_16")
        assert result is not None
        assert result.sha1 == _sha1(payload)

    def test_empty_zip_returns_none(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass
        assert hash_rom(zip_path, None) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert hash_rom(tmp_path / "nope.bin", None) is None


# ---------------------------------------------------------------------------
# hash_library — orchestration + cache
# ---------------------------------------------------------------------------


class TestHashLibrary:
    def test_hashes_only_unhashed_roms(self, seeded_db, tmp_path):
        payload = b"\xAA" * 512
        rom_path = tmp_path / "a.gb"
        rom_path.write_bytes(payload)
        _enroll_rom(seeded_db, rom_path, "gb")

        count = hash_library(seeded_db, workers=2)
        assert count == 1

        # Second call should find nothing stale.
        again = hash_library(seeded_db, workers=2)
        assert again == 0

    def test_progress_callback_fires_per_file(self, seeded_db, tmp_path):
        for i in range(3):
            rom = tmp_path / f"r{i}.gb"
            rom.write_bytes(b"\x00" * (256 + i))
            _enroll_rom(seeded_db, rom, "gb")

        events: list[tuple[int, int, str]] = []

        def cb(done: int, total: int, path: str) -> None:
            events.append((done, total, path))

        count = hash_library(seeded_db, progress_callback=cb, workers=2)
        assert count == 3
        assert len(events) == 3
        assert all(total == 3 for _, total, _ in events)
        # done values should cover 1..3.
        assert {done for done, _, _ in events} == {1, 2, 3}

    def test_stale_hash_is_recomputed(self, seeded_db, tmp_path):
        payload = b"x" * 256
        rom_path = tmp_path / "stale.gb"
        rom_path.write_bytes(payload)
        rom_id = _enroll_rom(seeded_db, rom_path, "gb")

        assert hash_library(seeded_db, workers=1) == 1
        first = queries.get_hash(seeded_db, rom_id)
        assert first is not None
        first_hashed_at = first["hashed_at"]

        # Re-enroll with an mtime newer than hashed_at to simulate a touch.
        time.sleep(0.01)
        new_mtime = time.time() + 60
        seeded_db.execute(
            "UPDATE roms SET mtime = ? WHERE id = ?",
            (new_mtime, rom_id),
        )
        seeded_db.commit()

        rehashed = hash_library(seeded_db, workers=1)
        assert rehashed == 1
        second = queries.get_hash(seeded_db, rom_id)
        assert second is not None
        assert second["hashed_at"] >= first_hashed_at

    def test_no_pending_returns_zero(self, seeded_db):
        assert hash_library(seeded_db, workers=2) == 0


# ---------------------------------------------------------------------------
# Hash queries
# ---------------------------------------------------------------------------


class TestHashQueries:
    def test_upsert_and_get_hash(self, seeded_db, tmp_path):
        rom_path = tmp_path / "q.gb"
        rom_path.write_bytes(b"\x00" * 16)
        rom_id = _enroll_rom(seeded_db, rom_path, "gb")
        queries.upsert_hash(seeded_db, rom_id, "deadbeef", "a" * 40, "b" * 32)
        row = queries.get_hash(seeded_db, rom_id)
        assert row is not None
        assert row["crc32"] == "deadbeef"
        assert row["sha1"] == "a" * 40

    def test_upsert_replaces_existing(self, seeded_db, tmp_path):
        rom_path = tmp_path / "q.gb"
        rom_path.write_bytes(b"\x00" * 16)
        rom_id = _enroll_rom(seeded_db, rom_path, "gb")
        queries.upsert_hash(seeded_db, rom_id, "11111111", "a" * 40, None)
        queries.upsert_hash(seeded_db, rom_id, "22222222", "b" * 40, None)
        row = queries.get_hash(seeded_db, rom_id)
        assert row["crc32"] == "22222222"
        assert row["sha1"] == "b" * 40

    def test_get_unhashed_excludes_hashed(self, seeded_db, tmp_path):
        a = tmp_path / "a.gb"
        b = tmp_path / "b.gb"
        a.write_bytes(b"\x00" * 16)
        b.write_bytes(b"\x00" * 16)
        a_id = _enroll_rom(seeded_db, a, "gb")
        _enroll_rom(seeded_db, b, "gb")
        queries.upsert_hash(seeded_db, a_id, "1", "1", "1")
        rows = queries.get_unhashed_roms(seeded_db)
        assert [r["filename"] for r in rows] == ["b.gb"]

    def test_get_stale_hashes(self, seeded_db, tmp_path):
        rom_path = tmp_path / "stale.gb"
        rom_path.write_bytes(b"\x00" * 16)
        rom_id = _enroll_rom(seeded_db, rom_path, "gb")
        queries.upsert_hash(seeded_db, rom_id, "x", "x", "x")
        # Bump mtime past hashed_at.
        seeded_db.execute(
            "UPDATE roms SET mtime = ? WHERE id = ?", (time.time() + 1000, rom_id)
        )
        seeded_db.commit()
        stale = queries.get_stale_hashes(seeded_db)
        assert len(stale) == 1
        assert stale[0]["id"] == rom_id


# ---------------------------------------------------------------------------
# Security regression — zip decompression bomb (audit v0.1.0 finding #2)
# ---------------------------------------------------------------------------


class TestZipBombCap:
    """Regression suite for the streaming zip-decompression cap.

    Without the cap, a small archive on disk (kilobytes) decompresses to
    gigabytes when ``inner.read()`` is called — eight Heavy-Scan worker
    threads would each blow up memory and OOM-kill the app. The cap streams
    chunks and aborts the moment cumulative bytes exceed
    ``_MAX_ZIP_DECOMPRESSED_BYTES``.
    """

    def test_zip_with_payload_under_cap_succeeds(self, tmp_path: Path) -> None:
        from romulus.core.hasher import _read_zip_payload

        # Real ROM-sized payload (a few KB) — easily under the 2 GiB cap.
        zip_path = tmp_path / "ok.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("game.gb", b"\x00" * 4096)
        payload = _read_zip_payload(zip_path)
        assert payload == b"\x00" * 4096

    def test_zip_bomb_aborts_at_cap(self, tmp_path: Path) -> None:
        """A highly-compressible payload >cap must raise ZipPayloadTooLargeError.

        We can't ship a real 2 GiB bomb in a unit test, so we monkey the cap
        down to 4 KiB and zip a 16 KiB payload — the streaming reader should
        abort at the 4 KiB threshold without materializing the full payload
        into memory.
        """
        import pytest

        from romulus.core import hasher
        from romulus.core.hasher import (
            ZipPayloadTooLargeError,
            _read_zip_payload,
        )

        zip_path = tmp_path / "bomb.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("game.gb", b"A" * 16384)
        # Confirm the archive ITSELF is much smaller than its payload — i.e.
        # this is a representative "compresses well" case even though it isn't
        # a true 42.zip recursive bomb.
        assert zip_path.stat().st_size < 1000

        with pytest.raises(ZipPayloadTooLargeError):
            _read_zip_payload(zip_path, max_bytes=4096)

        # And via the public ``hash_rom`` path: bomb returns None (skipped),
        # the worker pool keeps running for the other ROMs.
        original_cap = hasher._MAX_ZIP_DECOMPRESSED_BYTES
        hasher._MAX_ZIP_DECOMPRESSED_BYTES = 4096
        try:
            assert hasher.hash_rom(zip_path, header_rule=None) is None
        finally:
            hasher._MAX_ZIP_DECOMPRESSED_BYTES = original_cap
