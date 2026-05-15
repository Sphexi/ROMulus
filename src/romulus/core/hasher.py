"""ROM hashing — SHA-1 / CRC32 with header stripping and ZIP support.

`hash_rom` is the per-file entry point: it knows how to peek inside a single-file
.zip, apply the system's `header_rule`, and stream the normalized bytes through
both digests in one pass. `hash_library` orchestrates the whole table with a
ThreadPoolExecutor and a (path, mtime, size) cache check so re-runs are cheap.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import zipfile
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from romulus.core._n64 import (
    N64_MAGIC_N64,
    N64_MAGIC_V64,
    N64_MAGIC_Z64,
    byteswap_n64_to_z64,
    byteswap_v64_to_z64,
)
from romulus.db import queries

logger = logging.getLogger(__name__)

# Match the existing "NES\x1a" / "LYNX\x00" magics used by the identifier.
_INES_MAGIC = b"NES\x1a"
_LYNX_MAGIC = b"LYNX\x00"
# N64 magic bytes — single source of truth lives in ``core/_n64.py``; re-bound
# locally so in-file references keep their existing names.
_N64_MAGIC_Z64 = N64_MAGIC_Z64
_N64_MAGIC_V64 = N64_MAGIC_V64
_N64_MAGIC_N64 = N64_MAGIC_N64

_CHUNK = 1 << 20  # 1 MiB streaming chunk; matches ROM-DEDUP §5.3 example.

# Hard cap on the bytes we will decompress from a single zip entry. Real ROM
# images stay well under 2 GiB (the largest legitimate disc images cap out
# below this), so anything claiming more uncompressed bytes is either a zip
# bomb or unsupportable. ``info.file_size`` in the central directory is
# attacker-controlled, so pre-checks against it are not sufficient — only a
# bounded streaming read is safe. See security audit v0.1.0 finding #2.
_MAX_ZIP_DECOMPRESSED_BYTES = 2 * (1 << 30)  # 2 GiB


class ZipPayloadTooLargeError(Exception):
    """Raised when a zipped ROM exceeds the safe decompression cap."""


@dataclass(frozen=True)
class HashResult:
    """The CRC32/SHA-1/MD5 + final byte count of a normalized ROM payload."""

    crc32: str
    sha1: str
    md5: str
    size: int


# ---------------------------------------------------------------------------
# Header normalization
# ---------------------------------------------------------------------------


# Local aliases for callers in this module — the canonical implementations
# live in ``core/_n64.py`` and are imported above.
_byteswap_v64_to_z64 = byteswap_v64_to_z64
_byteswap_n64_to_z64 = byteswap_n64_to_z64


def normalize_rom_content(content: bytes, header_rule: str | None) -> bytes:
    """Apply the per-system strip/byteswap rule before hashing.

    Unknown or None rules pass content through unchanged. Magic-byte checks
    mean it's safe to call this on already-normalized content — no double
    stripping.
    """
    if header_rule is None:
        return content
    match header_rule:
        case "smc_512":
            if len(content) % 1024 == 512:
                return content[512:]
            return content
        case "ines_16":
            if content[:4] == _INES_MAGIC:
                return content[16:]
            return content
        case "lynx_64":
            if content[:5] == _LYNX_MAGIC:
                return content[64:]
            return content
        case "n64_byteswap":
            head = content[:4]
            if head == _N64_MAGIC_Z64:
                return content
            if head == _N64_MAGIC_V64:
                return _byteswap_v64_to_z64(content)
            if head == _N64_MAGIC_N64:
                return _byteswap_n64_to_z64(content)
            return content
        case _:
            return content


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------


def _read_zip_payload(
    path: Path, max_bytes: int | None = None
) -> bytes | None:
    """Return the inner bytes of a .zip: the only file, or the largest one.

    Returns None for empty archives or unreadable zips so the caller can skip.

    Security (audit v0.1.0 finding #2): the inner file is streamed in
    ``_CHUNK`` slices and aborted the moment cumulative bytes exceed
    ``max_bytes``. Without this cap, a small "42.zip"-style decompression
    bomb (kilobytes on disk, gigabytes uncompressed) would OOM-kill the
    Heavy Scan worker pool when ``inner.read()`` materializes the entire
    payload. ``info.file_size`` is attacker-controlled metadata, so any
    pre-check against the central directory is bypassable — only the
    streaming cap is safe.

    ``max_bytes=None`` (the default) means read the module-level
    ``_MAX_ZIP_DECOMPRESSED_BYTES`` constant at call time so test code can
    monkey-patch the cap down to a few KiB without rebuilding a real bomb.
    """
    if max_bytes is None:
        max_bytes = _MAX_ZIP_DECOMPRESSED_BYTES
    try:
        with zipfile.ZipFile(path) as zf:
            files = [info for info in zf.infolist() if not info.is_dir()]
            if not files:
                return None
            target = (
                files[0]
                if len(files) == 1
                else max(files, key=lambda i: i.file_size)
            )
            with zf.open(target) as inner:
                buf = bytearray()
                total = 0
                while True:
                    chunk = inner.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ZipPayloadTooLargeError(
                            f"zip entry exceeds {max_bytes}B cap "
                            f"(path={path!s}, entry={target.filename!r})"
                        )
                    buf.extend(chunk)
                return bytes(buf)
    except ZipPayloadTooLargeError:
        # Log + skip so a single bomb doesn't poison the whole Heavy Scan,
        # but make sure it's visible to operators.
        raise
    except (zipfile.BadZipFile, OSError):
        return None


# ---------------------------------------------------------------------------
# Per-file hashing
# ---------------------------------------------------------------------------


def _digest_bytes(content: bytes) -> HashResult:
    crc = zlib.crc32(content) & 0xFFFFFFFF
    sha1 = hashlib.sha1(content).hexdigest()
    md5 = hashlib.md5(content).hexdigest()
    return HashResult(crc32=f"{crc:08x}", sha1=sha1, md5=md5, size=len(content))


def _digest_stream(path: Path) -> HashResult:
    """Stream-hash a file without loading it all into RAM."""
    crc = 0
    sha1 = hashlib.sha1()
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            sha1.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return HashResult(
        crc32=f"{crc & 0xFFFFFFFF:08x}",
        sha1=sha1.hexdigest(),
        md5=md5.hexdigest(),
        size=size,
    )


def hash_rom(
    file_path: str | os.PathLike[str],
    header_rule: str | None,
) -> HashResult | None:
    """Compute CRC32/SHA-1/MD5 for a single ROM, honoring header rules and zips.

    Returns None if the file can't be read or a .zip has no extractable payload.
    The returned hashes always reflect the NORMALIZED (header-stripped,
    byte-swapped, zip-extracted) byte stream.
    """
    path = Path(file_path)
    try:
        if path.suffix.lower() == ".zip":
            try:
                payload = _read_zip_payload(path)
            except ZipPayloadTooLargeError as exc:
                # Don't crash the Heavy Scan worker pool — a single bomb
                # should poison only its own ROM row, not its 7 siblings.
                logger.warning("zip payload too large, skipping: %s", exc)
                return None
            if payload is None:
                return None
            normalized = normalize_rom_content(payload, header_rule)
            return _digest_bytes(normalized)

        if header_rule is None:
            # No normalization needed — stream the file directly.
            return _digest_stream(path)

        with path.open("rb") as f:
            raw = f.read()
    except OSError:
        return None

    normalized = normalize_rom_content(raw, header_rule)
    return _digest_bytes(normalized)


# ---------------------------------------------------------------------------
# Library-wide hashing
# ---------------------------------------------------------------------------


def _rows_needing_hash(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ROMs that have never been hashed plus those whose mtime has drifted."""
    rows = conn.execute(
        """
        SELECT r.id, r.path, r.mtime, r.size_bytes, s.header_rule
        FROM roms r
        LEFT JOIN systems s ON s.id = r.system_id
        LEFT JOIN hashes h ON h.rom_id = r.id
        WHERE h.rom_id IS NULL OR h.hashed_at < r.mtime
        ORDER BY r.id
        """
    ).fetchall()
    return list(rows)


def hash_library(
    conn: sqlite3.Connection,
    progress_callback: Callable[[int, int, str], None] | None = None,
    workers: int = 8,
) -> int:
    """Hash every ROM with a missing or stale hash, parallelized across `workers`.

    `progress_callback(done, total, path)` fires once per completed file.
    Returns the count of ROMs successfully hashed this call. Skips files whose
    on-disk mtime no longer matches the recorded mtime (caller should re-scan).
    """
    pending = _rows_needing_hash(conn)
    total = len(pending)
    if total == 0:
        return 0

    def _work(row: sqlite3.Row) -> tuple[int, str, HashResult | None]:
        rom_id = row["id"]
        path = row["path"]
        header_rule = row["header_rule"]
        result = hash_rom(path, header_rule)
        return rom_id, path, result

    done = 0
    successes = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_work, row) for row in pending]
        for fut in as_completed(futures):
            rom_id, path, result = fut.result()
            done += 1
            if result is not None:
                queries.upsert_hash(conn, rom_id, result.crc32, result.sha1, result.md5)
                successes += 1
            if progress_callback is not None:
                progress_callback(done, total, path)

    conn.commit()
    return successes
