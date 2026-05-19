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

import importlib.resources
import logging
import re
import sqlite3
import sys
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


def _resolve_install_dir() -> Path:
    """Frozen-exe parent dir, or the repo root in a dev clone.

    Duplicates :func:`romulus.app._resolve_install_dir` to keep the exporter
    importable without dragging in Qt. The two implementations MUST stay in
    sync — there's a test (test_install_dir_consistency) pinning that.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    cursor = Path(__file__).resolve()
    for parent in cursor.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.home() / ".romulus"


def _resolve_install_profiles_dir() -> Path:
    """Where install-time bundled profiles live (top-level ``profiles/``)."""
    return _resolve_install_dir() / "profiles"


def _resolve_package_profiles_dir() -> Path:
    """Legacy in-wheel location, kept as the deepest fallback."""
    return Path(str(importlib.resources.files("romulus.data.profiles")))


def _resolve_builtin_profiles_dir() -> Path:
    """Locate the built-in profile YAMLs.

    Prefers ``<install_dir>/profiles/`` (the portable layout introduced for
    v0.2.0 ZIP distributions) and falls back to the legacy in-wheel location
    when the install-dir copy is missing. Either way callers get a usable
    directory at module import time.
    """
    install_profiles = _resolve_install_profiles_dir()
    if install_profiles.is_dir() and any(install_profiles.glob("*.yaml")):
        return install_profiles
    return _resolve_package_profiles_dir()


#: Default location of the built-in destination profile YAMLs. Resolved at
#: import; callers that want the live three-tier search should use
#: :func:`load_all_profiles` instead.
BUILTIN_PROFILES_DIR: Path = _resolve_builtin_profiles_dir()

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
    """Toggles for the export run.

    ``include_roms`` is the master switch. When False, the per-row copy
    loop is short-circuited — no ROM bytes move — but the per-system
    classification still runs so the sidecar phase (gamelist / artwork)
    can target the right systems. Use case: after enrichment, push the
    fresh covers + rebuild gamelist.xml without re-copying gigabytes
    of already-synced ROMs.
    """

    include_roms: bool = True
    include_artwork: bool = True
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
class PerSystemExportCounts:
    """Per-system breakdown of an export run.

    Populated alongside the aggregate counters in :class:`ExportSummary`
    so the post-export summary dialog can show why each system landed
    where it did (copied vs already-present vs unsupported vs refused
    vs failed). Stays a plain dataclass instead of a TypedDict so the
    PySide6 ``Signal(object)`` round-trip preserves the type.

    ``artwork_copied`` records how many cover files this system's
    sidecar pass actually published (i.e. covers that weren't already
    on the destination at the same size + mtime). Critical for the
    artwork-only mode where every other counter is 0 — without it the
    summary dialog would show empty rows.
    """

    copied: int = 0
    bytes_copied: int = 0
    skipped_unsupported: int = 0
    skipped_already_present: int = 0
    skipped_refused: int = 0
    errors: int = 0
    artwork_copied: int = 0


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
    #: Per-system breakdown keyed by ``system_id``. Every system that
    #: contributed at least one row to the candidate set gets an entry,
    #: including systems that ended up entirely skipped.
    per_system: dict[str, PerSystemExportCounts] = field(default_factory=dict)


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
    profile = DestinationProfile.model_validate(data)
    logger.debug(
        "load_profile: path=%s id=%s systems=%d",
        path,
        profile.id,
        len(profile.systems),
    )
    return profile


def load_all_profiles(
    builtin_dir: Path | str | None = None,
    user_dir: Path | str | None = None,
    install_dir: Path | str | None = None,
) -> dict[str, DestinationProfile]:
    """Load ``*.yaml`` profiles from up to three directories.

    Three-tier precedence — later entries override earlier ones, so the user
    gets the final say:

    1. ``builtin_dir`` (default: in-wheel ``romulus.data.profiles`` or
       install-dir copy resolved at import) — the factory defaults.
    2. ``install_dir`` (default: ``<install_dir>/profiles/``) — the editable
       copy seeded next to the exe on first launch.
    3. ``user_dir`` (default: caller's ``~/.romulus/profiles/``) — per-user
       overrides; absolute final say.

    Pass ``None`` for any tier to skip it (useful in tests). Profiles that
    fail to parse are logged and skipped — one broken YAML never blocks the
    rest from loading.
    """
    profiles: dict[str, DestinationProfile] = {}
    install_default = _resolve_install_profiles_dir()
    install_arg: Path | str | None
    if install_dir is None:
        install_arg = (
            install_default
            if install_default.is_dir()
            and install_default.resolve() != BUILTIN_PROFILES_DIR.resolve()
            else None
        )
    else:
        install_arg = install_dir
    sources: tuple[Path | str | None, ...] = (
        builtin_dir or BUILTIN_PROFILES_DIR,
        install_arg,
        user_dir,
    )
    for directory in sources:
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
    # Cache the per-system destination so the security guard in
    # ``_system_dest_dir`` runs once per (profile, system) rather than once per
    # ROM — same validation, less ``resolve()`` overhead on large libraries.
    dest_cache: dict[str, Path] = {}
    for row in _candidate_roms(conn, filters):
        system_id = str(row["system_id"])
        mapping = profile.systems.get(system_id)
        if mapping is None or not mapping.is_supported:
            unsupported.add(system_id)
            continue
        preview.file_count += 1
        preview.total_size_bytes += int(row["size_bytes"] or 0)
        by_system[system_id] += 1
        if system_id not in dest_cache:
            dest_cache[system_id] = _system_dest_dir(target, profile, mapping)
        folder_key = str(dest_cache[system_id]).replace("\\", "/")
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
    """Compute the destination directory for a system under ``target``.

    Defense-in-depth (security audit v0.1.0 finding #1): even though
    :class:`DestinationProfile` field validators reject absolute paths,
    ``..`` traversal, drive-letter prefixes, and Windows reserved names at
    load time, we resolve the final path and assert it stays inside the
    requested target directory. A user-supplied profile that somehow
    bypasses the load-time check (or a bug in a future validator change)
    cannot cause writes outside ``target``.
    """
    dest_dir = target / profile.base_path / mapping.folder
    try:
        resolved = dest_dir.resolve()
        target_resolved = target.resolve()
    except OSError as exc:
        # ``resolve()`` can fail on Windows when the path contains characters
        # the filesystem cannot represent. Refuse the export rather than
        # writing to an unexpected location.
        raise ValueError(
            f"cannot resolve export destination {dest_dir!s}: {exc}"
        ) from exc
    if (
        resolved != target_resolved
        and target_resolved not in resolved.parents
    ):
        raise ValueError(
            f"profile would write outside target: "
            f"resolved={resolved!s} target={target_resolved!s}"
        )
    return dest_dir


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
    logger.debug(
        "export_collection: start profile=%s target=%s candidates=%d",
        profile.id,
        target,
        len(candidates),
    )
    summary = ExportSummary()
    systems_touched: set[str] = set()
    # Group rows by system_id so we can emit one gamelist.xml / artwork pass
    # per system after the file copy is complete.
    by_system: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)

    def _bucket(system_id: str) -> PerSystemExportCounts:
        return summary.per_system.setdefault(system_id, PerSystemExportCounts())

    total = len(candidates)
    # Phase 1 label is verb-prefixed ("Copying foo.sfc") so the dialog
    # can render it directly instead of hard-coding "Exporting" — phase
    # 2 (sidecars) emits its own verb. The previous bare-filename format
    # produced a "stuck at 100%" UX once the ROM loop finished because
    # nothing updated the label during the sidecar pass.
    for index, row in enumerate(candidates, start=1):
        if progress_callback is not None:
            progress_callback(
                index, total, f"Copying {row['filename']}"
            )

        system_id = str(row["system_id"])
        mapping = profile.systems.get(system_id)
        if mapping is None or not mapping.is_supported:
            logger.debug(
                "export: skip-unsupported filename=%s system_id=%s",
                row["filename"],
                system_id,
            )
            summary.files_skipped += 1
            _bucket(system_id).skipped_unsupported += 1
            continue
        if not options.include_roms:
            # Artwork-only mode (the "Include ROMs" checkbox is off).
            # Register the row for the sidecar phase so gamelist.xml and
            # cover-art copies still target the right systems, but skip
            # every per-row ROM operation — no source/dest stat, no
            # atomic_copy. Counters are intentionally not bumped: the
            # user knows ROMs were excluded by choice, a "skipped: N"
            # tally would just be noise.
            systems_touched.add(system_id)
            by_system[system_id].append(row)
            continue
        source = Path(str(row["path"]))
        if not source.exists():
            logger.debug(
                "export: skip-missing-source filename=%s src=%s",
                row["filename"],
                source,
            )
            summary.files_skipped += 1
            summary.errors.append(f"source missing: {source}")
            bucket = _bucket(system_id)
            bucket.skipped_refused += 1
            bucket.errors += 1
            continue
        dest_dir = _system_dest_dir(target, profile, mapping)
        dest = dest_dir / str(row["filename"])
        size_bytes = int(row["size_bytes"] or 0)
        if dest.exists():
            existing_size = dest.stat().st_size
            if existing_size == size_bytes:
                # Already exported — treat as a successful no-op so re-running
                # the exporter against a partially-populated SD card is
                # idempotent.
                logger.debug(
                    "export: skip-already-present filename=%s dest=%s size=%d",
                    row["filename"],
                    dest,
                    size_bytes,
                )
                summary.files_skipped += 1
                systems_touched.add(system_id)
                by_system[system_id].append(row)
                _bucket(system_id).skipped_already_present += 1
                continue
            # Security audit v0.1.0 finding #4: refuse to silently overwrite a
            # pre-existing file whose size differs from the source. The
            # destination almost certainly belongs to something else (the user
            # picked the wrong target folder, or a profile escaped its base).
            # Skip + report so the user can investigate rather than losing
            # data.
            logger.debug(
                "export: refuse-overwrite filename=%s dest=%s "
                "existing_size=%d source_size=%d",
                row["filename"],
                dest,
                existing_size,
                size_bytes,
            )
            summary.files_skipped += 1
            summary.errors.append(
                f"refusing to overwrite existing file at {dest} "
                f"(existing={existing_size}B, source={size_bytes}B)"
            )
            bucket = _bucket(system_id)
            bucket.skipped_refused += 1
            bucket.errors += 1
            continue
        logger.debug(
            "export: copy filename=%s src=%s dest=%s size=%d",
            row["filename"],
            source,
            dest,
            size_bytes,
        )
        try:
            atomic.atomic_copy(source, dest)
        except OSError as exc:
            logger.debug(
                "export: copy failed filename=%s src=%s dest=%s err=%s",
                row["filename"],
                source,
                dest,
                exc,
            )
            summary.errors.append(f"copy failed {source} -> {dest}: {exc}")
            _bucket(system_id).errors += 1
            continue
        summary.files_copied += 1
        summary.bytes_copied += size_bytes
        systems_touched.add(system_id)
        by_system[system_id].append(row)
        bucket = _bucket(system_id)
        bucket.copied += 1
        bucket.bytes_copied += size_bytes

    summary.systems = sorted(systems_touched)
    logger.debug(
        "export_collection: copy phase complete copied=%d skipped=%d errors=%d "
        "systems=%d",
        summary.files_copied,
        summary.files_skipped,
        len(summary.errors),
        len(summary.systems),
    )

    # ---- Sidecars -------------------------------------------------------
    # Phase 2 progress: re-scale the bar to per-system count so the
    # user sees motion during the artwork + gamelist passes. Without
    # this the dialog used to sit at 100% with a stale filename label
    # while ``copy_artwork`` slogged through thousands of cover files.
    total_systems = len(summary.systems)
    for sys_idx, system_id in enumerate(summary.systems, start=1):
        if progress_callback is not None:
            progress_callback(
                sys_idx,
                total_systems,
                f"Refreshing sidecars: {system_id}",
            )
        mapping = profile.systems[system_id]
        dest_dir = _system_dest_dir(target, profile, mapping)
        if options.generate_gamelist and profile.gamelist_format == "emulationstation_xml":
            try:
                generate_gamelist_xml(
                    conn, system_id, dest_dir, by_system[system_id], profile=profile
                )
                summary.gamelists_written += 1
            except OSError as exc:
                summary.errors.append(
                    f"gamelist.xml failed for {system_id}: {exc}"
                )
                _bucket(system_id).errors += 1
        if options.generate_m3u and profile.multi_disc == "m3u":
            try:
                count = generate_m3u_playlists(dest_dir, by_system[system_id])
                summary.m3u_written += count
            except OSError as exc:
                summary.errors.append(f".m3u failed for {system_id}: {exc}")
                _bucket(system_id).errors += 1
        if options.include_artwork and profile.artwork_subdir:
            try:
                count = copy_artwork(
                    conn, system_id, profile, target, by_system[system_id]
                )
                summary.artwork_copied += count
                _bucket(system_id).artwork_copied += count
            except OSError as exc:
                summary.errors.append(f"artwork copy failed for {system_id}: {exc}")
                _bucket(system_id).errors += 1
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
    profile: DestinationProfile | None = None,
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
        filename = str(row["filename"])
        game_node = ET.SubElement(root, "game")
        ET.SubElement(game_node, "path").text = f"./{filename}"
        # Prefer canonical name; otherwise the parsed title; otherwise the
        # filename without its extension as a last resort. ``sqlite3.Row``'s
        # ``in`` operator iterates values rather than keys, so we materialize
        # the column list explicitly.
        row_columns = set(row.keys())
        canonical = row["canonical_name"] if "canonical_name" in row_columns else None
        title = row["title"] if "title" in row_columns else None
        display = canonical or title or Path(filename).stem
        ET.SubElement(game_node, "name").text = str(display)

        # Reference the artwork that ``copy_artwork`` will write. EmulationStation
        # uses this relative path to find the image; without it the launcher
        # shows no cover even when the file is on disk.
        game_id = row["game_id"]
        image_ext: str | None = None
        if profile is not None and profile.artwork_subdir and game_id is not None:
            cover_row = conn.execute(
                "SELECT local_path FROM covers WHERE game_id = ? "
                "AND local_path IS NOT NULL "
                "ORDER BY is_preferred DESC, id ASC LIMIT 1",
                (int(game_id),),
            ).fetchone()
            if cover_row is not None:
                local_path = Path(str(cover_row["local_path"]))
                if local_path.suffix:
                    image_ext = local_path.suffix
        if image_ext:
            assert profile is not None  # narrowed by image_ext check above
            image_ref = _artwork_relative_ref(profile, filename, image_ext)
            if image_ref:
                ET.SubElement(game_node, "image").text = image_ref

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

    # Pretty-print the XML so it's human-readable when the user opens it
    # in a text editor. ``ET.indent`` rewrites the tree in place with 2-space
    # nesting; ``\n`` line endings are fine on Android/Linux (where gamelist
    # files actually live) and modern Windows editors handle them too.
    ET.indent(root, space="  ")
    payload = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="utf-8"
    ) + b"\n"
    dest = _gamelist_path(system_dir)
    logger.debug(
        "gamelist.xml: writing system_id=%s dest=%s bytes=%d games=%d",
        system_id,
        dest,
        len(payload),
        len(seen_game_ids),
    )
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


#: Mtime tolerance for the artwork freshness compare. FAT32 / SMB /
#: archive extraction routinely re-stamp mtimes within a 2 s window
#: even when content didn't change; the same tolerance is already used
#: in core/scrub.py for ROM drift detection.
_ARTWORK_MTIME_TOLERANCE_SECONDS: float = 2.0


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

    Returns the number of cover files successfully copied. Missing
    covers are silently skipped — the metadata enrichment pipeline is
    best-effort.

    Size + mtime compare against any existing dest file: a cover that
    already matches the local copy is left in place. This makes the
    "Include ROMs off → push fresh artwork" workflow O(changed covers)
    rather than O(all games), so a re-export after a partial enrichment
    pass doesn't re-copy every cover on the device.
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
        # Honor the user's "Make preferred" choice from the detail panel:
        # is_preferred=1 sorts first, then by id. Without this the exporter
        # picked the lowest-id cover regardless of which one the user chose
        # to display.
        cover_row = conn.execute(
            "SELECT local_path FROM covers WHERE game_id = ? "
            "AND local_path IS NOT NULL "
            "ORDER BY is_preferred DESC, id ASC LIMIT 1",
            (int(game_id),),
        ).fetchone()
        if cover_row is None:
            logger.debug(
                "artwork: no cover for game_id=%d filename=%s",
                int(game_id),
                row["filename"],
            )
            continue
        local_path = Path(str(cover_row["local_path"]))
        if not local_path.exists():
            logger.debug(
                "artwork: cover missing on disk game_id=%d local_path=%s",
                int(game_id),
                local_path,
            )
            continue
        # Filename template is per-profile so each launcher's convention is
        # honored: EmulationStation classic wants ``{stem}-image{ext}``,
        # modern launchers (Daijisho/Onion/muOS/ES-DE) want ``{stem}{ext}``.
        stem = Path(str(row["filename"])).stem
        filename = profile.artwork_filename_template.format(
            stem=stem, ext=local_path.suffix
        )
        dest = artwork_dir / filename
        if _artwork_already_current(local_path, dest):
            logger.debug(
                "artwork: skip-current game_id=%d dest=%s",
                int(game_id),
                dest,
            )
            continue
        logger.debug(
            "artwork: copy game_id=%d src=%s dest=%s",
            int(game_id),
            local_path,
            dest,
        )
        try:
            atomic.atomic_copy(local_path, dest)
        except OSError as exc:
            logger.warning("artwork copy failed: src=%s err=%s", local_path, exc)
            continue
        copied += 1
    logger.debug(
        "artwork: complete system_id=%s copied=%d",
        system_id,
        copied,
    )
    return copied


def _artwork_already_current(local_path: Path, dest: Path) -> bool:
    """True if ``dest`` already matches ``local_path`` by size + mtime.

    Stat errors on either side are treated as "not current" so the
    copy attempt fires and any failure surfaces via the OSError handler
    in :func:`copy_artwork`. Mtime tolerance matches scrub's drift
    band so FAT32 / SMB second-precision rounding doesn't trigger
    spurious re-copies.
    """
    try:
        dest_stat = dest.stat()
        local_stat = local_path.stat()
    except OSError:
        return False
    if dest_stat.st_size != local_stat.st_size:
        return False
    return abs(dest_stat.st_mtime - local_stat.st_mtime) < (
        _ARTWORK_MTIME_TOLERANCE_SECONDS
    )


def _artwork_relative_ref(
    profile: DestinationProfile, rom_filename: str, image_ext: str
) -> str | None:
    """Return a gamelist.xml-style relative path (``./Imgs/foo.png``) to the
    artwork for ``rom_filename``, or None if the profile doesn't ship artwork.

    The path is relative to the system folder (where gamelist.xml lives), so
    EmulationStation finds the image whether the ROM pack is mounted at
    ``/storage/emulated/0/Roms/`` or ``/userdata/roms/``.
    """
    if not profile.artwork_subdir:
        return None
    stem = Path(rom_filename).stem
    filename = profile.artwork_filename_template.format(stem=stem, ext=image_ext)
    return f"./{profile.artwork_subdir}/{filename}"
