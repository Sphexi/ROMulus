"""SQLite schema definitions for ROMulus v0.4.0+.

The ``games`` table has been removed. Every identity column that was on
``games`` (title, canonical_name, region, revision, is_hack, is_homebrew,
is_bios) now lives directly on the ``roms`` row. The model is strictly 1:1:
one ROM file = one ``roms`` row with its own metadata, covers, and collection
membership.

``ON DELETE CASCADE`` is declared on every FK that references ``roms.id`` so
deleting a rom row atomically cleans up its metadata, covers, and collection
memberships — no explicit ``prune_*`` step required.

No migration framework. Per CLAUDE.md rule #14, pre-v0.4.0 databases must be
wiped and rebuilt. Detection: :func:`romulus.app.initialize_database` checks
``PRAGMA table_info(games)`` and aborts with :data:`REQUIRES_FRESH_DB_MESSAGE`
if the old table is found.
"""

from __future__ import annotations

import sqlite3

#: Human-readable error surfaced when a pre-v0.4.0 database is detected.
REQUIRES_FRESH_DB_MESSAGE: str = (
    "Your library database predates v0.4.0 (it still has a 'games' table). "
    "Delete data/romulus.db and rescan to upgrade to the new 1:1 ROM model."
)

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
    # ROM files on disk — identity unit in the 1:1 model.
    # Identity columns (title … is_bios) come from filename parse (Quick Scan)
    # or DAT match (Heavy Scan); omitting them on upsert preserves any existing
    # values via the COALESCE pattern in upsert_rom.
    """
    CREATE TABLE IF NOT EXISTS roms (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        path             TEXT NOT NULL UNIQUE,
        filename         TEXT NOT NULL,
        extension        TEXT NOT NULL,
        size_bytes       INTEGER NOT NULL,
        mtime            REAL NOT NULL,
        system_id        TEXT REFERENCES systems(id),
        scan_id          INTEGER REFERENCES scan_history(id),
        fuzzy_key        TEXT,
        header_title     TEXT,
        dat_match        TEXT,
        match_confidence TEXT DEFAULT 'unmatched',
        library_root     TEXT,
        missing          INTEGER NOT NULL DEFAULT 0,
        title            TEXT,
        canonical_name   TEXT,
        region           TEXT,
        revision         TEXT,
        is_hack          INTEGER NOT NULL DEFAULT 0,
        is_homebrew      INTEGER NOT NULL DEFAULT 0,
        is_bios          INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_roms_system ON roms(system_id)",
    "CREATE INDEX IF NOT EXISTS idx_roms_fuzzy ON roms(system_id, fuzzy_key)",
    "CREATE INDEX IF NOT EXISTS idx_roms_title ON roms(system_id, title)",
    "CREATE INDEX IF NOT EXISTS idx_roms_library_root ON roms(library_root)",
    "CREATE INDEX IF NOT EXISTS idx_roms_missing ON roms(missing) WHERE missing = 1",
    # Hash cache (expensive to compute; reused if mtime unchanged)
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
    # Per-ROM metadata (1:1 with roms; CASCADE so deleting a rom drops its row)
    """
    CREATE TABLE IF NOT EXISTS metadata (
        rom_id       INTEGER PRIMARY KEY REFERENCES roms(id) ON DELETE CASCADE,
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
    # Cover art references (CASCADE so deleting a rom drops its cover rows)
    """
    CREATE TABLE IF NOT EXISTS covers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        rom_id       INTEGER REFERENCES roms(id) ON DELETE CASCADE,
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
    # Collection membership — renamed from collection_games; CASCADE on rom delete
    """
    CREATE TABLE IF NOT EXISTS collection_roms (
        collection_id INTEGER REFERENCES collections(id),
        rom_id        INTEGER REFERENCES roms(id) ON DELETE CASCADE,
        PRIMARY KEY (collection_id, rom_id)
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
    # Destination sync — saved targets (§4.1)
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
    # Destination sync — cached file state per destination (§4.2)
    # game_id column REMOVED; rom_id is the sole anchor.
    """
    CREATE TABLE IF NOT EXISTS dest_inventory (
        dest_id      INTEGER NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
        rel_path     TEXT NOT NULL,
        size_bytes   INTEGER NOT NULL,
        mtime        REAL NOT NULL,
        sha1         TEXT,
        rom_id       INTEGER REFERENCES roms(id),
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (dest_id, rel_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dest_inventory_sha1 ON dest_inventory(sha1)",
    "CREATE INDEX IF NOT EXISTS idx_dest_inventory_rom ON dest_inventory(rom_id)",
    # Destination sync — persisted plans for history + resume (§4.3)
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


def create_tables(conn: sqlite3.Connection) -> None:
    """Execute every CREATE TABLE / CREATE INDEX statement in order.

    Idempotent via ``IF NOT EXISTS``. Safe to call on every startup.

    Note: Does NOT check for the legacy ``games`` table — that guard lives in
    :func:`romulus.app.initialize_database` so it fires before any UI is shown,
    not silently inside schema init.
    """
    cursor = conn.cursor()
    for statement in SCHEMA_STATEMENTS:
        cursor.execute(statement)
    conn.commit()
