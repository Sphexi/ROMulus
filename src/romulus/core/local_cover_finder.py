"""Local cover discovery — scan the library tree for images matching enrolled ROMs.

Many existing ROM collections (organized by WBM, Skraper, EmulationStation scrapers,
etc.) already have ``media/images/``, ``media/boxart/``, ``screenshots/``, etc.
subfolders with cover art already downloaded. This module finds those before
anything is fetched from libretro-thumbnails.

Design notes:
- The filesystem is walked *once* per discovery run; all image files are bucketed
  by their fuzzy key so lookups are O(1) per ROM rather than O(filesystem).
- ``cover_type`` is inferred from the parent folder name using ``COVER_TYPE_HINTS``.
- Images are inserted with ``source_url=NULL`` to distinguish locally-discovered
  covers from downloaded ones.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from romulus.core.scanner import generate_fuzzy_key, parse_filename

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
)

# Folder-name keyword → cover_type value (matching libretro-thumbnails naming).
# Keys are lowercased folder basenames. Multi-segment paths like ``media/images``
# are handled by checking the *last* path component (the leaf dirname) first;
# if that misses, we also check the immediate parent.
COVER_TYPE_HINTS: dict[str, str] = {
    "boxart": "Named_Boxarts",
    "box": "Named_Boxarts",
    "images": "Named_Boxarts",
    "named_boxarts": "Named_Boxarts",
    "screenshots": "Named_Snaps",
    "snaps": "Named_Snaps",
    "named_snaps": "Named_Snaps",
    "titles": "Named_Titles",
    "wheel": "Named_Titles",
    "title": "Named_Titles",
    "named_titles": "Named_Titles",
}

# Relative subdirectory paths (from the system folder) to probe for images.
# Ordered by priority: same dir first, then common media sub-trees.
MEDIA_SUBDIRS: tuple[str, ...] = (
    "",  # same directory as the ROM (system folder root)
    "media",
    "media/images",
    "media/boxart",
    "images",
    "boxart",
    "box",
    "screenshots",
    "snaps",
    "titles",
    "wheel",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalCoverMatch:
    """A single discovered local cover image matched against an enrolled ROM.

    Attributes:
        rom_id: The database row ID of the matched ROM.
        game_id: The database row ID of the game this ROM belongs to.
        image_path: Absolute path to the discovered image file.
        cover_type: Inferred cover type (e.g. ``"Named_Boxarts"``).
    """

    rom_id: int
    game_id: int
    image_path: str
    cover_type: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Summary of a :func:`discover_local_covers` run.

    Attributes:
        roms_scanned: Total number of ROM rows examined.
        covers_found: New cover rows inserted into the DB.
        covers_skipped_existing: Cover rows skipped because they already existed.
        errors: Number of non-fatal errors logged (unreadable directories, etc.).
    """

    roms_scanned: int
    covers_found: int
    covers_skipped_existing: int
    errors: int


# ---------------------------------------------------------------------------
# Cover-type inference
# ---------------------------------------------------------------------------


def _infer_cover_type(image_path: Path) -> str:
    """Infer a libretro-thumbnails cover_type from an image's parent folder name.

    Checks the leaf directory name first, then (for paths like ``media/images``)
    also checks the grandparent. Falls back to ``"Named_Boxarts"`` when no hint
    matches.

    Args:
        image_path: Absolute (or relative) path to the image file.

    Returns:
        One of ``"Named_Boxarts"``, ``"Named_Snaps"``, or ``"Named_Titles"``.
    """
    leaf = image_path.parent.name.lower()
    if leaf in COVER_TYPE_HINTS:
        return COVER_TYPE_HINTS[leaf]
    # Try grandparent for paths like ``media/images/`` where leaf is ``images``
    # but the hint might be on the next level up.
    grandparent = image_path.parent.parent.name.lower()
    if grandparent in COVER_TYPE_HINTS:
        return COVER_TYPE_HINTS[grandparent]
    return "Named_Boxarts"


# ---------------------------------------------------------------------------
# Filesystem image bucket builder
# ---------------------------------------------------------------------------


def _build_image_bucket(
    system_dir: Path,
) -> dict[str, list[tuple[str, str]]]:
    """Walk all probe subdirs under ``system_dir`` and bucket images by fuzzy key.

    The bucket maps ``fuzzy_key -> [(absolute_image_path, cover_type), ...]``.
    Building once per system_dir makes per-ROM lookup O(1) instead of O(FS).

    Args:
        system_dir: The root directory for a single system (e.g. ``library/snes``).

    Returns:
        A dict mapping each image's fuzzy key to a list of
        ``(absolute_path_string, cover_type)`` pairs.
    """
    bucket: dict[str, list[tuple[str, str]]] = {}

    for subdir in MEDIA_SUBDIRS:
        probe_dir = system_dir / subdir if subdir else system_dir
        if not probe_dir.is_dir():
            continue
        try:
            for entry in os.scandir(probe_dir):
                if not entry.is_file(follow_symlinks=False):
                    continue
                img_path = Path(entry.path)
                if img_path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                # Compute the fuzzy key for the image stem (tag-stripped like ROM names).
                # release_type is included so a `Sonic (Virtual Console).png`
                # bucket-keys differently from `Sonic.png` and matches its sibling ROM.
                parsed = parse_filename(entry.name)
                fkey = generate_fuzzy_key(parsed.clean_name, parsed.release_type)
                if not fkey:
                    continue
                cover_type = _infer_cover_type(img_path)
                bucket.setdefault(fkey, []).append(
                    (str(img_path.resolve()), cover_type)
                )
        except OSError as exc:
            logger.debug("local_cover_finder: scandir failed dir=%s err=%s", probe_dir, exc)

    return bucket


# ---------------------------------------------------------------------------
# Per-ROM finder (no DB; used in tests and by the orchestrator)
# ---------------------------------------------------------------------------


def find_local_covers_for_rom(
    rom_id: int,
    game_id: int,
    rom_path: str,
    fuzzy_key: str,
    clean_name: str,
    system_dir: Path,
    image_bucket: dict[str, list[tuple[str, str]]] | None = None,
) -> list[LocalCoverMatch]:
    """Find local image files whose name matches ``rom_path``'s title.

    When ``image_bucket`` is supplied the function uses it directly (fast path
    for the :func:`discover_local_covers` orchestrator).  If omitted the bucket
    is built from ``system_dir`` on demand (useful in isolated tests).

    Matching strategy — three fuzzy keys are checked against the bucket:
    1. The ROM's stored ``fuzzy_key`` (already normalized).
    2. The filename stem's fuzzy key (catches stem-only image names).
    3. The ``clean_name``'s fuzzy key (tag-stripped display title).

    Args:
        rom_id: DB row ID of the ROM.
        game_id: DB row ID of the owning game.
        rom_path: Absolute path string of the ROM file.
        fuzzy_key: Pre-computed fuzzy key from ``roms.fuzzy_key``.
        clean_name: Tag-stripped title from ``parse_filename().clean_name``.
        system_dir: Root directory of the system containing this ROM.
        image_bucket: Pre-built image bucket (optional).

    Returns:
        List of :class:`LocalCoverMatch` instances (may be empty).
    """
    if image_bucket is None:
        image_bucket = _build_image_bucket(system_dir)

    rom_stem = Path(rom_path).stem
    stem_parsed = parse_filename(rom_stem)
    stem_key = generate_fuzzy_key(stem_parsed.clean_name, stem_parsed.release_type)
    clean_key = (
        generate_fuzzy_key(clean_name, stem_parsed.release_type)
        if clean_name
        else ""
    )

    # Deduplicate candidate keys while preserving priority order.
    seen: set[str] = set()
    candidate_keys: list[str] = []
    for k in (fuzzy_key, stem_key, clean_key):
        if k and k not in seen:
            candidate_keys.append(k)
            seen.add(k)

    seen_paths: set[str] = set()
    matches: list[LocalCoverMatch] = []
    for key in candidate_keys:
        for img_path, cover_type in image_bucket.get(key, []):
            if img_path not in seen_paths:
                seen_paths.add(img_path)
                matches.append(
                    LocalCoverMatch(
                        rom_id=rom_id,
                        game_id=game_id,
                        image_path=img_path,
                        cover_type=cover_type,
                    )
                )

    return matches


# ---------------------------------------------------------------------------
# DB helpers (local imports keep queries.py as single SQL source of truth)
# ---------------------------------------------------------------------------


def _get_roms_with_games(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all ROM rows that are linked to a game.

    Columns: rom_id, rom_path, system_id, game_id, fuzzy_key, clean_name.
    ROMs without a game_id are excluded — covers are attached to games, not ROMs.
    """
    return conn.execute(
        """
        SELECT r.id       AS rom_id,
               r.path     AS rom_path,
               r.system_id,
               r.game_id,
               r.fuzzy_key,
               COALESCE(r.dat_match, '') AS clean_name
        FROM roms r
        WHERE r.game_id IS NOT NULL
          AND r.fuzzy_key IS NOT NULL
          AND r.fuzzy_key != ''
        ORDER BY r.system_id, r.id
        """
    ).fetchall()


def _has_cover_for_path(
    conn: sqlite3.Connection, game_id: int, local_path: str
) -> bool:
    """Return True if a cover row with this exact local_path already exists.

    Used for idempotent re-runs — avoids inserting duplicate rows when the
    discovery is run more than once on the same library.

    Args:
        conn: SQLite connection.
        game_id: Game to check.
        local_path: Absolute path string of the image.
    """
    row = conn.execute(
        "SELECT 1 FROM covers WHERE game_id = ? AND local_path = ? LIMIT 1",
        (game_id, local_path),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[int, int, str], None]


def discover_local_covers(
    conn: sqlite3.Connection,
    library_path: str | os.PathLike[str],
    progress_callback: ProgressCallback | None = None,
    scope_rom_ids: list[int] | None = None,
) -> DiscoveryResult:
    """Walk all enrolled ROMs and link locally discovered images to the covers table.

    Algorithm (O(N + M) where N = ROMs, M = images):
    1. Group ROMs by system directory.
    2. For each unique system directory, walk the MEDIA_SUBDIRS once and build
       a fuzzy-key → image-paths bucket.
    3. For each ROM in that system, look up candidate keys in the bucket.
    4. Insert new cover rows; skip rows that already exist (idempotent).

    Args:
        conn: SQLite connection (caller owns the transaction boundary).
        library_path: Root of the library being scanned.
        progress_callback: Optional ``(current, total, current_rom_path)``
            callback called once per ROM processed.
        scope_rom_ids: When supplied, only process ROMs whose id is in this
            list. All other ROMs are skipped.

    Returns:
        :class:`DiscoveryResult` summary.
    """
    from romulus.db.queries import insert_cover

    rows = _get_roms_with_games(conn)
    if scope_rom_ids is not None:
        allowed = frozenset(scope_rom_ids)
        rows = [r for r in rows if int(r["rom_id"]) in allowed]
    total = len(rows)
    roms_scanned = 0
    covers_found = 0
    covers_skipped = 0
    errors = 0

    # Group rows by system directory so we build the image bucket once per dir.
    # The system directory is the parent directory of the ROM file.
    from collections import defaultdict

    by_system_dir: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        rom_dir = str(Path(row["rom_path"]).parent)
        by_system_dir[rom_dir].append(row)

    for system_dir_str, dir_rows in by_system_dir.items():
        system_dir = Path(system_dir_str)
        try:
            image_bucket = _build_image_bucket(system_dir)
        except OSError as exc:
            logger.debug(
                "local_cover_finder: bucket build failed dir=%s err=%s",
                system_dir,
                exc,
            )
            errors += 1
            image_bucket = {}

        for row in dir_rows:
            roms_scanned += 1
            rom_path = row["rom_path"]
            game_id = int(row["game_id"])
            rom_id = int(row["rom_id"])
            fuzzy_key = row["fuzzy_key"] or ""
            clean_name = row["clean_name"] or ""

            if progress_callback is not None:
                progress_callback(roms_scanned, total, Path(rom_path).name)

            try:
                matches = find_local_covers_for_rom(
                    rom_id=rom_id,
                    game_id=game_id,
                    rom_path=rom_path,
                    fuzzy_key=fuzzy_key,
                    clean_name=clean_name,
                    system_dir=system_dir,
                    image_bucket=image_bucket,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "local_cover_finder: match failed rom_id=%d err=%s",
                    rom_id,
                    exc,
                )
                errors += 1
                continue

            for match in matches:
                if _has_cover_for_path(conn, match.game_id, match.image_path):
                    covers_skipped += 1
                    continue
                try:
                    insert_cover(
                        conn,
                        match.game_id,
                        match.cover_type,
                        source_url=None,
                        local_path=match.image_path,
                    )
                    covers_found += 1
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "local_cover_finder: insert_cover failed game_id=%d err=%s",
                        match.game_id,
                        exc,
                    )
                    errors += 1

    conn.commit()

    return DiscoveryResult(
        roms_scanned=roms_scanned,
        covers_found=covers_found,
        covers_skipped_existing=covers_skipped,
        errors=errors,
    )
