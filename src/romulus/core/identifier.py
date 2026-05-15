"""ROM identification pipeline — Layer 2 (internal headers).

Each `extract_*` helper reads the smallest slice of the file needed to recover
the cartridge's internal title. We never read the whole ROM here — that's
Layer 3 (hasher.py). Read errors and malformed dumps return None.
"""

from __future__ import annotations

import os
from pathlib import Path

# Magic bytes used to detect on-disk byte order before reading internal titles.
_N64_MAGIC_Z64 = b"\x80\x37\x12\x40"  # big-endian
_N64_MAGIC_V64 = b"\x37\x80\x40\x12"  # halfword-swapped
_N64_MAGIC_N64 = b"\x40\x12\x37\x80"  # little-endian (word-swapped)
_MD_MAGIC_OVERSEAS = b"SEGA MEGA DRIVE"
_MD_MAGIC_DOMESTIC = b"SEGA GENESIS"
_MD_MAGIC_32X = b"SEGA 32X"
_MD_MAGIC_PICO = b"SEGA PICO"
_MD_MAGICS = (
    _MD_MAGIC_OVERSEAS,
    _MD_MAGIC_DOMESTIC,
    _MD_MAGIC_32X,
    _MD_MAGIC_PICO,
)

# How much of the head of a file to read for header inspection. 64 KB covers
# every cartridge layout we care about (SNES HiROM title is at 0xFFC0).
_HEADER_READ_BYTES = 64 * 1024

# Title slot lengths from TECHNICAL_PLAN.md §6.
_SNES_TITLE_LEN = 21
_N64_TITLE_LEN = 20
_MD_TITLE_LEN = 48
_GB_TITLE_LEN = 16
_GBA_TITLE_LEN = 12
_DS_TITLE_LEN = 12


def _clean_ascii_title(raw: bytes) -> str | None:
    """Decode ASCII, trim at first null, drop control chars, collapse whitespace."""
    text = raw.decode("ascii", errors="replace")
    if "\x00" in text:
        text = text.split("\x00", 1)[0]
    cleaned = "".join(c for c in text if c.isprintable()).strip()
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _printable_ratio(raw: bytes) -> float:
    """Fraction of bytes that decode to printable ASCII (space..~)."""
    if not raw:
        return 0.0
    printable = sum(1 for b in raw if 0x20 <= b <= 0x7E)
    return printable / len(raw)


def _byteswap_v64_to_z64(data: bytes) -> bytes:
    """Halfword-swap (v64 -> z64): swap adjacent bytes in each 2-byte unit."""
    ba = bytearray(data)
    if len(ba) % 2:
        ba.append(0)
    for i in range(0, len(ba), 2):
        ba[i], ba[i + 1] = ba[i + 1], ba[i]
    return bytes(ba)


def _byteswap_n64_to_z64(data: bytes) -> bytes:
    """Word-swap (n64 -> z64): reverse byte order in each 4-byte unit."""
    ba = bytearray(data)
    pad = (-len(ba)) % 4
    if pad:
        ba.extend(b"\x00" * pad)
    for i in range(0, len(ba), 4):
        ba[i : i + 4] = ba[i : i + 4][::-1]
    return bytes(ba)


def _normalize_n64_to_z64(data: bytes) -> bytes | None:
    """Detect N64 byte order from leading magic; return z64-form data or None."""
    head = data[:4]
    if head == _N64_MAGIC_Z64:
        return data
    if head == _N64_MAGIC_V64:
        return _byteswap_v64_to_z64(data)
    if head == _N64_MAGIC_N64:
        return _byteswap_n64_to_z64(data)
    return None


def _strip_smc_header(data: bytes) -> bytes:
    """Drop the 512-byte SMC copier header if `size % 1024 == 512`."""
    if len(data) % 1024 == 512:
        return data[512:]
    return data


def _extract_snes_title(data: bytes) -> str | None:
    """Try LoROM (0x7FC0) and HiROM (0xFFC0), pick the higher printable-ASCII ratio."""
    body = _strip_smc_header(data)
    candidates: list[tuple[float, str]] = []
    for offset in (0x7FC0, 0xFFC0):
        if len(body) < offset + _SNES_TITLE_LEN:
            continue
        raw = body[offset : offset + _SNES_TITLE_LEN]
        title = _clean_ascii_title(raw)
        if title:
            candidates.append((_printable_ratio(raw), title))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _extract_n64_title(data: bytes) -> str | None:
    z64 = _normalize_n64_to_z64(data)
    if z64 is None or len(z64) < 0x20 + _N64_TITLE_LEN:
        return None
    return _clean_ascii_title(z64[0x20 : 0x20 + _N64_TITLE_LEN])


def _extract_md_title(data: bytes) -> str | None:
    if len(data) < 0x100 + 16:
        return None
    magic_slice = data[0x100 : 0x100 + 16]
    if not magic_slice.startswith(_MD_MAGICS):
        return None
    candidates: list[tuple[float, str]] = []
    for offset in (0x150, 0x120):
        if len(data) < offset + _MD_TITLE_LEN:
            continue
        raw = data[offset : offset + _MD_TITLE_LEN]
        title = _clean_ascii_title(raw)
        if title:
            candidates.append((_printable_ratio(raw), title))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _extract_gb_title(data: bytes) -> str | None:
    if len(data) < 0x134 + _GB_TITLE_LEN:
        return None
    return _clean_ascii_title(data[0x134 : 0x134 + _GB_TITLE_LEN])


def _extract_gba_title(data: bytes) -> str | None:
    if len(data) < 0xA0 + _GBA_TITLE_LEN:
        return None
    return _clean_ascii_title(data[0xA0 : 0xA0 + _GBA_TITLE_LEN])


def _extract_ds_title(data: bytes) -> str | None:
    if len(data) < _DS_TITLE_LEN:
        return None
    return _clean_ascii_title(data[0:_DS_TITLE_LEN])


def extract_header_title(
    file_path: str | os.PathLike[str],
    system_id: str,
    header_rule: str | None = None,
) -> str | None:
    """Read the internal title from a ROM file based on its system.

    `system_id` chooses the offset table (per TECHNICAL_PLAN.md §6). `header_rule`
    is accepted for API symmetry with `hash_rom` and reserved for future use;
    SNES and N64 read paths already handle their own SMC strip / byte-swap
    internally. Returns None when the system has no internal title slot, the
    file is too small or corrupt, or no printable title was found.
    """
    path = Path(file_path)
    try:
        with path.open("rb") as f:
            data = f.read(_HEADER_READ_BYTES)
    except OSError:
        return None

    if not data:
        return None

    match system_id:
        case "snes":
            return _extract_snes_title(data)
        case "n64":
            return _extract_n64_title(data)
        case "megadrive":
            return _extract_md_title(data)
        case "gb" | "gbc":
            return _extract_gb_title(data)
        case "gba":
            return _extract_gba_title(data)
        case "nds":
            return _extract_ds_title(data)
        case _:
            # Systems without an internal title slot (NES, PCE, etc.) — see
            # ROM-DEDUP §4.3. Caller falls back to filename.
            _ = header_rule
            return None
