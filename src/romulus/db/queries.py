"""Database query functions — all SQL operations go through here.

Keeping SQL in one place makes it easier to audit, optimize indexes, and swap
storage backends if we ever need to. Other modules should call these helpers
rather than constructing their own queries.

v0.4.0 — strict 1:1 ROM model: the ``games`` table is gone. Every function
that previously referenced ``game_id`` now references ``rom_id`` directly.
Collection membership uses the renamed ``collection_roms`` table. Cascade
deletes on ``roms.id`` mean there is no longer a ``prune_orphan_*`` helper —
deleting a rom row atomically clears its metadata, covers, and collection rows.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from romulus.core.dat_parser import DatEntry
    from romulus.metadata._types import MetadataPayload

logger = logging.getLogger(__name__)

# Ordering of match_confidence values. Used in upsert_rom so a re-scan never
# downgrades a stronger match (e.g. dat_verified) back to a weaker one (fuzzy).
# Single source of truth across the codebase — imported by ui.detail_panel and
# expanded into the upsert_rom SQL CASE expression below via f-string at
# module-import time (rank values are integer literals, not user input).
CONFIDENCE_RANK: dict[str, int] = {
    "unmatched": 0,
    "fuzzy": 1,
    "header": 2,
    "dat_verified": 3,
}
# Back-compat alias for any code still importing the private name.
_CONFIDENCE_RANK = CONFIDENCE_RANK


def _sql_confidence_case(column: str) -> str:
    """Build a SQL CASE expression that maps ``column`` to its rank integer.

    Built from :data:`CONFIDENCE_RANK` at module-import time so the Python dict
    and the SQL CASE can never drift. Rank values are integer literals (not
    user input) so f-string interpolation is safe here.
    """
    whens = "\n            ".join(
        f"WHEN '{name}' THEN {rank}" for name, rank in CONFIDENCE_RANK.items()
    )
    return f"""CASE {column}
            {whens}
            ELSE 0
        END"""


_UPSERT_ROM_CONFIDENCE_CASE: str = _sql_confidence_case("roms.match_confidence")


class RomUpsertData(TypedDict):
    """Shape of the dict accepted by :func:`upsert_rom`.

    Required keys: path, filename, extension, size_bytes, mtime, system_id.
    Optional identity keys (title … is_bios): supplied by the filename parser
    on Quick Scan; by the DAT matcher on Heavy Scan. Omitting them on a
    subsequent upsert preserves whatever was stored previously (COALESCE
    pattern in the SQL). ``match_confidence`` is monotonic — a rescan never
    downgrades a stronger match.
    """

    path: str
    filename: str
    extension: str
    size_bytes: int
    mtime: float
    system_id: str
    scan_id: NotRequired[int | None]
    fuzzy_key: NotRequired[str | None]
    header_title: NotRequired[str | None]
    dat_match: NotRequired[str | None]
    match_confidence: NotRequired[str]
    library_root: NotRequired[str | None]
    # Identity fields merged from the old games table
    title: NotRequired[str | None]
    canonical_name: NotRequired[str | None]
    region: NotRequired[str | None]
    revision: NotRequired[str | None]
    is_hack: NotRequired[bool]
    is_homebrew: NotRequired[bool]
    is_bios: NotRequired[bool]


class ScanHistoryData(TypedDict):
    """Shape of the dict accepted by :func:`insert_scan_history`."""

    scan_type: str
    started_at: str
    root_path: str
    finished_at: NotRequired[str | None]
    files_found: NotRequired[int]
    files_matched: NotRequired[int]
    files_new: NotRequired[int]
    errors: NotRequired[int]


class ScanHistoryUpdate(TypedDict, total=False):
    """Optional update fields for :func:`update_scan_history`."""

    finished_at: str | None
    files_found: int
    files_matched: int
    files_new: int
    errors: int


# ---------------------------------------------------------------------------
# ROMs
# ---------------------------------------------------------------------------


def upsert_rom(conn: sqlite3.Connection, rom_data: RomUpsertData) -> int:
    """Insert a ROM row or update it in place if ``path`` already exists.

    Returns the row id of the upserted ROM. Caller is responsible for committing
    the surrounding transaction; this function does not commit on its own so
    bulk scans can batch many upserts.

    ``match_confidence`` is monotonic — a rescan never downgrades a previously
    stronger match (e.g. ``dat_verified``) back to a weaker one (``fuzzy``).

    Every successful upsert resets ``missing = 0`` — re-scanning a file that
    was previously marked missing un-tombstones it without losing any prior
    enrichment work. ``library_root`` is stamped on each upsert.

    Identity fields (``title``, ``canonical_name``, ``region``, ``revision``,
    ``is_hack``, ``is_homebrew``, ``is_bios``) are written when supplied and
    preserved via ``COALESCE(excluded.field, roms.field)`` when omitted so a
    follow-up upsert that doesn't include them (e.g. a plain path-refresh
    rescan) doesn't wipe DAT-derived values that Heavy Scan populated earlier.

    Required keys: path, filename, extension, size_bytes, mtime, system_id.
    Optional keys: fuzzy_key, header_title, scan_id, dat_match,
    match_confidence, library_root, title, canonical_name, region, revision,
    is_hack, is_homebrew, is_bios.
    """
    incoming_confidence = rom_data.get("match_confidence", "unmatched")
    incoming_rank = CONFIDENCE_RANK.get(incoming_confidence, 0)

    # Convert optional booleans to integers for SQLite storage.
    # The INSERT leg must always supply a concrete integer (schema: NOT NULL DEFAULT 0).
    # Use 0 when the caller omits the flag so the default is respected; COALESCE
    # in the UPDATE leg preserves the existing value when the incoming integer is 0
    # AND the column is already non-zero — but that case is fine because 0 is the
    # "not a hack" default. The only case where we truly need COALESCE is when the
    # caller omits the key entirely and a previous upsert stored a non-zero value.
    # To distinguish "omitted" from "explicitly False", we use a sentinel: when
    # the key is absent from rom_data we pass NULL to the INSERT leg too, which
    # would fail the NOT NULL check — so instead we fall back to 0 for the INSERT
    # column and NULL for the COALESCE slot in the UPDATE SET.
    #
    # Actual fix: for the INSERT values array, supply 0 (never None) for these
    # three columns. For the UPDATE COALESCE, pass the same value — if the caller
    # supplied a real flag, use it; if not, NULL so COALESCE keeps the old value.
    is_hack_supplied = "is_hack" in rom_data
    is_homebrew_supplied = "is_homebrew" in rom_data
    is_bios_supplied = "is_bios" in rom_data

    is_hack_insert = int(bool(rom_data.get("is_hack", False)))
    is_homebrew_insert = int(bool(rom_data.get("is_homebrew", False)))
    is_bios_insert = int(bool(rom_data.get("is_bios", False)))

    # For UPDATE COALESCE: None means "not supplied → preserve existing value"
    is_hack_update = is_hack_insert if is_hack_supplied else None
    is_homebrew_update = is_homebrew_insert if is_homebrew_supplied else None
    is_bios_update = is_bios_insert if is_bios_supplied else None

    # ``RETURNING id`` makes this work whether the UPSERT takes the INSERT or
    # UPDATE branch — ``cursor.lastrowid`` can't be trusted for the UPDATE
    # branch because SQLite's connection-level ``last_insert_rowid`` doesn't
    # change on UPDATE, so it returns whatever the most recent INSERT id was
    # in the connection (often the wrong row). Hit it in a multi-row rescan
    # and ``visited_rom_ids`` collects wrong ids → the scanner's missing
    # sweep flags healthy files as missing.
    row = conn.execute(
        f"""
        INSERT INTO roms (
            path, filename, extension, size_bytes, mtime,
            system_id, scan_id, fuzzy_key, header_title,
            dat_match, match_confidence, library_root, missing,
            title, canonical_name, region, revision,
            is_hack, is_homebrew, is_bios
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                  ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            filename = excluded.filename,
            extension = excluded.extension,
            size_bytes = excluded.size_bytes,
            mtime = excluded.mtime,
            system_id = excluded.system_id,
            scan_id = excluded.scan_id,
            fuzzy_key = excluded.fuzzy_key,
            header_title = COALESCE(excluded.header_title, roms.header_title),
            dat_match = COALESCE(excluded.dat_match, roms.dat_match),
            match_confidence = CASE
                WHEN ? >= {_UPSERT_ROM_CONFIDENCE_CASE}
                THEN excluded.match_confidence
                ELSE roms.match_confidence
            END,
            library_root = COALESCE(excluded.library_root, roms.library_root),
            missing = 0,
            title          = COALESCE(excluded.title,          roms.title),
            canonical_name = COALESCE(excluded.canonical_name, roms.canonical_name),
            region         = COALESCE(excluded.region,         roms.region),
            revision       = COALESCE(excluded.revision,       roms.revision),
            is_hack        = COALESCE(?,                       roms.is_hack),
            is_homebrew    = COALESCE(?,                       roms.is_homebrew),
            is_bios        = COALESCE(?,                       roms.is_bios)
        RETURNING id
        """,
        (
            rom_data["path"],
            rom_data["filename"],
            rom_data["extension"],
            rom_data["size_bytes"],
            rom_data["mtime"],
            rom_data["system_id"],
            rom_data.get("scan_id"),
            rom_data.get("fuzzy_key"),
            rom_data.get("header_title"),
            rom_data.get("dat_match"),
            incoming_confidence,
            rom_data.get("library_root"),
            # identity fields — INSERT leg (never None; NOT NULL DEFAULT 0 columns)
            rom_data.get("title"),
            rom_data.get("canonical_name"),
            rom_data.get("region"),
            rom_data.get("revision"),
            is_hack_insert,
            is_homebrew_insert,
            is_bios_insert,
            # confidence-rank parameter for the CASE expression
            incoming_rank,
            # UPDATE COALESCE leg — None when caller omitted the flag (preserves DB value)
            is_hack_update,
            is_homebrew_update,
            is_bios_update,
        ),
    ).fetchone()
    return row["id"]


def get_roms_by_system(conn: sqlite3.Connection, system_id: str) -> list[sqlite3.Row]:
    """Return all ROM rows for the given system, ordered by filename."""
    return conn.execute(
        "SELECT * FROM roms WHERE system_id = ? ORDER BY filename",
        (system_id,),
    ).fetchall()


def find_rom_by_path(
    conn: sqlite3.Connection, abs_path: str
) -> sqlite3.Row | None:
    """Return the ``roms`` row for ``abs_path`` or None if not enrolled.

    Used by :mod:`romulus.core.importer` for path-level dupe detection and
    by :mod:`romulus.core.organizer` for collision detection + header-rule
    lookup at delete-duplicate execute time.

    Tolerates either slash direction in ``abs_path``. The DB stores paths in
    whatever form the scanner wrote them — on Windows that's backslash
    (``\\\\server\\share\\file.ext``) because ``os.walk`` + ``Path`` produce
    OS-native form. Various callers further downstream (the organizer's
    rename detector, ``_header_rule_for``) normalize to forward-slash before
    handing the path back to this function, which would otherwise produce a
    silent MISS — the exact bug that caused 594 organize failures in v0.4.0
    against UNC libraries. A direct ``=`` lookup is tried first (the common
    case); on miss we flip slashes and try once more. The "single canonical
    convention everywhere" rewrite is deferred (see Option 2 in
    docs/architecture.md §"Path convention") — this function is the chokepoint
    every path lookup goes through, so a tolerant lookup here covers every
    caller without per-site changes.
    """
    row = conn.execute(
        "SELECT * FROM roms WHERE path = ? LIMIT 1", (abs_path,)
    ).fetchone()
    if row is not None:
        return row
    # Try the opposite slash direction. Cheap (one indexed lookup) and only
    # fires when the first lookup missed.
    if "\\" in abs_path:
        alt = abs_path.replace("\\", "/")
    elif "/" in abs_path:
        alt = abs_path.replace("/", "\\")
    else:
        return None
    if alt == abs_path:
        return None
    return conn.execute(
        "SELECT * FROM roms WHERE path = ? LIMIT 1", (alt,)
    ).fetchone()


def find_rom_by_sha1(
    conn: sqlite3.Connection, sha1: str
) -> sqlite3.Row | None:
    """Return a ``roms`` row whose hashed payload matches ``sha1``, or None.

    Used by :mod:`romulus.core.importer` for hash-level dupe detection when
    the user opts into Heavy Identify before import. Joins through ``hashes``
    which is indexed on ``sha1`` so this is an O(log n) lookup.
    """
    if not sha1:
        return None
    return conn.execute(
        """
        SELECT r.*
        FROM roms r
        JOIN hashes h ON h.rom_id = r.id
        WHERE h.sha1 = ?
        LIMIT 1
        """,
        (sha1,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Library cleanup — stale entries + library-root change handling
# ---------------------------------------------------------------------------


def mark_missing_under_root(
    conn: sqlite3.Connection,
    library_root: str,  # noqa: ARG001 — kept for backward compat
    excluded_rom_ids: set[int] | None = None,
    *,
    scope_system_id: str | None = None,
) -> int:
    """Flag every row NOT in ``excluded_rom_ids`` as missing. Returns count.

    Called by the scanner at the end of a Quick Scan: every row visited
    during the walk is in ``excluded_rom_ids``; everything else is either
    (a) a file that disappeared from under the current library since the
    last scan, or (b) a stale row from a previous library the user has
    since switched away from. Either way, it gets ``missing = 1``.

    ``scope_system_id`` narrows the sweep to a single system, used by
    the right-click "Quick Scan this system" action. Rows from other
    systems are left alone — a single-system rescan should never
    tombstone a NES rom just because the user is rescanning Atari 7800.

    Design note — single-library assumption: the sweep deliberately does
    NOT filter by ``library_root``. ROMulus treats one library folder at
    a time as the source of truth (see the CLAUDE.md design rule and the
    library-change prompt in :class:`MainWindow`). A user who has just
    pointed at a new library expects stale entries from previous roots to
    show up as missing on the next scan — not to silently accumulate.
    The ``library_root`` parameter is retained for signature stability
    but ignored.

    Tombstoning rather than deleting preserves any enrichment, hash
    cache, or metadata work attached to the row. A later reconnect /
    rescan flips ``missing`` back to 0 via :func:`upsert_rom`'s path-keyed
    UPSERT and the user keeps their data. Use :func:`delete_missing_roms`
    to actually drop the rows when the user opts into a "Clean Missing
    Entries" action.
    """
    ids = excluded_rom_ids or set()

    where_clauses = ["missing = 0"]
    params: list[object] = []
    if scope_system_id is not None:
        where_clauses.append("system_id = ?")
        params.append(scope_system_id)

    if not ids:
        sql = "UPDATE roms SET missing = 1 WHERE " + " AND ".join(where_clauses)
        cursor = conn.execute(sql, params)
        return cursor.rowcount

    # Fast path for visited sets that fit comfortably under
    # ``SQLITE_MAX_VARIABLE_NUMBER`` — the stock Windows Python build
    # ships SQLite with 999, newer builds bump to 32766. We pick 900
    # to leave headroom for the ``scope_system_id`` parameter and any
    # future where-clause additions, and to stay below every supported
    # SQLite build's limit. Using inline ``NOT IN`` here avoids the
    # temp-table dance below, which was implicated in a Linux CI
    # segfault during ``conn.close()`` on the worker thread (CI run
    # against commit 4f42f9f).
    if len(ids) <= 900:
        placeholders = ",".join("?" * len(ids))
        where_clauses.append(f"id NOT IN ({placeholders})")
        sql = "UPDATE roms SET missing = 1 WHERE " + " AND ".join(where_clauses)
        cursor = conn.execute(sql, [*params, *ids])
        return cursor.rowcount

    # Big-library path: stash the visited-id set in a temp table and let
    # SQLite do the set difference. The inline ``NOT IN`` above trips
    # ``OperationalError: too many SQL variables`` once the library
    # crosses ``SQLITE_MAX_VARIABLE_NUMBER``. ``executemany`` binds at
    # most one variable per statement so we can feed any size set.
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _visited_rom_ids "
        "(id INTEGER PRIMARY KEY)"
    )
    conn.execute("DELETE FROM _visited_rom_ids")
    conn.executemany(
        "INSERT INTO _visited_rom_ids (id) VALUES (?)",
        ((rom_id,) for rom_id in ids),
    )
    where_clauses.append("id NOT IN (SELECT id FROM _visited_rom_ids)")
    sql = "UPDATE roms SET missing = 1 WHERE " + " AND ".join(where_clauses)
    cursor = conn.execute(sql, params)
    rowcount = cursor.rowcount
    conn.execute("DROP TABLE _visited_rom_ids")
    return rowcount


def count_missing_roms(conn: sqlite3.Connection) -> int:
    """Return the total number of roms currently flagged ``missing = 1``."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM roms WHERE missing = 1"
    ).fetchone()
    return row["n"] if row else 0


def delete_missing_roms(
    conn: sqlite3.Connection,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    """Permanently remove every row flagged ``missing = 1``. Returns count.

    Caller owns the surrounding transaction commit. FK ``ON DELETE CASCADE``
    on ``metadata``, ``covers``, and ``collection_roms`` means dependents are
    cleaned up automatically when the roms row is deleted. ``hashes`` and
    ``dest_inventory`` do NOT have CASCADE (they predate v0.4.0 and adding
    CASCADE requires a table-recreate migration), so we still delete those
    explicitly via :func:`_delete_rom_dependents` first.

    ``progress_callback`` (optional, signature ``(current, total, label)``)
    fires once per dependent-row chunk so a worker thread can drive a
    progress dialog.
    """
    rom_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM roms WHERE missing = 1"
        ).fetchall()
    ]
    if not rom_ids:
        logger.info("delete_missing_roms: no missing rows to delete")
        return 0
    logger.info(
        "delete_missing_roms: start count=%d (with FK dependents)",
        len(rom_ids),
    )
    _delete_rom_dependents(conn, rom_ids, progress_callback=progress_callback)
    cursor = conn.execute("DELETE FROM roms WHERE missing = 1")
    logger.info("delete_missing_roms: deleted rom rows=%d", cursor.rowcount)
    return cursor.rowcount


def _delete_rom_dependents(
    conn: sqlite3.Connection,
    rom_ids: list[int],
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> None:
    """Drop ``hashes`` and ``dest_inventory`` rows referencing ``rom_ids``.

    ``metadata``, ``covers``, and ``collection_roms`` rows are cleaned up
    automatically via ``ON DELETE CASCADE`` when the ``roms`` row is deleted,
    so they are NOT handled here.

    Splits the work into chunks of 500 ids so a very large clean (e.g. the
    user switching libraries with tens of thousands of stale entries) doesn't
    hit SQLite's parameter-count limit (default 999).

    ``progress_callback`` (optional) fires once per processed chunk with
    ``(done, total, label)`` so a worker can update a determinate progress
    dialog during long deletes over SMB / spinning disks.
    """
    if not rom_ids:
        return
    chunk_size = 500
    total = len(rom_ids)
    for start in range(0, total, chunk_size):
        chunk = rom_ids[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM hashes WHERE rom_id IN ({placeholders})", chunk
        )
        conn.execute(
            f"DELETE FROM dest_inventory WHERE rom_id IN ({placeholders})",
            chunk,
        )
        done = min(start + chunk_size, total)
        logger.debug(
            "_delete_rom_dependents: chunk done=%d/%d",
            done,
            total,
        )
        if progress_callback is not None:
            progress_callback(done, total, "Deleting dependent rows…")


def delete_roms_by_ids(
    conn: sqlite3.Connection,
    rom_ids: list[int],
) -> int:
    """Delete a hand-picked set of rom rows + their FK dependents.

    Returns the rowcount of the ``roms`` delete. Mirrors
    :func:`delete_missing_roms` but driven by an explicit id list
    instead of the ``missing = 1`` filter — used by the reverse-scrub
    flow, which classifies rows row-by-row and needs to delete only
    the ones the user approved. Caller owns the transaction commit.
    """
    if not rom_ids:
        return 0
    logger.info("delete_roms_by_ids: deleting %d rom rows", len(rom_ids))
    _delete_rom_dependents(conn, rom_ids)
    placeholders = ",".join("?" for _ in rom_ids)
    cursor = conn.execute(
        f"DELETE FROM roms WHERE id IN ({placeholders})", rom_ids
    )
    return cursor.rowcount


def count_roms_with_other_library_root(
    conn: sqlite3.Connection, current_root: str
) -> int:
    """Count rows whose ``library_root`` is set but doesn't equal ``current_root``.

    Used by the settings dialog to decide whether to prompt the user to
    wipe old-library entries when they pick a new library path.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM roms "
        "WHERE library_root IS NOT NULL AND library_root != ?",
        (current_root,),
    ).fetchone()
    return row["n"] if row else 0


def delete_roms_with_other_library_root(
    conn: sqlite3.Connection, keep_root: str
) -> int:
    """Delete every row whose ``library_root`` is set and not equal to ``keep_root``.

    Used when the user confirms a library-root switch with "wipe old
    entries". Drops FK-dependent ``hashes`` and ``dest_inventory`` rows
    first via :func:`_delete_rom_dependents`.

    Safety: ``keep_root`` must be a non-empty string. Passing ``""`` or
    ``None`` would otherwise wipe rows whose library_root happens to be
    empty. Callers are responsible for filtering empty values before
    invoking this — see the guard in :meth:`MainWindow._on_open_library`.
    """
    if not keep_root:
        raise ValueError("keep_root must be a non-empty path")
    rom_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM roms "
            "WHERE library_root IS NOT NULL AND library_root != ?",
            (keep_root,),
        ).fetchall()
    ]
    if not rom_ids:
        return 0
    _delete_rom_dependents(conn, rom_ids)
    cursor = conn.execute(
        "DELETE FROM roms "
        "WHERE library_root IS NOT NULL AND library_root != ?",
        (keep_root,),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Scan history
# ---------------------------------------------------------------------------


def insert_scan_history(conn: sqlite3.Connection, scan_data: ScanHistoryData) -> int:
    """Insert a new scan_history row; return its id.

    Required keys: scan_type, started_at, root_path.
    Optional: finished_at, files_found, files_matched, files_new, errors.
    """
    cursor = conn.execute(
        """
        INSERT INTO scan_history (
            scan_type, started_at, finished_at, root_path,
            files_found, files_matched, files_new, errors
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_data["scan_type"],
            scan_data["started_at"],
            scan_data.get("finished_at"),
            scan_data["root_path"],
            scan_data.get("files_found", 0),
            scan_data.get("files_matched", 0),
            scan_data.get("files_new", 0),
            scan_data.get("errors", 0),
        ),
    )
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into scan_history did not produce a lastrowid"
    return new_id


def update_scan_history(
    conn: sqlite3.Connection, scan_id: int, updates: ScanHistoryUpdate
) -> None:
    """Update a scan_history row in place with arbitrary fields.

    The SQL SET clause is built dynamically, but column names are checked
    against the hard-coded ``allowed`` whitelist before interpolation, so this
    is not an injection vector — values are still passed as ``?`` parameters.
    """
    if not updates:
        return
    allowed = {
        "finished_at",
        "files_found",
        "files_matched",
        "files_new",
        "errors",
    }
    fields = [k for k in updates if k in allowed]
    if not fields:
        return
    # Safe: each name in ``fields`` is guaranteed to be in the ``allowed`` whitelist.
    set_clause = ", ".join(f"{f} = ?" for f in fields)
    values: list[object] = [updates[f] for f in fields]  # type: ignore[literal-required]
    values.append(scan_id)
    conn.execute(f"UPDATE scan_history SET {set_clause} WHERE id = ?", values)


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------


def upsert_hash(
    conn: sqlite3.Connection,
    rom_id: int,
    crc32: str | None,
    sha1: str | None,
    md5: str | None,
) -> None:
    """Insert or replace the hash row for a ROM.

    ``hashed_at`` is stamped to the current wall-clock time; the heavy-scan
    pipeline compares this against ``roms.mtime`` to detect stale entries.
    """
    conn.execute(
        """
        INSERT INTO hashes (rom_id, crc32, sha1, md5, hashed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rom_id) DO UPDATE SET
            crc32 = excluded.crc32,
            sha1  = excluded.sha1,
            md5   = excluded.md5,
            hashed_at = excluded.hashed_at
        """,
        (rom_id, crc32, sha1, md5, datetime.now(UTC).timestamp()),
    )


def get_hash(conn: sqlite3.Connection, rom_id: int) -> sqlite3.Row | None:
    """Return the hash row for a ROM, or None if it hasn't been hashed yet."""
    return conn.execute(
        "SELECT * FROM hashes WHERE rom_id = ?", (rom_id,)
    ).fetchone()


def get_unhashed_roms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ROMs that have no row in the hashes table at all."""
    return conn.execute(
        """
        SELECT r.*
        FROM roms r
        LEFT JOIN hashes h ON h.rom_id = r.id
        WHERE h.rom_id IS NULL
        ORDER BY r.id
        """
    ).fetchall()


def get_stale_hashes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ROMs whose recorded mtime is newer than their last hash timestamp."""
    return conn.execute(
        """
        SELECT r.*
        FROM roms r
        JOIN hashes h ON h.rom_id = r.id
        WHERE h.hashed_at < r.mtime
        ORDER BY r.id
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# DAT entries
# ---------------------------------------------------------------------------


def insert_dat_entry(conn: sqlite3.Connection, entry: DatEntry) -> int:
    """Insert one parsed DAT row; return its new id."""
    cursor = conn.execute(
        """
        INSERT INTO dat_entries (
            dat_file, system_id, game_name, rom_name, size_bytes,
            crc32, md5, sha1, region, revision, is_bios
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.dat_file,
            entry.system_id,
            entry.game_name,
            entry.rom_name,
            entry.size_bytes,
            entry.crc32,
            entry.md5,
            entry.sha1,
            entry.region,
            entry.revision,
            int(bool(entry.is_bios)),
        ),
    )
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into dat_entries did not produce a lastrowid"
    return new_id


def get_dat_by_sha1(
    conn: sqlite3.Connection, sha1: str | None
) -> sqlite3.Row | None:
    """Authoritative DAT lookup by SHA-1. Returns None if ``sha1`` is falsy."""
    if not sha1:
        return None
    return conn.execute(
        "SELECT * FROM dat_entries WHERE sha1 = ? LIMIT 1",
        (sha1.lower(),),
    ).fetchone()


def get_dat_by_crc_size(
    conn: sqlite3.Connection, crc32: str | None, size_bytes: int | None
) -> sqlite3.Row | None:
    """Fallback DAT lookup by (CRC32, size).

    Returns None if either argument is missing OR if more than one entry
    matches — ambiguous CRC32s shouldn't be auto-applied (ROM-DEDUP §5.4).
    The ambiguous case is logged at DEBUG level so a future "why isn't my
    ROM DAT-verified?" support question can be diagnosed without changing
    behaviour.
    """
    if not crc32 or size_bytes is None:
        return None
    rows = conn.execute(
        "SELECT * FROM dat_entries WHERE crc32 = ? AND size_bytes = ? LIMIT 2",
        (crc32.lower(), size_bytes),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        import logging

        logging.getLogger(__name__).debug(
            "ambiguous DAT match for crc32=%s size=%s — %d entries; not "
            "auto-applying (ROM-DEDUP §5.4)",
            crc32,
            size_bytes,
            len(rows),
        )
    return None


def update_rom_match(
    conn: sqlite3.Connection,
    rom_id: int,
    dat_match: str,
    confidence: str,
) -> None:
    """Stamp a ROM with the canonical name and upgrade its match confidence."""
    conn.execute(
        "UPDATE roms SET dat_match = ?, match_confidence = ? WHERE id = ?",
        (dat_match, confidence, rom_id),
    )


# ---------------------------------------------------------------------------
# Metadata & covers
# ---------------------------------------------------------------------------

_METADATA_FIELDS: tuple[str, ...] = (
    "description",
    "genre",
    "developer",
    "publisher",
    "release_date",
    "release_year",
    "players",
    "rating",
)


def upsert_metadata(
    conn: sqlite3.Connection,
    rom_id: int,
    metadata: MetadataPayload,
    source: str,
) -> None:
    """Insert or replace a ROM's metadata row.

    Unknown keys are ignored; missing keys are stored as NULL. ``source``
    records which provider (hasheous / launchbox / screenscraper / etc.)
    supplied the row.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to attach metadata to.
        metadata: Provider-supplied metadata payload.
        source: Provider name string.
    """
    values = [metadata.get(field) for field in _METADATA_FIELDS]  # type: ignore[literal-required]
    conn.execute(
        """
        INSERT INTO metadata (
            rom_id, description, genre, developer, publisher,
            release_date, release_year, players, rating, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rom_id) DO UPDATE SET
            description  = excluded.description,
            genre        = excluded.genre,
            developer    = excluded.developer,
            publisher    = excluded.publisher,
            release_date = excluded.release_date,
            release_year = excluded.release_year,
            players      = excluded.players,
            rating       = excluded.rating,
            source       = excluded.source
        """,
        (rom_id, *values, source),
    )


def get_metadata(conn: sqlite3.Connection, rom_id: int) -> sqlite3.Row | None:
    """Return the metadata row for a ROM, or None if unenriched.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to look up.
    """
    return conn.execute(
        "SELECT * FROM metadata WHERE rom_id = ?", (rom_id,)
    ).fetchone()


def insert_cover(
    conn: sqlite3.Connection,
    rom_id: int,
    cover_type: str,
    source_url: str | None,
    local_path: str | None,
    width: int | None = None,
    height: int | None = None,
    is_preferred: int = 0,
) -> int:
    """Insert a covers row and return its id.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to attach the cover to.
        cover_type: Cover type string (e.g. ``"Named_Boxarts"``).
        source_url: Remote URL the cover was fetched from, or None.
        local_path: Absolute path to the cached image on disk, or None.
        width: Image width in pixels, or None.
        height: Image height in pixels, or None.
        is_preferred: 1 if this cover should be the default display cover.
    """
    cursor = conn.execute(
        """
        INSERT INTO covers (
            rom_id, cover_type, source_url, local_path, width, height, is_preferred
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (rom_id, cover_type, source_url, local_path, width, height, is_preferred),
    )
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into covers did not produce a lastrowid"
    return new_id


def get_covers(conn: sqlite3.Connection, rom_id: int) -> list[sqlite3.Row]:
    """Return all cover rows for a ROM.

    Ordered ``is_preferred DESC, id ASC`` so the preferred cover is always
    first — the UI can rely on index 0 being the default display cover.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to look up covers for.
    """
    return conn.execute(
        "SELECT * FROM covers WHERE rom_id = ? ORDER BY is_preferred DESC, id ASC",
        (rom_id,),
    ).fetchall()


def get_preferred_cover(
    conn: sqlite3.Connection,
    rom_id: int,
    cover_type: str = "Named_Boxarts",
) -> sqlite3.Row | None:
    """Return the preferred cover row for a ROM/type, or None if absent.

    Args:
        conn: SQLite connection.
        rom_id: ROM to look up.
        cover_type: Cover type to filter by (default ``"Named_Boxarts"``).
    """
    return conn.execute(
        """
        SELECT * FROM covers
        WHERE rom_id = ? AND cover_type = ? AND is_preferred = 1
        LIMIT 1
        """,
        (rom_id, cover_type),
    ).fetchone()


def set_preferred_cover(conn: sqlite3.Connection, cover_id: int) -> None:
    """Mark ``cover_id`` as preferred and reset all other rows in its group.

    A "group" is all covers sharing the same ``(rom_id, cover_type)``. The
    operation is atomic — both the reset and the promotion happen inside a
    single transaction so there is never a moment where zero rows are preferred.

    Args:
        conn: SQLite connection.
        cover_id: The id of the cover row to promote.
    """
    row = conn.execute(
        "SELECT rom_id, cover_type FROM covers WHERE id = ?", (cover_id,)
    ).fetchone()
    if row is None:
        return
    rom_id = int(row["rom_id"])
    cover_type = str(row["cover_type"])
    with conn:
        conn.execute(
            "UPDATE covers SET is_preferred = 0 WHERE rom_id = ? AND cover_type = ?",
            (rom_id, cover_type),
        )
        conn.execute(
            "UPDATE covers SET is_preferred = 1 WHERE id = ?",
            (cover_id,),
        )


def count_covers(
    conn: sqlite3.Connection,
    rom_id: int,
    cover_type: str = "Named_Boxarts",
) -> int:
    """Return the number of cover rows for a ROM/type.

    Args:
        conn: SQLite connection.
        rom_id: ROM to count covers for.
        cover_type: Cover type to filter by (default ``"Named_Boxarts"``).
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM covers WHERE rom_id = ? AND cover_type = ?",
        (rom_id, cover_type),
    ).fetchone()
    return int(row[0]) if row else 0


def _ensure_preferred(
    conn: sqlite3.Connection, rom_id: int, cover_type: str
) -> None:
    """Promote the first cover row per group if no preferred row exists yet.

    Called after each insert during discovery to guarantee at least one row
    has ``is_preferred=1`` without overriding an existing user preference.

    Args:
        conn: SQLite connection.
        rom_id: ROM to check.
        cover_type: Cover type group to check.
    """
    existing_preferred = conn.execute(
        "SELECT 1 FROM covers WHERE rom_id = ? AND cover_type = ? AND is_preferred = 1 LIMIT 1",
        (rom_id, cover_type),
    ).fetchone()
    if existing_preferred is None:
        first = conn.execute(
            "SELECT id FROM covers WHERE rom_id = ? AND cover_type = ? ORDER BY id ASC LIMIT 1",
            (rom_id, cover_type),
        ).fetchone()
        if first is not None:
            conn.execute(
                "UPDATE covers SET is_preferred = 1 WHERE id = ?",
                (int(first["id"]),),
            )


def has_cover(conn: sqlite3.Connection, rom_id: int, cover_type: str) -> bool:
    """True if a cover row of the given type already exists for this ROM.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to check.
        cover_type: Cover type to filter by.
    """
    row = conn.execute(
        "SELECT 1 FROM covers WHERE rom_id = ? AND cover_type = ? LIMIT 1",
        (rom_id, cover_type),
    ).fetchone()
    return row is not None


def get_roms_needing_enrichment(
    conn: sqlite3.Connection,
    *,
    include_fuzzy: bool = False,
    include_already_enriched: bool = False,
) -> list[sqlite3.Row]:
    """Return ROMs that should be considered for enrichment.

    Two opt-in flags loosen the default filters:

    * ``include_fuzzy`` — when False (default) only ROMs with
      ``match_confidence='dat_verified'`` are returned. When True every
      confidence level (fuzzy, header, dat_verified) is eligible. The risky
      case is fuzzy: name-based metadata lookups can attach *wrong* metadata
      when the canonical name was guessed.
    * ``include_already_enriched`` — when False (default) ROMs that already
      carry a metadata row are excluded. When True they are kept, so a
      user-triggered re-run can top up partial enrichments after a new
      provider has been configured.

    The two flags are independent and combine multiplicatively. Setting both
    to True returns the broadest possible candidate set.

    Args:
        conn: SQLite connection.
        include_fuzzy: Include fuzzy/header-matched ROMs as enrichment candidates.
        include_already_enriched: Include ROMs that already have a metadata row.
    """
    sql = [
        "SELECT r.id, r.title, r.system_id, r.canonical_name,",
        "       r.dat_match AS dat_match",
        "FROM roms r",
        "LEFT JOIN metadata m ON m.rom_id = r.id",
    ]
    where: list[str] = []
    if not include_fuzzy:
        where.append("r.match_confidence = 'dat_verified'")
    if not include_already_enriched:
        where.append("m.rom_id IS NULL")
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY r.system_id, r.title")
    return conn.execute("\n".join(sql)).fetchall()


# ---------------------------------------------------------------------------
# Detail panel lookups
# ---------------------------------------------------------------------------


def get_rom_by_id(conn: sqlite3.Connection, rom_id: int) -> sqlite3.Row | None:
    """Return the roms row for ``rom_id``, or None if it does not exist.

    Args:
        conn: SQLite connection.
        rom_id: The ROM id to look up.
    """
    return conn.execute(
        "SELECT * FROM roms WHERE id = ?", (rom_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

FAVORITES_NAME = "Favorites"


def ensure_favorites_collection(conn: sqlite3.Connection) -> int:
    """Return the id of the system "Favorites" collection, creating it if needed."""
    row = conn.execute(
        "SELECT id FROM collections WHERE name = ?", (FAVORITES_NAME,)
    ).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO collections (name, description, is_system) VALUES (?, ?, 1)",
        (FAVORITES_NAME, "Built-in favorites collection"),
    )
    conn.commit()
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into collections did not produce a lastrowid"
    return new_id


def create_collection(
    conn: sqlite3.Connection,
    name: str,
    description: str | None = None,
    is_system: bool = False,
) -> int:
    """Insert a new collection row; return its id.

    Raises sqlite3.IntegrityError if a collection with the same name exists.

    Args:
        conn: SQLite connection.
        name: Unique collection name.
        description: Optional human-readable description.
        is_system: True for built-in collections (e.g. Favorites).
    """
    cursor = conn.execute(
        "INSERT INTO collections (name, description, is_system) VALUES (?, ?, ?)",
        (name, description, int(bool(is_system))),
    )
    conn.commit()
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into collections did not produce a lastrowid"
    return new_id


def delete_collection(conn: sqlite3.Connection, collection_id: int) -> None:
    """Remove a collection and every ROM-link pointing at it.

    System collections (is_system=1) are protected — calling this on one raises
    ``ValueError`` so callers must guard against it explicitly.

    Args:
        conn: SQLite connection.
        collection_id: The collection to delete.
    """
    row = conn.execute(
        "SELECT is_system FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()
    if row is None:
        return
    if int(row["is_system"]):
        raise ValueError("Cannot delete system collection")
    conn.execute(
        "DELETE FROM collection_roms WHERE collection_id = ?", (collection_id,)
    )
    conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    conn.commit()


def add_rom_to_collection(
    conn: sqlite3.Connection, collection_id: int, rom_id: int
) -> None:
    """Link a ROM to a collection. Idempotent — duplicate inserts are ignored.

    Args:
        conn: SQLite connection.
        collection_id: Target collection.
        rom_id: ROM to add.
    """
    conn.execute(
        "INSERT OR IGNORE INTO collection_roms (collection_id, rom_id) VALUES (?, ?)",
        (collection_id, rom_id),
    )
    conn.commit()


def remove_rom_from_collection(
    conn: sqlite3.Connection, collection_id: int, rom_id: int
) -> None:
    """Remove a single ROM from a collection.

    Args:
        conn: SQLite connection.
        collection_id: Target collection.
        rom_id: ROM to remove.
    """
    conn.execute(
        "DELETE FROM collection_roms WHERE collection_id = ? AND rom_id = ?",
        (collection_id, rom_id),
    )
    conn.commit()


def get_collection_roms(
    conn: sqlite3.Connection, collection_id: int
) -> list[int]:
    """Return every rom_id linked to a collection, ordered by insertion.

    Args:
        conn: SQLite connection.
        collection_id: The collection to query.
    """
    rows = conn.execute(
        "SELECT rom_id FROM collection_roms WHERE collection_id = ?",
        (collection_id,),
    ).fetchall()
    return [int(row["rom_id"]) for row in rows]


def get_collections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every collection row with a rom_count aggregate.

    Columns: ``id, name, description, is_system, rom_count``. System collections
    (is_system=1) sort to the top so the UI can render Favorites above any
    user-created entries.
    """
    return conn.execute(
        """
        SELECT c.id, c.name, c.description, c.is_system,
               COUNT(cr.rom_id) AS rom_count
        FROM collections c
        LEFT JOIN collection_roms cr ON cr.collection_id = c.id
        GROUP BY c.id, c.name, c.description, c.is_system
        ORDER BY c.is_system DESC, c.name
        """
    ).fetchall()


def get_collection_by_name(
    conn: sqlite3.Connection, name: str
) -> sqlite3.Row | None:
    """Return the collection row with this name, or None.

    Args:
        conn: SQLite connection.
        name: Collection name to look up.
    """
    return conn.execute(
        "SELECT * FROM collections WHERE name = ?", (name,)
    ).fetchone()


def is_rom_in_collection(
    conn: sqlite3.Connection, collection_id: int, rom_id: int
) -> bool:
    """True if a (collection, ROM) link row exists.

    Args:
        conn: SQLite connection.
        collection_id: Collection to check.
        rom_id: ROM to check.
    """
    row = conn.execute(
        "SELECT 1 FROM collection_roms WHERE collection_id = ? AND rom_id = ? LIMIT 1",
        (collection_id, rom_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Organize / library reorganization
# ---------------------------------------------------------------------------


def get_alias_folder_pairs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return ROMs grouped by (system_id, folder) for alias-folder analysis.

    Each row contains ``system_id``, the lowercase basename of the parent
    folder (``folder``), and a ROM count. The organizer compares this against
    the system registry's ``folder_aliases`` list to detect non-canonical
    folders that should be merged into the canonical one.
    """
    return conn.execute(
        """
        SELECT system_id,
               LOWER(
                   CASE
                       WHEN INSTR(path, '/') > 0
                           THEN SUBSTR(
                               REPLACE(path, '\\', '/'),
                               1,
                               LENGTH(REPLACE(path, '\\', '/'))
                                   - LENGTH(filename) - 1
                           )
                       ELSE ''
                   END
               ) AS folder_path,
               COUNT(*) AS rom_count
        FROM roms
        WHERE system_id IS NOT NULL
        GROUP BY system_id, folder_path
        """
    ).fetchall()


def get_duplicate_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return groups of ROMs that share an identical SHA-1.

    Joins ``roms`` with ``hashes`` and yields one row per (sha1, rom_id) for
    every SHA-1 that appears on two or more ROMs. Hack ROMs (is_hack=1) are
    excluded so the organizer never proposes deduping a hack against its
    original even if the hasher (incorrectly) reported identical content.
    """
    return conn.execute(
        """
        SELECT h.sha1, r.id AS rom_id, r.path, r.filename, r.extension,
               r.system_id, r.size_bytes,
               COALESCE(r.is_hack, 0) AS is_hack
        FROM hashes h
        JOIN roms r ON r.id = h.rom_id
        WHERE h.sha1 IS NOT NULL
          AND h.sha1 IN (
              SELECT sha1
              FROM hashes
              WHERE sha1 IS NOT NULL
              GROUP BY sha1
              HAVING COUNT(*) > 1
          )
          AND COALESCE(r.is_hack, 0) = 0
        ORDER BY h.sha1, r.id
        """
    ).fetchall()


def get_dat_matched_roms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return ROMs with a DAT-verified canonical name (Layer 3 match).

    Used by the organizer to find rename candidates — only ROMs that survived
    Layer 3 identification have a canonical name we trust enough to rename
    against. Quick-scan (fuzzy/header) matches are NEVER renamed.
    """
    return conn.execute(
        """
        SELECT r.id, r.path, r.filename, r.extension, r.system_id,
               r.dat_match, r.match_confidence
        FROM roms r
        WHERE r.match_confidence = 'dat_verified'
          AND r.dat_match IS NOT NULL
          AND r.dat_match != ''
        ORDER BY r.id
        """
    ).fetchall()


def update_rom_path(
    conn: sqlite3.Connection, rom_id: int, new_path: str, new_filename: str
) -> None:
    """Update a ROM's path/filename after a rename or move.

    Caller is responsible for committing the surrounding transaction.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to update.
        new_path: New absolute path on disk.
        new_filename: New filename (basename).
    """
    conn.execute(
        "UPDATE roms SET path = ?, filename = ? WHERE id = ?",
        (new_path, new_filename, rom_id),
    )


def delete_rom(conn: sqlite3.Connection, rom_id: int) -> None:
    """Remove a ROM row (and its hash row) after a duplicate removal.

    Caller is responsible for committing the surrounding transaction.
    ``metadata``, ``covers``, and ``collection_roms`` rows are dropped
    automatically via ``ON DELETE CASCADE``.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to delete.
    """
    conn.execute("DELETE FROM hashes WHERE rom_id = ?", (rom_id,))
    conn.execute("DELETE FROM roms WHERE id = ?", (rom_id,))


def delete_rom_by_id(conn: sqlite3.Connection, rom_id: int) -> bool:
    """Permanently drop one ROM row, its FK dependents, and commit.

    Used by the user-initiated ``Delete this ROM`` right-click action.
    Cleans up ``hashes`` and ``dest_inventory`` rows first (those tables
    do not have ``ON DELETE CASCADE`` in the v0.4.0 schema). ``metadata``,
    ``covers``, and ``collection_roms`` are cleaned up automatically by
    the CASCADE on ``roms.id``.

    Returns True when a rom row was actually deleted, False when the
    id didn't match anything in the table.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to delete.
    """
    exists = conn.execute(
        "SELECT 1 FROM roms WHERE id = ?", (rom_id,)
    ).fetchone()
    if exists is None:
        return False
    _delete_rom_dependents(conn, [rom_id])
    conn.execute("DELETE FROM roms WHERE id = ?", (rom_id,))
    conn.commit()
    return True


def get_rom_path(conn: sqlite3.Connection, rom_id: int) -> str | None:
    """Return the on-disk path stored for a rom id, or None when unknown.

    Used by ``Reveal in Explorer`` and as a pre-flight for ``Delete this ROM``.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to look up.
    """
    row = conn.execute(
        "SELECT path FROM roms WHERE id = ?", (rom_id,)
    ).fetchone()
    if row is None:
        return None
    return str(row["path"])


def insert_organize_plan(
    conn: sqlite3.Connection, plan_json: str, status: str = "pending"
) -> int:
    """Record an organize plan (its serialized JSON) and return its row id.

    Args:
        conn: SQLite connection.
        plan_json: JSON-serialized plan payload.
        status: Initial status string (default ``"pending"``).
    """
    cursor = conn.execute(
        """
        INSERT INTO organize_plans (created_at, status, plan_json)
        VALUES (?, ?, ?)
        """,
        (
            datetime_now_iso(),
            status,
            plan_json,
        ),
    )
    conn.commit()
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into organize_plans did not produce a lastrowid"
    return new_id


def update_plan_status(
    conn: sqlite3.Connection, plan_id: int, status: str
) -> None:
    """Stamp an organize plan with its terminal status (applied/cancelled/failed).

    Args:
        conn: SQLite connection.
        plan_id: The plan to update.
        status: New status string.
    """
    conn.execute(
        "UPDATE organize_plans SET status = ? WHERE id = ?", (status, plan_id)
    )
    conn.commit()


def datetime_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (used by plan rows)."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Sync destinations
# ---------------------------------------------------------------------------


class SyncDestinationData(TypedDict):
    """Shape accepted by :func:`insert_sync_destination`."""

    name: str
    target_path: str
    profile_id: str


def insert_sync_destination(
    conn: sqlite3.Connection, data: SyncDestinationData
) -> int:
    """Create a saved sync destination row and return its id.

    Caller is responsible for committing the surrounding transaction. Raises
    ``sqlite3.IntegrityError`` if a destination with the same ``name`` already
    exists (the name has a UNIQUE constraint per spec §4.1).

    Args:
        conn: SQLite connection.
        data: Destination fields.
    """
    cursor = conn.execute(
        """
        INSERT INTO sync_destinations (
            name, target_path, profile_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            data["name"],
            data["target_path"],
            data["profile_id"],
            datetime_now_iso(),
        ),
    )
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into sync_destinations did not produce a lastrowid"
    return new_id


def update_sync_destination(
    conn: sqlite3.Connection,
    dest_id: int,
    *,
    name: str | None = None,
    target_path: str | None = None,
    profile_id: str | None = None,
) -> None:
    """Edit a saved destination — used by the "Edit destination" UI flow.

    Any field left as ``None`` is preserved. ``last_inventory_signature`` is
    NOT settable here — that flows through :func:`set_sync_dest_signature`.

    Args:
        conn: SQLite connection.
        dest_id: Destination to update.
        name: New name, or None to leave unchanged.
        target_path: New target path, or None to leave unchanged.
        profile_id: New profile id, or None to leave unchanged.
    """
    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if target_path is not None:
        fields.append("target_path = ?")
        values.append(target_path)
    if profile_id is not None:
        fields.append("profile_id = ?")
        values.append(profile_id)
    if not fields:
        return
    values.append(dest_id)
    conn.execute(
        f"UPDATE sync_destinations SET {', '.join(fields)} WHERE id = ?",
        values,
    )


def delete_sync_destination(conn: sqlite3.Connection, dest_id: int) -> None:
    """Remove a saved destination AND its cached inventory rows.

    Args:
        conn: SQLite connection.
        dest_id: Destination to delete.
    """
    conn.execute("DELETE FROM dest_inventory WHERE dest_id = ?", (dest_id,))
    conn.execute("DELETE FROM sync_destinations WHERE id = ?", (dest_id,))


def get_sync_destinations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every saved destination, ordered by name (case-insensitive)."""
    return conn.execute(
        "SELECT * FROM sync_destinations ORDER BY LOWER(name)"
    ).fetchall()


def get_sync_destination(
    conn: sqlite3.Connection, dest_id: int
) -> sqlite3.Row | None:
    """Return a single sync_destinations row, or None if it does not exist.

    Args:
        conn: SQLite connection.
        dest_id: Destination id to look up.
    """
    return conn.execute(
        "SELECT * FROM sync_destinations WHERE id = ?", (dest_id,)
    ).fetchone()


def get_sync_destination_by_name(
    conn: sqlite3.Connection, name: str
) -> sqlite3.Row | None:
    """Return a destination by name, or None.

    Args:
        conn: SQLite connection.
        name: Destination name to look up.
    """
    return conn.execute(
        "SELECT * FROM sync_destinations WHERE name = ?", (name,)
    ).fetchone()


def get_sync_destination_by_target_path(
    conn: sqlite3.Connection, target_path: str
) -> sqlite3.Row | None:
    """Return the first destination matching ``target_path``, or None.

    ``target_path`` is not UNIQUE in the schema (only ``name`` is), so this
    helper returns the lowest-id row that matches — matching the row the user
    would see in the dropdown.

    Args:
        conn: SQLite connection.
        target_path: Filesystem path to look up.
    """
    return conn.execute(
        "SELECT * FROM sync_destinations WHERE target_path = ? "
        "ORDER BY id LIMIT 1",
        (target_path,),
    ).fetchone()


def ensure_sync_destination_by_path(
    conn: sqlite3.Connection,
    target_path: str,
    profile_id: str,
    *,
    name_hint: str | None = None,
) -> int:
    """Idempotently resolve ``target_path`` to a ``sync_destinations.id``.

    Used by the MainWindow one-shot sync slot to avoid the previous ``dest_id
    = -1`` sentinel that triggered FOREIGN KEY constraint failures on every
    ``upsert_dest_inventory`` / ``insert_sync_plan`` write. Behaviour:

    * If a saved destination already points at ``target_path`` return its id.
    * Otherwise insert a new ``sync_destinations`` row with an auto-generated
      ``"Quick Sync — <basename>"`` name. If that name collides with an
      existing row's (unrelated) name, suffix with ``" (N)"`` until unique.

    Caller is responsible for committing the surrounding transaction.

    Args:
        conn: SQLite connection.
        target_path: Filesystem path of the sync destination.
        profile_id: Profile id to assign to new rows.
        name_hint: Override the auto-generated name for new rows.
    """
    existing = get_sync_destination_by_target_path(conn, target_path)
    if existing is not None:
        return int(existing["id"])

    basename = Path(target_path).name or target_path
    base_name = name_hint or f"Quick Sync — {basename}"
    candidate = base_name
    counter = 2
    while counter < 1000:
        if get_sync_destination_by_name(conn, candidate) is None:
            break
        candidate = f"{base_name} ({counter})"
        counter += 1

    return insert_sync_destination(
        conn,
        {
            "name": candidate,
            "target_path": target_path,
            "profile_id": profile_id,
        },
    )


def set_sync_dest_signature(
    conn: sqlite3.Connection, dest_id: int, signature: str | None
) -> None:
    """Stamp / clear the inventory signature for swap-the-SD detection (§4.5).

    Args:
        conn: SQLite connection.
        dest_id: Destination to update.
        signature: New signature string, or None to clear.
    """
    conn.execute(
        "UPDATE sync_destinations SET last_inventory_signature = ? WHERE id = ?",
        (signature, dest_id),
    )


def set_sync_dest_last_synced(
    conn: sqlite3.Connection, dest_id: int, timestamp: str | None = None
) -> None:
    """Stamp ``last_synced_at`` after a successful sync apply.

    Args:
        conn: SQLite connection.
        dest_id: Destination to update.
        timestamp: ISO-8601 timestamp string, or None to use the current time.
    """
    conn.execute(
        "UPDATE sync_destinations SET last_synced_at = ? WHERE id = ?",
        (timestamp if timestamp is not None else datetime_now_iso(), dest_id),
    )


# ---------------------------------------------------------------------------
# Destination inventory cache
# ---------------------------------------------------------------------------


class DestInventoryUpsert(TypedDict):
    """Shape accepted by :func:`upsert_dest_inventory`."""

    dest_id: int
    rel_path: str
    size_bytes: int
    mtime: float
    sha1: NotRequired[str | None]
    rom_id: NotRequired[int | None]


def upsert_dest_inventory(
    conn: sqlite3.Connection, row: DestInventoryUpsert
) -> None:
    """Insert or update a cached destination-inventory row.

    Caller commits the surrounding transaction. Existing SHA-1 / rom_id
    values are preserved when the new payload supplies ``None``, so a Quick
    Sync pass doesn't clobber a previous Deep Verify's cached hash.

    Args:
        conn: SQLite connection.
        row: Inventory row fields.
    """
    conn.execute(
        """
        INSERT INTO dest_inventory (
            dest_id, rel_path, size_bytes, mtime, sha1, rom_id, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dest_id, rel_path) DO UPDATE SET
            size_bytes   = excluded.size_bytes,
            mtime        = excluded.mtime,
            sha1         = COALESCE(excluded.sha1, dest_inventory.sha1),
            rom_id       = COALESCE(excluded.rom_id, dest_inventory.rom_id),
            last_seen_at = excluded.last_seen_at
        """,
        (
            row["dest_id"],
            row["rel_path"],
            row["size_bytes"],
            row["mtime"],
            row.get("sha1"),
            row.get("rom_id"),
            datetime_now_iso(),
        ),
    )


def get_dest_inventory(
    conn: sqlite3.Connection, dest_id: int
) -> list[sqlite3.Row]:
    """Return every cached inventory row for a destination.

    Args:
        conn: SQLite connection.
        dest_id: Destination to query.
    """
    return conn.execute(
        "SELECT * FROM dest_inventory WHERE dest_id = ? ORDER BY rel_path",
        (dest_id,),
    ).fetchall()


def get_dest_inventory_row(
    conn: sqlite3.Connection, dest_id: int, rel_path: str
) -> sqlite3.Row | None:
    """Look up a single cached inventory row by (dest_id, rel_path).

    Args:
        conn: SQLite connection.
        dest_id: Destination id.
        rel_path: Relative path within the destination.
    """
    return conn.execute(
        "SELECT * FROM dest_inventory WHERE dest_id = ? AND rel_path = ?",
        (dest_id, rel_path),
    ).fetchone()


def clear_dest_inventory(conn: sqlite3.Connection, dest_id: int) -> None:
    """Forget every cached inventory row for a destination ("Forget cache").

    Args:
        conn: SQLite connection.
        dest_id: Destination to clear.
    """
    conn.execute("DELETE FROM dest_inventory WHERE dest_id = ?", (dest_id,))
    conn.execute(
        "UPDATE sync_destinations SET last_inventory_signature = NULL WHERE id = ?",
        (dest_id,),
    )


def delete_dest_inventory_row(
    conn: sqlite3.Connection, dest_id: int, rel_path: str
) -> None:
    """Remove a single cached inventory row (file is gone from destination).

    Args:
        conn: SQLite connection.
        dest_id: Destination id.
        rel_path: Relative path of the file that was removed.
    """
    conn.execute(
        "DELETE FROM dest_inventory WHERE dest_id = ? AND rel_path = ?",
        (dest_id, rel_path),
    )


def prune_dest_inventory_missing(
    conn: sqlite3.Connection, dest_id: int, present_rel_paths: list[str]
) -> int:
    """Remove cached rows whose files are no longer present on disk.

    ``present_rel_paths`` is the post-scan list of every file the walker
    observed. Anything in the cache but not in this list is deleted. Returns
    the row-count of pruned entries.

    Args:
        conn: SQLite connection.
        dest_id: Destination to prune.
        present_rel_paths: List of relative paths currently on disk.
    """
    if not present_rel_paths:
        cursor = conn.execute(
            "DELETE FROM dest_inventory WHERE dest_id = ?", (dest_id,)
        )
        return cursor.rowcount
    conn.execute("DROP TABLE IF EXISTS _sync_present_paths")
    conn.execute("CREATE TEMP TABLE _sync_present_paths (rel_path TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO _sync_present_paths (rel_path) VALUES (?)",
        [(p,) for p in present_rel_paths],
    )
    cursor = conn.execute(
        """
        DELETE FROM dest_inventory
        WHERE dest_id = ?
          AND rel_path NOT IN (SELECT rel_path FROM _sync_present_paths)
        """,
        (dest_id,),
    )
    pruned = cursor.rowcount
    conn.execute("DROP TABLE IF EXISTS _sync_present_paths")
    return pruned


# ---------------------------------------------------------------------------
# Sync plans
# ---------------------------------------------------------------------------


def insert_sync_plan(
    conn: sqlite3.Connection,
    dest_id: int,
    mode: str,
    summary_json: str,
    plan_json: str,
    status: str = "pending",
) -> int:
    """Persist a sync plan and return its id. Mirrors ``insert_organize_plan``.

    Args:
        conn: SQLite connection.
        dest_id: Destination this plan targets.
        mode: Sync mode string.
        summary_json: JSON-serialized summary payload.
        plan_json: JSON-serialized plan payload.
        status: Initial status (default ``"pending"``).
    """
    cursor = conn.execute(
        """
        INSERT INTO sync_plans (dest_id, mode, created_at, status, summary, plan_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            dest_id,
            mode,
            datetime_now_iso(),
            status,
            summary_json,
            plan_json,
        ),
    )
    conn.commit()
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into sync_plans did not produce a lastrowid"
    return new_id


def update_sync_plan_status(
    conn: sqlite3.Connection, plan_id: int, status: str
) -> None:
    """Stamp a sync plan with its terminal status (applied/cancelled/partial).

    Args:
        conn: SQLite connection.
        plan_id: Plan to update.
        status: New status string.
    """
    conn.execute(
        "UPDATE sync_plans SET status = ? WHERE id = ?", (status, plan_id)
    )
    conn.commit()


def get_sync_plan(
    conn: sqlite3.Connection, plan_id: int
) -> sqlite3.Row | None:
    """Return a sync plan row by id, or None.

    Args:
        conn: SQLite connection.
        plan_id: Plan id to look up.
    """
    return conn.execute(
        "SELECT * FROM sync_plans WHERE id = ?", (plan_id,)
    ).fetchone()


def get_sync_plans_for_dest(
    conn: sqlite3.Connection, dest_id: int
) -> list[sqlite3.Row]:
    """Return every persisted sync plan for a destination, newest first.

    Args:
        conn: SQLite connection.
        dest_id: Destination to query.
    """
    return conn.execute(
        "SELECT * FROM sync_plans WHERE dest_id = ? ORDER BY id DESC",
        (dest_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Identity-match lookups used by core.sync
# ---------------------------------------------------------------------------


def get_local_roms_for_match(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return every local ROM with the columns sync's identity matcher needs.

    Joins ``roms`` against ``hashes`` so a single pass over the DB hydrates
    all four tiers of identity matching (§3): path equivalence, fuzzy_key +
    region, hash-by-name, and deep-verify by SHA-1. Identity columns
    (``region``) now live directly on the ``roms`` row.
    """
    return conn.execute(
        """
        SELECT r.id          AS rom_id,
               r.path        AS path,
               r.filename    AS filename,
               r.system_id   AS system_id,
               r.size_bytes  AS size_bytes,
               r.fuzzy_key   AS fuzzy_key,
               COALESCE(r.region, '') AS region,
               h.sha1        AS sha1
        FROM roms r
        LEFT JOIN hashes h ON h.rom_id = r.id
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Enrichment-status query (used by the game table's enrichment filter)
# ---------------------------------------------------------------------------


def get_roms_for_cover_discovery(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every ROM eligible for cover discovery.

    Columns: ``rom_id, rom_path, system_id, fuzzy_key, clean_name``.
    ROMs without a fuzzy_key are excluded — they have no identity signal
    to match cover art against.

    Used by the local cover discovery pipeline.
    """
    return conn.execute(
        """
        SELECT r.id       AS rom_id,
               r.path     AS rom_path,
               r.system_id,
               r.fuzzy_key,
               COALESCE(r.dat_match, '') AS clean_name
        FROM roms r
        WHERE r.fuzzy_key IS NOT NULL
          AND r.fuzzy_key != ''
        ORDER BY r.system_id, r.id
        """
    ).fetchall()


def has_cover_for_path(
    conn: sqlite3.Connection, rom_id: int, local_path: str
) -> bool:
    """Return True if a covers row with this exact local_path already exists for the ROM.

    Used by the local cover discovery pipeline for idempotent re-runs — avoids
    inserting duplicate rows when discovery is run more than once.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to check.
        local_path: Absolute path string of the candidate image file.
    """
    row = conn.execute(
        "SELECT 1 FROM covers WHERE rom_id = ? AND local_path = ? LIMIT 1",
        (rom_id, local_path),
    ).fetchone()
    return row is not None


def get_rom_ids_for_scope(
    conn: sqlite3.Connection,
    *,
    rom_id: int | None = None,
    system_id: str | None = None,
    collection_id: int | None = None,
) -> list[int]:
    """Resolve a UI scope into a flat list of ROM ids.

    Exactly one of the keyword arguments should be supplied. If none are given,
    an empty list is returned (callers should treat that as "no scope, use
    default behaviour"). If multiple are supplied, the narrowest wins:
    ``rom_id`` > ``system_id`` > ``collection_id``.

    Args:
        conn: SQLite connection.
        rom_id: Return a single ROM by id.
        system_id: Return all ROMs in a system.
        collection_id: Return ROMs belonging to the collection.

    Returns:
        Ordered list of rom row ids matching the scope.
    """
    if rom_id is not None:
        rows = conn.execute(
            "SELECT id FROM roms WHERE id = ? ORDER BY id",
            (rom_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    if system_id is not None:
        rows = conn.execute(
            "SELECT id FROM roms WHERE system_id = ? ORDER BY id",
            (system_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    if collection_id is not None:
        rows = conn.execute(
            """
            SELECT cr.rom_id
            FROM collection_roms cr
            WHERE cr.collection_id = ?
            ORDER BY cr.rom_id
            """,
            (collection_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    return []


def get_roms_with_enrichment_status(
    conn: sqlite3.Connection,
    system_id: str | None = None,
    rom_ids: list[int] | None = None,
    limit: int = 5000,
) -> list[sqlite3.Row]:
    """Return ROM rows annotated with ``has_cover`` and ``has_metadata`` flags.

    This is the backing query for the game-table enrichment filter. Each
    returned row exposes the same columns as the base ``load_rom_rows`` query
    PLUS:

    * ``has_cover``    — 1 if at least one covers row exists for this ROM
    * ``has_metadata`` — 1 if a metadata row exists for this ROM
    * ``rom_path``     — path of the ROM on disk

    Args:
        conn: SQLite connection.
        system_id: Filter to a single system, or None for all.
        rom_ids: Explicit list of rom ids to include, or None for all.
        limit: Maximum number of rows to return.
    """
    base = """
        SELECT
            r.id            AS rom_id,
            r.filename      AS name,
            r.system_id,
            r.path          AS rom_path,
            COALESCE(s.short_name, s.display_name, r.system_id) AS system_name,
            COALESCE(r.region, '')  AS region,
            r.size_bytes,
            r.match_confidence,
            CASE WHEN c.rom_id IS NOT NULL THEN 1 ELSE 0 END AS has_cover,
            CASE WHEN m.rom_id IS NOT NULL THEN 1 ELSE 0 END AS has_metadata
        FROM roms r
        LEFT JOIN systems   s ON s.id = r.system_id
        LEFT JOIN (
            SELECT DISTINCT rom_id FROM covers
        ) c ON c.rom_id = r.id
        LEFT JOIN (
            SELECT DISTINCT rom_id FROM metadata
        ) m ON m.rom_id = r.id
    """
    clauses: list[str] = []
    params: list[object] = []
    if system_id is not None:
        clauses.append("r.system_id = ?")
        params.append(system_id)
    if rom_ids is not None:
        if not rom_ids:
            return []
        placeholders = ",".join("?" for _ in rom_ids)
        clauses.append(f"r.id IN ({placeholders})")
        params.extend(rom_ids)
    if clauses:
        base += " WHERE " + " AND ".join(clauses)
    base += " ORDER BY r.filename LIMIT ?"
    params.append(limit)
    return conn.execute(base, params).fetchall()


# ---------------------------------------------------------------------------
# Forward-looking sibling-metadata helpers (wired up in Session 15)
# ---------------------------------------------------------------------------


def find_sibling_metadata(
    conn: sqlite3.Connection,
    rom_id: int,
) -> sqlite3.Row | None:
    """Find a metadata row attached to a *different* ROM with the same identity.

    Identity resolution priority (highest to lowest):

    1. **SHA-1 match** — when both ROMs have a hash entry with the same ``sha1``
       the match is byte-identical content.  Highest confidence; no false
       positives because SHA-1 collisions are cryptographically negligible
       for ROM files.

    2. **``(system_id, canonical_name)`` match** — both ROMs resolved to the
       same canonical title on the same platform.  Same game, different dump
       paths (e.g. two copies of ``Super Mario World (USA).sfc`` in different
       sub-folders).  Requires ``canonical_name`` to be populated (Heavy Scan).

    3. **``(system_id, fuzzy_key)`` match** — lower-confidence fallback for
       Quick-Scan-only libraries that haven't been Heavy-Scanned.  Two ROMs
       with the same title tokens on the same platform are *probably* the same
       game, but region/revision differences mean this tier can copy metadata
       from e.g. a Japanese release onto a USA release.  Accepted trade-off:
       the user gets *some* metadata rather than none, and re-enriching after
       Heavy Scan will overwrite with a more precise result.

    Returns one matching ``metadata`` row (including its ``rom_id`` so the
    caller knows the source), or None if no sibling exists yet.

    Args:
        conn: SQLite connection.
        rom_id: The ROM we are about to enrich (excluded from the search so
            we never copy from ourselves).
    """
    # Look up the target rom's own identity fields + sha1 in one query.
    self_row = conn.execute(
        """
        SELECT r.system_id, r.canonical_name, r.fuzzy_key, h.sha1
        FROM roms r
        LEFT JOIN hashes h ON h.rom_id = r.id
        WHERE r.id = ?
        LIMIT 1
        """,
        (rom_id,),
    ).fetchone()
    if self_row is None:
        return None

    sha1 = self_row["sha1"]
    system_id = self_row["system_id"]
    canonical_name = self_row["canonical_name"]
    fuzzy_key = self_row["fuzzy_key"]

    # Tier 1 — SHA-1 match (byte-identical content)
    if sha1:
        row = conn.execute(
            """
            SELECT m.*
            FROM metadata m
            JOIN hashes h ON h.rom_id = m.rom_id
            WHERE h.sha1 = ?
              AND m.rom_id != ?
            LIMIT 1
            """,
            (sha1, rom_id),
        ).fetchone()
        if row is not None:
            return row

    # Tier 2 — (system_id, canonical_name) match (same game, different path)
    if system_id and canonical_name:
        row = conn.execute(
            """
            SELECT m.*
            FROM metadata m
            JOIN roms r ON r.id = m.rom_id
            WHERE r.system_id = ?
              AND r.canonical_name = ?
              AND m.rom_id != ?
            LIMIT 1
            """,
            (system_id, canonical_name, rom_id),
        ).fetchone()
        if row is not None:
            return row

    # Tier 3 — (system_id, fuzzy_key) match (Quick-Scan-only fallback).
    # Lower confidence: same title tokens on the same platform.  Region or
    # revision may differ.  Documented trade-off: gives the user *something*
    # without a network call; Heavy Scan + re-enrich will correct any
    # mis-match later.
    if system_id and fuzzy_key:
        row = conn.execute(
            """
            SELECT m.*
            FROM metadata m
            JOIN roms r ON r.id = m.rom_id
            WHERE r.system_id = ?
              AND r.fuzzy_key = ?
              AND m.rom_id != ?
            LIMIT 1
            """,
            (system_id, fuzzy_key, rom_id),
        ).fetchone()
        if row is not None:
            return row

    return None


def copy_metadata(
    conn: sqlite3.Connection, source_rom_id: int, dest_rom_id: int
) -> None:
    """Copy the metadata row from ``source_rom_id`` to ``dest_rom_id``.

    If ``source_rom_id`` has no metadata row this is a no-op.
    If ``dest_rom_id`` already has a metadata row it is replaced (same
    upsert semantics as :func:`upsert_metadata`).

    Used by Session 15's copy-on-enrich logic so byte-identical ROM duplicates
    don't require a second network round-trip.

    Args:
        conn: SQLite connection.
        source_rom_id: ROM whose metadata row to copy from.
        dest_rom_id: ROM to attach the copied metadata to.
    """
    source = conn.execute(
        "SELECT * FROM metadata WHERE rom_id = ?", (source_rom_id,)
    ).fetchone()
    if source is None:
        return
    conn.execute(
        """
        INSERT INTO metadata (
            rom_id, description, genre, developer, publisher,
            release_date, release_year, players, rating, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rom_id) DO UPDATE SET
            description  = excluded.description,
            genre        = excluded.genre,
            developer    = excluded.developer,
            publisher    = excluded.publisher,
            release_date = excluded.release_date,
            release_year = excluded.release_year,
            players      = excluded.players,
            rating       = excluded.rating,
            source       = excluded.source
        """,
        (
            dest_rom_id,
            source["description"],
            source["genre"],
            source["developer"],
            source["publisher"],
            source["release_date"],
            source["release_year"],
            source["players"],
            source["rating"],
            source["source"],
        ),
    )


def find_sibling_covers(
    conn: sqlite3.Connection,
    rom_id: int,
) -> list[sqlite3.Row]:
    """Find cover rows attached to a *different* ROM with the same identity.

    Identity resolution priority mirrors :func:`find_sibling_metadata`:

    1. **SHA-1 match** — byte-identical content.
    2. **``(system_id, canonical_name)`` match** — same game, different path.
    3. **``(system_id, fuzzy_key)`` match** — Quick-Scan-only fallback.

    Returns all cover rows from the *first* sibling found (all three cover
    types in one shot) so the caller can copy the complete artwork set.
    Returns an empty list when no sibling has covers.

    Args:
        conn: SQLite connection.
        rom_id: The ROM we are about to fetch covers for (excluded from
            the search so we never copy from ourselves).
    """
    self_row = conn.execute(
        """
        SELECT r.system_id, r.canonical_name, r.fuzzy_key, h.sha1
        FROM roms r
        LEFT JOIN hashes h ON h.rom_id = r.id
        WHERE r.id = ?
        LIMIT 1
        """,
        (rom_id,),
    ).fetchone()
    if self_row is None:
        return []

    sha1 = self_row["sha1"]
    system_id = self_row["system_id"]
    canonical_name = self_row["canonical_name"]
    fuzzy_key = self_row["fuzzy_key"]

    # Tier 1 — SHA-1 match
    if sha1:
        rows = conn.execute(
            """
            SELECT c.*
            FROM covers c
            JOIN hashes h ON h.rom_id = c.rom_id
            WHERE h.sha1 = ?
              AND c.rom_id != ?
            """,
            (sha1, rom_id),
        ).fetchall()
        if rows:
            return list(rows)

    # Tier 2 — (system_id, canonical_name) match
    if system_id and canonical_name:
        rows = conn.execute(
            """
            SELECT c.*
            FROM covers c
            JOIN roms r ON r.id = c.rom_id
            WHERE r.system_id = ?
              AND r.canonical_name = ?
              AND c.rom_id != ?
            """,
            (system_id, canonical_name, rom_id),
        ).fetchall()
        if rows:
            return list(rows)

    # Tier 3 — (system_id, fuzzy_key) match
    if system_id and fuzzy_key:
        rows = conn.execute(
            """
            SELECT c.*
            FROM covers c
            JOIN roms r ON r.id = c.rom_id
            WHERE r.system_id = ?
              AND r.fuzzy_key = ?
              AND c.rom_id != ?
            """,
            (system_id, fuzzy_key, rom_id),
        ).fetchall()
        if rows:
            return list(rows)

    return []


def copy_covers(
    conn: sqlite3.Connection, source_rom_id: int, dest_rom_id: int
) -> None:
    """Copy cover rows from ``source_rom_id`` to ``dest_rom_id``.

    The on-disk image file (``local_path``) is shared between ROM rows;
    no filesystem copy is performed. If ``source_rom_id`` has no cover rows
    this is a no-op. Existing cover rows for ``dest_rom_id`` are left in
    place — this is an additive operation, not a replace.

    After all rows are inserted, :func:`_ensure_preferred` is called for
    each distinct ``cover_type`` so the destination ROM ends up with exactly
    one preferred cover per type (required by the detail-panel display logic).

    Used by Session 15's copy-on-enrich logic.

    Args:
        conn: SQLite connection.
        source_rom_id: ROM whose covers to copy from.
        dest_rom_id: ROM to attach the copied covers to.
    """
    source_rows = conn.execute(
        "SELECT * FROM covers WHERE rom_id = ?", (source_rom_id,)
    ).fetchall()
    cover_types_seen: set[str] = set()
    for src in source_rows:
        # Skip rows where the destination already has a cover at the same path.
        # The covers table has no UNIQUE constraint on (rom_id, local_path) so
        # we guard manually to keep copy_covers idempotent.
        local_path = src["local_path"]
        if local_path and has_cover_for_path(conn, dest_rom_id, local_path):
            cover_types_seen.add(str(src["cover_type"]))
            continue
        conn.execute(
            """
            INSERT INTO covers (
                rom_id, cover_type, source_url, local_path,
                width, height, is_preferred
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dest_rom_id,
                src["cover_type"],
                src["source_url"],
                local_path,
                src["width"],
                src["height"],
                src["is_preferred"],
            ),
        )
        cover_types_seen.add(str(src["cover_type"]))
    # Ensure at least one preferred cover per type for the destination ROM.
    for cover_type in cover_types_seen:
        _ensure_preferred(conn, dest_rom_id, cover_type)
