"""Reverse-direction library scrub — verify every DB row against the disk.

The Quick Scan walks the *library folder* and reconciles against the
database (with a tombstone sweep for paths not visited). This module
runs the inverse: walk the *database* and verify each ``roms`` row
still corresponds to a file on disk. The asymmetry matters because
rows can drift in ways the forward scan can't catch — e.g. a
``library_root`` mismatch that quietly excludes them from the sweep,
or a row pointing outside the current library entirely.

The scrub classifies every row into one of four buckets:

* ``missing_unflagged`` — file gone but ``missing = 0``; fix is to set
  ``missing = 1`` so the user can prune via Clean Missing.
* ``outside_root`` — ``library_root`` is set and not equal to the current
  one; row belongs to a library the user has switched away from. Fix is
  to delete (with FK-dependent cleanup + orphan-game prune).
* ``flagged_but_present`` — ``missing = 1`` but the file is back on disk;
  fix is to set ``missing = 0`` so the row rejoins the live library.
* ``drift`` — file present at the recorded path but its stored size /
  mtime have drifted from disk; fix is to clear the cached hash row
  (force Heavy Scan to re-identify) and update the stored stat values.

Apply is structured per-bucket: each bucket's actions run inside one
SAVEPOINT, committed at the end. A failure in one bucket rolls back
that bucket only — the other three commit independently. After every
``outside_root`` apply the orphan-game prune runs once so games left
dangling by the row deletes get cleaned up too.

Stat errors (PermissionError / OSError from a disconnected SMB share,
ACL denial, etc.) are NOT treated as "missing". They're recorded on
the action so the dialog can show how many rows couldn't be verified,
and those rows are excluded from the missing-unflagged bucket — better
to leave a row alone than to falsely tombstone it because the share
was offline mid-scrub.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from romulus.db import queries as q

logger = logging.getLogger(__name__)


ScrubStatus = Literal[
    "missing_unflagged",
    "outside_root",
    "flagged_but_present",
    "drift",
]


@dataclass(slots=True)
class ScrubAction:
    """One row-level mismatch surfaced by :func:`analyse`.

    The combination of ``status`` + the stored ``current_*`` fields is
    enough for :func:`apply_plan` to execute the right DB operation;
    callers shouldn't need to re-stat the file during apply.
    """

    rom_id: int
    path: str
    filename: str
    system_id: str | None
    library_root: str | None
    status: ScrubStatus
    # Stored values (from the DB row).
    stored_size: int = 0
    stored_mtime: float = 0.0
    # Disk values when status is ``drift`` (None for the other statuses).
    current_size: int | None = None
    current_mtime: float | None = None
    # Filled in by :func:`apply_plan` once execution has been attempted.
    executed: bool = False
    error: str | None = None


@dataclass(slots=True)
class ScrubPlan:
    """Result of :func:`analyse` — a snapshot of what the scrub found.

    ``rows_scanned`` is the total rows the analyse walked; ``rows_unreadable``
    counts how many of those threw on stat (typically SMB / offline drive).
    Those rows are not represented in ``actions`` — they're excluded so the
    user can re-run the scrub when the share is online.
    """

    library_root: str
    actions: list[ScrubAction] = field(default_factory=list)
    rows_scanned: int = 0
    rows_unreadable: int = 0

    def counts_by_status(self) -> dict[ScrubStatus, int]:
        """Return the action count per bucket. Empty buckets are omitted."""
        counts: dict[ScrubStatus, int] = {}
        for action in self.actions:
            counts[action.status] = counts.get(action.status, 0) + 1
        return counts


@dataclass(slots=True)
class ScrubSummary:
    """Result of :func:`apply_plan` — what actually changed on the DB."""

    flagged_missing: int = 0
    deleted_outside_root: int = 0
    untombstoned: int = 0
    drift_fixed: int = 0
    pruned_games: int = 0
    errors: list[str] = field(default_factory=list)


ProgressCallback = Callable[[int, int, str], None]


class _ScrubCancelled(Exception):  # noqa: N818 - cancel marker, not an error
    """Cooperative-cancel marker raised from a scrub progress callback."""


def _stat_or_none(
    path: str,
) -> tuple[int | None, float | None, bool, bool]:
    """Stat ``path`` and return (size, mtime, exists, unreadable).

    The unreadable flag distinguishes "file is genuinely missing" from
    "we couldn't tell because the OS raised". A row that's unreadable
    must NOT be auto-tombstoned — that's the SMB-share-offline footgun
    the design notes call out.
    """
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return None, None, False, False
    except (PermissionError, OSError):
        # Drive offline, ACL denial, SMB hiccup. Treat as "couldn't verify"
        # rather than "missing" — we don't want to flag a 40K-row library
        # missing because the share went down for two seconds mid-scrub.
        return None, None, False, True
    return st.st_size, st.st_mtime, True, False


def _drift_detected(
    stored_size: int,
    stored_mtime: float,
    disk_size: int,
    disk_mtime: float,
) -> bool:
    """Return True iff size differs or mtime drifted past the tolerance.

    Mtime tolerance matches the hasher's path/mtime/size cache key — two
    timestamps within 2s of each other are treated as the same file
    (FAT32 / SMB / archive extractions routinely re-stamp mtime within
    that band even when content didn't change).
    """
    if stored_size != disk_size:
        return True
    return abs(stored_mtime - disk_mtime) >= 2.0


def analyse(
    conn: sqlite3.Connection,
    library_root: str,
    progress_callback: ProgressCallback | None = None,
) -> ScrubPlan:
    """Walk every row in ``roms`` and classify mismatches against disk.

    ``library_root`` is the user's currently-active library path (the
    config value). Rows whose ``library_root`` column is set and not
    equal to this value land in the ``outside_root`` bucket regardless
    of whether the file still exists — they belong to a library the
    user has switched away from.

    ``progress_callback`` (optional) fires once per row with
    ``(current, total, filename)`` so a worker can drive a determinate
    progress dialog. Cancellation is cooperative — raise
    :class:`_ScrubCancelled` from the callback and it'll unwind out of
    :func:`analyse`.
    """
    rows = conn.execute(
        "SELECT id, path, filename, system_id, library_root, "
        "size_bytes, mtime, missing FROM roms"
    ).fetchall()
    total = len(rows)
    plan = ScrubPlan(library_root=library_root)
    plan.rows_scanned = total
    logger.info("scrub.analyse: scanning %d rows", total)

    library_root_norm = library_root.rstrip("\\/").rstrip()

    for idx, row in enumerate(rows, start=1):
        if progress_callback is not None:
            progress_callback(idx, total, row["filename"])

        rom_id = int(row["id"])
        path = str(row["path"])
        filename = str(row["filename"])
        system_id = row["system_id"]
        row_library_root = row["library_root"]
        stored_size = int(row["size_bytes"] or 0)
        stored_mtime = float(row["mtime"] or 0.0)
        is_missing_flag = int(row["missing"] or 0) == 1

        # Bucket 2 — outside_root. Check this first because such a row
        # could ALSO be missing-on-disk, and the right action for an
        # outside-root row is delete (not tombstone), regardless.
        if (
            row_library_root
            and str(row_library_root).rstrip("\\/").rstrip() != library_root_norm
        ):
            plan.actions.append(
                ScrubAction(
                    rom_id=rom_id,
                    path=path,
                    filename=filename,
                    system_id=system_id,
                    library_root=str(row_library_root),
                    status="outside_root",
                    stored_size=stored_size,
                    stored_mtime=stored_mtime,
                )
            )
            continue

        disk_size, disk_mtime, exists, unreadable = _stat_or_none(path)
        if unreadable:
            plan.rows_unreadable += 1
            continue

        if not exists:
            # Bucket 1 — missing_unflagged. Only surface if the row
            # claims to be present; if it's already flagged missing,
            # nothing to do.
            if not is_missing_flag:
                plan.actions.append(
                    ScrubAction(
                        rom_id=rom_id,
                        path=path,
                        filename=filename,
                        system_id=system_id,
                        library_root=(
                            str(row_library_root) if row_library_root else None
                        ),
                        status="missing_unflagged",
                        stored_size=stored_size,
                        stored_mtime=stored_mtime,
                    )
                )
            continue

        # File present on disk.
        if is_missing_flag:
            # Bucket 3 — flagged_but_present. The reverse of bucket 1.
            plan.actions.append(
                ScrubAction(
                    rom_id=rom_id,
                    path=path,
                    filename=filename,
                    system_id=system_id,
                    library_root=(
                        str(row_library_root) if row_library_root else None
                    ),
                    status="flagged_but_present",
                    stored_size=stored_size,
                    stored_mtime=stored_mtime,
                    current_size=disk_size,
                    current_mtime=disk_mtime,
                )
            )
            continue

        # Bucket 4 — drift. File present, missing flag matches disk,
        # but stored size/mtime have drifted.
        if disk_size is not None and disk_mtime is not None and _drift_detected(
            stored_size, stored_mtime, disk_size, disk_mtime
        ):
            plan.actions.append(
                ScrubAction(
                    rom_id=rom_id,
                    path=path,
                    filename=filename,
                    system_id=system_id,
                    library_root=(
                        str(row_library_root) if row_library_root else None
                    ),
                    status="drift",
                    stored_size=stored_size,
                    stored_mtime=stored_mtime,
                    current_size=disk_size,
                    current_mtime=disk_mtime,
                )
            )

    counts = plan.counts_by_status()
    logger.info(
        "scrub.analyse: scanned=%d unreadable=%d actions=%d (%s)",
        plan.rows_scanned,
        plan.rows_unreadable,
        len(plan.actions),
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none",
    )
    return plan


def _apply_missing_unflagged(
    conn: sqlite3.Connection, action: ScrubAction
) -> None:
    conn.execute(
        "UPDATE roms SET missing = 1 WHERE id = ?", (action.rom_id,)
    )


def _apply_flagged_but_present(
    conn: sqlite3.Connection, action: ScrubAction
) -> None:
    conn.execute(
        "UPDATE roms SET missing = 0 WHERE id = ?", (action.rom_id,)
    )


def _apply_drift(conn: sqlite3.Connection, action: ScrubAction) -> None:
    """Update stored stat values + clear the cached hash row.

    Clearing the hash forces Heavy Scan to re-hash the file on its
    next run — the previous hash describes the previous contents and
    is no longer trustworthy. Metadata + cover art are left alone:
    they're keyed off ``game_id`` (not the hash), so the user keeps
    their enrichment until they explicitly re-enrich.
    """
    if action.current_size is None or action.current_mtime is None:
        # Defensive — analyse only emits drift actions with both values
        # populated, but a hand-crafted plan could miss them.
        return
    conn.execute(
        "UPDATE roms SET size_bytes = ?, mtime = ? WHERE id = ?",
        (action.current_size, action.current_mtime, action.rom_id),
    )
    conn.execute("DELETE FROM hashes WHERE rom_id = ?", (action.rom_id,))


def _apply_outside_root_bucket(
    conn: sqlite3.Connection,
    actions: list[ScrubAction],
    summary: ScrubSummary,
) -> None:
    """Bulk-delete every outside_root row using the existing helpers.

    Delegates to ``delete_roms_by_ids`` so FK-dependent ``hashes`` and
    ``dest_inventory`` rows go first — the same plumbing Clean Missing
    Entries uses. ``prune_orphan_games`` runs once at the end so games
    left with no remaining roms get cleaned up too.
    """
    rom_ids = [action.rom_id for action in actions]
    if not rom_ids:
        return
    deleted = q.delete_roms_by_ids(conn, rom_ids)
    summary.deleted_outside_root += deleted
    pruned = q.prune_orphan_games(conn)
    summary.pruned_games += pruned
    for action in actions:
        action.executed = True


def apply_plan(
    conn: sqlite3.Connection,
    approved_actions: list[ScrubAction],
    progress_callback: ProgressCallback | None = None,
) -> ScrubSummary:
    """Execute every approved action, bucketed by status, one SAVEPOINT per bucket.

    The four buckets commit independently: a failure inside the
    ``missing_unflagged`` bucket rolls back only that bucket, the
    others still apply. Per-bucket SAVEPOINT was chosen over per-action
    SAVEPOINT (the Import/Sync pattern) because every operation here is
    pure DB write — no file I/O, no third-party API call — so the
    failure modes are correlated, not independent.

    Cooperative cancel: raise :class:`_ScrubCancelled` from the
    progress callback. Any bucket already committed stays applied;
    the currently-executing bucket rolls back.
    """
    summary = ScrubSummary()
    if not approved_actions:
        return summary

    by_status: dict[ScrubStatus, list[ScrubAction]] = {
        "missing_unflagged": [],
        "outside_root": [],
        "flagged_but_present": [],
        "drift": [],
    }
    for action in approved_actions:
        by_status.setdefault(action.status, []).append(action)

    total = len(approved_actions)
    done = 0
    bucket_order: list[ScrubStatus] = [
        "outside_root",
        "missing_unflagged",
        "flagged_but_present",
        "drift",
    ]

    for status in bucket_order:
        bucket_actions = by_status.get(status, [])
        if not bucket_actions:
            continue
        savepoint = f"scrub_{status}"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            if status == "outside_root":
                _apply_outside_root_bucket(conn, bucket_actions, summary)
                done += len(bucket_actions)
                if progress_callback is not None:
                    progress_callback(
                        done,
                        total,
                        f"Deleted {len(bucket_actions)} outside-library row(s)",
                    )
            else:
                for action in bucket_actions:
                    if progress_callback is not None:
                        progress_callback(done + 1, total, action.filename)
                    if status == "missing_unflagged":
                        _apply_missing_unflagged(conn, action)
                        summary.flagged_missing += 1
                    elif status == "flagged_but_present":
                        _apply_flagged_but_present(conn, action)
                        summary.untombstoned += 1
                    elif status == "drift":
                        _apply_drift(conn, action)
                        summary.drift_fixed += 1
                    action.executed = True
                    done += 1
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
        except _ScrubCancelled:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - rollback intentionally catches all
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
            for action in bucket_actions:
                if not action.executed:
                    action.error = str(exc)
            summary.errors.append(
                f"{status} bucket failed: {type(exc).__name__}: {exc}"
            )
            logger.exception("scrub apply bucket=%s failed", status)

    logger.info(
        "scrub.apply: flagged_missing=%d deleted_outside_root=%d "
        "untombstoned=%d drift_fixed=%d pruned_games=%d errors=%d",
        summary.flagged_missing,
        summary.deleted_outside_root,
        summary.untombstoned,
        summary.drift_fixed,
        summary.pruned_games,
        len(summary.errors),
    )
    return summary
