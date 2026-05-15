"""Database query functions — all SQL operations go through here.

Keeping SQL in one place makes it easier to audit, optimize indexes, and swap
storage backends if we ever need to. Other modules should call these helpers
rather than constructing their own queries.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from romulus.core.dat_parser import DatEntry
    from romulus.metadata._types import MetadataPayload

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
    """Shape of the dict accepted by :func:`upsert_rom`."""

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


class GameUpsertData(TypedDict):
    """Shape of the dict accepted by :func:`upsert_game`."""

    title: str
    system_id: str
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
    """Insert a ROM row or update it in place if `path` already exists.

    Returns the row id of the upserted ROM. Caller is responsible for committing
    the surrounding transaction; this function does not commit on its own so
    bulk scans can batch many upserts.

    `match_confidence` is monotonic — a rescan never downgrades a previously
    stronger match (e.g. dat_verified) back to a weaker one (fuzzy).

    Required keys: path, filename, extension, size_bytes, mtime, system_id.
    Optional keys: fuzzy_key, header_title, scan_id, dat_match, match_confidence.
    """
    incoming_confidence = rom_data.get("match_confidence", "unmatched")
    incoming_rank = CONFIDENCE_RANK.get(incoming_confidence, 0)
    cursor = conn.execute(
        f"""
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
            match_confidence = CASE
                WHEN ? >= {_UPSERT_ROM_CONFIDENCE_CASE}
                THEN excluded.match_confidence
                ELSE roms.match_confidence
            END
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
            incoming_rank,
        ),
    )
    if cursor.lastrowid:
        return cursor.lastrowid
    # ON CONFLICT UPDATE: lastrowid is 0; look up by path.
    row = conn.execute(
        "SELECT id FROM roms WHERE path = ?", (rom_data["path"],)
    ).fetchone()
    return row["id"]


def get_roms_by_system(conn: sqlite3.Connection, system_id: str) -> list[sqlite3.Row]:
    """Return all ROM rows for the given system, ordered by filename."""
    return conn.execute(
        "SELECT * FROM roms WHERE system_id = ? ORDER BY filename",
        (system_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


def upsert_game(conn: sqlite3.Connection, game_data: GameUpsertData) -> int:
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
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into games did not produce a lastrowid"
    return new_id


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
    return row["id"] if row else None


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
    against the hard-coded `allowed` whitelist before interpolation, so this
    is not an injection vector — values are still passed as `?` parameters.
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
    # Safe: each name in `fields` is guaranteed to be in the `allowed` whitelist.
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
    """Authoritative DAT lookup by SHA-1. Returns None if `sha1` is falsy."""
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
        # Lazy logger import — queries.py is otherwise log-free, and pulling
        # logging at module level would only matter for this one rare path.
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
    "players",
    "rating",
)


def upsert_metadata(
    conn: sqlite3.Connection,
    game_id: int,
    metadata: MetadataPayload,
    source: str,
) -> None:
    """Insert or replace a game's metadata row.

    Unknown keys are ignored; missing keys are stored as NULL. `source` records
    which provider (hasheous / launchbox / screenscraper) supplied the row.
    """
    values = [metadata.get(field) for field in _METADATA_FIELDS]  # type: ignore[literal-required]
    conn.execute(
        """
        INSERT INTO metadata (
            game_id, description, genre, developer, publisher,
            release_date, players, rating, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            description  = excluded.description,
            genre        = excluded.genre,
            developer    = excluded.developer,
            publisher    = excluded.publisher,
            release_date = excluded.release_date,
            players      = excluded.players,
            rating       = excluded.rating,
            source       = excluded.source
        """,
        (game_id, *values, source),
    )


def get_metadata(conn: sqlite3.Connection, game_id: int) -> sqlite3.Row | None:
    """Return the metadata row for a game, or None if unenriched."""
    return conn.execute(
        "SELECT * FROM metadata WHERE game_id = ?", (game_id,)
    ).fetchone()


def insert_cover(
    conn: sqlite3.Connection,
    game_id: int,
    cover_type: str,
    source_url: str | None,
    local_path: str | None,
    width: int | None = None,
    height: int | None = None,
) -> int:
    """Insert a covers row and return its id."""
    cursor = conn.execute(
        """
        INSERT INTO covers (game_id, cover_type, source_url, local_path, width, height)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, cover_type, source_url, local_path, width, height),
    )
    new_id = cursor.lastrowid
    assert new_id is not None, "INSERT into covers did not produce a lastrowid"
    return new_id


def get_covers(conn: sqlite3.Connection, game_id: int) -> list[sqlite3.Row]:
    """Return all cover rows for a game, oldest first."""
    return conn.execute(
        "SELECT * FROM covers WHERE game_id = ? ORDER BY id", (game_id,)
    ).fetchall()


def has_cover(conn: sqlite3.Connection, game_id: int, cover_type: str) -> bool:
    """True if a cover row of the given type already exists for this game."""
    row = conn.execute(
        "SELECT 1 FROM covers WHERE game_id = ? AND cover_type = ? LIMIT 1",
        (game_id, cover_type),
    ).fetchone()
    return row is not None


def get_games_needing_enrichment(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return games with DAT-verified ROMs but no metadata row yet.

    Joins games -> roms (filtered to dat_verified matches) -> LEFT JOIN metadata
    so we only surface games that have a canonical hit but haven't been enriched.
    """
    return conn.execute(
        """
        SELECT DISTINCT g.id, g.title, g.system_id, g.canonical_name,
               r.dat_match AS dat_match
        FROM games g
        JOIN roms r ON r.game_id = g.id
        LEFT JOIN metadata m ON m.game_id = g.id
        WHERE r.match_confidence = 'dat_verified'
          AND m.game_id IS NULL
        ORDER BY g.system_id, g.title
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Detail panel lookups
# ---------------------------------------------------------------------------


def get_game_by_id(conn: sqlite3.Connection, game_id: int) -> sqlite3.Row | None:
    """Return the games row for `game_id`, or None if it does not exist."""
    return conn.execute(
        "SELECT * FROM games WHERE id = ?", (game_id,)
    ).fetchone()


def get_rom_by_id(conn: sqlite3.Connection, rom_id: int) -> sqlite3.Row | None:
    """Return the roms row for `rom_id`, or None if it does not exist."""
    return conn.execute(
        "SELECT * FROM roms WHERE id = ?", (rom_id,)
    ).fetchone()


def get_roms_for_game(conn: sqlite3.Connection, game_id: int) -> list[sqlite3.Row]:
    """Return every ROM linked to a game, ordered by filename."""
    return conn.execute(
        "SELECT * FROM roms WHERE game_id = ? ORDER BY filename",
        (game_id,),
    ).fetchall()


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
    """Remove a collection and every game-link pointing at it.

    System collections (is_system=1) are protected — calling this on one raises
    ValueError so callers must guard against it explicitly.
    """
    row = conn.execute(
        "SELECT is_system FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()
    if row is None:
        return
    if int(row["is_system"]):
        raise ValueError("Cannot delete system collection")
    conn.execute(
        "DELETE FROM collection_games WHERE collection_id = ?", (collection_id,)
    )
    conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    conn.commit()


def add_game_to_collection(
    conn: sqlite3.Connection, collection_id: int, game_id: int
) -> None:
    """Link a game to a collection. Idempotent — duplicate inserts are ignored."""
    conn.execute(
        "INSERT OR IGNORE INTO collection_games (collection_id, game_id) VALUES (?, ?)",
        (collection_id, game_id),
    )
    conn.commit()


def remove_game_from_collection(
    conn: sqlite3.Connection, collection_id: int, game_id: int
) -> None:
    """Remove a single game from a collection."""
    conn.execute(
        "DELETE FROM collection_games WHERE collection_id = ? AND game_id = ?",
        (collection_id, game_id),
    )
    conn.commit()


def get_collection_games(
    conn: sqlite3.Connection, collection_id: int
) -> list[int]:
    """Return every game_id linked to a collection, ordered by insertion."""
    rows = conn.execute(
        "SELECT game_id FROM collection_games WHERE collection_id = ?",
        (collection_id,),
    ).fetchall()
    return [int(row["game_id"]) for row in rows]


def get_collections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every collection row with a game_count aggregate.

    Columns: `id, name, description, is_system, game_count`. System collections
    (is_system=1) sort to the top so the UI can render Favorites above any
    user-created entries.
    """
    return conn.execute(
        """
        SELECT c.id, c.name, c.description, c.is_system,
               COUNT(cg.game_id) AS game_count
        FROM collections c
        LEFT JOIN collection_games cg ON cg.collection_id = c.id
        GROUP BY c.id, c.name, c.description, c.is_system
        ORDER BY c.is_system DESC, c.name
        """
    ).fetchall()


def get_collection_by_name(
    conn: sqlite3.Connection, name: str
) -> sqlite3.Row | None:
    """Return the collection row with this name, or None."""
    return conn.execute(
        "SELECT * FROM collections WHERE name = ?", (name,)
    ).fetchone()


def is_game_in_collection(
    conn: sqlite3.Connection, collection_id: int, game_id: int
) -> bool:
    """True if a (collection, game) link row exists."""
    row = conn.execute(
        "SELECT 1 FROM collection_games WHERE collection_id = ? AND game_id = ? LIMIT 1",
        (collection_id, game_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Organize / library reorganization
# ---------------------------------------------------------------------------


def get_alias_folder_pairs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return ROMs grouped by (system_id, folder) for alias-folder analysis.

    Each row contains `system_id`, the lowercase basename of the parent folder
    (`folder`), and a ROM count. The organizer compares this against the system
    registry's `folder_aliases` list to detect non-canonical folders that should
    be merged into the canonical one.
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

    Joins `roms` with `hashes` and yields one row per (sha1, rom_id) for every
    SHA-1 that appears on two or more ROMs. Hacks (games.is_hack=1) are excluded
    so the organizer never proposes deduping a hack against its original even
    if the hasher (incorrectly) reported identical content.
    """
    return conn.execute(
        """
        SELECT h.sha1, r.id AS rom_id, r.path, r.filename, r.extension,
               r.system_id, r.game_id, r.size_bytes,
               COALESCE(g.is_hack, 0) AS is_hack
        FROM hashes h
        JOIN roms r ON r.id = h.rom_id
        LEFT JOIN games g ON g.id = r.game_id
        WHERE h.sha1 IS NOT NULL
          AND h.sha1 IN (
              SELECT sha1
              FROM hashes
              WHERE sha1 IS NOT NULL
              GROUP BY sha1
              HAVING COUNT(*) > 1
          )
          AND COALESCE(g.is_hack, 0) = 0
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
               r.game_id, r.dat_match, r.match_confidence
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
    """
    conn.execute(
        "UPDATE roms SET path = ?, filename = ? WHERE id = ?",
        (new_path, new_filename, rom_id),
    )


def delete_rom(conn: sqlite3.Connection, rom_id: int) -> None:
    """Remove a ROM row (and its hash row) after a duplicate removal.

    Caller is responsible for committing the surrounding transaction.
    """
    conn.execute("DELETE FROM hashes WHERE rom_id = ?", (rom_id,))
    conn.execute("DELETE FROM roms WHERE id = ?", (rom_id,))


def insert_organize_plan(
    conn: sqlite3.Connection, plan_json: str, status: str = "pending"
) -> int:
    """Record an organize plan (its serialized JSON) and return its row id."""
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
    """Stamp an organize plan with its terminal status (applied/cancelled/failed)."""
    conn.execute(
        "UPDATE organize_plans SET status = ? WHERE id = ?", (status, plan_id)
    )
    conn.commit()


def datetime_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (used by plan rows)."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Enrichment-status query (used by the game table's enrichment filter)
# ---------------------------------------------------------------------------


def get_roms_with_games(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every ROM that is linked to a game, with identifiers for cover matching.

    Columns: rom_id, rom_path, system_id, game_id, fuzzy_key, clean_name.
    ROMs without a game_id are excluded because covers attach to games, not ROMs.
    Used by the local cover discovery pipeline.
    """
    return conn.execute(
        """
        SELECT r.id       AS rom_id,
               r.path     AS rom_path,
               r.system_id,
               r.game_id,
               r.fuzzy_key,
               COALESCE(r.dat_match, '') AS clean_name
        FROM roms r
        WHERE r.game_id IS NOT NULL
          AND r.fuzzy_key IS NOT NULL
          AND r.fuzzy_key != ''
        ORDER BY r.system_id, r.id
        """
    ).fetchall()


def has_cover_for_path(
    conn: sqlite3.Connection, game_id: int, local_path: str
) -> bool:
    """Return True if a covers row with this exact local_path already exists for the game.

    Used by the local cover discovery pipeline for idempotent re-runs — avoids
    inserting duplicate rows when discovery is run more than once.

    Args:
        conn: SQLite connection.
        game_id: The game to check.
        local_path: Absolute path string of the candidate image file.
    """
    row = conn.execute(
        "SELECT 1 FROM covers WHERE game_id = ? AND local_path = ? LIMIT 1",
        (game_id, local_path),
    ).fetchone()
    return row is not None


def get_rom_ids_for_scope(
    conn: sqlite3.Connection,
    *,
    game_id: int | None = None,
    system_id: str | None = None,
    collection_id: int | None = None,
) -> list[int]:
    """Resolve a UI scope into a flat list of ROM ids.

    Exactly one of the keyword arguments should be supplied. If none are given,
    an empty list is returned (callers should treat that as "no scope, use
    default behaviour"). If multiple are supplied, the narrowest wins:
    ``game_id`` > ``system_id`` > ``collection_id``.

    Args:
        conn: SQLite connection.
        game_id: Return ROMs belonging to a single game.
        system_id: Return all ROMs in a system.
        collection_id: Return ROMs belonging to games in the collection.

    Returns:
        Ordered list of rom row ids matching the scope.
    """
    if game_id is not None:
        rows = conn.execute(
            "SELECT id FROM roms WHERE game_id = ? ORDER BY id",
            (game_id,),
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
            SELECT r.id
            FROM roms r
            JOIN collection_games cg ON cg.game_id = r.game_id
            WHERE cg.collection_id = ?
            ORDER BY r.id
            """,
            (collection_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    return []


def get_game_ids_for_scope(
    conn: sqlite3.Connection,
    *,
    game_id: int | None = None,
    system_id: str | None = None,
    collection_id: int | None = None,
) -> list[int] | None:
    """Resolve a UI scope into a list of game ids for enrichment filtering.

    Returns ``None`` when no scope is specified (meaning "all games").
    Returns a (possibly empty) list when a specific scope is requested.

    Args:
        conn: SQLite connection.
        game_id: Scope to a single game.
        system_id: Scope to all games in a system.
        collection_id: Scope to games in a collection.
    """
    if game_id is not None:
        return [game_id]

    if system_id is not None:
        rows = conn.execute(
            "SELECT DISTINCT id FROM games WHERE system_id = ? ORDER BY id",
            (system_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    if collection_id is not None:
        rows = conn.execute(
            "SELECT DISTINCT game_id FROM collection_games"
            " WHERE collection_id = ? ORDER BY game_id",
            (collection_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    return None


def get_games_with_enrichment_status(
    conn: sqlite3.Connection,
    system_id: str | None = None,
    game_ids: list[int] | None = None,
    limit: int = 5000,
) -> list[sqlite3.Row]:
    """Return game rows annotated with ``has_cover`` and ``has_metadata`` flags.

    This is the backing query for the game-table enrichment filter added in the
    UI/UX pass.  Each returned row exposes the same columns as the base
    ``load_rom_rows`` query PLUS:

    * ``has_cover``    — 1 if at least one covers row exists for this game
    * ``has_metadata`` — 1 if a metadata row exists for this game
    * ``rom_path``     — path of the first linked ROM (ORDER BY filename)
    """
    base = """
        SELECT
            r.id            AS rom_id,
            r.filename      AS name,
            r.system_id,
            r.path          AS rom_path,
            COALESCE(s.short_name, s.display_name, r.system_id) AS system_name,
            COALESCE(g.region, '')  AS region,
            r.size_bytes,
            r.match_confidence,
            r.game_id,
            CASE WHEN c.game_id IS NOT NULL THEN 1 ELSE 0 END AS has_cover,
            CASE WHEN m.game_id IS NOT NULL THEN 1 ELSE 0 END AS has_metadata
        FROM roms r
        LEFT JOIN systems   s ON s.id = r.system_id
        LEFT JOIN games     g ON g.id = r.game_id
        LEFT JOIN (
            SELECT DISTINCT game_id FROM covers
        ) c ON c.game_id = r.game_id
        LEFT JOIN (
            SELECT DISTINCT game_id FROM metadata
        ) m ON m.game_id = r.game_id
    """
    clauses: list[str] = []
    params: list[object] = []
    if system_id is not None:
        clauses.append("r.system_id = ?")
        params.append(system_id)
    if game_ids is not None:
        if not game_ids:
            return []
        placeholders = ",".join("?" for _ in game_ids)
        clauses.append(f"r.game_id IN ({placeholders})")
        params.extend(game_ids)
    if clauses:
        base += " WHERE " + " AND ".join(clauses)
    base += " ORDER BY r.filename LIMIT ?"
    params.append(limit)
    return conn.execute(base, params).fetchall()
