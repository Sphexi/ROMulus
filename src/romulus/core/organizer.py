"""Library organizer — preview/commit reorganization of an on-disk ROM library.

The organizer is intentionally conservative. It will only propose:

* **merge_folder** — moving the contents of a non-canonical folder (an alias of
  a known system) into the canonical folder for that system.
* **rename** — renaming a ROM file to its canonical No-Intro name. Only applied
  to ROMs whose ``match_confidence == 'dat_verified'`` (Layer 3 hit). Filename
  parses, header titles, and fuzzy matches are NEVER renamed.
* **delete_duplicate** — removing a redundant copy of a ROM that has an
  identical SHA-1 on disk. Preference order: canonical extension (.sfc over
  .smc, etc.), then shorter filename, then lower ROM id (stable tiebreak).
* **collision** — surfaced for manual review whenever an action would put two
  different files at the same destination path. The organizer never overwrites
  existing files automatically.

Filesystem operations delegate to :mod:`romulus.core.atomic` so the organizer
and exporter share a single implementation of the staging-via-tempfile then
``os.replace`` pattern first established in
``romulus.metadata.libretro.fetch_cover``. A crash mid-copy can therefore only
leave a ``.part`` tempfile — never a corrupted final artifact.

Hacks are first-class: rows whose owning game has ``is_hack=1`` are excluded
from duplicate detection and never merged with an original.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from romulus.core import atomic
from romulus.db import queries as q
from romulus.models.system import SYSTEM_REGISTRY

logger = logging.getLogger(__name__)

# Action type literals used in serialized plans.
ACTION_MERGE_FOLDER: str = "merge_folder"
ACTION_RENAME: str = "rename"
ACTION_DELETE_DUPLICATE: str = "delete_duplicate"
ACTION_COLLISION: str = "collision"

# Canonical extension preference order. Lower index = more canonical. Used by
# duplicate-resolution to pick the keeper out of a group of byte-identical
# files. Anything not in this list sorts after everything in it.
_EXTENSION_PREFERENCE: dict[str, int] = {
    ".sfc": 0,
    ".smc": 1,
    ".z64": 0,
    ".n64": 1,
    ".v64": 2,
    ".gb": 0,
    ".gbc": 0,
    ".gba": 0,
    ".md": 0,
    ".gen": 1,
    ".smd": 2,
    ".bin": 3,
}

ProgressCallback = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# Plan + action structures
# ---------------------------------------------------------------------------


@dataclass
class OrganizeAction:
    """One proposed change to the library.

    ``kind`` is one of the ``ACTION_*`` literals. The other fields are
    populated selectively depending on the kind:

    * ``merge_folder`` — ``source_path`` is the alias folder, ``target_path``
      is the canonical folder.
    * ``rename`` — ``rom_id``, ``source_path``, ``target_path`` (full file
      paths).
    * ``delete_duplicate`` — ``rom_id`` (the row being removed),
      ``source_path`` (file path being deleted), ``target_path`` (the keeper).
    * ``collision`` — ``source_path``, ``target_path``, both populated;
      ``rom_id`` left as None.
    """

    kind: str
    rom_id: int | None = None
    source_path: str = ""
    target_path: str = ""
    reason: str = ""
    # Populated by ``execute_plan`` after the action has been attempted.
    executed: bool = False
    error: str | None = None


@dataclass
class OrganizePlan:
    """Read-only snapshot of a proposed library reorganization."""

    actions: list[OrganizeAction] = field(default_factory=list)

    def counts_by_kind(self) -> dict[str, int]:
        """Return ``{action_kind: count}`` over every action in the plan."""
        out: dict[str, int] = {}
        for action in self.actions:
            out[action.kind] = out.get(action.kind, 0) + 1
        return out

    def to_json(self) -> str:
        """Serialize the plan to JSON for storage in ``organize_plans``."""
        return json.dumps({"actions": [asdict(a) for a in self.actions]})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_folder(path: str) -> str:
    """Return the parent directory of ``path`` with forward slashes."""
    normalized = path.replace("\\", "/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def _folder_basename(folder: str) -> str:
    """Return the lowercase basename of ``folder`` (forward-slash form)."""
    if not folder:
        return ""
    return folder.rsplit("/", 1)[-1].lower()


def _join(folder: str, filename: str) -> str:
    """Join ``folder`` and ``filename`` with a forward slash."""
    if not folder:
        return filename
    return f"{folder}/{filename}"


def _sanitize_canonical_filename(name: str) -> str:
    """Strip characters that are invalid on Windows filesystems."""
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip()


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def find_alias_merges(conn: sqlite3.Connection) -> list[OrganizeAction]:
    """Identify non-canonical alias folders that should be merged.

    For each system that has ROMs in more than one folder where each folder's
    basename is a known alias of that system, we propose merging the
    non-canonical folders into the canonical one (the first entry in the
    system's ``folder_aliases`` list).
    """
    rows = q.get_alias_folder_pairs(conn)
    folders_by_system: dict[str, list[str]] = {}
    for row in rows:
        sid = str(row["system_id"])
        folder = str(row["folder_path"]) if row["folder_path"] is not None else ""
        folders_by_system.setdefault(sid, []).append(folder)

    actions: list[OrganizeAction] = []
    for sys_def in SYSTEM_REGISTRY:
        if sys_def.id not in folders_by_system:
            continue
        aliases = {a.lower() for a in sys_def.folder_aliases}
        if not aliases:
            continue
        canonical_alias = sys_def.folder_aliases[0].lower()
        system_folders = folders_by_system[sys_def.id]
        canonical_folders = [
            f for f in system_folders if _folder_basename(f) == canonical_alias
        ]
        if not canonical_folders:
            continue
        target_folder = sorted(canonical_folders)[0]
        for folder in system_folders:
            if folder == target_folder:
                continue
            basename = _folder_basename(folder)
            if basename in aliases and basename != canonical_alias:
                actions.append(
                    OrganizeAction(
                        kind=ACTION_MERGE_FOLDER,
                        source_path=folder,
                        target_path=target_folder,
                        reason=f"{basename!r} is an alias of {canonical_alias!r}",
                    )
                )
    return actions


def find_renameable_roms(conn: sqlite3.Connection) -> list[OrganizeAction]:
    """ROMs whose DAT-verified canonical name differs from their current name."""
    actions: list[OrganizeAction] = []
    for row in q.get_dat_matched_roms(conn):
        rom_id = int(row["id"])
        path = str(row["path"]).replace("\\", "/")
        current_name = str(row["filename"])
        extension = str(row["extension"]) or ""
        dat_match = str(row["dat_match"])
        target_name = _sanitize_canonical_filename(dat_match)
        if not target_name:
            continue
        if extension and not target_name.lower().endswith(extension.lower()):
            target_name = f"{target_name}{extension}"
        if target_name == current_name:
            continue
        folder = _normalize_folder(path)
        target_path = _join(folder, target_name)
        if target_path == path:
            continue
        actions.append(
            OrganizeAction(
                kind=ACTION_RENAME,
                rom_id=rom_id,
                source_path=path,
                target_path=target_path,
                reason=f"DAT-verified name: {dat_match}",
            )
        )
    return actions


def _pick_duplicate_keeper(
    rows: list[sqlite3.Row],
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    """Pick the keeper out of a duplicate group.

    Preference: more-canonical extension (per ``_EXTENSION_PREFERENCE``), then
    shorter filename, then lower rom_id. Returns ``(keeper_row, dupes)``.
    """

    def sort_key(row: sqlite3.Row) -> tuple[int, int, int]:
        ext = str(row["extension"] or "").lower()
        ext_rank = _EXTENSION_PREFERENCE.get(ext, 1000)
        filename_len = len(str(row["filename"]))
        rom_id = int(row["rom_id"])
        return (ext_rank, filename_len, rom_id)

    ordered = sorted(rows, key=sort_key)
    return ordered[0], ordered[1:]


def find_duplicates(conn: sqlite3.Connection) -> list[OrganizeAction]:
    """Same-SHA-1 ROMs grouped together — propose deleting the redundant ones.

    Hacks are excluded by the underlying query (see ``get_duplicate_groups``)
    so a hack will never be deduplicated against an original.
    """
    rows = q.get_duplicate_groups(conn)
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["sha1"]), []).append(row)

    actions: list[OrganizeAction] = []
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        keeper, dupes = _pick_duplicate_keeper(group_rows)
        for dup in dupes:
            actions.append(
                OrganizeAction(
                    kind=ACTION_DELETE_DUPLICATE,
                    rom_id=int(dup["rom_id"]),
                    source_path=str(dup["path"]).replace("\\", "/"),
                    target_path=str(keeper["path"]).replace("\\", "/"),
                    reason=f"SHA-1 matches {keeper['filename']!r}",
                )
            )
    return actions


def find_cross_extension_dupes(conn: sqlite3.Connection) -> list[OrganizeAction]:
    """Same game/system/folder, multiple extensions (.smc + .sfc).

    Detects ROMs that share a ``game_id`` and live in the same parent folder
    but use different on-disk extensions. The less-canonical extension is
    proposed for deletion. Only emitted for games whose extensions are ranked
    in ``_EXTENSION_PREFERENCE`` — otherwise we have no basis to pick a
    keeper. Hacks are excluded.
    """
    rows = conn.execute(
        """
        SELECT r.id AS rom_id, r.path, r.filename, r.extension,
               r.system_id, r.game_id,
               COALESCE(g.is_hack, 0) AS is_hack
        FROM roms r
        LEFT JOIN games g ON g.id = r.game_id
        WHERE r.game_id IS NOT NULL
          AND COALESCE(g.is_hack, 0) = 0
        ORDER BY r.game_id, r.id
        """
    ).fetchall()
    groups: dict[tuple[int, str], list[sqlite3.Row]] = {}
    for row in rows:
        folder = _normalize_folder(str(row["path"]))
        groups.setdefault((int(row["game_id"]), folder), []).append(row)

    actions: list[OrganizeAction] = []
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        exts = {str(r["extension"] or "").lower() for r in group_rows}
        if len(exts) < 2:
            continue
        ranked = [
            (
                _EXTENSION_PREFERENCE.get(
                    str(r["extension"] or "").lower(), 1000
                ),
                r,
            )
            for r in group_rows
        ]
        ranked.sort(key=lambda pair: (pair[0], int(pair[1]["rom_id"])))
        keeper_rank, keeper = ranked[0]
        if keeper_rank >= 1000:
            continue
        for rank, dup in ranked[1:]:
            if rank >= 1000:
                continue
            actions.append(
                OrganizeAction(
                    kind=ACTION_DELETE_DUPLICATE,
                    rom_id=int(dup["rom_id"]),
                    source_path=str(dup["path"]).replace("\\", "/"),
                    target_path=str(keeper["path"]).replace("\\", "/"),
                    reason=(f"cross-extension dupe of {keeper['filename']!r}"),
                )
            )
    return actions


def detect_collisions(
    actions: list[OrganizeAction],
) -> list[OrganizeAction]:
    """Augment a plan with ``collision`` actions for unsafe destinations.

    A collision is recorded whenever two distinct ``rename`` actions would
    land on the same target path, or a ``rename`` target path already exists
    as the source of another rename (i.e. would overwrite another ROM's
    current file). The original conflicting rename actions are filtered out —
    the user must resolve the collision manually.
    """
    rename_actions = [a for a in actions if a.kind == ACTION_RENAME]
    if not rename_actions:
        return list(actions)

    target_to_sources: dict[str, list[OrganizeAction]] = {}
    for action in rename_actions:
        target_to_sources.setdefault(action.target_path, []).append(action)

    rename_sources = {a.source_path for a in rename_actions}
    colliding_targets: set[str] = set()
    collisions: list[OrganizeAction] = []
    for target, group in target_to_sources.items():
        if len(group) > 1 or (target in rename_sources and target != group[0].source_path):
            colliding_targets.add(target)
            collisions.append(
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    source_path=group[0].source_path,
                    target_path=target,
                    reason=(
                        f"{len(group)} rename(s) target this path"
                        if len(group) > 1
                        else "target path already exists in library"
                    ),
                )
            )
    if not colliding_targets:
        return list(actions)
    safe = [
        a
        for a in actions
        if not (a.kind == ACTION_RENAME and a.target_path in colliding_targets)
    ]
    return safe + collisions


# ---------------------------------------------------------------------------
# Plan analysis
# ---------------------------------------------------------------------------


def analyze_library(conn: sqlite3.Connection) -> OrganizePlan:
    """Run every detector against the current library state and assemble a plan.

    The returned plan is fully serializable (``to_json``). Callers typically
    pass the resulting list to ``OrganizePreviewDialog`` so the user can
    approve/reject individual actions before ``execute_plan`` is invoked.
    """
    actions: list[OrganizeAction] = []
    actions.extend(find_alias_merges(conn))
    actions.extend(find_renameable_roms(conn))
    actions.extend(find_duplicates(conn))
    actions.extend(find_cross_extension_dupes(conn))
    actions = detect_collisions(actions)
    return OrganizePlan(actions=actions)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _atomic_replace(source: Path, dest: Path) -> None:
    """Move ``source`` to ``dest`` atomically.

    Thin wrapper around :func:`romulus.core.atomic.atomic_replace` so the
    organizer and exporter share a single implementation of the staging-via-
    tempfile dance.
    """
    atomic.atomic_replace(source, dest)


def _execute_rename(
    conn: sqlite3.Connection, action: OrganizeAction
) -> None:
    """Apply a single rename action: filesystem move + DB path update."""
    source = Path(action.source_path)
    dest = Path(action.target_path)
    if not source.exists():
        raise FileNotFoundError(f"source not found: {source}")
    if dest.exists() and dest.resolve() != source.resolve():
        raise FileExistsError(f"target already exists: {dest}")
    _atomic_replace(source, dest)
    if action.rom_id is not None:
        q.update_rom_path(conn, action.rom_id, action.target_path, dest.name)


def _execute_delete_duplicate(
    conn: sqlite3.Connection, action: OrganizeAction
) -> None:
    """Apply a duplicate-removal action: unlink file + delete DB row."""
    source = Path(action.source_path)
    if source.exists():
        os.remove(source)
    if action.rom_id is not None:
        q.delete_rom(conn, action.rom_id)


def _execute_merge_folder(
    conn: sqlite3.Connection, action: OrganizeAction
) -> None:
    """Move every file from the alias folder into the canonical folder.

    Per-file errors are raised so the outer loop's rollback bookkeeping kicks
    in. We update each ROM row's path inline; the surrounding caller commits
    the transaction.
    """
    source_folder = Path(action.source_path)
    target_folder = Path(action.target_path)
    if not source_folder.exists():
        raise FileNotFoundError(f"source folder missing: {source_folder}")
    target_folder.mkdir(parents=True, exist_ok=True)
    for child in sorted(source_folder.iterdir()):
        if not child.is_file():
            continue
        dest = target_folder / child.name
        if dest.exists():
            raise FileExistsError(f"collision merging folder: {dest}")
        _atomic_replace(child, dest)
        old_path_fwd = str(child).replace("\\", "/")
        new_path_fwd = str(dest).replace("\\", "/")
        conn.execute(
            "UPDATE roms SET path = ? WHERE path = ?",
            (new_path_fwd, old_path_fwd),
        )
    with contextlib.suppress(OSError):
        source_folder.rmdir()


def execute_plan(
    conn: sqlite3.Connection,
    approved_actions: list[OrganizeAction],
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Apply approved actions, updating the DB and filesystem.

    Returns a summary dict::

        {
            "applied": int,        # actions executed successfully
            "skipped": int,        # collisions or unsupported kinds
            "failed":  int,        # actions that raised an exception
            "errors":  list[str],  # per-action error messages
        }

    Per-action rollback: each action runs inside its own SAVEPOINT so a single
    failed rename never leaves the DB out of sync with the disk. The loop
    continues with the next action so a localized I/O error doesn't abort the
    whole organization.
    """
    summary: dict[str, Any] = {
        "applied": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    total = len(approved_actions)
    for index, action in enumerate(approved_actions, start=1):
        if progress_callback is not None:
            progress_callback(index, total, action.source_path)
        if action.kind == ACTION_COLLISION:
            action.executed = False
            action.error = "collision left for manual review"
            summary["skipped"] += 1
            continue
        savepoint = f"org_{index}"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            match action.kind:
                case "rename":
                    _execute_rename(conn, action)
                case "delete_duplicate":
                    _execute_delete_duplicate(conn, action)
                case "merge_folder":
                    _execute_merge_folder(conn, action)
                case _:
                    raise ValueError(f"unsupported action: {action.kind!r}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = True
            summary["applied"] += 1
        except Exception as exc:  # noqa: BLE001 - rollback intentionally catches all
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = False
            action.error = str(exc)
            summary["failed"] += 1
            summary["errors"].append(
                f"{action.kind} {action.source_path!s}: {exc}"
            )
            logger.warning(
                "organize action failed: kind=%s src=%s err=%s",
                action.kind,
                action.source_path,
                exc,
            )
    conn.commit()
    return summary
