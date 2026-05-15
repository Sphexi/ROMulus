"""Shared N64 byte-order normalization helpers.

The identifier (Layer 2) and the hasher (Layer 3) both need to detect the
on-disk byte order of an N64 ROM and re-emit it in canonical ``z64`` (big-
endian) form before doing anything with the bytes. The helpers and their
magic-byte constants used to be duplicated bit-for-bit between
``core/identifier.py`` and ``core/hasher.py``; centralizing them here keeps
the two layers from drifting.

The three N64 byte-order variants seen in the wild:

* **z64** — big-endian, the canonical form used by every DAT.
* **v64** — halfword-swapped (adjacent byte pairs reversed).
* **n64** — word-swapped (byte order reversed inside each 4-byte word).
"""

from __future__ import annotations

#: N64 ROM magic bytes — big-endian (canonical z64 form).
N64_MAGIC_Z64: bytes = b"\x80\x37\x12\x40"
#: N64 ROM magic bytes — halfword-swapped (v64).
N64_MAGIC_V64: bytes = b"\x37\x80\x40\x12"
#: N64 ROM magic bytes — word-swapped little-endian (n64).
N64_MAGIC_N64: bytes = b"\x40\x12\x37\x80"


def byteswap_v64_to_z64(data: bytes) -> bytes:
    """Halfword-swap ``v64`` payload into canonical ``z64`` byte order.

    Swaps adjacent bytes within every 2-byte unit. Pads a trailing zero byte
    if the input length is odd so the swap is well-defined.
    """
    ba = bytearray(data)
    if len(ba) % 2:
        ba.append(0)
    for i in range(0, len(ba), 2):
        ba[i], ba[i + 1] = ba[i + 1], ba[i]
    return bytes(ba)


def byteswap_n64_to_z64(data: bytes) -> bytes:
    """Word-swap ``n64`` payload into canonical ``z64`` byte order.

    Reverses byte order inside every 4-byte word. Pads with zeros up to the
    next 4-byte boundary if the input length is not a multiple of 4.
    """
    ba = bytearray(data)
    pad = (-len(ba)) % 4
    if pad:
        ba.extend(b"\x00" * pad)
    for i in range(0, len(ba), 4):
        ba[i : i + 4] = ba[i : i + 4][::-1]
    return bytes(ba)
