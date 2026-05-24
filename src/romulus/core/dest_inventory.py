"""Destination inventory — walk a target, cache file state, detect swaps.

This is the read side of the destination-sync engine (sync-design spec §4.2,
§4.5). Given a saved :class:`sync_destinations` row, it:

1. Walks the on-disk target (depth-capped to keep accidental
   ``C:\\Users\\…\\ROMulus\\`` scans from running away),
2. Reuses cached ``(size, mtime)`` rows from ``dest_inventory`` when the file
   hasn't changed since the previous sync — same staleness check the hash
   cache uses,
3. Optionally hashes every file when the caller passes ``deep_verify=True``
   (the "Deep verify (slow)" checkbox in the preview dialog),
4. Computes a small 32-path signature (§4.5) used to detect the case where
   the user mounted a different SD card at the same drive letter; if the
   signature drifts we clear the cache and re-scan from scratch.

The walker hands back a :class:`DestInventory` snapshot rather than mutating
the DB directly — :mod:`romulus.core.sync` does the actual diff. Splitting the
read side from the diff side makes it trivial to unit-test the cache reuse
logic without dragging in the entire identity-match pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from romulus.db import queries as q

logger = logging.getLogger(__name__)

#: Progress callback signature shared with every other worker: current,
#: total (best-effort, ``-1`` while the walker is still discovering files),
#: human-readable label.
ProgressCallback = Callable[[int, int, str], None]

#: Safety cap on how deep below the target the walker is willing to descend.
#: A poorly-typed target path (``C:\\``, ``/``) would otherwise enumerate the
#: entire drive. v0.3.0+ may expose this as a profile-level setting.
_DEFAULT_DEPTH_CAP: int = 12

#: Number of bytes streamed through SHA-1 per ``read()``. Matches
#: :data:`romulus.core.atomic._COPY_CHUNK_BYTES` so a deep-verify pass and a
#: subsequent ``atomic_copy`` share the same I/O profile.
_HASH_CHUNK_BYTES: int = 1024 * 1024

#: How many sorted ``rel_path`` strings get folded into the inventory signature
#: (§4.5). 32 is enough to make accidental collisions vanishingly rare while
#: keeping the recompute cheap.
_SIGNATURE_PATH_COUNT: int = 32


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One file on the destination, with the cache columns we care about.

    ``rel_path`` is forward-slash, relative to the destination root. ``sha1``
    is populated when the caller passed ``deep_verify=True`` OR a previous
    deep-verify cached the hash in ``dest_inventory.sha1``.

    v0.4.0: ``game_id`` removed — the strict 1:1 model anchors everything
    on ``rom_id``.
    """

    rel_path: str
    size_bytes: int
    mtime: float
    sha1: str | None = None
    rom_id: int | None = None


@dataclass(slots=True)
class DestInventory:
    """Read-side snapshot of a destination after a scan."""

    dest_id: int
    target_path: str
    entries: list[InventoryEntry] = field(default_factory=list)
    signature: str = ""
    #: True when the scan detected signature drift and the cache was cleared.
    cache_was_invalidated: bool = False
    #: Total bytes summed across :pyattr:`entries` — convenient for the UI.
    total_size_bytes: int = 0

    def by_rel_path(self) -> dict[str, InventoryEntry]:
        """Return a dict view keyed on the forward-slash relative path."""
        return {entry.rel_path: entry for entry in self.entries}


# ---------------------------------------------------------------------------
# Signature helpers (§4.5)
# ---------------------------------------------------------------------------


def compute_signature(rel_paths: list[str]) -> str:
    """SHA-1 of the first :data:`_SIGNATURE_PATH_COUNT` sorted paths.

    The hash is computed over forward-slash, NUL-separated paths so the
    bytewise representation is unambiguous across OSes. Returns an empty
    string when the destination is empty — an empty target signs to "" which
    will never compare equal to a populated signature.
    """
    if not rel_paths:
        return ""
    paths = sorted(rel_paths)[:_SIGNATURE_PATH_COUNT]
    payload = "\x00".join(paths).encode("utf-8")
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def signature_matches(previous: str | None, current: str) -> bool:
    """True when the cached signature still describes this destination.

    A previously-unknown signature (``None`` or empty) is treated as "first
    visit" — the caller treats that as "no drift, populate the signature".
    """
    if not previous:
        return True
    return previous == current


# ---------------------------------------------------------------------------
# Walk + cache reuse
# ---------------------------------------------------------------------------


def _to_relative_forward_slash(path: Path, root: Path) -> str:
    """``Path.relative_to`` is OS-native; we want forward slashes for storage."""
    rel = path.relative_to(root)
    return str(rel).replace("\\", "/")


def _sha1_file(path: Path) -> str:
    """Stream a file through SHA-1. Used by deep-verify mode."""
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _walk_target(
    root: Path,
    *,
    depth_cap: int,
) -> list[tuple[Path, str, os.stat_result]]:
    """Walk ``root`` depth-capped. Returns ``(abs_path, rel_path, stat)`` triples.

    Files whose ``stat()`` fails are skipped with a debug log entry — same
    behaviour as the main library scanner.
    """
    out: list[tuple[Path, str, os.stat_result]] = []
    root_str = str(root)
    base_depth = root_str.count(os.sep)
    for current_root, dirs, files in os.walk(root):
        depth = current_root.count(os.sep) - base_depth
        if depth >= depth_cap:
            # Stop descending: clearing ``dirs`` is the documented os.walk
            # idiom for pruning a subtree mid-walk.
            dirs[:] = []
            continue
        current_path = Path(current_root)
        for name in files:
            abs_path = current_path / name
            try:
                stat_result = abs_path.stat()
            except OSError as exc:
                logger.debug("dest_inventory stat failed: path=%s err=%s", abs_path, exc)
                continue
            rel = _to_relative_forward_slash(abs_path, root)
            out.append((abs_path, rel, stat_result))
    return out


def scan_destination(
    conn: sqlite3.Connection,
    dest_id: int,
    target_path: Path | str,
    *,
    deep_verify: bool = False,
    progress_callback: ProgressCallback | None = None,
    depth_cap: int = _DEFAULT_DEPTH_CAP,
) -> DestInventory:
    """Walk ``target_path``, refresh the inventory cache, return a snapshot.

    Cache reuse semantics:

    * A cached row with matching ``(size_bytes, mtime)`` is treated as fresh —
      its SHA-1 (if any) and ``rom_id`` / ``game_id`` are preserved.
    * Any drift in size or mtime invalidates the cached SHA-1; ``rom_id`` and
      ``game_id`` are cleared too because the file's identity may have
      changed.
    * After the walk, rows in the cache whose ``rel_path`` is no longer
      present are pruned (someone deleted the file off the destination
      between syncs).
    * If the signature has drifted (the user swapped SD cards), the cache is
      wiped before the walk so we never reuse stale rows for a different
      physical device.

    When ``deep_verify=True`` every file is rehashed even if its
    ``(size, mtime)`` matched the cache — the user explicitly asked for the
    expensive pass.
    """
    target = Path(target_path)
    if not target.exists():
        # Treat a missing target as an empty destination so the calling UI
        # can surface "destination unreachable" rather than crashing.
        return DestInventory(
            dest_id=dest_id,
            target_path=str(target),
            entries=[],
            signature="",
            cache_was_invalidated=False,
            total_size_bytes=0,
        )
    # Existing cache rows, keyed for O(1) lookup during the walk.
    cached_rows = {
        str(row["rel_path"]): row for row in q.get_dest_inventory(conn, dest_id)
    }

    walked = _walk_target(target, depth_cap=depth_cap)
    total = len(walked)
    if progress_callback is not None:
        progress_callback(0, total, "Indexing destination…")

    rel_paths = [rel for _abs, rel, _stat in walked]
    current_signature = compute_signature(rel_paths)
    dest_row = q.get_sync_destination(conn, dest_id)
    previous_signature = (
        str(dest_row["last_inventory_signature"])
        if dest_row is not None and dest_row["last_inventory_signature"]
        else None
    )
    cache_was_invalidated = False
    if (
        previous_signature is not None
        and not signature_matches(previous_signature, current_signature)
    ):
        # The user swapped the SD card. Wipe the cache before the walk so we
        # don't reuse rom_id/sha1 from the previous device.
        logger.info(
            "dest_inventory: signature drifted (previous=%s current=%s) — "
            "clearing cache for dest_id=%d",
            previous_signature[:8],
            current_signature[:8],
            dest_id,
        )
        q.clear_dest_inventory(conn, dest_id)
        cached_rows = {}
        cache_was_invalidated = True

    entries: list[InventoryEntry] = []
    total_size = 0
    for index, (abs_path, rel_path, stat_result) in enumerate(walked, start=1):
        cached = cached_rows.get(rel_path)
        size = stat_result.st_size
        mtime = stat_result.st_mtime
        sha1: str | None = None
        rom_id: int | None = None
        cached_is_fresh = (
            cached is not None
            and int(cached["size_bytes"]) == size
            and float(cached["mtime"]) == mtime
        )
        if cached_is_fresh:
            sha1 = (
                str(cached["sha1"]) if cached["sha1"] is not None else None
            )
            rom_id = (
                int(cached["rom_id"]) if cached["rom_id"] is not None else None
            )
        elif cached is not None:
            # Drift detected — the cached identity columns can't be trusted
            # for the new bytes. Drop the row so the upsert below starts
            # from a clean slate; the COALESCE in upsert_dest_inventory
            # would otherwise preserve the stale SHA-1 / rom_id / game_id.
            q.delete_dest_inventory_row(conn, dest_id, rel_path)
        if deep_verify:
            # Deep verify always recomputes the SHA-1 — that's the entire
            # point of the opt-in pass. We then upsert below so the hash is
            # cached for the next sync.
            try:
                sha1 = _sha1_file(abs_path)
            except OSError as exc:
                logger.debug(
                    "dest_inventory deep-verify failed: path=%s err=%s",
                    abs_path,
                    exc,
                )
                sha1 = None
        q.upsert_dest_inventory(
            conn,
            {
                "dest_id": dest_id,
                "rel_path": rel_path,
                "size_bytes": size,
                "mtime": mtime,
                "sha1": sha1,
                "rom_id": rom_id,
            },
        )
        entries.append(
            InventoryEntry(
                rel_path=rel_path,
                size_bytes=size,
                mtime=mtime,
                sha1=sha1,
                rom_id=rom_id,
            )
        )
        total_size += size
        if progress_callback is not None and (index % 16 == 0 or index == total):
            progress_callback(index, total, rel_path)

    # Drop rows for files that vanished off the destination since the last
    # sync. Without this the cache would grow unboundedly with old paths.
    if cached_rows:
        q.prune_dest_inventory_missing(conn, dest_id, rel_paths)

    # Stamp the signature so re-recognition works on the next pass.
    q.set_sync_dest_signature(conn, dest_id, current_signature)
    conn.commit()

    return DestInventory(
        dest_id=dest_id,
        target_path=str(target),
        entries=entries,
        signature=current_signature,
        cache_was_invalidated=cache_was_invalidated,
        total_size_bytes=total_size,
    )


def forget_cache(conn: sqlite3.Connection, dest_id: int) -> None:
    """User-visible "Forget cache" button on the destinations dropdown.

    Delegates to :func:`romulus.db.queries.clear_dest_inventory` so the same
    semantics apply: every cached inventory row is removed AND the saved
    signature is wiped so the next sync re-scans from scratch.
    """
    q.clear_dest_inventory(conn, dest_id)
    conn.commit()
