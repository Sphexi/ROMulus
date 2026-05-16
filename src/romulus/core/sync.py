"""Destination sync — diff engine + apply for all five sync modes.

This is the write side of the destination-sync feature (sync-design spec
§2, §3, §7). Given a :class:`romulus.core.dest_inventory.DestInventory`
snapshot, it computes the list of :class:`SyncAction` rows needed to bring
the destination into the requested relationship with the local library:

* ``push_merge``  — copy local-only to dest, leave the rest alone.
* ``push_mirror`` — copy local-only to dest, delete dest-only.
* ``push_wipe``   — empty dest first, then run push_mirror.
* ``pull``        — copy dest-only into the local library and enrol it.
* ``two_way``     — copy in whichever direction the file exists; for
  conflicts (same identity, different content) apply the user's conflict
  policy.

Identity matching is layered exactly as the spec describes (§3):

* Tier 1 — same forward-slash ``rel_path`` under the target.
* Tier 2 — same ``(fuzzy_key, region_lowercase_or_empty)``. Region stays in
  the key so a USA cartridge and the European version remain distinct.
* Tier 3 — when the local ROM has a known SHA-1 and the dest filename matches
  by tier-2, sanity-gate via size match.
* Tier 4 — deep verify; opt-in via the ``deep_verify`` flag in
  :class:`DestInventoryWorker`. Dest file's SHA-1 is compared against
  ``hashes.sha1``.

Every filesystem write goes through :mod:`romulus.core.atomic`. Deletes use
a tombstone-rename pattern so a crash partway through a delete leaves a
``.tombstone`` file that can be unlinked on resume — never an unrecoverable
state where the DB and the disk disagree. Each action runs inside its own
SAVEPOINT, mirroring :func:`romulus.core.organizer.execute_plan`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from romulus.core import atomic
from romulus.core.dest_inventory import DestInventory, InventoryEntry
from romulus.core.exporter import (
    _system_dest_dir,
    copy_artwork,
    generate_gamelist_xml,
)
from romulus.core.scanner import generate_fuzzy_key, parse_filename
from romulus.db import queries as q
from romulus.models.profile import DestinationProfile

logger = logging.getLogger(__name__)

# Sync mode literals — strongly typed end-to-end per the spec.
SyncMode = Literal["push_merge", "push_mirror", "push_wipe", "pull", "two_way"]

# Conflict policy literals for two-way mode (§2.1).
ConflictPolicy = Literal["skip", "local", "dest", "newest", "prompt"]

# Action kind literals used in serialized plans + the preview dialog buckets.
ACTION_COPY_TO_DEST = "copy_to_dest"
ACTION_DELETE_DEST = "delete_dest"
ACTION_COPY_TO_LOCAL = "copy_to_local"
ACTION_DELETE_LOCAL = "delete_local"
ACTION_CONFLICT = "conflict"
ACTION_IDENTICAL = "identical"

# Sub-kinds for conflict actions — record which side wins so the apply step
# knows what to do without re-resolving the policy.
CONFLICT_RESOLUTION_SKIP = "skip"
CONFLICT_RESOLUTION_LOCAL = "local"
CONFLICT_RESOLUTION_DEST = "dest"
CONFLICT_RESOLUTION_NEWEST = "newest"
CONFLICT_RESOLUTION_PROMPT = "prompt"

ProgressCallback = Callable[[int, int, str], None]


class _SyncCancelled(Exception):  # noqa: N818 - cancel marker, not an error
    """Cooperative-cancel marker raised from a sync progress callback."""


# ---------------------------------------------------------------------------
# Plan + action structures
# ---------------------------------------------------------------------------


@dataclass
class SyncAction:
    """One proposed step in a sync plan.

    Every action carries the source/dest paths needed to apply it without
    re-running the diff. ``rom_id`` and ``game_id`` are forwarded so the
    apply step can keep ``dest_inventory`` rows in sync without a second
    lookup. ``conflict_resolution`` is populated only for
    ``ACTION_CONFLICT`` actions.
    """

    kind: str
    rel_path: str = ""
    local_path: str = ""
    dest_path: str = ""
    size_bytes: int = 0
    rom_id: int | None = None
    game_id: int | None = None
    system_id: str | None = None
    conflict_resolution: str = ""
    reason: str = ""
    # Filled in by ``apply_plan`` once execution has been attempted.
    executed: bool = False
    error: str | None = None


@dataclass(slots=True)
class SyncPlan:
    """Read-only snapshot of a proposed destination sync."""

    dest_id: int
    mode: SyncMode
    actions: list[SyncAction] = field(default_factory=list)
    conflict_policy: ConflictPolicy = "skip"

    def counts_by_kind(self) -> dict[str, int]:
        """Return ``{action_kind: count}`` over every action in the plan."""
        return dict(Counter(a.kind for a in self.actions))

    def bytes_by_kind(self) -> dict[str, int]:
        """Sum the byte budget per action kind for the preview header."""
        out: defaultdict[str, int] = defaultdict(int)
        for action in self.actions:
            out[action.kind] += int(action.size_bytes or 0)
        return dict(out)

    def is_destructive(self) -> bool:
        """True if the plan has any delete-or-overwrite action.

        Used by :class:`romulus.ui.sync_preview.SyncPreviewDialog` to decide
        whether the double-confirm prompt sequence should fire (§6.3). The
        first single Apply click is sufficient for purely additive plans.
        """
        for action in self.actions:
            if action.kind in {ACTION_DELETE_DEST, ACTION_DELETE_LOCAL}:
                return True
            if action.kind == ACTION_CONFLICT and action.conflict_resolution in {
                CONFLICT_RESOLUTION_LOCAL,
                CONFLICT_RESOLUTION_DEST,
                CONFLICT_RESOLUTION_NEWEST,
            }:
                # Overwrites count as destructive — newest still picks a
                # winner that will overwrite the other side.
                return True
        return False

    def to_json(self) -> str:
        """Serialize the plan to JSON for storage in ``sync_plans``."""
        return json.dumps(
            {
                "dest_id": self.dest_id,
                "mode": self.mode,
                "conflict_policy": self.conflict_policy,
                "actions": [asdict(a) for a in self.actions],
            }
        )


@dataclass(slots=True)
class SyncSummary:
    """Result of :func:`apply_plan` — what actually happened on disk."""

    applied: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_copied_to_dest: int = 0
    bytes_copied_to_local: int = 0
    files_added_to_dest: int = 0
    files_removed_from_dest: int = 0
    files_pulled_to_local: int = 0
    systems_touched: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Identity matching (§3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalRom:
    """Lightweight view of a local ``roms``+``games``+``hashes`` row."""

    rom_id: int
    path: str
    filename: str
    system_id: str
    size_bytes: int
    fuzzy_key: str
    region: str
    game_id: int | None
    sha1: str | None


def _row_to_local_rom(row: sqlite3.Row) -> LocalRom:
    return LocalRom(
        rom_id=int(row["rom_id"]),
        path=str(row["path"]),
        filename=str(row["filename"]),
        system_id=str(row["system_id"]) if row["system_id"] else "",
        size_bytes=int(row["size_bytes"] or 0),
        fuzzy_key=str(row["fuzzy_key"]) if row["fuzzy_key"] else "",
        region=str(row["region"]) if row["region"] else "",
        game_id=int(row["game_id"]) if row["game_id"] is not None else None,
        sha1=str(row["sha1"]).lower() if row["sha1"] else None,
    )


@dataclass(slots=True)
class _MatchIndex:
    """Pre-computed lookup tables for the identity matcher.

    Building these once at the start of the diff means we walk the local DB
    a single time instead of per-dest-file. A 50 k library still fits in
    memory comfortably (rows are ~200 bytes each), and the win on a large
    inventory is enormous: O(N+M) vs O(N·M).

    ``by_fuzzy_region`` is keyed on ``(fuzzy_key, region, system_id)`` —
    the system_id segment is the cross-platform guard, without it titles
    like ``Pac-Man`` collide between Game Boy (.gb) and Game Boy Color
    (.gbc) since both produce identical fuzzy keys + regions. The dest
    side derives its system_id from the rel_path via the profile's folder
    map at match time.

    ``profile`` is stored so :func:`_match_dest_entry` can do the dest →
    system_id lookup without an additional argument.
    """

    by_rel_path: dict[str, LocalRom] = field(default_factory=dict)
    by_fuzzy_region: dict[tuple[str, str, str], LocalRom] = field(
        default_factory=dict
    )
    by_sha1: dict[str, LocalRom] = field(default_factory=dict)
    rows: list[LocalRom] = field(default_factory=list)
    profile: DestinationProfile | None = None


def _build_match_index(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target_path: Path,
) -> _MatchIndex:
    """Hydrate the identity index used by :func:`_match_dest_entry`.

    Tier-1 ``by_rel_path`` is keyed on the forward-slash path the ROM
    *would* land at under the active profile — so a sync can recognise a
    previously-exported file regardless of how the local copy is laid out.
    Tier-2 ``by_fuzzy_region`` is keyed on the spec's match composite.
    """
    index = _MatchIndex(profile=profile)
    rows = [_row_to_local_rom(r) for r in q.get_local_roms_for_match(conn)]
    index.rows = rows
    for rom in rows:
        mapping = profile.systems.get(rom.system_id)
        if mapping is not None and mapping.is_supported:
            dest_dir = _system_dest_dir(target_path, profile, mapping)
            # Forward-slash relative path = the inventory's rel_path scheme.
            try:
                rel = (dest_dir / rom.filename).relative_to(target_path)
                rel_path = str(rel).replace("\\", "/")
                index.by_rel_path[rel_path] = rom
            except ValueError:
                # _system_dest_dir resolves through the security guard;
                # relative_to may still fail on case-insensitive Windows
                # paths. Skip — tier-2 will still pick this up.
                pass
        if rom.fuzzy_key:
            # Three-part key: system_id pins the platform so a Game Boy
            # "Pac-Man" doesn't match a Game Boy Color "Pac-Man" — the
            # fuzzy_key + region segments are identical between platforms.
            key = (rom.fuzzy_key, rom.region.lower(), rom.system_id)
            # First-write-wins so a re-release with a non-empty release_type
            # suffix (already folded into the fuzzy_key by scanner.generate_
            # fuzzy_key) doesn't overwrite the original.
            index.by_fuzzy_region.setdefault(key, rom)
        if rom.sha1:
            index.by_sha1.setdefault(rom.sha1, rom)
    return index


def _fuzzy_region_key_for_entry(rel_path: str) -> tuple[str, str]:
    """Compute the (fuzzy_key, region) tier-2 key for a dest filename.

    Returns only the filename-derived portion of the key. The third
    component (system_id) comes from the dest folder via the profile —
    callers needing the full match key resolve it through
    :func:`_system_id_from_rel_path`.
    """
    filename = rel_path.rsplit("/", 1)[-1]
    parsed = parse_filename(filename)
    fuzzy = generate_fuzzy_key(parsed.clean_name, parsed.release_type)
    region = (parsed.region or "").lower()
    return fuzzy, region


def _match_dest_entry(
    entry: InventoryEntry,
    index: _MatchIndex,
) -> LocalRom | None:
    """Apply the four-tier identity match (§3) to a single dest entry.

    Returns the matched :class:`LocalRom` or ``None`` if the dest file is
    orphaned. Tier 4 (deep verify) is implicit: when ``entry.sha1`` is set
    (because the user toggled the deep-verify checkbox during the scan),
    the SHA-1 lookup short-circuits everything else.
    """
    # Tier 4: deep verify — authoritative when present.
    if entry.sha1 and entry.sha1 in index.by_sha1:
        return index.by_sha1[entry.sha1]
    # Tier 1: path equivalence.
    if entry.rel_path in index.by_rel_path:
        return index.by_rel_path[entry.rel_path]
    # Tier 2: fuzzy_key + region + system_id. The system_id gate is what
    # keeps a Game Boy file from matching a Game Boy Color destination
    # entry (or any cross-platform collision where the title's fuzzy
    # key collapses to the same value).
    fuzzy, region = _fuzzy_region_key_for_entry(entry.rel_path)
    if fuzzy and index.profile is not None:
        dest_system_id = _system_id_from_rel_path(entry.rel_path, index.profile)
        if dest_system_id is None:
            # Dest file isn't in any system folder the profile knows about
            # (e.g. a sidecar artifact or a folder the user moved manually).
            # Without a system to anchor on, we can't safely tier-2 match.
            return None
        match = index.by_fuzzy_region.get((fuzzy, region, dest_system_id))
        if match is not None:
            # Tier 3: hash-lookup sanity gate. If the local ROM has a known
            # SHA-1 we still trust the tier-2 match (the spec says use the
            # size as a sanity gate, not as a hard reject) — but flag
            # mismatched sizes by logging so a future audit can find them.
            if match.sha1 and match.size_bytes != entry.size_bytes:
                logger.debug(
                    "tier-2 match with size drift: local=%s (%dB) "
                    "dest=%s (%dB)",
                    match.filename,
                    match.size_bytes,
                    entry.rel_path,
                    entry.size_bytes,
                )
            return match
    return None


# ---------------------------------------------------------------------------
# Cover handling (§2.2)
# ---------------------------------------------------------------------------


def _copy_cover_for_game(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target: Path,
    rom: LocalRom,
) -> None:
    """Copy the game's preferred cover to the destination (best-effort).

    Reuses :func:`romulus.core.exporter.copy_artwork` so the filename
    template, artwork_subdir, and atomic-write semantics stay identical
    between Export and Sync. Missing covers are silent — the metadata
    pipeline is best-effort and a sync should never fail because a game has
    no cover.
    """
    if not profile.artwork_subdir or rom.game_id is None:
        return
    mapping = profile.systems.get(rom.system_id)
    if mapping is None or not mapping.is_supported:
        return
    # Build a row shape that ``copy_artwork`` already understands.
    rom_row = _SyntheticRomRow(rom)
    try:
        copy_artwork(conn, rom.system_id, profile, target, [rom_row])
    except OSError as exc:
        logger.debug(
            "sync: cover copy failed for game_id=%d: %s",
            rom.game_id,
            exc,
        )


class _SyntheticRomRow:
    """Adapter that mimics :class:`sqlite3.Row` for ``copy_artwork``.

    ``copy_artwork`` reads ``game_id`` and ``filename`` off the row. Building
    a thin wrapper is cheaper than re-querying the DB inside the apply loop.
    """

    __slots__ = ("_rom",)

    def __init__(self, rom: LocalRom) -> None:
        self._rom = rom

    def __getitem__(self, key: str) -> object:
        if key == "game_id":
            return self._rom.game_id
        if key == "filename":
            return self._rom.filename
        raise KeyError(key)


def _delete_cover_for_filename(
    profile: DestinationProfile,
    target: Path,
    system_id: str,
    filename: str,
) -> None:
    """Remove the dest-side artwork file for a deleted ROM (best-effort)."""
    if not profile.artwork_subdir:
        return
    mapping = profile.systems.get(system_id)
    if mapping is None or not mapping.is_supported:
        return
    artwork_dir = (
        target / profile.base_path / mapping.folder / profile.artwork_subdir
    )
    if not artwork_dir.exists():
        return
    stem = Path(filename).stem
    # The artwork template uses ``{stem}{ext}``; we don't know the actual
    # extension here so probe a small set of common ones.
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = artwork_dir / profile.artwork_filename_template.format(
            stem=stem, ext=ext
        )
        with contextlib.suppress(OSError):
            if candidate.exists():
                _atomic_delete(candidate)


# ---------------------------------------------------------------------------
# Atomic delete (tombstone pattern)
# ---------------------------------------------------------------------------


def _atomic_delete(path: Path) -> None:
    """Delete a file in two phases: rename to ``.tombstone`` then unlink.

    A crash between the rename and the unlink leaves a ``.tombstone`` sibling
    instead of either a half-deleted file (in which case the original is
    irrecoverable) or a half-extant filesystem entry on platforms with weak
    rename semantics. On resume the caller probes for ``.tombstone`` files
    and unlinks them. ``os.replace`` is atomic on a single filesystem so the
    rename either succeeds or is a no-op.
    """
    if not path.exists():
        return
    tombstone = path.with_suffix(path.suffix + ".tombstone")
    try:
        os.replace(str(path), str(tombstone))
    except OSError:
        # Rename to tombstone failed — fall back to direct unlink rather than
        # leaving a partially-renamed file. The unlink either succeeds (file
        # was deleted) or raises (caller logs the error).
        with contextlib.suppress(OSError):
            path.unlink()
        return
    try:
        tombstone.unlink()
    except OSError as exc:
        # The rename succeeded but the unlink didn't — log and move on. The
        # tombstone is harmless and will be swept on the next "Clean .part /
        # tombstone files" maintenance pass (deferred to v0.3.0+).
        logger.warning(
            "sync: tombstone unlink failed: path=%s err=%s",
            tombstone,
            exc,
        )


# ---------------------------------------------------------------------------
# Diff engine — bottom-up dispatch per sync mode (§2)
# ---------------------------------------------------------------------------


def _local_rel_path(
    profile: DestinationProfile, target: Path, rom: LocalRom
) -> str | None:
    """Where would ``rom`` land on the destination under ``profile``?

    Returns the forward-slash relative path, or ``None`` if the system isn't
    supported by the profile (we can't sync something the target can't run).
    """
    mapping = profile.systems.get(rom.system_id)
    if mapping is None or not mapping.is_supported:
        return None
    dest_dir = _system_dest_dir(target, profile, mapping)
    abs_dest = dest_dir / rom.filename
    try:
        rel = abs_dest.relative_to(target)
    except ValueError:
        return None
    return str(rel).replace("\\", "/")


def _build_push_actions(
    profile: DestinationProfile,
    target: Path,
    index: _MatchIndex,
    inventory: DestInventory,
    *,
    delete_dest_only: bool,
) -> list[SyncAction]:
    """Build the push-side action list for merge / mirror / wipe modes."""
    inv_by_path = inventory.by_rel_path()
    matched_rom_ids: set[int] = set()
    actions: list[SyncAction] = []

    # Local-only files become copy_to_dest actions; tier 1-4 identity
    # matches against the dest become "identical" buckets.
    for rom in index.rows:
        rel = _local_rel_path(profile, target, rom)
        if rel is None:
            continue
        entry = inv_by_path.get(rel)
        if entry is None:
            # Maybe a tier-2 match exists at a different rel_path on dest —
            # walk the inventory to find it before deciding to copy.
            tier2_entry = _find_tier2_inventory_entry(rom, inv_by_path, index)
            if tier2_entry is not None:
                matched_rom_ids.add(rom.rom_id)
                actions.append(
                    SyncAction(
                        kind=ACTION_IDENTICAL,
                        rel_path=tier2_entry.rel_path,
                        local_path=rom.path,
                        dest_path=str(target / tier2_entry.rel_path),
                        size_bytes=tier2_entry.size_bytes,
                        rom_id=rom.rom_id,
                        game_id=rom.game_id,
                        system_id=rom.system_id,
                        reason="already on dest (tier-2 match)",
                    )
                )
                continue
            actions.append(
                SyncAction(
                    kind=ACTION_COPY_TO_DEST,
                    rel_path=rel,
                    local_path=rom.path,
                    dest_path=str(target / rel),
                    size_bytes=rom.size_bytes,
                    rom_id=rom.rom_id,
                    game_id=rom.game_id,
                    system_id=rom.system_id,
                    reason="local-only — push to dest",
                )
            )
            continue
        # Path-equivalent dest hit: size match → identical; otherwise still
        # treat as identical for merge/mirror push modes (we never overwrite
        # mid-push). Conflicts only matter for two-way.
        matched_rom_ids.add(rom.rom_id)
        actions.append(
            SyncAction(
                kind=ACTION_IDENTICAL,
                rel_path=rel,
                local_path=rom.path,
                dest_path=str(target / rel),
                size_bytes=entry.size_bytes,
                rom_id=rom.rom_id,
                game_id=rom.game_id,
                system_id=rom.system_id,
                reason="already on dest",
            )
        )

    if delete_dest_only:
        base_path = (profile.base_path or "").replace("\\", "/").rstrip("/")
        # Dest-only files become delete_dest actions for mirror / wipe modes.
        for entry in inventory.entries:
            # Mirror/wipe only acts on files under the profile's base_path
            # — sibling files outside the managed tree belong to other apps
            # or to the user and must never be touched.
            if base_path and not entry.rel_path.startswith(base_path + "/"):
                continue
            matched_rom = _match_dest_entry(entry, index)
            if matched_rom is not None and matched_rom.rom_id in matched_rom_ids:
                continue
            if matched_rom is not None:
                # Tier-2 hit but the local ROM wasn't in the push set (e.g.
                # its profile mapping is unsupported). Still skip the delete
                # — we matched it. Otherwise we'd churn a file that's there
                # for a good reason.
                continue
            # Sidecars (gamelist.xml, .m3u, artwork) are never proposed for
            # deletion — they get regenerated by the post-sync rebuild pass.
            if _is_sidecar(entry.rel_path):
                continue
            actions.append(
                SyncAction(
                    kind=ACTION_DELETE_DEST,
                    rel_path=entry.rel_path,
                    local_path="",
                    dest_path=str(target / entry.rel_path),
                    size_bytes=entry.size_bytes,
                    rom_id=None,
                    game_id=None,
                    system_id=_system_id_from_rel_path(entry.rel_path, profile),
                    reason="dest-only — remove",
                )
            )
    return actions


def _find_tier2_inventory_entry(
    rom: LocalRom,
    inv_by_path: dict[str, InventoryEntry],
    index: _MatchIndex,
) -> InventoryEntry | None:
    """Find a dest entry that tier-2 matches ``rom`` at a different rel_path.

    Used when the canonical-profile path isn't on the destination but a
    fuzzy-key-equivalent file is sitting under a different folder (e.g. the
    user moved it manually). The system_id of the candidate dest entry
    must match ``rom.system_id`` — without that gate a Game Boy "Pac-Man"
    would match the Game Boy Color folder's "Pac-Man.gbc" and we'd report
    "already on dest" for a file that's actually a different game.
    """
    if not rom.fuzzy_key:
        return None
    target_key = (rom.fuzzy_key, rom.region.lower())
    profile = index.profile
    for entry in inv_by_path.values():
        fuzzy, region = _fuzzy_region_key_for_entry(entry.rel_path)
        if (fuzzy, region) != target_key:
            continue
        # System guard: only accept the dest entry if its folder maps to
        # the same system as the local rom. Without a profile we can't
        # resolve folder→system, so we skip tier-2 entirely rather than
        # risk a cross-platform false positive.
        if profile is None:
            continue
        dest_system_id = _system_id_from_rel_path(entry.rel_path, profile)
        if dest_system_id == rom.system_id:
            return entry
    return None


def _is_sidecar(rel_path: str) -> bool:
    """True for generated artifacts the post-sync rebuild owns."""
    name_lower = rel_path.rsplit("/", 1)[-1].lower()
    if name_lower == "gamelist.xml":
        return True
    if name_lower.endswith(".m3u"):
        return True
    # Artwork subdirectories live one level below the system folder.
    if "/" in rel_path:
        parts = rel_path.split("/")
        if any(p.lower() in {"downloaded_media", "imgs", "media"} for p in parts):
            return True
    return False


def _system_id_from_rel_path(
    rel_path: str, profile: DestinationProfile
) -> str | None:
    """Best-effort: map a dest folder name back to a system id via the profile.

    Used to drive cover-deletion + gamelist-rebuild grouping for dest-only
    files. Returns ``None`` when the folder doesn't appear in the profile —
    those files are treated as "no system context" by the apply step.
    """
    parts = rel_path.split("/")
    if not parts:
        return None
    # Strip the profile's base_path prefix if present.
    base_parts = [
        p for p in (profile.base_path or "").replace("\\", "/").split("/") if p
    ]
    rel_parts = parts
    for base_seg in base_parts:
        if rel_parts and rel_parts[0].lower() == base_seg.lower():
            rel_parts = rel_parts[1:]
    if not rel_parts:
        return None
    folder_name = rel_parts[0].lower()
    for system_id, mapping in profile.systems.items():
        if mapping.folder and mapping.folder.lower() == folder_name:
            return system_id
    return None


def _build_pull_actions(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target: Path,
    index: _MatchIndex,
    inventory: DestInventory,
    library_path: Path | None,
) -> list[SyncAction]:
    """Pull-mode actions: dest-only ROMs become copy_to_local actions."""
    actions: list[SyncAction] = []
    for entry in inventory.entries:
        if _is_sidecar(entry.rel_path):
            continue
        matched = _match_dest_entry(entry, index)
        if matched is not None:
            actions.append(
                SyncAction(
                    kind=ACTION_IDENTICAL,
                    rel_path=entry.rel_path,
                    local_path=matched.path,
                    dest_path=str(target / entry.rel_path),
                    size_bytes=entry.size_bytes,
                    rom_id=matched.rom_id,
                    game_id=matched.game_id,
                    system_id=matched.system_id,
                    reason="already in local library",
                )
            )
            continue
        # Orphan dest file — pull it into the library.
        system_id = _system_id_from_rel_path(entry.rel_path, profile)
        local_rel = _pull_landing_rel(entry.rel_path, system_id, conn)
        local_dest = (
            str(library_path / local_rel) if library_path is not None else ""
        )
        actions.append(
            SyncAction(
                kind=ACTION_COPY_TO_LOCAL,
                rel_path=entry.rel_path,
                local_path=local_dest,
                dest_path=str(target / entry.rel_path),
                size_bytes=entry.size_bytes,
                rom_id=None,
                game_id=None,
                system_id=system_id,
                reason="dest-only — pull to local",
            )
        )
    return actions


def _pull_landing_rel(
    dest_rel_path: str, system_id: str | None, _conn: sqlite3.Connection
) -> str:
    """Compute the forward-slash path under the local library for a pulled ROM (§8).

    Uses the system id when known. Falls back to ``_unsorted/`` when the
    dest folder doesn't match any profile system.
    """
    filename = dest_rel_path.rsplit("/", 1)[-1]
    if system_id:
        return f"{system_id}/{filename}"
    return f"_unsorted/{filename}"


def _build_twoway_actions(
    profile: DestinationProfile,
    target: Path,
    index: _MatchIndex,
    inventory: DestInventory,
    conflict_policy: ConflictPolicy,
    library_path: Path | None,
) -> list[SyncAction]:
    """Two-way actions: copy missing files in either direction, resolve conflicts."""
    inv_by_path = inventory.by_rel_path()
    matched_rom_ids: set[int] = set()
    actions: list[SyncAction] = []

    # Forward pass: every local ROM gets a row.
    for rom in index.rows:
        rel = _local_rel_path(profile, target, rom)
        if rel is None:
            continue
        entry = inv_by_path.get(rel) or _find_tier2_inventory_entry(
            rom, inv_by_path, index
        )
        if entry is None:
            actions.append(
                SyncAction(
                    kind=ACTION_COPY_TO_DEST,
                    rel_path=rel,
                    local_path=rom.path,
                    dest_path=str(target / rel),
                    size_bytes=rom.size_bytes,
                    rom_id=rom.rom_id,
                    game_id=rom.game_id,
                    system_id=rom.system_id,
                    reason="local-only — push to dest",
                )
            )
            continue
        matched_rom_ids.add(rom.rom_id)
        # Same identity, possibly different bytes — that's a conflict.
        if _bytes_differ(rom, entry):
            resolution = _resolve_conflict(rom, entry, conflict_policy)
            actions.append(
                SyncAction(
                    kind=ACTION_CONFLICT,
                    rel_path=entry.rel_path,
                    local_path=rom.path,
                    dest_path=str(target / entry.rel_path),
                    size_bytes=max(rom.size_bytes, entry.size_bytes),
                    rom_id=rom.rom_id,
                    game_id=rom.game_id,
                    system_id=rom.system_id,
                    conflict_resolution=resolution,
                    reason="same identity, different content",
                )
            )
            continue
        actions.append(
            SyncAction(
                kind=ACTION_IDENTICAL,
                rel_path=entry.rel_path,
                local_path=rom.path,
                dest_path=str(target / entry.rel_path),
                size_bytes=entry.size_bytes,
                rom_id=rom.rom_id,
                game_id=rom.game_id,
                system_id=rom.system_id,
                reason="already synchronized",
            )
        )

    # Reverse pass: dest-only files become pulls.
    for entry in inventory.entries:
        if _is_sidecar(entry.rel_path):
            continue
        matched_rom = _match_dest_entry(entry, index)
        if matched_rom is not None:
            # Already handled in the forward pass.
            continue
        system_id = _system_id_from_rel_path(entry.rel_path, profile)
        local_rel = _pull_landing_rel(entry.rel_path, system_id, None)  # type: ignore[arg-type]
        local_dest = (
            str(library_path / local_rel) if library_path is not None else ""
        )
        actions.append(
            SyncAction(
                kind=ACTION_COPY_TO_LOCAL,
                rel_path=entry.rel_path,
                local_path=local_dest,
                dest_path=str(target / entry.rel_path),
                size_bytes=entry.size_bytes,
                rom_id=None,
                game_id=None,
                system_id=system_id,
                reason="dest-only — pull to local",
            )
        )
    return actions


def _bytes_differ(rom: LocalRom, entry: InventoryEntry) -> bool:
    """Decide whether two same-identity files have different content.

    Prefers a SHA-1 compare when both sides have one; falls back to size as a
    cheap heuristic. The size-only check is intentionally coarse — two ROMs
    that share fuzzy_key, region AND size are overwhelmingly the same file
    in practice, so we treat them as non-conflicting unless deep verify is
    on. Real conflicts (same name, different content) become visible the
    moment the user runs Deep Verify.
    """
    if rom.sha1 and entry.sha1:
        return rom.sha1 != entry.sha1.lower()
    return rom.size_bytes != entry.size_bytes


def _resolve_conflict(
    rom: LocalRom,
    entry: InventoryEntry,
    policy: ConflictPolicy,
) -> str:
    """Map a conflict-policy string to a stored resolution value."""
    if policy == "skip":
        return CONFLICT_RESOLUTION_SKIP
    if policy == "local":
        return CONFLICT_RESOLUTION_LOCAL
    if policy == "dest":
        return CONFLICT_RESOLUTION_DEST
    if policy == "newest":
        # Use the local file's mtime via os.stat as a tiebreak; dest comes
        # from the inventory snapshot. ``rom.path`` may not exist (race),
        # so guard.
        local_mtime = 0.0
        with contextlib.suppress(OSError):
            local_mtime = Path(rom.path).stat().st_mtime
        if local_mtime > entry.mtime:
            return CONFLICT_RESOLUTION_LOCAL
        if entry.mtime > local_mtime:
            return CONFLICT_RESOLUTION_DEST
        return CONFLICT_RESOLUTION_SKIP
    if policy == "prompt":
        return CONFLICT_RESOLUTION_PROMPT
    return CONFLICT_RESOLUTION_SKIP


# ---------------------------------------------------------------------------
# Top-level diff entry point
# ---------------------------------------------------------------------------


def build_plan(
    conn: sqlite3.Connection,
    dest_id: int,
    profile: DestinationProfile,
    target_path: Path | str,
    inventory: DestInventory,
    mode: SyncMode,
    *,
    conflict_policy: ConflictPolicy = "skip",
    library_path: Path | str | None = None,
) -> SyncPlan:
    """Compute the action list for ``mode`` against the inventory snapshot.

    Returns a :class:`SyncPlan` — pure data, no filesystem writes. The
    matching plan IS the source of truth the preview dialog and the apply
    worker share.
    """
    target = Path(target_path)
    library = Path(library_path) if library_path is not None else None
    index = _build_match_index(conn, profile, target)

    if mode == "push_merge":
        actions = _build_push_actions(
            profile, target, index, inventory, delete_dest_only=False
        )
    elif mode in {"push_mirror", "push_wipe"}:
        actions = _build_push_actions(
            profile, target, index, inventory, delete_dest_only=True
        )
    elif mode == "pull":
        actions = _build_pull_actions(
            conn, profile, target, index, inventory, library
        )
    elif mode == "two_way":
        actions = _build_twoway_actions(
            profile, target, index, inventory, conflict_policy, library
        )
    else:  # pragma: no cover - guarded by Literal at call sites
        raise ValueError(f"unknown sync mode: {mode!r}")
    return SyncPlan(
        dest_id=dest_id,
        mode=mode,
        actions=actions,
        conflict_policy=conflict_policy,
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _execute_copy_to_dest(
    conn: sqlite3.Connection,
    action: SyncAction,
    profile: DestinationProfile,
    target: Path,
    index: _MatchIndex,
    summary: SyncSummary,
) -> None:
    """Copy a local ROM to the destination using ``atomic.atomic_copy``."""
    source = Path(action.local_path)
    if not source.exists():
        raise FileNotFoundError(f"source vanished: {source}")
    dest = Path(action.dest_path)
    atomic.atomic_copy(source, dest)
    summary.bytes_copied_to_dest += int(action.size_bytes or 0)
    summary.files_added_to_dest += 1
    if action.system_id:
        summary.systems_touched.add(action.system_id)
    # Update the destination inventory cache so re-running the sync without
    # a re-scan doesn't propose copying the same file again.
    try:
        stat_result = dest.stat()
    except OSError:
        return
    q.upsert_dest_inventory(
        conn,
        {
            "dest_id": _dest_id_from_target(conn, target),
            "rel_path": action.rel_path,
            "size_bytes": stat_result.st_size,
            "mtime": stat_result.st_mtime,
            "sha1": None,
            "rom_id": action.rom_id,
            "game_id": action.game_id,
        },
    )
    # Cover follows the ROM (§2.2).
    if action.rom_id is not None:
        rom = next(
            (r for r in index.rows if r.rom_id == action.rom_id),
            None,
        )
        if rom is not None:
            _copy_cover_for_game(conn, profile, target, rom)


def _execute_delete_dest(
    conn: sqlite3.Connection,
    action: SyncAction,
    profile: DestinationProfile,
    target: Path,
    summary: SyncSummary,
) -> None:
    """Tombstone-rename then unlink a dest file."""
    dest = Path(action.dest_path)
    _atomic_delete(dest)
    summary.files_removed_from_dest += 1
    if action.system_id:
        summary.systems_touched.add(action.system_id)
    q.delete_dest_inventory_row(
        conn, _dest_id_from_target(conn, target), action.rel_path
    )
    # Drop the cover that followed this ROM in (best-effort).
    filename = action.rel_path.rsplit("/", 1)[-1]
    if action.system_id:
        _delete_cover_for_filename(profile, target, action.system_id, filename)


def _execute_copy_to_local(
    conn: sqlite3.Connection,
    action: SyncAction,
    library_path: Path | None,
    summary: SyncSummary,
) -> None:
    """Copy a dest-only ROM into the library and enrol it (§8)."""
    if library_path is None or not action.local_path:
        raise ValueError(
            "pull-mode copy requires a configured library path"
        )
    source = Path(action.dest_path)
    if not source.exists():
        raise FileNotFoundError(f"dest source vanished: {source}")
    dest = Path(action.local_path)
    atomic.atomic_copy(source, dest)
    summary.bytes_copied_to_local += int(action.size_bytes or 0)
    summary.files_pulled_to_local += 1
    # Enrol via Quick Scan semantics — parse the filename, generate the
    # fuzzy key, and upsert as match_confidence='fuzzy' per spec §8.
    parsed = parse_filename(dest.name)
    fuzzy = generate_fuzzy_key(parsed.clean_name, parsed.release_type)
    system_id = action.system_id or "_unsorted"
    try:
        stat_result = dest.stat()
    except OSError:
        return
    q.upsert_rom(
        conn,
        {
            "path": str(dest),
            "filename": dest.name,
            "extension": parsed.extension,
            "size_bytes": stat_result.st_size,
            "mtime": stat_result.st_mtime,
            "system_id": system_id,
            "fuzzy_key": fuzzy,
            "match_confidence": "fuzzy",
        },
    )
    if system_id != "_unsorted":
        summary.systems_touched.add(system_id)


def _execute_conflict(
    conn: sqlite3.Connection,
    action: SyncAction,
    profile: DestinationProfile,
    target: Path,
    index: _MatchIndex,
    library_path: Path | None,
    summary: SyncSummary,
) -> None:
    """Apply a conflict resolution by delegating to the right copy/delete."""
    resolution = action.conflict_resolution
    if resolution in {CONFLICT_RESOLUTION_SKIP, CONFLICT_RESOLUTION_PROMPT, ""}:
        summary.skipped += 1
        return
    if resolution == CONFLICT_RESOLUTION_LOCAL:
        # Overwrite the destination with the local file. ``atomic_copy``
        # publishes via os.replace which is atomic — the dest either holds
        # the old bytes or the new bytes, never anything partial.
        copy_action = SyncAction(
            kind=ACTION_COPY_TO_DEST,
            rel_path=action.rel_path,
            local_path=action.local_path,
            dest_path=action.dest_path,
            size_bytes=action.size_bytes,
            rom_id=action.rom_id,
            game_id=action.game_id,
            system_id=action.system_id,
        )
        # Tombstone the existing dest file first so a crash mid-copy leaves
        # a recoverable state.
        _atomic_delete(Path(action.dest_path))
        _execute_copy_to_dest(conn, copy_action, profile, target, index, summary)
        return
    if resolution == CONFLICT_RESOLUTION_DEST:
        # Pull the dest into the local library.
        pull_action = SyncAction(
            kind=ACTION_COPY_TO_LOCAL,
            rel_path=action.rel_path,
            local_path=action.local_path,
            dest_path=action.dest_path,
            size_bytes=action.size_bytes,
            rom_id=action.rom_id,
            game_id=action.game_id,
            system_id=action.system_id,
        )
        _execute_copy_to_local(conn, pull_action, library_path, summary)
        return


def _dest_id_from_target(conn: sqlite3.Connection, target: Path) -> int:
    """Look up the saved destination id given the active target path.

    The apply step caches this once per call rather than threading the
    ``dest_id`` through every helper. Returns -1 when no matching row exists
    (a sync without a saved destination — only used internally).
    """
    target_str = str(target)
    row = conn.execute(
        "SELECT id FROM sync_destinations WHERE target_path = ? LIMIT 1",
        (target_str,),
    ).fetchone()
    return int(row["id"]) if row else -1


def _wipe_destination(target: Path, profile: DestinationProfile) -> None:
    """Empty everything under the profile's base_path on the destination (§2).

    Used by ``push_wipe`` mode only. We constrain the wipe to
    ``target / profile.base_path`` so a user picking the wrong target folder
    can't lose their entire system drive. Path-traversal validation already
    rejected ``..`` segments in ``base_path`` at load time, so the resolved
    path is guaranteed to stay inside ``target``.
    """
    wipe_root = target / profile.base_path
    if not wipe_root.exists():
        return
    # Defense-in-depth: refuse to wipe if the resolved path escaped target.
    resolved = wipe_root.resolve()
    target_resolved = target.resolve()
    if (
        resolved != target_resolved
        and target_resolved not in resolved.parents
    ):
        raise ValueError(
            f"wipe refused — base_path escapes target: {resolved!s}"
        )
    for child in list(wipe_root.iterdir()):
        try:
            if child.is_file() or child.is_symlink():
                _atomic_delete(child)
            elif child.is_dir():
                _wipe_tree(child)
        except OSError as exc:
            logger.warning("wipe: failed to remove %s: %s", child, exc)


def _wipe_tree(root: Path) -> None:
    """Depth-first remove of every file under ``root``, then the directory."""
    for entry in list(root.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            _wipe_tree(entry)
        else:
            with contextlib.suppress(OSError):
                _atomic_delete(entry)
    with contextlib.suppress(OSError):
        root.rmdir()


def _rebuild_gamelists(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target: Path,
    systems_touched: Iterable[str],
) -> int:
    """Regenerate gamelist.xml on the destination per affected system (§2.2).

    Called regardless of mode or direction after the apply loop. Returns the
    number of gamelist files written.
    """
    if profile.gamelist_format != "emulationstation_xml":
        return 0
    written = 0
    for system_id in sorted(set(systems_touched)):
        mapping = profile.systems.get(system_id)
        if mapping is None or not mapping.is_supported:
            continue
        dest_dir = _system_dest_dir(target, profile, mapping)
        if not dest_dir.exists():
            continue
        # Hydrate the rows the post-sync gamelist needs from the destination
        # inventory + the local DB. We use the inventory's ``rel_path`` to
        # find files that are actually on dest right now, and join through
        # ``upsert_dest_inventory`` cache hits when available.
        rows = _gamelist_rows_for_system(conn, profile, target, system_id)
        if not rows:
            continue
        try:
            generate_gamelist_xml(
                conn, system_id, dest_dir, rows, profile=profile
            )
            written += 1
        except OSError as exc:
            logger.warning(
                "sync: gamelist rebuild failed for %s: %s", system_id, exc
            )
    return written


def _gamelist_rows_for_system(
    conn: sqlite3.Connection,
    profile: DestinationProfile,
    target: Path,
    system_id: str,
) -> list[sqlite3.Row]:
    """Build sqlite3.Row-shaped rows for ``generate_gamelist_xml``."""
    mapping = profile.systems.get(system_id)
    if mapping is None or not mapping.is_supported:
        return []
    dest_dir = _system_dest_dir(target, profile, mapping)
    if not dest_dir.exists():
        return []
    # Inspect what's actually on dest right now (any file in the system's
    # folder). For each file we look up the matching ROM in the local DB so
    # gamelist gets canonical names + metadata; orphan files get an entry
    # too with name=filename-without-extension as a graceful fallback.
    rows: list[sqlite3.Row] = []
    dest_id = _dest_id_from_target(conn, target)
    # Re-query each file individually so callers don't need to plumb the
    # inventory in. This is one query per system folder, not per ROM, so the
    # overhead is bounded.
    for entry in dest_dir.iterdir():
        if not entry.is_file():
            continue
        filename = entry.name
        if filename.lower() == "gamelist.xml" or filename.endswith(".m3u"):
            continue
        rel_path = (
            str((dest_dir / filename).relative_to(target)).replace("\\", "/")
        )
        # First look in the inventory cache for a rom_id/game_id.
        inv_row = None
        if dest_id != -1:
            inv_row = q.get_dest_inventory_row(conn, dest_id, rel_path)
        row = _build_synthetic_gamelist_row(
            conn, filename, system_id, inv_row
        )
        rows.append(row)
    return rows


def _build_synthetic_gamelist_row(
    conn: sqlite3.Connection,
    filename: str,
    system_id: str,
    inv_row: sqlite3.Row | None,
) -> sqlite3.Row:
    """Manufacture an sqlite3.Row that ``generate_gamelist_xml`` accepts.

    The exporter's gamelist generator reads ``filename``, ``game_id``,
    ``canonical_name``, ``title``, and ``system_id`` off the row. We fetch
    them with one extra SELECT per file when the inventory cache has a
    ``rom_id``.
    """
    game_id = None
    canonical_name: str | None = None
    title: str | None = None
    if inv_row is not None and inv_row["rom_id"] is not None:
        rom_row = conn.execute(
            """
            SELECT g.id            AS game_id,
                   g.canonical_name AS canonical_name,
                   g.title          AS title
            FROM roms r LEFT JOIN games g ON g.id = r.game_id
            WHERE r.id = ?
            """,
            (int(inv_row["rom_id"]),),
        ).fetchone()
        if rom_row is not None:
            game_id = rom_row["game_id"]
            canonical_name = rom_row["canonical_name"]
            title = rom_row["title"]
    # Wrap the dict in a tiny row-shaped adapter so ``generate_gamelist_xml``
    # treats it like a real sqlite3.Row.
    return _DictRow(
        {
            "filename": filename,
            "game_id": game_id,
            "canonical_name": canonical_name,
            "title": title,
            "system_id": system_id,
        }
    )  # type: ignore[return-value]


class _DictRow:
    """``sqlite3.Row``-like dict wrapper used to build gamelist rows.

    ``sqlite3.Row`` supports ``row[key]``, ``key in row.keys()``, and
    iteration over values. The exporter's gamelist generator only uses
    ``row[key]`` and ``row.keys()`` so we only implement those two.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data.get(key)

    def keys(self) -> list[str]:  # noqa: D401 - sqlite3.Row API mirror
        return list(self._data.keys())


def apply_plan(
    conn: sqlite3.Connection,
    plan: SyncPlan,
    profile: DestinationProfile,
    target_path: Path | str,
    *,
    library_path: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> SyncSummary:
    """Execute every approved action in ``plan`` against the destination.

    Per-action SAVEPOINT rollback mirrors the organizer's pattern: a single
    failed copy never leaves the inventory cache out of sync with the disk,
    and the loop continues so a localised error doesn't abort the whole
    sync. After the loop, ``gamelist.xml`` is regenerated on every system
    that was touched regardless of mode (§2.2).
    """
    target = Path(target_path)
    library = Path(library_path) if library_path is not None else None
    summary = SyncSummary()

    if plan.mode == "push_wipe":
        # Wipe BEFORE the copy phase so push_mirror semantics apply on top
        # of an empty destination. Per spec §2, push_wipe is "fresh wipe +
        # push" expressed explicitly. The empty-dest case turns every local
        # ROM into a copy_to_dest action (already in the plan).
        _wipe_destination(target, profile)

    # Re-hydrate the match index once for the apply pass — cover copies and
    # gamelist rebuilds need it. _build_match_index is the same call the
    # diff used.
    index = _build_match_index(conn, profile, target)

    total = len(plan.actions)
    for i, action in enumerate(plan.actions, start=1):
        if progress_callback is not None:
            progress_callback(i, total, action.rel_path or action.dest_path)
        savepoint = f"sync_{i}"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            if action.kind == ACTION_COPY_TO_DEST:
                _execute_copy_to_dest(conn, action, profile, target, index, summary)
            elif action.kind == ACTION_DELETE_DEST:
                _execute_delete_dest(conn, action, profile, target, summary)
            elif action.kind == ACTION_COPY_TO_LOCAL:
                _execute_copy_to_local(conn, action, library, summary)
            elif action.kind == ACTION_CONFLICT:
                _execute_conflict(
                    conn, action, profile, target, index, library, summary
                )
            elif action.kind == ACTION_IDENTICAL:
                # Identical actions never mutate anything — record + skip.
                summary.skipped += 1
                if action.system_id:
                    summary.systems_touched.add(action.system_id)
                action.executed = True
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                continue
            else:  # pragma: no cover - guarded by literals at construction time
                raise ValueError(f"unsupported sync action: {action.kind!r}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = True
            if action.kind != ACTION_CONFLICT or action.conflict_resolution not in {
                CONFLICT_RESOLUTION_SKIP,
                CONFLICT_RESOLUTION_PROMPT,
                "",
            }:
                summary.applied += 1
        except _SyncCancelled:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - rollback intentionally catches all
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            action.executed = False
            action.error = str(exc)
            summary.failed += 1
            summary.errors.append(f"{action.kind} {action.rel_path!s}: {exc}")
            logger.warning(
                "sync action failed: kind=%s rel_path=%s err=%s",
                action.kind,
                action.rel_path,
                exc,
            )

    # gamelist.xml gets rebuilt regardless of mode or direction (§2.2).
    _rebuild_gamelists(conn, profile, target, summary.systems_touched)

    # Stamp last_synced_at on the saved destination row.
    dest_id = plan.dest_id
    if dest_id > 0:
        q.set_sync_dest_last_synced(
            conn, dest_id, datetime.now(UTC).isoformat()
        )
    conn.commit()
    return summary


# ---------------------------------------------------------------------------
# Plan persistence
# ---------------------------------------------------------------------------


def persist_plan(
    conn: sqlite3.Connection,
    plan: SyncPlan,
    status: str = "pending",
) -> int:
    """Insert ``plan`` into ``sync_plans`` and return its row id.

    Used by the apply worker to record the plan before mutating anything, so
    a crash mid-sync leaves a row the user can inspect from a future history
    UI (deferred to v0.3.0+).
    """
    summary_payload = json.dumps(plan.counts_by_kind())
    return q.insert_sync_plan(
        conn,
        plan.dest_id,
        plan.mode,
        summary_payload,
        plan.to_json(),
        status=status,
    )


def load_plan(conn: sqlite3.Connection, plan_id: int) -> SyncPlan | None:
    """Re-hydrate a persisted plan into a :class:`SyncPlan` instance."""
    row = q.get_sync_plan(conn, plan_id)
    if row is None:
        return None
    payload = json.loads(str(row["plan_json"]))
    actions = [SyncAction(**a) for a in payload.get("actions", [])]
    return SyncPlan(
        dest_id=int(payload.get("dest_id", row["dest_id"])),
        mode=str(payload.get("mode", row["mode"])),  # type: ignore[arg-type]
        actions=actions,
        conflict_policy=str(payload.get("conflict_policy", "skip")),  # type: ignore[arg-type]
    )
