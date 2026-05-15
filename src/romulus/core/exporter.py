"""Export engine — copy ROMs to a device in a profile-defined folder layout.

The exporter is the third filesystem-mutating subsystem (after the scanner
and the organizer). Like the organizer, it never overwrites destination
files: every write goes through :mod:`romulus.core.atomic` so a half-written
ROM or gamelist.xml can never end up at the final path. A cancelled export
can only leave ``.part`` tempfiles in the destination directory; those are
harmless and can be cleaned up by re-running the export.

The flow:

1. ``load_profile`` parses one YAML file into a :class:`DestinationProfile`.
2. ``load_all_profiles`` walks the built-in directory and (optionally) a user
   directory, returning every profile keyed by id.
3. ``preview_export`` walks the database with the requested filters and
   returns a :class:`ExportPreview` with the file count, total bytes, and the
   destination folder tree — no filesystem writes.
4. ``export_collection`` does the actual copy. Per-ROM it looks up the system
   in the active profile, computes the destination path under
   ``{target}/{base_path}/{system_folder}/{filename}``, and uses
   ``atomic_copy`` to publish the file. Optional steps generate gamelist.xml,
   .m3u playlists, and copy artwork into the profile's ``artwork_subdir``.

Session 10 carry-forward:

* ``games.canonical_name`` is NULL for nearly every game until real No-Intro
  DATs are committed — gamelist.xml generation falls back to ``games.title``.
* Cooperative cancel is handled by the caller's ``progress_callback``: if it
  raises, the exporter propagates the exception out so the wrapping worker
  can surface it as a normal cancel.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from romulus.core import atomic
from romulus.models.profile import DestinationProfile, SystemMapping

logger = logging.getLogger(__name__)

#: Default name used to find the built-in profile directory when the caller
#: doesn't pass one. Resolved relative to the package data folder.
BUILTIN_PROFILES_DIR: Path = (
    Path(__file__).resolve().parents[3] / "data" / "profiles"
)

#: A progress callback for the export loop. Signature is ``(current_index,
#: total, filename)`` mirroring the scanner/organizer workers.
ProgressCallback = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# Filters & options dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportFilters:
    """Constraints applied to the candidate ROM set.

    Every field defaults to "no filter". ``systems`` and ``regions`` are
    matched case-insensitively; ``collection_id`` (when set) intersects the
    candidate set with ``collection_games``.
    """

    systems: list[str] | None = None
    regions: list[str] | None = None
    collection_id: int | None = None


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Toggles for the optional sidecar artifacts."""

    include_artwork: bool = False
    generate_gamelist: bool = True
    generate_m3u: bool = True


@dataclass(slots=True)
class ExportPreview:
    """Result of ``preview_export`` — bookkeeping with no filesystem writes."""

    file_count: int = 0
    total_size_bytes: int = 0
    by_system: dict[str, int] = field(default_factory=dict)
    folder_tree: dict[str, list[str]] = field(default_factory=dict)
    unsupported_systems: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExportSummary:
    """Result of ``export_collection`` — what actually happened on disk."""

    files_copied: int = 0
    files_skipped: int = 0
    bytes_copied: int = 0
    systems: list[str] = field(default_factory=list)
    gamelists_written: int = 0
    m3u_written: int = 0
    artwork_copied: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


def load_profile(yaml_path: Path | str) -> DestinationProfile:
    """Parse a single YAML profile and return its :class:`DestinationProfile`.

    Raises ``yaml.YAMLError`` if the file is not valid YAML and
    ``pydantic.ValidationError`` if the schema is wrong. The caller is
    expected to handle both.
    """
    path = Path(yaml_path)
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"profile YAML is not a mapping: {path}")
    return DestinationProfile.model_validate(data)


def load_all_profiles(
    builtin_dir: Path | str | None = None,
    user_dir: Path | str | None = None,
) -> dict[str, DestinationProfile]:
    """Load every ``*.yaml`` profile from the two directories.

    Built-in profiles are loaded first; user profiles loaded second override
    a built-in with the same ``id`` so users can customise without editing
    package files. Profiles that fail to parse are logged and skipped — one
    broken YAML never blocks the rest from loading.
    """
    profiles: dict[str, DestinationProfile] = {}
    for directory in (builtin_dir or BUILTIN_PROFILES_DIR, user_dir):
        if directory is None:
            continue
        d = Path(directory)
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.yaml")):
            try:
                profile = load_profile(path)
            except Exception as exc:  # noqa: BLE001 - log + skip
                logger.warning("failed to load profile %s: %s", path, exc)
                continue
            profiles[profile.id] = profile
    return profiles


# ---------------------------------------------------------------------------
# Candidate ROM selection
# ---------------------------------------------------------------------------


def _build_rom_query(filters: ExportFilters) -> tuple[str, list[Any]]:
    """Compose the parameterized SELECT used by preview + export.

    Returns ``(sql, params)``. Filters are applied as ``AND`` clauses and use
    safe placeholders — never string-interpolation — so user-supplied region
    strings can't injection.
    """
    clauses: list[str] = ["r.system_id IS NOT NULL"]
    params: list[Any] = []
    if filters.systems:
        placeholders = ",".join("?" for _ in filters.systems)
        clauses.append(f"r.system_id IN ({placeholders})")
        params.extend(filters.systems)
    if filters.regions:
        placeholders = ",".join("?" for _ in filters.regions)
        clauses.append(
            f"(g.region IS NULL OR g.region IN ({placeholders}))"
            if "Other" in filters.regions
            else f"g.region IN ({placeholders})"
        )
        params.extend(filters.regions)
    if filters.collection_id is not None:
        clauses.append(
            "r.game_id IN (SELECT game_id FROM collection_games "
            "WHERE collection_id = ?)"
        )
        params.append(filters.collection_id)
    sql = (
        "SELECT r.id, r.path, r.filename, r.extension, r.size_bytes, "
        "       r.system_id, r.game_id, r.dat_match, "
        "       g.title AS title, g.canonical_name AS canonical_name, "
        "       g.region AS region "
        "FROM roms r LEFT JOIN games g ON g.id = r.game_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY r.system_id, r.filename"
    )
    return sql, params


def _candidate_roms(
    conn: sqlite3.Connection, filters: ExportFilters
) -> list[sqlite3.Row]:
    """Run the candidate query and return the matching ROM rows."""
    sql, params = _build_rom_query(filters)
    return list(conn.execute(sql, params).fetchall())


def _is_multi_disc_filename(filename: str) -> bool:
    """Heuristic: filenames containing ``(Disc N)`` are multi-disc."""
    lower = filename.lower()
    return "(disc " in lower or "(cd " in lower or "(disk " in lower


_MULTI_DISC_RE: re.Pattern[str] = re.compile(
    r"\s*\((?:Disc|CD|Disk)\s+[^)]+\)", flags=re.IGNORECASE
)


def _multi_disc_basename(filename: str) -> str:
    """Strip a ``(Disc N)`` segment from a filename for grouping into m3u."""
    return _MULTI_DISC_RE.sub("", filename)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def preview_export(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target_path: Path | str,
    filters: ExportFilters | None = None,
) -> ExportPreview:
    """Compute the file count, total size, and folder tree without copying.

    Iterates the same candidate set ``export_collection`` would walk and
    bookkeeping every file. Systems the profile marks as unsupported are
    counted in ``unsupported_systems`` so the UI can surface them as a
    warning ("3 GBA ROMs will be skipped — the target does not support GBA").
    """
    filters = filters or ExportFilters()
    preview = ExportPreview()
    target = Path(target_path)
    unsupported: set[str] = set()
    by_system: Counter[str] = Counter()
    folder_tree: defaultdict[str, list[str]] = defaultdict(list)
    for row in _candidate_roms(conn, filters):
        system_id = str(row["system_id"])
        mapping = profile.systems.get(system_id)
        if mapping is None or not mapping.is_supported:
            unsupported.add(system_id)
            continue
        preview.file_count += 1
        preview.total_size_bytes += int(row["size_bytes"] or 0)
        by_system[system_id] += 1
        folder_key = str(
            target / profile.base_path / mapping.folder
        ).replace("\\", "/")
        folder_tree[folder_key].append(str(row["filename"]))
    preview.by_system = dict(by_system)
    preview.folder_tree = dict(folder_tree)
    preview.unsupported_systems = sorted(unsupported)
    return preview


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _system_dest_dir(
    target: Path, profile: DestinationProfile, mapping: SystemMapping
) -> Path:
    """Compute the destination directory for a system under ``target``."""
    return target / profile.base_path / mapping.folder


def export_collection(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target_path: Path | str,
    filters: ExportFilters | None = None,
    options: ExportOptions | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ExportSummary:
    """Copy every ROM matching ``filters`` to ``target_path`` using ``profile``.

    For each ROM:

    * Skip if the profile does not support the ROM's system.
    * Otherwise compute the destination path under
      ``{target_path}/{profile.base_path}/{mapping.folder}/{filename}``.
    * Skip (without an error) if the destination already exists with the same
      size — assume it was already exported in a previous run.
    * Otherwise call :func:`romulus.core.atomic.atomic_copy` to publish it.

    After all ROMs are copied, optional sidecars are written: gamelist.xml
    per system (EmulationStation targets), .m3u for multi-disc games, and
    artwork copied into the profile's ``artwork_subdir``.

    ``progress_callback`` (if supplied) is invoked once per ROM with
    ``(index_1based, total, filename)``. Raising from inside the callback
    aborts the export — used by :class:`ExportWorker` for cooperative cancel.
    """
    filters = filters or ExportFilters()
    options = options or ExportOptions()
    target = Path(target_path)
    target.mkdir(parents=True, exist_ok=True)

    candidates = _candidate_roms(conn, filters)
    summary = ExportSummary()
    systems_touched: set[str] = set()
    # Group rows by system_id so we can emit one gamelist.xml / artwork pass
    # per system after the file copy is complete.
    by_system: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)

    total = len(candidates)
    for index, row in enumerate(candidates, start=1):
        if progress_callback is not None:
            progress_callback(index, total, str(row["filename"]))

        system_id = str(row["system_id"])
        mapping = profile.systems.get(system_id)
        if mapping is None or not mapping.is_supported:
            summary.files_skipped += 1
            continue
        source = Path(str(row["path"]))
        if not source.exists():
            summary.files_skipped += 1
            summary.errors.append(f"source missing: {source}")
            continue
        dest_dir = _system_dest_dir(target, profile, mapping)
        dest = dest_dir / str(row["filename"])
        size_bytes = int(row["size_bytes"] or 0)
        if dest.exists() and dest.stat().st_size == size_bytes:
            # Already exported — treat as a successful no-op so re-running the
            # exporter against a partially-populated SD card is idempotent.
            summary.files_skipped += 1
            systems_touched.add(system_id)
            by_system[system_id].append(row)
            continue
        try:
            atomic.atomic_copy(source, dest)
        except OSError as exc:
            summary.errors.append(f"copy failed {source} -> {dest}: {exc}")
            continue
        summary.files_copied += 1
        summary.bytes_copied += size_bytes
        systems_touched.add(system_id)
        by_system[system_id].append(row)

    summary.systems = sorted(systems_touched)

    # ---- Sidecars -------------------------------------------------------
    for system_id in summary.systems:
        mapping = profile.systems[system_id]
        dest_dir = _system_dest_dir(target, profile, mapping)
        if options.generate_gamelist and profile.gamelist_format == "emulationstation_xml":
            try:
                generate_gamelist_xml(conn, system_id, dest_dir, by_system[system_id])
                summary.gamelists_written += 1
            except OSError as exc:
                summary.errors.append(
                    f"gamelist.xml failed for {system_id}: {exc}"
                )
        if options.generate_m3u and profile.multi_disc == "m3u":
            try:
                count = generate_m3u_playlists(dest_dir, by_system[system_id])
                summary.m3u_written += count
            except OSError as exc:
                summary.errors.append(f".m3u failed for {system_id}: {exc}")
        if options.include_artwork and profile.artwork_subdir:
            try:
                count = copy_artwork(
                    conn, system_id, profile, target, by_system[system_id]
                )
                summary.artwork_copied += count
            except OSError as exc:
                summary.errors.append(f"artwork copy failed for {system_id}: {exc}")
    return summary


# ---------------------------------------------------------------------------
# gamelist.xml
# ---------------------------------------------------------------------------


def _gamelist_path(system_dir: Path) -> Path:
    return system_dir / "gamelist.xml"


def generate_gamelist_xml(
    conn: sqlite3.Connection,
    system_id: str,
    system_dir: Path,
    rows: Iterable[sqlite3.Row],
) -> Path:
    """Write an EmulationStation gamelist.xml into ``system_dir``.

    Per game we emit ``<path>``, ``<name>``, and any metadata available via
    the ``metadata`` table. ``games.canonical_name`` is preferred for the
    ``<name>`` element but we fall back to ``games.title`` whenever the
    canonical name is NULL — true for nearly every game until real No-Intro
    DATs are committed (session-10 carry-forward).
    """
    root = ET.Element("gameList")
    seen_game_ids: set[int] = set()
    for row in rows:
        path_element_text = f"./{row['filename']}"
        game_node = ET.SubElement(root, "game")
        ET.SubElement(game_node, "path").text = path_element_text
        # Prefer canonical name; otherwise the parsed title; otherwise the
        # filename without its extension as a last resort. ``sqlite3.Row``'s
        # ``in`` operator iterates values rather than keys, so we materialize
        # the column list explicitly.
        row_columns = set(row.keys())
        canonical = row["canonical_name"] if "canonical_name" in row_columns else None
        title = row["title"] if "title" in row_columns else None
        display = canonical or title or Path(str(row["filename"])).stem
        ET.SubElement(game_node, "name").text = str(display)
        # Pull metadata if we have it (joined per row to avoid an N+1 join in
        # the candidate query — this stays simple and is dwarfed by the file
        # copy in any case).
        game_id = row["game_id"]
        if game_id is None or game_id in seen_game_ids:
            continue
        seen_game_ids.add(int(game_id))
        meta = conn.execute(
            "SELECT description, genre, developer, publisher, release_date, "
            "players, rating FROM metadata WHERE game_id = ?",
            (int(game_id),),
        ).fetchone()
        if meta is None:
            continue
        for tag, value in (
            ("desc", meta["description"]),
            ("releasedate", meta["release_date"]),
            ("developer", meta["developer"]),
            ("publisher", meta["publisher"]),
            ("genre", meta["genre"]),
            ("players", meta["players"]),
            ("rating", meta["rating"]),
        ):
            if value:
                ET.SubElement(game_node, tag).text = str(value)

    # Render with an XML declaration so EmulationStation accepts it cleanly.
    payload = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="utf-8"
    )
    dest = _gamelist_path(system_dir)
    atomic.atomic_write_bytes(payload, dest)
    return dest


# ---------------------------------------------------------------------------
# .m3u playlists
# ---------------------------------------------------------------------------


def generate_m3u_playlists(
    system_dir: Path, rows: Iterable[sqlite3.Row]
) -> int:
    """Group multi-disc ROMs by their stripped basename and write one .m3u.

    Returns the number of playlists written. Skips groups with fewer than
    two discs — single-disc games don't need a playlist.
    """
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        filename = str(row["filename"])
        if not _is_multi_disc_filename(filename):
            continue
        stem = _multi_disc_basename(Path(filename).stem)
        groups[stem].append(filename)

    written = 0
    for stem, members in groups.items():
        if len(members) < 2:
            continue
        playlist = "\n".join(sorted(members)) + "\n"
        dest = system_dir / f"{stem}.m3u"
        atomic.atomic_write_text(playlist, dest)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------


def copy_artwork(
    conn: sqlite3.Connection,
    system_id: str,
    profile: DestinationProfile,
    target: Path,
    rows: Iterable[sqlite3.Row],
) -> int:
    """Copy cover-art files for every game in ``rows`` into the target.

    The destination layout follows EmulationStation conventions:
    ``{target}/{base_path}/{system_folder}/{artwork_subdir}/{filename-stem}-image.png``.
    Returns the number of cover files successfully copied. Missing covers
    are silently skipped — the metadata enrichment pipeline is best-effort.
    """
    if not profile.artwork_subdir:
        return 0
    mapping = profile.systems.get(system_id)
    if mapping is None or not mapping.is_supported:
        return 0
    artwork_dir = (
        target / profile.base_path / mapping.folder / profile.artwork_subdir
    )

    copied = 0
    seen_game_ids: set[int] = set()
    for row in rows:
        game_id = row["game_id"]
        if game_id is None or int(game_id) in seen_game_ids:
            continue
        seen_game_ids.add(int(game_id))
        cover_row = conn.execute(
            "SELECT local_path FROM covers WHERE game_id = ? "
            "AND local_path IS NOT NULL ORDER BY id LIMIT 1",
            (int(game_id),),
        ).fetchone()
        if cover_row is None:
            continue
        local_path = Path(str(cover_row["local_path"]))
        if not local_path.exists():
            continue
        # EmulationStation expects {rom-stem}-image.ext alongside its sibling
        # gamelist.xml entry. Use the rom filename's stem so the link is
        # stable even if the canonical name changes later.
        stem = Path(str(row["filename"])).stem
        dest = artwork_dir / f"{stem}-image{local_path.suffix}"
        try:
            atomic.atomic_copy(local_path, dest)
        except OSError as exc:
            logger.warning("artwork copy failed: src=%s err=%s", local_path, exc)
            continue
        copied += 1
    return copied
