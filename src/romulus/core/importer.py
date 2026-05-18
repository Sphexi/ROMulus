"""Inbound ROM import — the staging-folder counterpart to the Sync engine.

The user points at a staging folder (Downloads, a USB stick, a mounted
archive). We walk the folder using the same identifier pipeline Quick Scan
uses (``_resolve_system_for_directory`` + extension fallback + optional L3
hash + DAT match), build an :class:`ImportPlan` of per-file
:class:`ImportAction` rows, and let the user resolve conflicts in
:class:`romulus.ui.import_dialog.ImportDialog` before any bytes move.

Apply is structured exactly like :func:`romulus.core.sync.apply_plan`:
per-action SAVEPOINT, atomic copy via :mod:`romulus.core.atomic`,
cooperative cancel between actions (mid-file ``atomic_copy`` has no safe
abort point), and a :class:`ImportSummary` returned at the end.

The import target is always the current ``library_root`` from config —
there is no "import into a different library" mode (CLAUDE.md design rule
"Single library at a time").
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from romulus.core import atomic
from romulus.core.scanner import (
    ARCHIVE_EXTENSIONS,
    SIDE_FILE_EXTENSIONS,
    _resolve_system_for_directory,
    generate_fuzzy_key,
    group_into_games,
    is_rom_file,
    is_side_file,
    parse_filename,
)
from romulus.db import queries as q
from romulus.models.system import get_extensions_by_system, get_systems_by_alias

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status + resolution vocab
# ---------------------------------------------------------------------------

ImportStatus = Literal[
    "new",
    "dupe_path",
    "dupe_filename",
    "dupe_hash",
    "multi_rom_archive",
]

ImportResolution = Literal[
    "copy",
    "move",
    "skip",
    "replace",
    "keep_both",
]

#: Sentinel folder for ROMs that could not be resolved to a system. Mirrors
#: the destination-sync engine's ``_unsorted/`` fallback so the two flows
#: pile orphan ROMs into the same well-known bucket.
UNSORTED_FOLDER: str = "_unsorted"


ProgressCallback = Callable[[int, int, str], None]


class _ImportCancelled(Exception):  # noqa: N818 - cancel marker, not an error
    """Cooperative-cancel marker raised from an import progress callback.

    Mirrors :class:`romulus.core.sync._SyncCancelled` — kept private to the
    module and surfaced to the UI as a generic "Import cancelled" message via
    :class:`romulus.ui.workers._DbWorker`'s shared ``failed`` signal.
    """


# ---------------------------------------------------------------------------
# Plan + action structures
# ---------------------------------------------------------------------------


@dataclass
class ImportAction:
    """One proposed step in an import plan.

    Mirrors :class:`romulus.core.sync.SyncAction` in shape so the preview
    dialog can drive both engines with the same row model.
    """

    source_path: Path
    target_path: Path
    system_id: str | None
    status: ImportStatus = "new"
    resolution: ImportResolution = "copy"
    confidence: str = "unmatched"
    size_bytes: int = 0
    reason: str = ""
    # Path of the existing ROM that triggered a dupe classification (kept so
    # the apply step can update / remove the original row without re-querying).
    existing_rom_path: str | None = None
    existing_rom_id: int | None = None
    # Filled in by :func:`apply_plan` once execution has been attempted.
    executed: bool = False
    error: str | None = None


@dataclass(slots=True)
class ImportPlan:
    """Read-only snapshot of a proposed import."""

    staging_root: Path
    library_root: Path
    actions: list[ImportAction] = field(default_factory=list)
    created_systems: set[str] = field(default_factory=set)
    heavy_identify: bool = False
    total_bytes: int = 0

    def to_json(self) -> str:
        """Serialize the plan to a versioned, self-describing JSON document."""
        payload = {
            "version": 1,
            "kind": "romulus.import_plan",
            "generated_at": datetime.now(UTC).isoformat(),
            "staging_root": str(self.staging_root),
            "library_root": str(self.library_root),
            "heavy_identify": self.heavy_identify,
            "total_bytes": self.total_bytes,
            "created_systems": sorted(self.created_systems),
            "actions": [_action_to_jsonable(a) for a in self.actions],
        }
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ImportPlan:
        """Re-hydrate a plan from a ``to_json`` payload."""
        payload = json.loads(text)
        if payload.get("kind") != "romulus.import_plan":
            raise ValueError("not an import plan JSON document")
        if int(payload.get("version", 0)) != 1:
            raise ValueError(
                f"unsupported import plan version: {payload.get('version')}"
            )
        actions = [_action_from_jsonable(a) for a in payload.get("actions", [])]
        return cls(
            staging_root=Path(payload["staging_root"]),
            library_root=Path(payload["library_root"]),
            actions=actions,
            created_systems=set(payload.get("created_systems", [])),
            heavy_identify=bool(payload.get("heavy_identify", False)),
            total_bytes=int(payload.get("total_bytes", 0)),
        )


def _action_to_jsonable(action: ImportAction) -> dict[str, object]:
    """Convert an :class:`ImportAction` to a JSON-safe dict.

    ``Path`` objects don't round-trip through ``json.dumps``; coerce to str
    and let :func:`_action_from_jsonable` rebuild them on the way back in.
    """
    data = asdict(action)
    data["source_path"] = str(action.source_path)
    data["target_path"] = str(action.target_path)
    return data


def _action_from_jsonable(data: dict[str, object]) -> ImportAction:
    """Rebuild an :class:`ImportAction` from a :func:`_action_to_jsonable` dict."""
    return ImportAction(
        source_path=Path(str(data["source_path"])),
        target_path=Path(str(data["target_path"])),
        system_id=data.get("system_id"),  # type: ignore[arg-type]
        status=data.get("status", "new"),  # type: ignore[arg-type]
        resolution=data.get("resolution", "copy"),  # type: ignore[arg-type]
        confidence=str(data.get("confidence", "unmatched")),
        size_bytes=int(data.get("size_bytes", 0) or 0),
        reason=str(data.get("reason", "")),
        existing_rom_path=data.get("existing_rom_path"),  # type: ignore[arg-type]
        existing_rom_id=data.get("existing_rom_id"),  # type: ignore[arg-type]
        executed=bool(data.get("executed", False)),
        error=data.get("error"),  # type: ignore[arg-type]
    )


@dataclass(slots=True)
class ImportSummary:
    """Result of :func:`apply_plan` — what actually happened on disk."""

    files_imported: int = 0
    files_skipped: int = 0
    files_replaced: int = 0
    files_kept_both: int = 0
    bytes_imported: int = 0
    systems_touched: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ImportOptions:
    """User-facing options for an import run.

    Heavy identification (SHA-1 hashing + DAT cross-reference) is mandatory
    on every analyse pass — without it the dupe_hash check can't fire and
    the user would get false-negative "this is new" badges for files that
    are actually already in the library under a different name. The dialog
    therefore doesn't expose a toggle; it just warns about the duration up
    front when the staging folder is large.
    """

    #: Default per-action resolution for newly-discovered files.
    #: ``copy`` preserves the staging folder for re-imports; ``move`` is the
    #: opt-in alternative the dialog exposes via a radio.
    default_resolution: ImportResolution = "copy"
    #: Internal flag kept for the plan JSON round-trip — always True. Tests
    #: that simulate the legacy "skip hashing" path still pass False so the
    #: hash-dupe branch is exercised directly without churning the dialog.
    heavy_identify: bool = True


# ---------------------------------------------------------------------------
# System resolution
# ---------------------------------------------------------------------------


def _build_extension_to_system(
    extensions_by_system: dict[str, list[str]],
) -> dict[str, str]:
    """Build a reverse map ``ext -> system_id`` for unambiguous extensions only.

    Many extensions belong to a single system (``.sfc`` → snes, ``.nes`` → nes);
    a small number are shared (``.bin`` on every CD system, ``.zip`` everywhere).
    Shared ones are dropped from the map so we don't guess wrong on import —
    those files fall through to the ``_unsorted`` bucket where the user can
    intervene manually.
    """
    out: dict[str, set[str]] = {}
    for system_id, exts in extensions_by_system.items():
        for ext in exts:
            out.setdefault(ext.lower(), set()).add(system_id)
    unambiguous: dict[str, str] = {}
    for ext, owners in out.items():
        if len(owners) == 1:
            unambiguous[ext] = next(iter(owners))
    return unambiguous


def _canonical_folder_for_system(
    conn: sqlite3.Connection, system_id: str
) -> str | None:
    """Return the first ``folder_aliases`` entry for ``system_id``.

    Matches how the Organize / Export engines pick a canonical folder name.
    Returns None if the system isn't in the registry — caller falls back to
    the ``_unsorted`` bucket.
    """
    row = conn.execute(
        "SELECT folder_aliases FROM systems WHERE id = ?", (system_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        aliases = json.loads(row["folder_aliases"])
    except (TypeError, ValueError):
        return None
    if not aliases:
        return None
    return str(aliases[0])


def _resolve_system_for_file(
    file_path: Path,
    staging_root: Path,
    alias_map: dict[str, str],
    ext_to_system: dict[str, str],
    extensions_by_system: dict[str, list[str]],
) -> tuple[str | None, str]:
    """Return ``(system_id, confidence_label)`` for a staging file.

    Tries the directory-based alias resolution first (cheap, accurate when
    the staging folder is laid out by platform). Falls back to extension
    lookup for unambiguous extensions. Returns ``(None, "unmatched")`` for
    files that can't be placed — the dialog routes those to ``_unsorted/``.
    """
    folder_system = _resolve_system_for_directory(
        file_path.parent, staging_root, alias_map
    )
    if folder_system is not None:
        # Sanity-check the extension against the resolved system; if the
        # extension doesn't belong to that system at all (e.g. a stray .txt
        # someone moved into the snes folder) drop back to ``None``.
        accepted = extensions_by_system.get(folder_system, [])
        if is_rom_file(file_path.name, accepted):
            return folder_system, "fuzzy"
        # Folder matched but extension is wrong — let extension fallback try.

    ext = file_path.suffix.lower()
    ext_system = ext_to_system.get(ext)
    if ext_system is not None:
        return ext_system, "fuzzy"
    return None, "unmatched"


# ---------------------------------------------------------------------------
# Multi-ROM archive detection
# ---------------------------------------------------------------------------


def _zip_contains_multiple_roms(
    archive_path: Path, rom_extensions: set[str]
) -> bool:
    """Return True if ``archive_path`` contains more than one ROM-like file.

    Walks the zip central directory only (no extraction). Errors and corrupt
    archives return False — they're handled by the regular import flow which
    surfaces the read failure via the per-action error field.
    """
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            rom_count = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name_ext = Path(info.filename).suffix.lower()
                if name_ext in rom_extensions:
                    rom_count += 1
                    if rom_count > 1:
                        return True
            return False
    except (zipfile.BadZipFile, OSError):
        return False


# ---------------------------------------------------------------------------
# Path manipulation helpers
# ---------------------------------------------------------------------------


def _is_under(child: Path, parent: Path) -> bool:
    """Return True if ``child`` is the same as ``parent`` or below it.

    Canonicalizes both ends so ``library_root = /tmp/lib`` and
    ``staging = /tmp/lib/Downloads`` reliably classify as "child under parent"
    regardless of symlinks or case differences on Windows.
    """
    try:
        child_resolved = child.resolve()
    except OSError:
        child_resolved = child
    try:
        parent_resolved = parent.resolve()
    except OSError:
        parent_resolved = parent
    try:
        child_resolved.relative_to(parent_resolved)
    except ValueError:
        return False
    return True


def _disambiguate_path(target: Path) -> Path:
    """Return a ``keep_both`` target path that does not yet exist.

    Inserts ``_imported``, ``_imported_2``, … into the stem until the path
    is free on disk. Mirrors how the organizer disambiguates collisions —
    we keep the suffix vocabulary the same so a future "show me everything
    I imported" filter could regex on it.
    """
    candidate = target.with_name(f"{target.stem}_imported{target.suffix}")
    counter = 2
    while candidate.exists():
        candidate = target.with_name(
            f"{target.stem}_imported_{counter}{target.suffix}"
        )
        counter += 1
    return candidate


# ---------------------------------------------------------------------------
# Plan analysis
# ---------------------------------------------------------------------------


def _all_known_rom_extensions(
    extensions_by_system: dict[str, list[str]],
) -> set[str]:
    """Union of every system's accepted extensions — used for archive sniffing."""
    out: set[str] = set()
    for exts in extensions_by_system.values():
        out.update(e.lower() for e in exts)
    return out


def analyse_import(
    conn: sqlite3.Connection,
    staging_path: Path | str,
    library_path: Path | str,
    options: ImportOptions | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ImportPlan:
    """Walk the staging folder, identify each file, return an :class:`ImportPlan`.

    Refuses with ``ValueError`` when ``staging_path`` is the library root or
    a subdirectory of it — preventing the self-recursion footgun where a
    "re-import everything" reads the same files it's about to overwrite.
    """
    options = options or ImportOptions()
    staging_root = Path(staging_path)
    library_root = Path(library_path)
    if not staging_root.exists():
        raise ValueError(f"staging folder does not exist: {staging_root}")
    if not staging_root.is_dir():
        raise ValueError(f"staging path is not a directory: {staging_root}")
    if _is_under(staging_root, library_root):
        raise ValueError(
            "staging folder must be outside the library root "
            f"(library={library_root}, staging={staging_root})"
        )

    alias_map = get_systems_by_alias(conn)
    extensions_by_system = get_extensions_by_system(conn)
    ext_to_system = _build_extension_to_system(extensions_by_system)
    all_rom_exts = _all_known_rom_extensions(extensions_by_system)
    # Track which canonical folders already exist on disk so the preview
    # can flag "(new)" badges for the systems that need a fresh folder.
    canonical_folders: dict[str, str] = {}

    # Pre-walk to collect total file count for progress reporting.
    candidate_files: list[Path] = []
    for root, _dirs, files in _walk_staging(staging_root):
        for filename in files:
            candidate_files.append(Path(root) / filename)
    total = len(candidate_files)

    actions: list[ImportAction] = []
    created_systems: set[str] = set()
    total_bytes = 0

    for index, file_path in enumerate(candidate_files, start=1):
        if progress_callback is not None:
            progress_callback(index, total, file_path.name)

        if is_side_file(file_path.name):
            continue

        try:
            size_bytes = file_path.stat().st_size
        except OSError as exc:
            logger.debug("import analyse stat failed: path=%s err=%s", file_path, exc)
            continue

        system_id, confidence = _resolve_system_for_file(
            file_path,
            staging_root,
            alias_map,
            ext_to_system,
            extensions_by_system,
        )

        # Resolve canonical folder once per system_id and cache.
        if system_id is not None and system_id not in canonical_folders:
            folder = _canonical_folder_for_system(conn, system_id)
            canonical_folders[system_id] = folder or system_id

        target_dir = (
            library_root / canonical_folders[system_id]
            if system_id is not None
            else library_root / UNSORTED_FOLDER
        )
        target_path = target_dir / file_path.name

        # System-folder creation tracking. Only systems that resolve cleanly
        # get a "(new)" badge — _unsorted is always created on demand.
        if system_id is not None and not target_dir.exists():
            created_systems.add(system_id)

        action = ImportAction(
            source_path=file_path,
            target_path=target_path,
            system_id=system_id,
            confidence=confidence,
            size_bytes=size_bytes,
            resolution=options.default_resolution,
        )

        # Multi-ROM archive detection (zip only — 7z requires py7zr which
        # isn't a dependency). The badge defaults the action to ``skip``
        # because full multi-rom unpacking is out of scope for v1.
        ext = file_path.suffix.lower()
        if ext == ".zip" and _zip_contains_multiple_roms(file_path, all_rom_exts):
            action.status = "multi_rom_archive"
            action.resolution = "skip"
            action.reason = "archive contains multiple ROM files"
            actions.append(action)
            continue

        # Dupe detection — three levels, evaluated in order of cheapness.
        _classify_dupe(
            conn,
            action,
            options,
            extensions_by_system.get(system_id or "", []),
        )

        if action.resolution != "skip":
            total_bytes += size_bytes
        actions.append(action)

    plan = ImportPlan(
        staging_root=staging_root,
        library_root=library_root,
        actions=actions,
        created_systems=created_systems,
        heavy_identify=options.heavy_identify,
        total_bytes=total_bytes,
    )
    return plan


def _walk_staging(root: Path):
    """``os.walk`` wrapper that mirrors the scanner's followlinks=False default."""
    yield from os.walk(root, followlinks=False)


def _classify_dupe(
    conn: sqlite3.Connection,
    action: ImportAction,
    options: ImportOptions,
    accepted_extensions: list[str],
) -> None:
    """Set ``action.status`` / ``resolution`` if the file already exists.

    Three checks, in order:

    1. **Path dupe** — a row exists at the planned target path. Default skip.
    2. **Filename dupe** — a different file with the same basename already
       lives at the target path on disk (size or hash differs). Surfaces as
       a conflict the user resolves with replace / keep-both / skip.
    3. **Hash dupe** — same SHA-1 exists in the library under a different
       filename. Only checked when ``options.heavy_identify`` is enabled.

    ``accepted_extensions`` is the resolved system's extension list and is
    used to skip the hash step on files that aren't valid ROMs for the
    detected system (cheap guard against hashing random side files).
    """
    # Level 1: path-level dupe (DB-side). The path-keyed upsert will treat
    # this as un-tombstoning the existing row, but for the preview we still
    # want to flag it as a dupe so the user understands they're not adding
    # anything new.
    existing_by_path = q.find_rom_by_path(conn, str(action.target_path))
    if existing_by_path is not None:
        action.status = "dupe_path"
        action.resolution = "skip"
        action.existing_rom_id = int(existing_by_path["id"])
        action.existing_rom_path = str(existing_by_path["path"])
        action.reason = "already enrolled at this path"
        return

    # Level 2: filename dupe (disk-side). A file with the same basename
    # already lives at the target. Compare by size — same size + same name
    # is probably identical content; different size is definitely a conflict.
    if action.target_path.exists():
        try:
            existing_size = action.target_path.stat().st_size
        except OSError:
            existing_size = -1
        if existing_size == action.size_bytes and existing_size >= 0:
            # Same name, same size — overwhelmingly likely to be the same
            # file. Classify as a path dupe so it skips by default; the
            # user can flip to ``replace`` if they really mean to.
            action.status = "dupe_path"
            action.resolution = "skip"
            action.existing_rom_path = str(action.target_path)
            action.reason = "identical file already at target path"
        else:
            action.status = "dupe_filename"
            action.resolution = "skip"
            action.existing_rom_path = str(action.target_path)
            action.reason = (
                f"different file with same name already exists "
                f"({existing_size} vs {action.size_bytes} bytes)"
            )
        return

    # Level 3: hash-level dupe. Hashing every staging file is expensive on
    # network shares, so we only do it when the user opted in via the
    # "Heavy identify before import" checkbox. We also skip when the file
    # isn't an accepted ROM extension for the resolved system, since
    # hashing random side files just to find them in the library is a
    # waste.
    if not options.heavy_identify or action.system_id is None:
        return
    if accepted_extensions and not is_rom_file(
        action.source_path.name, accepted_extensions
    ):
        return
    # Hash the staging file. Use ``hash_rom`` with the system's header rule
    # so the SHA-1 matches what Heavy Scan stored in ``hashes.sha1``.
    from romulus.core.hasher import hash_rom

    header_rule = _header_rule_for_system(conn, action.system_id)
    try:
        digest = hash_rom(action.source_path, header_rule)
    except OSError:
        digest = None
    if digest is None:
        return
    existing_by_hash = q.find_rom_by_sha1(conn, digest.sha1)
    if existing_by_hash is not None:
        action.status = "dupe_hash"
        action.resolution = "skip"
        action.existing_rom_id = int(existing_by_hash["id"])
        action.existing_rom_path = str(existing_by_hash["path"])
        action.confidence = "dat_verified" if action.confidence == "fuzzy" else action.confidence
        action.reason = (
            "same content already in library under a different filename"
        )


def _header_rule_for_system(
    conn: sqlite3.Connection, system_id: str
) -> str | None:
    """Return the ``header_rule`` value for ``system_id`` or None."""
    row = conn.execute(
        "SELECT header_rule FROM systems WHERE id = ?", (system_id,)
    ).fetchone()
    if row is None:
        return None
    rule = row["header_rule"]
    return str(rule) if rule else None


# ---------------------------------------------------------------------------
# Plan application
# ---------------------------------------------------------------------------


def apply_plan(
    conn: sqlite3.Connection,
    plan: ImportPlan,
    progress_callback: ProgressCallback | None = None,
    *,
    library_root_override: Path | str | None = None,
) -> ImportSummary:
    """Execute every action in ``plan``, atomically, with per-action SAVEPOINT.

    Mirrors :func:`romulus.core.sync.apply_plan` — a failure on one action
    rolls back only that action and the loop continues so a single bad
    file doesn't poison the whole import. Cooperative cancel is honoured
    between actions (mid-file ``atomic_copy`` has no safe abort point).

    ``library_root_override`` lets callers stamp a different ``library_root``
    value on the new ``roms`` rows than the one the plan was analysed under.
    This matters for tests where the plan was built against a tmp path but
    we want to enrol under the same canonical string the scanner would use.
    Defaults to the plan's own ``library_root``.
    """
    summary = ImportSummary()
    library_root = (
        Path(library_root_override) if library_root_override else plan.library_root
    )
    try:
        library_root_str = str(library_root.resolve())
    except OSError:
        library_root_str = str(library_root)

    total = len(plan.actions)
    for index, action in enumerate(plan.actions, start=1):
        if progress_callback is not None:
            # Mid-action cancel is unsafe (atomic_copy has no abort point);
            # we sample the cancel flag here, between actions, so a long
            # apply can still be interrupted within bounded time.
            progress_callback(index, total, action.source_path.name)

        if action.resolution == "skip":
            summary.files_skipped += 1
            action.executed = True
            continue

        savepoint = f"import_{index}"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            executed_kind = _execute_action(
                conn, action, library_root_str, summary
            )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = True
            if action.system_id is not None:
                summary.systems_touched.add(action.system_id)
            if executed_kind == "imported":
                summary.files_imported += 1
                summary.bytes_imported += action.size_bytes
            elif executed_kind == "replaced":
                summary.files_replaced += 1
                summary.bytes_imported += action.size_bytes
            elif executed_kind == "kept_both":
                summary.files_kept_both += 1
                summary.bytes_imported += action.size_bytes
        except _ImportCancelled:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - rollback intentionally catches all
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = False
            action.error = str(exc)
            summary.errors.append(
                f"{action.source_path.name}: {exc}"
            )
            logger.warning(
                "import action failed: source=%s target=%s err=%s",
                action.source_path,
                action.target_path,
                exc,
            )

    # Re-link any system whose ROM count changed — same idempotent
    # ``group_into_games`` call Quick Scan makes after enrolment.
    for system_id in summary.systems_touched:
        group_into_games(conn, system_id)
    conn.commit()
    return summary


def _execute_action(
    conn: sqlite3.Connection,
    action: ImportAction,
    library_root_str: str,
    summary: ImportSummary,
) -> str:
    """Run a single :class:`ImportAction` and return its execution kind.

    Returns one of ``"imported"`` / ``"replaced"`` / ``"kept_both"`` so the
    caller can update the summary counters without re-checking the
    resolution.
    """
    resolution = action.resolution

    if resolution == "replace":
        # Overwrite the existing file in place. atomic_copy stages a temp
        # file in the destination dir and os.replace's it over the target
        # — same crash-safe semantics as the sync engine.
        atomic.atomic_copy(action.source_path, action.target_path)
        rom_id = _enrol_rom(conn, action, library_root_str)
        action.existing_rom_id = rom_id
        return "replaced"

    if resolution == "keep_both":
        # Disambiguate the filename and copy under the new name. The new
        # row is inserted fresh because the path is now different from
        # any existing row.
        disambiguated = _disambiguate_path(action.target_path)
        action.target_path = disambiguated
        atomic.atomic_copy(action.source_path, action.target_path)
        rom_id = _enrol_rom(conn, action, library_root_str)
        action.existing_rom_id = rom_id
        return "kept_both"

    if resolution == "move":
        # Move = copy first, then unlink source ONLY if the copy succeeded.
        # Never unlink before the copy lands; a partial transfer must leave
        # the source intact so the user can retry.
        atomic.atomic_copy(action.source_path, action.target_path)
        try:
            action.source_path.unlink()
        except OSError as exc:
            logger.warning(
                "import move: dest written OK but source unlink failed: "
                "src=%s err=%s",
                action.source_path,
                exc,
            )
            summary.errors.append(
                f"move {action.source_path.name}: copy ok but source remains ({exc})"
            )
        rom_id = _enrol_rom(conn, action, library_root_str)
        action.existing_rom_id = rom_id
        return "imported"

    # Default branch: ``copy`` (and any future opt-ins that behave like copy).
    atomic.atomic_copy(action.source_path, action.target_path)
    rom_id = _enrol_rom(conn, action, library_root_str)
    action.existing_rom_id = rom_id
    return "imported"


def _enrol_rom(
    conn: sqlite3.Connection,
    action: ImportAction,
    library_root_str: str,
) -> int:
    """Insert / update the ``roms`` row for an imported file via ``upsert_rom``.

    Path-keyed UPSERT semantics: if the target path matches a previously-
    tombstoned (``missing=1``) row, that row is un-tombstoned with the new
    mtime + size + system. This is the same code path the scanner uses
    (CLAUDE.md design rule "Upsert, don't insert").
    """
    target = action.target_path
    try:
        stat = target.stat()
    except OSError as exc:
        raise RuntimeError(
            f"could not stat imported file at {target}: {exc}"
        ) from exc

    parsed = parse_filename(target.name)
    fuzzy = generate_fuzzy_key(parsed.clean_name, parsed.release_type)
    system_id = action.system_id
    # ``upsert_rom`` requires a system_id (FK to systems). For the
    # _unsorted bucket we have nothing to bind to, so we skip enrolment;
    # the file is still copied to disk and a future scoped Quick Scan
    # under the unsorted folder (or a manual move) will catch it.
    if system_id is None:
        return -1

    rom_data = {
        "path": str(target),
        "filename": target.name,
        "extension": target.suffix.lower(),
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "system_id": system_id,
        "fuzzy_key": fuzzy,
        "match_confidence": action.confidence or "fuzzy",
        "library_root": library_root_str,
    }
    return q.upsert_rom(conn, rom_data)


# ---------------------------------------------------------------------------
# Cooperative-cancel helper
# ---------------------------------------------------------------------------


def raise_if_cancelled(flag_callable: Callable[[], bool] | None) -> None:
    """Raise :class:`_ImportCancelled` if ``flag_callable`` returns True.

    Public helper so :class:`romulus.ui.workers.ImportWorker` can thread
    its cancel flag through the progress callback without exposing the
    private exception class.
    """
    if flag_callable is not None and flag_callable():
        raise _ImportCancelled


# Keep a module-level alias so the worker can catch the cancel exception
# without reaching into a private name.
ImportCancelled = _ImportCancelled


# Re-export sentinel + helpers for the dialog and tests.
__all__ = [
    "ARCHIVE_EXTENSIONS",
    "SIDE_FILE_EXTENSIONS",
    "UNSORTED_FOLDER",
    "ImportAction",
    "ImportCancelled",
    "ImportOptions",
    "ImportPlan",
    "ImportResolution",
    "ImportStatus",
    "ImportSummary",
    "analyse_import",
    "apply_plan",
    "raise_if_cancelled",
]
