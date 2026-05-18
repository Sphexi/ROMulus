"""SQLite schema definitions and migration support.

The full schema is described in `docs/TECHNICAL_PLAN.md` §3. All CREATE TABLE
statements use IF NOT EXISTS so `create_tables()` is safe to call on every app
startup. There is no migration framework yet — schema changes are additive
for now (new tables, new columns with defaults).
"""

from __future__ import annotations

import sqlite3

SCHEMA_STATEMENTS: list[str] = [
    # App configuration (key-value)
    """
    CREATE TABLE IF NOT EXISTS config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # System/platform definitions (seeded from system registry)
    """
    CREATE TABLE IF NOT EXISTS systems (
        id              TEXT PRIMARY KEY,
        display_name    TEXT NOT NULL,
        short_name      TEXT NOT NULL,
        manufacturer    TEXT,
        generation      INTEGER,
        extensions      TEXT NOT NULL,
        header_rule     TEXT,
        libretro_name   TEXT,
        folder_aliases  TEXT NOT NULL,
        dat_name        TEXT
    )
    """,
    # Logical games (referenced by roms via FK; create before roms)
    """
    CREATE TABLE IF NOT EXISTS games (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        system_id       TEXT REFERENCES systems(id),
        canonical_name  TEXT,
        region          TEXT,
        revision        TEXT,
        is_hack         INTEGER DEFAULT 0,
        is_homebrew     INTEGER DEFAULT 0,
        is_bios         INTEGER DEFAULT 0
    )
    """,
    # Scan history (referenced by roms via FK; create before roms)
    """
    CREATE TABLE IF NOT EXISTS scan_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_type     TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        root_path     TEXT NOT NULL,
        files_found   INTEGER DEFAULT 0,
        files_matched INTEGER DEFAULT 0,
        files_new     INTEGER DEFAULT 0,
        errors        INTEGER DEFAULT 0
    )
    """,
    # ROM files on disk
    """
    CREATE TABLE IF NOT EXISTS roms (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        path             TEXT NOT NULL UNIQUE,
        filename         TEXT NOT NULL,
        extension        TEXT NOT NULL,
        size_bytes       INTEGER NOT NULL,
        mtime            REAL NOT NULL,
        system_id        TEXT REFERENCES systems(id),
        game_id          INTEGER REFERENCES games(id),
        scan_id          INTEGER REFERENCES scan_history(id),
        fuzzy_key        TEXT,
        header_title     TEXT,
        dat_match        TEXT,
        match_confidence TEXT DEFAULT 'unmatched',
        library_root     TEXT,
        missing          INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_roms_system ON roms(system_id)",
    "CREATE INDEX IF NOT EXISTS idx_roms_fuzzy ON roms(system_id, fuzzy_key)",
    "CREATE INDEX IF NOT EXISTS idx_roms_game ON roms(game_id)",
    "CREATE INDEX IF NOT EXISTS idx_roms_library_root ON roms(library_root)",
    "CREATE INDEX IF NOT EXISTS idx_roms_missing ON roms(missing) WHERE missing = 1",
    # Hash cache (expensive to compute, reused if mtime unchanged)
    """
    CREATE TABLE IF NOT EXISTS hashes (
        rom_id    INTEGER PRIMARY KEY REFERENCES roms(id),
        crc32     TEXT,
        sha1      TEXT,
        md5       TEXT,
        hashed_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hashes_sha1 ON hashes(sha1)",
    "CREATE INDEX IF NOT EXISTS idx_hashes_crc32 ON hashes(crc32)",
    # DAT entries (parsed from No-Intro/Redump/TOSEC XML files)
    """
    CREATE TABLE IF NOT EXISTS dat_entries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        dat_file   TEXT NOT NULL,
        system_id  TEXT REFERENCES systems(id),
        game_name  TEXT NOT NULL,
        rom_name   TEXT NOT NULL,
        size_bytes INTEGER,
        crc32      TEXT,
        md5        TEXT,
        sha1       TEXT,
        region     TEXT,
        revision   TEXT,
        is_bios    INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dat_sha1 ON dat_entries(sha1)",
    "CREATE INDEX IF NOT EXISTS idx_dat_crc32_size ON dat_entries(crc32, size_bytes)",
    # Game metadata (from enrichment sources)
    """
    CREATE TABLE IF NOT EXISTS metadata (
        game_id      INTEGER PRIMARY KEY REFERENCES games(id),
        description  TEXT,
        genre        TEXT,
        developer    TEXT,
        publisher    TEXT,
        release_date TEXT,
        release_year INTEGER,
        players      TEXT,
        rating       TEXT,
        source       TEXT NOT NULL
    )
    """,
    # Cover art references
    """
    CREATE TABLE IF NOT EXISTS covers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id      INTEGER REFERENCES games(id),
        cover_type   TEXT NOT NULL,
        source_url   TEXT,
        local_path   TEXT,
        width        INTEGER,
        height       INTEGER,
        is_preferred INTEGER NOT NULL DEFAULT 0
    )
    """,
    # User collections
    """
    CREATE TABLE IF NOT EXISTS collections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        description TEXT,
        is_system   INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_games (
        collection_id INTEGER REFERENCES collections(id),
        game_id       INTEGER REFERENCES games(id),
        PRIMARY KEY (collection_id, game_id)
    )
    """,
    # Organization plans (preview before commit)
    """
    CREATE TABLE IF NOT EXISTS organize_plans (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        status     TEXT DEFAULT 'pending',
        plan_json  TEXT NOT NULL
    )
    """,
    # Destination sync — saved targets the user can re-pick (§4.1).
    """
    CREATE TABLE IF NOT EXISTS sync_destinations (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        name                     TEXT NOT NULL UNIQUE,
        target_path              TEXT NOT NULL,
        profile_id               TEXT NOT NULL,
        last_synced_at           TEXT,
        created_at               TEXT NOT NULL,
        last_inventory_signature TEXT
    )
    """,
    # Destination sync — cached file state per destination (§4.2).
    """
    CREATE TABLE IF NOT EXISTS dest_inventory (
        dest_id      INTEGER NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
        rel_path     TEXT NOT NULL,
        size_bytes   INTEGER NOT NULL,
        mtime        REAL NOT NULL,
        sha1         TEXT,
        rom_id       INTEGER REFERENCES roms(id),
        game_id      INTEGER REFERENCES games(id),
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (dest_id, rel_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dest_inventory_sha1 ON dest_inventory(sha1)",
    "CREATE INDEX IF NOT EXISTS idx_dest_inventory_rom ON dest_inventory(rom_id)",
    # Destination sync — persisted plans for history + resume (§4.3).
    """
    CREATE TABLE IF NOT EXISTS sync_plans (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        dest_id    INTEGER NOT NULL REFERENCES sync_destinations(id),
        mode       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status     TEXT DEFAULT 'pending',
        summary    TEXT NOT NULL,
        plan_json  TEXT NOT NULL
    )
    """,
]


def _migrate_sync_destinations(conn: sqlite3.Connection) -> None:
    """Create the sync_destinations table on legacy DBs that predate v0.2.0.

    Mirrors :func:`_migrate_covers_add_is_preferred`'s ``PRAGMA table_info``
    detection. The ``CREATE TABLE IF NOT EXISTS`` statement above already runs
    during normal create_tables flow; this helper is the explicit migration
    hook called from :func:`create_tables` per the sync-design spec §4.
    """
    rows = conn.execute("PRAGMA table_info(sync_destinations)").fetchall()
    if rows:
        return
    conn.execute(
        """
        CREATE TABLE sync_destinations (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            name                     TEXT NOT NULL UNIQUE,
            target_path              TEXT NOT NULL,
            profile_id               TEXT NOT NULL,
            last_synced_at           TEXT,
            created_at               TEXT NOT NULL,
            last_inventory_signature TEXT
        )
        """
    )
    conn.commit()


def _migrate_dest_inventory(conn: sqlite3.Connection) -> None:
    """Create the dest_inventory table on legacy DBs that predate v0.2.0."""
    rows = conn.execute("PRAGMA table_info(dest_inventory)").fetchall()
    if rows:
        return
    conn.execute(
        """
        CREATE TABLE dest_inventory (
            dest_id      INTEGER NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
            rel_path     TEXT NOT NULL,
            size_bytes   INTEGER NOT NULL,
            mtime        REAL NOT NULL,
            sha1         TEXT,
            rom_id       INTEGER REFERENCES roms(id),
            game_id      INTEGER REFERENCES games(id),
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (dest_id, rel_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dest_inventory_sha1 ON dest_inventory(sha1)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dest_inventory_rom ON dest_inventory(rom_id)"
    )
    conn.commit()


def _migrate_sync_plans(conn: sqlite3.Connection) -> None:
    """Create the sync_plans table on legacy DBs that predate v0.2.0."""
    rows = conn.execute("PRAGMA table_info(sync_plans)").fetchall()
    if rows:
        return
    conn.execute(
        """
        CREATE TABLE sync_plans (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            dest_id    INTEGER NOT NULL REFERENCES sync_destinations(id),
            mode       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status     TEXT DEFAULT 'pending',
            summary    TEXT NOT NULL,
            plan_json  TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _migrate_covers_add_is_preferred(conn: sqlite3.Connection) -> None:
    """Add ``is_preferred`` to covers if the column is absent (backward-compat).

    Uses ``PRAGMA table_info`` to detect missing columns so the ALTER is
    idempotent — safe to call on every startup regardless of DB age.

    After adding the column, the first cover row per ``(game_id, cover_type)``
    is promoted to ``is_preferred=1`` so existing users see their covers
    immediately without any manual action.
    """
    rows = conn.execute("PRAGMA table_info(covers)").fetchall()
    existing_columns = {row["name"] for row in rows}
    if "is_preferred" not in existing_columns:
        conn.execute(
            "ALTER TABLE covers ADD COLUMN is_preferred INTEGER NOT NULL DEFAULT 0"
        )
        # Promote the first (lowest id) row per (game_id, cover_type).
        conn.execute(
            """
            UPDATE covers
            SET is_preferred = 1
            WHERE id IN (
                SELECT MIN(id)
                FROM covers
                GROUP BY game_id, cover_type
            )
            """
        )
        conn.commit()


def create_tables(conn: sqlite3.Connection) -> None:
    """Execute every CREATE TABLE / CREATE INDEX statement in order.

    Idempotent via IF NOT EXISTS. Safe to call on every startup.
    Runs :func:`_migrate_covers_add_is_preferred` after the schema statements
    so existing databases gain ``is_preferred`` on first launch.
    """
    cursor = conn.cursor()
    for statement in SCHEMA_STATEMENTS:
        cursor.execute(statement)
    conn.commit()
    _migrate_covers_add_is_preferred(conn)
    _migrate_sync_destinations(conn)
    _migrate_dest_inventory(conn)
    _migrate_sync_plans(conn)
