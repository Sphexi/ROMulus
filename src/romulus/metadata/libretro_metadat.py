"""Local-first libretro-database metadat metadata source.

Loads the clrmamepro-format DAT files shipped under
``data/libretro-metadat/<dimension>/<libretro_name>.dat``, one
dimension at a time, and exposes CRC32-keyed lookups.

Why this matters: libretro-database is the canonical offline metadata
layer used by RetroArch's "Explore" feature. Each dimension is a
separate file — ``genre/``, ``developer/``, ``publisher/``,
``releaseyear/``, ``maxusers/``, ``esrb/``, ``franchise/`` — so we
load whichever dimensions are present per system and merge their
results into the shared :class:`MetadataPayload` shape at lookup time.

Coverage is broader than GameDB (49 systems with genre data versus 1
in GameDB; 52 with developer versus 1 in GameDB) so this source slots
in *before* GameDB in the enrichment chain — see
``romulus.metadata._fetch_metadata_for_game``.

Source: https://github.com/libretro/libretro-database
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from romulus.metadata._types import MetadataPayload
from romulus.models.system import SYSTEM_REGISTRY

logger = logging.getLogger(__name__)

# Every dimension's bundled subfolder under ``data/libretro-metadat/``.
# Keep the keys aligned with the bundling script
# (``scripts/download_libretro_metadat.py::_DIMENSIONS``) and with the
# extractor below (``_DIM_TO_METADATA_KEY``). Ordering doesn't matter at
# runtime; alphabetical for readability.
_DIMENSIONS: tuple[str, ...] = (
    "developer",
    "esrb",
    "franchise",
    "genre",
    "maxusers",
    "publisher",
    "releaseyear",
)

# Map each libretro dimension to the matching ``MetadataPayload`` key.
# ``franchise`` is ingested but not currently surfaced in the UI — kept
# in storage for future use without re-bundling.
_DIM_TO_METADATA_KEY: dict[str, str] = {
    "genre": "genre",
    "developer": "developer",
    "publisher": "publisher",
    "maxusers": "players",
    "esrb": "rating",
}

# Per-system :class:`LibretroMetadatIndex` cache (or ``None`` when no
# files are bundled for the system). Lives for the process lifetime.
_index_cache: dict[str, LibretroMetadatIndex | None] = {}
_cache_lock = threading.Lock()


# Each ``game (...)`` block looks like::
#
#     game (
#         comment "..."
#         <dimension> "value"
#         rom ( crc XXXXXXXX )
#     )
#
# We split on the ``game (`` opener and then pull the CRC + dimension
# value with two regexes. Comments are ignored — the CRC is the
# canonical key and is reliable; comments diverge between dimensions
# (e.g. one DAT may carry a (USA) tag the other strips).
_BLOCK_OPENER = re.compile(r"^game\s*\(", re.MULTILINE)
_CRC_RE = re.compile(r"\brom\s*\(\s*crc\s+([0-9A-Fa-f]+)", re.IGNORECASE)


def _build_dim_value_re(dim: str) -> re.Pattern[str]:
    """Build a regex extracting the *dim* field value from a game block.

    The escape on ``dim`` is defensive — every name in ``_DIMENSIONS``
    is alphabetic, but the same parser may be reused for a future
    dimension whose key isn't safe as-is.
    """
    return re.compile(rf'\b{re.escape(dim)}\s+"([^"]*)"', re.IGNORECASE)


def parse_metadat_file(path: Path, dimension: str) -> dict[str, str]:
    """Parse one metadat .dat file into ``{crc32 (lowercase) -> value}``.

    Empty values and CRCs that fail the hex/length guard are dropped.
    Logs and returns an empty dict on read errors so a single bad file
    can't break the rest of the index build.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("libretro_metadat: read failed for %s: %s", path, exc)
        return {}
    dim_re = _build_dim_value_re(dimension)
    out: dict[str, str] = {}
    # Split on the game-block opener; the first chunk is the header.
    blocks = _BLOCK_OPENER.split(text)
    for block in blocks[1:]:
        crc_match = _CRC_RE.search(block)
        if crc_match is None:
            continue
        dim_match = dim_re.search(block)
        if dim_match is None:
            continue
        crc = crc_match.group(1).lower().zfill(8)[-8:]
        if len(crc) != 8:
            continue
        value = dim_match.group(1).strip()
        if not value:
            continue
        # First wins — duplicates exist (e.g. revisions of the same dump).
        out.setdefault(crc, value)
    return out


class LibretroMetadatIndex:
    """In-memory union of per-dimension CRC32 → value tables for one system.

    ``by_dim`` maps each loaded dimension (e.g. ``"genre"``) to its
    CRC32→value lookup. ``lookup`` joins across dimensions to produce
    the merged ``{dimension_key: value}`` view a single game's CRC
    resolves to.
    """

    __slots__ = ("by_dim", "entry_count")

    def __init__(self, by_dim: dict[str, dict[str, str]]) -> None:
        # Keep only non-empty dim tables so iteration is meaningful.
        self.by_dim: dict[str, dict[str, str]] = {
            dim: table for dim, table in by_dim.items() if table
        }
        # ``entry_count`` is the union of CRCs across all dimensions —
        # useful for logging / debugging. Computed lazily isn't worth it
        # at the small file sizes we deal with (~6500 entries × 7 dims
        # ≈ 50k strings worst case).
        crcs: set[str] = set()
        for table in self.by_dim.values():
            crcs.update(table.keys())
        self.entry_count = len(crcs)

    def lookup(self, crc32: str) -> dict[str, str]:
        """Return the merged ``{dimension: value}`` view for *crc32*.

        Empty dict when no dimension carries this CRC. Caller should
        treat empty as "no metadata available for this game" and fall
        through to the next provider.
        """
        result: dict[str, str] = {}
        for dim, table in self.by_dim.items():
            value = table.get(crc32)
            if value:
                result[dim] = value
        return result


def _normalise_crc32(value: str | None) -> str | None:
    """Lowercase + strip ``0x`` + 8-char pad/clamp. Mirrors gamedb.py's helper.

    Duplicating the helper rather than importing keeps the two metadata
    sources independent — a future libretro-only change shouldn't drag
    gamedb tests along, and vice versa.
    """
    if not value:
        return None
    text = value.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not re.fullmatch(r"[0-9a-f]+", text):
        return None
    return text.zfill(8)[-8:]


def resolve_dim_dir(dimension: str) -> Path | None:
    """Locate the bundled libretro-metadat dimension directory.

    Two layouts are accepted, mirroring the DAT and GameDB path
    resolution:

    1. ``<install_dir>/libretro-metadat/<dim>/`` — portable layout.
    2. ``<install_dir>/data/libretro-metadat/<dim>/`` — dev clone.
    """
    from romulus.app import _resolve_install_dir

    install_dir = _resolve_install_dir()
    for candidate in (
        install_dir / "libretro-metadat" / dimension,
        install_dir / "data" / "libretro-metadat" / dimension,
    ):
        if candidate.is_dir():
            return candidate
    return None


def _build_index_for_system(libretro_name: str) -> LibretroMetadatIndex | None:
    """Load every available dimension file for *libretro_name*.

    Returns ``None`` when the system has no bundled files at all.
    Returns an index with whatever subset of dimensions did exist
    otherwise — missing dimensions are simply absent from
    :attr:`LibretroMetadatIndex.by_dim`.
    """
    by_dim: dict[str, dict[str, str]] = {}
    for dim in _DIMENSIONS:
        dim_dir = resolve_dim_dir(dim)
        if dim_dir is None:
            continue
        candidate = dim_dir / f"{libretro_name}.dat"
        if not candidate.is_file():
            continue
        table = parse_metadat_file(candidate, dim)
        if table:
            by_dim[dim] = table
    if not by_dim:
        return None
    return LibretroMetadatIndex(by_dim)


def get_index_for_system(system_id: str) -> LibretroMetadatIndex | None:
    """Return the cached :class:`LibretroMetadatIndex` for *system_id*.

    Cache hit returns immediately. Cache miss locates the SystemDef,
    extracts its ``libretro_name``, walks every dimension folder, and
    builds the index in one pass. Both successful loads AND ``None``
    (no data) are cached — a system with no libretro coverage will
    miss every game during an enrich run, so we don't want to re-scan
    the filesystem on each one.
    """
    with _cache_lock:
        if system_id in _index_cache:
            return _index_cache[system_id]
    libretro_name = _libretro_name_for(system_id)
    if not libretro_name:
        with _cache_lock:
            _index_cache[system_id] = None
        return None
    index = _build_index_for_system(libretro_name)
    if index is not None:
        logger.info(
            "libretro_metadat: loaded system_id=%s dims=%s entries=%d",
            system_id,
            sorted(index.by_dim),
            index.entry_count,
        )
    with _cache_lock:
        _index_cache[system_id] = index
    return index


def reset_cache_for_tests() -> None:
    """Clear the per-system index cache. Test-only escape hatch."""
    with _cache_lock:
        _index_cache.clear()


def _libretro_name_for(system_id: str) -> str | None:
    """Return the ``libretro_name`` of the registered system, or None."""
    for entry in SYSTEM_REGISTRY:
        if entry.id == system_id:
            return entry.libretro_name
    return None


def lookup_by_crc32(
    crc32: str | None, index: LibretroMetadatIndex
) -> dict[str, str] | None:
    """Return the dimension→value map for *crc32*, or ``None`` on miss."""
    normalised = _normalise_crc32(crc32)
    if not normalised:
        return None
    merged = index.lookup(normalised)
    return merged or None


def entry_to_metadata(entry: dict[str, str]) -> MetadataPayload:
    """Project a libretro dimension dict into the shared MetadataPayload shape.

    ``releaseyear`` is parsed to an int when possible (libretro stores
    it as a 4-digit string). Other dimensions pass straight through as
    strings.
    """
    payload: MetadataPayload = {}  # type: ignore[typeddict-item]
    for dim, value in entry.items():
        key = _DIM_TO_METADATA_KEY.get(dim)
        if key is None:
            continue
        payload[key] = value  # type: ignore[literal-required]
    year_raw = entry.get("releaseyear")
    if year_raw:
        try:
            year = int(year_raw)
        except ValueError:
            year = None
        if year is not None and 1970 <= year <= 2100:
            payload["release_year"] = year
    return payload
