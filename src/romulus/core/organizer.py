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

Hacks are first-class: rows with ``is_hack=1`` are excluded from duplicate
detection and never merged with an original. Session 13 moved ``is_hack``
directly onto the ``roms`` table; there is no ``games`` join.

Cross-extension duplicate detection was removed in Session 17. Its concept
("two ROMs share ``game_id``") no longer exists in the strict 1:1 schema.
Legitimate same-content cross-extension pairs (e.g. ``Mario.sfc`` +
``Mario.zip``) are caught by ``find_duplicates`` once the TOCTOU guard
correctly uses ``hash_rom`` for normalization.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from romulus.core import atomic
from romulus.db import queries as q
from romulus.models.system import SYSTEM_REGISTRY

logger = logging.getLogger(__name__)

# Action type literals used in serialized plans.
ACTION_MERGE_FOLDER = "merge_folder"
ACTION_RENAME = "rename"
ACTION_DELETE_DUPLICATE = "delete_duplicate"
ACTION_COLLISION = "collision"

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

# Sort sentinel used to push unranked extensions to the tail of duplicate-keeper
# selection. ``sys.maxsize`` is self-documenting in a way the bare literal 1000
# is not — and ``_EXTENSION_PREFERENCE`` can grow without revisiting the sentinel.
_UNRANKED_SORT_KEY = sys.maxsize

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


@dataclass(frozen=True, slots=True)
class OrganizePlan:
    """Read-only snapshot of a proposed library reorganization."""

    actions: list[OrganizeAction] = field(default_factory=list)

    def counts_by_kind(self) -> dict[str, int]:
        """Return ``{action_kind: count}`` over every action in the plan."""
        return dict(Counter(a.kind for a in self.actions))

    def to_json(self) -> str:
        """Serialize the plan to JSON for storage in ``organize_plans``."""
        return json.dumps({"actions": [asdict(a) for a in self.actions]})


@dataclass(slots=True)
class OrganizeSummary:
    """Result of :func:`execute_plan` — what actually happened on disk.

    Mirrors :class:`romulus.core.exporter.ExportSummary` so both
    filesystem-mutating subsystems return strongly-typed result objects rather
    than ``dict[str, Any]``. Mutable so :func:`execute_plan` can increment
    counters in-place inside the loop.
    """

    applied: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


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


#: Windows reserved device names (case-insensitive). A filename whose stem
#: matches any of these is rerouted to a device driver instead of the disk —
#: a malicious DAT mapping a canonical ``CON`` or ``PRN`` to a real ROM would
#: render the file inaccessible on Windows. See security audit v0.1.0
#: finding #5.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    }
)


def _sanitize_canonical_filename(name: str) -> str:
    """Strip characters that are invalid on Windows filesystems.

    Beyond the standard Windows-illegal punctuation set, also:

    * fold ASCII control characters (``\\x00``-``\\x1f``) to ``_``;
    * strip trailing dots and spaces (Windows silently mangles these);
    * underscore-prefix the stem when it matches a Windows reserved device
      name — ``CON.sfc`` becomes ``_CON.sfc`` so the file remains accessible.

    Applied unconditionally even on POSIX so the resulting library can be
    safely copied to a Windows host or an exFAT SD card.
    """
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if (c in bad or ord(c) < 0x20) else c for c in name)
    cleaned = cleaned.strip().rstrip(". ")
    if not cleaned:
        return cleaned
    # Inspect the stem (portion before the FIRST dot — handles ``CON.foo.sfc``
    # which Windows also routes to the device).
    stem = cleaned.split(".", 1)[0].lower()
    if stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


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
    folders_by_system: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        sid = str(row["system_id"])
        folder = str(row["folder_path"]) if row["folder_path"] is not None else ""
        folders_by_system[sid].append(folder)

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
        # When several canonical-named folders coexist (e.g. ``/lib/megadrive``
        # alongside ``/lib/MegaDrive`` on a case-insensitive filesystem), break
        # the tie with ASCII sort order. Capital letters sort before lower-
        # case, so ``/lib/MegaDrive`` would win over ``/lib/megadrive``.
        # Documented here rather than surfacing as a collision because either
        # choice is correct — the merge moves the loser's files into the keeper.
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


def find_renameable_roms(
    conn: sqlite3.Connection,
    exclude_rom_ids: set[int] | None = None,
) -> list[OrganizeAction]:
    """ROMs whose DAT-verified canonical name differs from their current name.

    Args:
        conn: Database connection.
        exclude_rom_ids: Optional set of rom IDs to skip — used by
            :func:`analyze_library` to suppress rename proposals for roms
            already scheduled for deletion by :func:`find_duplicates`.
            Without this, a rom that's about to be deleted as a hash dupe
            would also get a redundant rename action and could end up as a
            false collision.
    """
    skip = exclude_rom_ids or set()
    actions: list[OrganizeAction] = []
    for row in q.get_dat_matched_roms(conn):
        rom_id = int(row["id"])
        if rom_id in skip:
            continue
        path = str(row["path"]).replace("\\", "/")
        current_name = str(row["filename"])
        extension = str(row["extension"]) or ""
        dat_match = str(row["dat_match"])
        target_name = _sanitize_canonical_filename(dat_match)
        if not target_name:
            logger.debug(
                "organize.rename: skip empty sanitized name rom_id=%d dat_match=%s",
                rom_id,
                dat_match,
            )
            continue
        if extension and not target_name.lower().endswith(extension.lower()):
            target_name = f"{target_name}{extension}"
        if target_name == current_name:
            logger.debug(
                "organize.rename: skip already-canonical rom_id=%d filename=%s",
                rom_id,
                current_name,
            )
            continue
        folder = _normalize_folder(path)
        target_path = _join(folder, target_name)
        if target_path == path:
            continue
        logger.debug(
            "organize.rename: planned rom_id=%d from=%s to=%s dat_match=%s",
            rom_id,
            current_name,
            target_name,
            dat_match,
        )
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
    rows: Iterable[sqlite3.Row],
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    """Pick the keeper out of a duplicate group.

    The keeper is the row that sorts FIRST under the composite key:

    1. ``ext_rank`` — index in :data:`_EXTENSION_PREFERENCE`; lower is more
       canonical. Unranked extensions get :data:`_UNRANKED_SORT_KEY`
       (``sys.maxsize``) so they always lose to a ranked candidate.
    2. ``filename_len`` — shorter filename wins. The intuition is that
       ``Mario.sfc`` is closer to a canonical No-Intro name than
       ``Mario (USA) (Rev 1).sfc``; the dupe most likely IS the canonical
       form, the rest are user-renamed copies. Cosmetic but stable.
    3. ``rom_id`` — final tiebreaker; lower id means the row was seen earlier
       and therefore deterministically wins. Pure determinism, no semantics.

    Returns ``(keeper_row, dupes)``.
    """

    def sort_key(row: sqlite3.Row) -> tuple[int, int, int]:
        ext = str(row["extension"] or "").lower()
        ext_rank = _EXTENSION_PREFERENCE.get(ext, _UNRANKED_SORT_KEY)
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
    groups: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[str(row["sha1"])].append(row)

    actions: list[OrganizeAction] = []
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        keeper, dupes = _pick_duplicate_keeper(group_rows)
        logger.debug(
            "organize.dedup: group sha1=%s keeper=%s dupes=%d",
            keeper["sha1"],
            keeper["filename"],
            len(dupes),
        )
        for dup in dupes:
            logger.debug(
                "organize.dedup: planned delete rom_id=%s path=%s keeper=%s",
                dup["rom_id"],
                dup["path"],
                keeper["filename"],
            )
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



def detect_collisions(
    conn: sqlite3.Connection,
    actions: Iterable[OrganizeAction],
) -> list[OrganizeAction]:
    """Augment a plan with collision or upgraded-dedup actions.

    Four cases are detected. The first three end up as ``ACTION_COLLISION``
    rows (manual review). The fourth promotes a would-be collision into an
    ``ACTION_DELETE_DUPLICATE`` when content equality is provable.

    1. Two distinct ``rename`` actions would land on the same target path.
    2. A ``rename`` target path is already the *source* of another rename in
       this plan (i.e. would overwrite a ROM that is itself being renamed).
    3. A ``rename`` target path matches an existing ``roms`` row that is NOT
       itself being renamed in this plan. SHA-1 comparison decides:
       3a. Both sides have a stored SHA-1 AND they match AND neither side is
           a hack → upgrade to ``ACTION_DELETE_DUPLICATE``. The canonical-
           named existing file is the keeper; the rename source becomes the
           file to delete. Catches the case where ``find_duplicates`` would
           normally pair them but didn't (typically because one side wasn't
           Heavy-Scanned when ``find_duplicates`` ran, or an ``is_hack``
           difference excluded the pair).
       3b. Both sides have a stored SHA-1 AND they differ → real collision.
           Two different ROMs want the same canonical name (e.g., a bad dump
           sitting at the canonical filename + a DAT-verified source).
       3c. One or both sides lack a stored SHA-1 → real collision. We can't
           prove content equality without Heavy-Scanning the missing side.

    The conflicting rename actions are filtered out of the result; their
    replacements (collision or upgraded delete_duplicate) take their place.

    Args:
        conn: Database connection used for case-3 lookups (path-keyed roms
            + per-rom SHA-1 via :func:`romulus.db.queries.get_sha1_for_rom`).
        actions: Iterable of proposed :class:`OrganizeAction` objects.
    """
    actions_list = list(actions)
    rename_actions = [a for a in actions_list if a.kind == ACTION_RENAME]
    if not rename_actions:
        return actions_list

    # Build a fast lookup: target_path → list of rename actions that want it.
    target_to_sources: defaultdict[str, list[OrganizeAction]] = defaultdict(list)
    for action in rename_actions:
        target_to_sources[action.target_path].append(action)

    # Set of paths that are themselves being moved in this plan.
    rename_sources = {a.source_path for a in rename_actions}
    # Set of rom_ids that ARE being renamed — used to exclude them from case 3.
    renamed_rom_ids: set[int] = {a.rom_id for a in rename_actions if a.rom_id is not None}

    # Renames whose target is being replaced — either by a collision row OR
    # by an upgraded delete_duplicate. Both classes filter the original
    # rename out of the result, but they accumulate into different lists so
    # the final action list keeps each kind's semantics distinct.
    replaced_targets: set[str] = set()
    collisions: list[OrganizeAction] = []
    upgraded_dupes: list[OrganizeAction] = []

    for target, group in target_to_sources.items():
        collision_reason: str | None = None
        upgrade_to_delete: OrganizeAction | None = None

        # Case 1 — multiple renames competing for the same target.
        if len(group) > 1:
            collision_reason = f"{len(group)} rename(s) target this path"

        # Case 2 — rename target equals the source of a different rename.
        elif target in rename_sources and target != group[0].source_path:
            collision_reason = "target path already exists in library"

        # Case 3 — an un-renamed DB row already occupies the target path.
        else:
            existing = q.find_rom_by_path(conn, target)
            if existing is not None and int(existing["id"]) not in renamed_rom_ids:
                source_action = group[0]
                source_rom_id = source_action.rom_id
                target_rom_id = int(existing["id"])
                source_is_hack = False  # source is dat_verified — query rom row for is_hack
                target_is_hack = bool(existing["is_hack"] or 0)

                source_sha1: str | None = None
                target_sha1: str | None = None
                if source_rom_id is not None:
                    source_sha1 = q.get_sha1_for_rom(conn, source_rom_id)
                    src_row = conn.execute(
                        "SELECT is_hack FROM roms WHERE id = ?", (source_rom_id,)
                    ).fetchone()
                    if src_row is not None:
                        source_is_hack = bool(src_row["is_hack"] or 0)
                target_sha1 = q.get_sha1_for_rom(conn, target_rom_id)

                if (
                    source_sha1 is not None
                    and target_sha1 is not None
                    and source_sha1 == target_sha1
                    and not source_is_hack
                    and not target_is_hack
                ):
                    # 3a — content equality proven, neither is a hack.
                    # Upgrade to delete_duplicate: keep the canonical-named
                    # existing file (target), delete the rename source.
                    upgrade_to_delete = OrganizeAction(
                        kind=ACTION_DELETE_DUPLICATE,
                        rom_id=source_rom_id,
                        source_path=source_action.source_path,
                        target_path=target,
                        reason=(
                            f"SHA-1 matches existing canonical-named file "
                            f"{Path(target).name!r}"
                        ),
                    )
                else:
                    if source_sha1 is None or target_sha1 is None:
                        # 3c — can't prove equality.
                        collision_reason = (
                            "target path already occupied; Heavy Scan both "
                            "files to determine if duplicate"
                        )
                    elif source_is_hack or target_is_hack:
                        # Hack on either side — never auto-merge.
                        collision_reason = (
                            "target path already occupied by a hack/non-hack "
                            "pair; manual review required"
                        )
                    else:
                        # 3b — different content.
                        collision_reason = (
                            "target path already occupied by a different "
                            "file in the library"
                        )

        if collision_reason is not None:
            replaced_targets.add(target)
            collisions.append(
                OrganizeAction(
                    kind=ACTION_COLLISION,
                    source_path=group[0].source_path,
                    target_path=target,
                    reason=collision_reason,
                )
            )
            logger.debug(
                "organize.collision: target=%s reason=%r",
                target,
                collision_reason,
            )
        elif upgrade_to_delete is not None:
            replaced_targets.add(target)
            upgraded_dupes.append(upgrade_to_delete)
            logger.debug(
                "organize.collision: upgraded to delete_duplicate target=%s "
                "source=%s sha1_match=%s",
                target,
                upgrade_to_delete.source_path,
                source_sha1,
            )

    if not replaced_targets:
        return actions_list
    safe = [
        a
        for a in actions_list
        if not (a.kind == ACTION_RENAME and a.target_path in replaced_targets)
    ]
    return safe + upgraded_dupes + collisions


# ---------------------------------------------------------------------------
# Plan analysis
# ---------------------------------------------------------------------------


def analyze_library(conn: sqlite3.Connection) -> OrganizePlan:
    """Run every detector against the current library state and assemble a plan.

    Tiered ordering — each phase narrows the candidate set for the next:

    1. :func:`find_alias_merges` — non-canonical folder merges.
    2. :func:`find_duplicates` — same-SHA-1 pairs. Wins first because content
       equality is the strongest claim; the rom about to be deleted as a
       hash duplicate should not also be considered for renaming.
    3. :func:`find_renameable_roms` — DAT-verified filename renames, excluding
       any rom_id already marked for deletion in step 2.
    4. :func:`detect_collisions` — post-processing pass that either replaces
       a rename with an ``ACTION_COLLISION`` (true conflict) or upgrades it
       to ``ACTION_DELETE_DUPLICATE`` when content equality is provable
       against the existing canonical-named file (catches pairs
       :func:`find_duplicates` missed due to Heavy-Scan gaps).

    The returned plan is fully serializable (``to_json``). Callers typically
    pass the resulting list to ``OrganizePreviewDialog`` so the user can
    approve/reject individual actions before ``execute_plan`` is invoked.
    """
    merges = find_alias_merges(conn)
    dupes = find_duplicates(conn)
    # Roms already scheduled for deletion (as hash dupes) should not get a
    # competing rename action. Build the exclusion set before running the
    # rename detector.
    deleted_rom_ids: set[int] = {
        a.rom_id for a in dupes if a.rom_id is not None
    }
    renames = find_renameable_roms(conn, exclude_rom_ids=deleted_rom_ids)
    actions: list[OrganizeAction] = []
    actions.extend(merges)
    actions.extend(dupes)
    actions.extend(renames)
    actions = detect_collisions(conn, actions)
    plan = OrganizePlan(actions=actions)
    logger.debug(
        "analyze_library: merges=%d dupes=%d renames=%d (skipped=%d) final=%d",
        len(merges),
        len(dupes),
        len(renames),
        len(deleted_rom_ids),
        len(plan.actions),
    )
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


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
    atomic.atomic_replace(source, dest)
    if action.rom_id is not None:
        q.update_rom_path(conn, action.rom_id, action.target_path, dest.name)


def _execute_delete_duplicate(
    conn: sqlite3.Connection, action: OrganizeAction
) -> None:
    """Apply a duplicate-removal action: unlink file + delete DB row.

    TOCTOU guard (security audit v0.1.0 finding #11): the plan was built
    against the ``hashes`` table at analysis time. Between then and now the
    user may have manually edited ``source`` or ``target`` — re-hash both
    just before the unlink and abort if the post-normalization SHA-1s no longer
    match. Adds two file reads to each delete, but the action set is small
    (typically a handful of dupes per library) and the cost is dwarfed by the
    safety win: a manually-modified file is never silently destroyed.

    The guard uses :func:`romulus.core.hasher.hash_rom` so that ZIP extraction
    and per-system header stripping are applied before comparison. A raw stream
    digest of ``.smc`` vs ``.sfc`` — or ``.sfc`` vs ``.zip`` — produces
    different bytes even when the normalized payloads are identical. ``hash_rom``
    resolves this by applying the same normalization path that ``hash_library``
    used when the ``hashes`` row was first written.
    """
    # Late import — ``romulus.core.hasher`` already imports
    # ``romulus.db.queries``; keep the dependency arrow one-way by deferring
    # until execute time. Only the public ``hash_rom`` entry point is imported;
    # the private raw-stream helper in hasher.py is intentionally unused here.
    from romulus.core.hasher import hash_rom

    source = Path(action.source_path)
    target = Path(action.target_path) if action.target_path else None

    if source.exists() and target is not None and target.exists():
        # Build a system_id → SystemDef lookup from the global registry so we
        # can resolve the header_rule for both the source and target ROMs.
        system_map = {sys_def.id: sys_def for sys_def in SYSTEM_REGISTRY}

        def _header_rule_for(path: Path) -> str | None:
            """Return the header_rule for the rom at *path* by querying the DB."""
            row = q.find_rom_by_path(conn, str(path).replace("\\", "/"))
            if row is None:
                return None
            sid = str(row["system_id"]) if row["system_id"] is not None else ""
            sys_def = system_map.get(sid)
            return sys_def.header_rule if sys_def is not None else None

        source_rule = _header_rule_for(source)
        target_rule = _header_rule_for(target)

        try:
            source_result = hash_rom(source, source_rule)
            target_result = hash_rom(target, target_rule)
        except OSError as exc:
            raise OSError(
                f"failed to verify duplicate before delete: {exc}"
            ) from exc

        if source_result is None or target_result is None:
            # If hash_rom returns None the file is unreadable or an empty zip;
            # abort rather than silently deleting an unverifiable file.
            raise ValueError(
                f"refusing to delete {source!s}: could not compute "
                "post-normalization SHA-1 for one or both files"
            )

        source_sha = source_result.sha1
        target_sha = target_result.sha1
        if source_sha != target_sha:
            raise ValueError(
                f"refusing to delete {source!s}: post-normalization SHA-1 no "
                f"longer matches keeper {target!s} "
                f"(source={source_sha} target={target_sha})"
            )

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
        atomic.atomic_replace(child, dest)
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
    approved_actions: Sequence[OrganizeAction],
    progress_callback: ProgressCallback | None = None,
) -> OrganizeSummary:
    """Apply approved actions, updating the DB and filesystem.

    Returns a strongly-typed :class:`OrganizeSummary` with ``applied``,
    ``skipped``, ``failed`` counters and a per-action ``errors`` list.

    Per-action rollback: each action runs inside its own SAVEPOINT so a single
    failed rename never leaves the DB out of sync with the disk. The loop
    continues with the next action so a localized I/O error doesn't abort the
    whole organization.
    """
    summary = OrganizeSummary()
    total = len(approved_actions)
    for index, action in enumerate(approved_actions, start=1):
        if progress_callback is not None:
            progress_callback(index, total, action.source_path)
        if action.kind == ACTION_COLLISION:
            action.executed = False
            action.error = "collision left for manual review"
            summary.skipped += 1
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
            summary.applied += 1
        except Exception as exc:  # noqa: BLE001 - rollback intentionally catches all
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = False
            action.error = str(exc)
            summary.failed += 1
            summary.errors.append(
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
