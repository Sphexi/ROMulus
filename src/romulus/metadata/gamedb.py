"""Local-first GameDB metadata source.

GameDB (https://github.com/niemasd/GameDB) ships per-console JSON files
keyed by serial number, each entry carrying identifier fields (CRC32,
canonical release name, region) and — for a handful of consoles —
curated developer / publisher / release date.

This module loads one such file per system on demand, caches the parsed
index for the process lifetime, and exposes two lookups:

* :func:`lookup_by_crc32` — the primary matcher; needs the local ROM's
  CRC32 from the ``hashes`` table (Heavy Scan output).
* :func:`lookup_by_title` — fuzzy fallback matching the same normalised-
  title scheme used elsewhere in this package.

When both fail the orchestrator falls through to the remote APIs.
Designed to slot in *first* in the enrichment chain so we minimise
network traffic on libraries that are well-covered by GameDB.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from romulus.metadata._types import MetadataPayload
from romulus.models.system import SYSTEM_REGISTRY

logger = logging.getLogger(__name__)

# Reuse the same title-normalisation rules as the TGDB client so a fuzzy
# match against GameDB and a fuzzy match against TGDB agree on what
# counts as "the same title". Parenthesised tags first, then punctuation.
_PARENTHESISED_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*", re.UNICODE)
_TITLE_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)

# CRC32 values are case-insensitive hex; GameDB writes them with a
# ``0x`` prefix while ROMulus's ``hashes.crc32`` carries lowercase hex
# without the prefix. Normalise to the latter.
_HEX_RE = re.compile(r"^[0-9a-fA-F]{1,8}$")

# Cache the per-system :class:`GameDBIndex` once loaded. ``None`` is
# also cached when the file is missing or malformed to avoid retrying
# on every game in a large enrich run.
_index_cache: dict[str, GameDBIndex | None] = {}
_cache_lock = threading.Lock()


def _normalise_title(title: str) -> str:
    """Lower + strip parenthesised tags + strip punctuation."""
    stripped = _PARENTHESISED_RE.sub(" ", title)
    return _TITLE_NOISE_RE.sub("", stripped.lower())


def _normalise_crc32(value: object) -> str | None:
    """Return a lowercase 8-char hex string for *value*, or ``None``.

    Accepts ``"0x26b5cf8b"`` (GameDB form), ``"26b5cf8b"`` (DAT form),
    and rejects anything that isn't hex or is wrong length after the
    ``0x`` prefix is removed.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not _HEX_RE.match(text):
        return None
    return text.zfill(8)[-8:]  # pad/clamp to 8 chars to match DAT form


def _extract_year(release_date: object) -> int | None:
    """Pull a year out of an ISO date string, plain year, or anything else.

    GameDB's ``release_date`` is typically ``"1990-11-21"`` or ``"1990"``
    on the consoles that carry it. The pattern below treats any leading
    4-digit substring as a year, rejecting plausibly-out-of-range values
    (rom history starts in 1972 and emulator users definitely aren't
    filing 30th-century releases).
    """
    if release_date in (None, ""):
        return None
    text = str(release_date)
    match = re.match(r"(\d{4})", text)
    if not match:
        return None
    year = int(match.group(1))
    if 1970 <= year <= 2100:
        return year
    return None


class GameDBIndex:
    """In-memory lookup tables over one console's GameDB JSON.

    Built once per system and cached. ``__slots__`` keeps the per-system
    overhead small (the SNES file holds ~4000 entries; even a thin dict
    per entry adds up).
    """

    __slots__ = ("by_crc32", "by_normalised_title", "entry_count")

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.by_crc32: dict[str, dict[str, Any]] = {}
        self.by_normalised_title: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            crc = _normalise_crc32(entry.get("crc32"))
            if crc:
                self.by_crc32.setdefault(crc, entry)
            # Index by both display title and release name. Both fields
            # may be present; release_name is the canonical No-Intro
            # form with region tags, title is the shorter user-facing
            # form. Either can match what the scanner derived locally.
            for field in ("title", "release_name"):
                value = entry.get(field)
                if isinstance(value, str) and value:
                    norm = _normalise_title(value)
                    if norm:
                        self.by_normalised_title.setdefault(norm, entry)
        self.entry_count = len(entries)


def load_index(path: Path) -> GameDBIndex | None:
    """Parse and index a single GameDB JSON file.

    Returns ``None`` when the file can't be opened or parsed — logged as
    a warning rather than raised so enrichment continues gracefully even
    if one console's bundled file is corrupt.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("gamedb: failed to load %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "gamedb: %s top-level is not a dict (got %s)",
            path,
            type(raw).__name__,
        )
        return None
    entries = [v for v in raw.values() if isinstance(v, dict)]
    if not entries:
        logger.info("gamedb: %s contains no usable entries", path)
        return None
    return GameDBIndex(entries)


def resolve_index_path(gamedb_file: str) -> Path | None:
    """Locate the bundled JSON for one system, dev clone or portable build.

    Two layouts are accepted, mirroring the DAT-path resolution:

    1. ``<install_dir>/gamedb/<file>`` — portable Windows ZIP layout.
    2. ``<install_dir>/data/gamedb/<file>`` — in-repo dev clone.

    Returns ``None`` if neither path resolves to a file on disk.
    """
    # Deferred import: app.py imports the system registry, and importing
    # app at module load time would build a cycle through ``models.system``
    # -> ``ui.artwork`` -> back into app's resolver.
    from romulus.app import _resolve_install_dir

    install_dir = _resolve_install_dir()
    for candidate in (
        install_dir / "gamedb" / gamedb_file,
        install_dir / "data" / "gamedb" / gamedb_file,
    ):
        if candidate.is_file():
            return candidate
    return None


def _find_system_def_with_gamedb(system_id: str) -> str | None:
    """Return the ``gamedb_file`` field for *system_id*, or None."""
    for entry in SYSTEM_REGISTRY:
        if entry.id == system_id:
            return entry.gamedb_file
    return None


def get_index_for_system(system_id: str) -> GameDBIndex | None:
    """Return the cached :class:`GameDBIndex` for *system_id*, loading if needed.

    Caches both successful loads and ``None`` (missing/malformed) so
    long enrich runs over a fixed system don't re-stat the file or
    re-parse the JSON. The cache lives for the process lifetime; tests
    can clear it via :func:`reset_cache_for_tests`.
    """
    with _cache_lock:
        if system_id in _index_cache:
            return _index_cache[system_id]
    gamedb_file = _find_system_def_with_gamedb(system_id)
    if not gamedb_file:
        with _cache_lock:
            _index_cache[system_id] = None
        return None
    path = resolve_index_path(gamedb_file)
    if path is None:
        logger.debug(
            "gamedb: file %s declared for system_id=%s but not found on disk",
            gamedb_file,
            system_id,
        )
        with _cache_lock:
            _index_cache[system_id] = None
        return None
    index = load_index(path)
    if index is not None:
        logger.info(
            "gamedb: loaded %s for system_id=%s entries=%d",
            path.name,
            system_id,
            index.entry_count,
        )
    with _cache_lock:
        _index_cache[system_id] = index
    return index


def reset_cache_for_tests() -> None:
    """Clear the per-system index cache. Test-only escape hatch."""
    with _cache_lock:
        _index_cache.clear()


def lookup_by_crc32(
    crc32: str | None, index: GameDBIndex
) -> dict[str, Any] | None:
    """Return the GameDB entry whose ``crc32`` matches *crc32*, or ``None``."""
    if not crc32:
        return None
    normalised = _normalise_crc32(crc32)
    if not normalised:
        return None
    return index.by_crc32.get(normalised)


def lookup_by_title(
    title: str | None, index: GameDBIndex
) -> dict[str, Any] | None:
    """Return the entry whose normalised title equals *title*, or ``None``."""
    if not title:
        return None
    norm = _normalise_title(title)
    return index.by_normalised_title.get(norm) if norm else None


def entry_to_metadata(entry: dict[str, Any]) -> MetadataPayload:
    """Project a GameDB entry into the shared MetadataPayload shape.

    Fills only the fields ROMulus actually surfaces: publisher (when
    present), release_date (when present), release_year (derived if a
    full release_date is given, else taken directly from a year-only
    field if the future schema ever ships one). Identifier-only fields
    (crc32, internal_title, serial, etc.) are NOT mapped — they belong
    on the rom rows / DAT pipeline, not the metadata table.
    """
    publisher = entry.get("publisher")
    release_date = entry.get("release_date")
    release_year = _extract_year(release_date)
    payload: MetadataPayload = {  # type: ignore[typeddict-item]
        "title": entry.get("title"),
        "publisher": publisher if isinstance(publisher, str) and publisher else None,
        "release_date": (
            release_date if isinstance(release_date, str) and release_date else None
        ),
        "release_year": release_year,
    }
    return payload
