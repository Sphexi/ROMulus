"""Database query functions — all SQL operations go through here.

Keeping SQL in one place makes it easier to audit, optimize indexes, and swap
storage backends if we ever need to. Other modules should call these helpers
rather than constructing their own queries.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from romulus.core.dat_parser import DatEntry

# ---------------------------------------------------------------------------
# ROMs
# ---------------------------------------------------------------------------


def upsert_rom(conn: sqlite3.Connection, rom_data: dict[str, Any]) -> int:
    """Insert a ROM row or update it in place if `path` already exists.

    Returns the row id of the upserted ROM. Caller is responsible for committing
    the surrounding transaction; this function does not commit on its own so
    bulk scans can batch many upserts.

    Required keys: path, filename, extension, size_bytes, mtime, system_id.
    Optional keys: fuzzy_key, header_title, scan_id, dat_match, match_confidence.
    """
    cursor = conn.execute(
        """
        INSERT INTO roms (
            path, filename, extension, size_bytes, mtime,
            system_id, scan_id, fuzzy_key, header_title,
            dat_match, match_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            match_confidence = excluded.match_confidence
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
            rom_data.get("match_confidence", "unmatched"),
        ),
    )
    if cursor.lastrowid:
        return cursor.lastrowid
    # ON CONFLICT UPDATE: lastrowid is 0; look up by path.
    row = conn.execute(
        "SELECT id FROM roms WHERE path = ?", (rom_data["path"],)
    ).fetchone()
    return row[0]


def get_roms_by_system(conn: sqlite3.Connection, system_id: str) -> list[sqlite3.Row]:
    """Return all ROM rows for the given system, ordered by filename."""
    rows = conn.execute(
        "SELECT * FROM roms WHERE system_id = ? ORDER BY filename",
        (system_id,),
    ).fetchall()
    return list(rows)


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


def upsert_game(conn: sqlite3.Connection, game_data: dict[str, Any]) -> int:
    """Insert a Game row, or return the existing id if one already matches.

    Matches by (system_id, title) — this is good enough for Quick Scan, where
    titles come from parsed filenames. Later sessions will add canonical-name
    matching via DAT lookups.

    Required keys: title, system_id.
    Optional: canonical_name, region, revision, is_hack, is_homebrew, is_bios.
    """
    existing = conn.execute(
        "SELECT id FROM games WHERE system_id = ? AND title = ?",
        (game_data["system_id"], game_data["title"]),
    ).fetchone()
    if existing:
        return existing[0]
    cursor = conn.execute(
        """
        INSERT INTO games (
            title, system_id, canonical_name, region, revision,
            is_hack, is_homebrew, is_bios
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            game_data["title"],
            game_data["system_id"],
            game_data.get("canonical_name"),
            game_data.get("region"),
            game_data.get("revision"),
            int(bool(game_data.get("is_hack", False))),
            int(bool(game_data.get("is_homebrew", False))),
            int(bool(game_data.get("is_bios", False))),
        ),
    )
    return cursor.lastrowid


def link_rom_to_game(conn: sqlite3.Connection, rom_id: int, game_id: int) -> None:
    """Set roms.game_id = game_id for a single ROM."""
    conn.execute("UPDATE roms SET game_id = ? WHERE id = ?", (game_id, rom_id))


def find_game_id_for_fuzzy_key(
    conn: sqlite3.Connection, system_id: str, fuzzy_key: str
) -> int | None:
    """Find an existing game id by joining through any ROM sharing the fuzzy key.

    This avoids storing fuzzy_key on the games table directly while still
    allowing the scanner to find "the game" that a new ROM belongs to.
    Returns None if no ROM with that key is yet linked to a game.
    """
    row = conn.execute(
        """
        SELECT DISTINCT g.id
        FROM games g
        JOIN roms r ON r.game_id = g.id
        WHERE g.system_id = ? AND r.fuzzy_key = ?
        LIMIT 1
        """,
        (system_id, fuzzy_key),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Scan history
# ---------------------------------------------------------------------------


def insert_scan_history(conn: sqlite3.Connection, scan_data: dict[str, Any]) -> int:
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
    return cursor.lastrowid


def update_scan_history(
    conn: sqlite3.Connection, scan_id: int, updates: dict[str, Any]
) -> None:
    """Update a scan_history row in place with arbitrary fields."""
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
    set_clause = ", ".join(f"{f} = ?" for f in fields)
    values = [updates[f] for f in fields] + [scan_id]
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

    `hashed_at` is stamped to the current wall-clock time; the heavy-scan
    pipeline compares this against `roms.mtime` to detect stale entries.
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
        (rom_id, crc32, sha1, md5, time.time()),
    )


def get_hash(conn: sqlite3.Connection, rom_id: int) -> sqlite3.Row | None:
    """Return the hash row for a ROM, or None if it hasn't been hashed yet."""
    return conn.execute(
        "SELECT * FROM hashes WHERE rom_id = ?", (rom_id,)
    ).fetchone()


def get_unhashed_roms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ROMs that have no row in the hashes table at all."""
    rows = conn.execute(
        """
        SELECT r.*
        FROM roms r
        LEFT JOIN hashes h ON h.rom_id = r.id
        WHERE h.rom_id IS NULL
        ORDER BY r.id
        """
    ).fetchall()
    return list(rows)


def get_stale_hashes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ROMs whose recorded mtime is newer than their last hash timestamp."""
    rows = conn.execute(
        """
        SELECT r.*
        FROM roms r
        JOIN hashes h ON h.rom_id = r.id
        WHERE h.hashed_at < r.mtime
        ORDER BY r.id
        """
    ).fetchall()
    return list(rows)


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
    return cursor.lastrowid


def get_dat_by_sha1(conn: sqlite3.Connection, sha1: str) -> sqlite3.Row | None:
    """Authoritative DAT lookup by SHA-1."""
    return conn.execute(
        "SELECT * FROM dat_entries WHERE sha1 = ? LIMIT 1",
        (sha1.lower(),),
    ).fetchone()


def get_dat_by_crc_size(
    conn: sqlite3.Connection, crc32: str, size_bytes: int
) -> sqlite3.Row | None:
    """Fallback DAT lookup by (CRC32, size). Returns None if more than one
    entry matches — ambiguous CRC32s shouldn't be auto-applied (ROM-DEDUP §5.4)."""
    rows = conn.execute(
        "SELECT * FROM dat_entries WHERE crc32 = ? AND size_bytes = ? LIMIT 2",
        (crc32.lower(), size_bytes),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
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
