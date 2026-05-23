# Session 13: Strict 1:1 — Schema + Queries Layer

**Type:** Build (Phase 1 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

The project moves from a "1 game : N ROMs" data model (one `games` row aggregating every regional/variant copy) to a strict **1:1 model where the ROM file IS the identity unit**. Every ROM file gets its own row carrying its own title, region, revision, metadata, covers, and collection membership. Byte-identical copies at two different paths are two distinct rows — that's the point: the user wants visibility into duplicates by sorting/searching, not implicit folding.

The `games` table is being **deleted entirely**. Its columns merge onto `roms`. Every other table that referenced `game_id` now references `rom_id` 1:1, with `ON DELETE CASCADE` so deleting a rom row cleans up its metadata/covers/collection memberships atomically.

**No migration. No backward-compat shims.** Per CLAUDE.md design rule #14, pre-v0.3.0 schemas were already non-migratable; this is the same deal — wipe `data/romulus.db` and rescan.

**Sessions 14–19 depend on this one.** Each subsequent phase renames its callers (`game_id` → `rom_id`) to match the schema this session ships. The full suite **will not be green at the end of this session** — every caller of the deleted/renamed query functions is broken until its phase lands. That's expected; session 19 is the test re-baseline.

In one sentence: **"Delete `games`, expand `roms`, rename every FK in every other table, rewrite the queries module to match."**

**Workflow:**

1. Rewrite `src/romulus/db/schema.py` with the new shape; drop legacy migration helpers (no users to protect).
2. Rewrite `src/romulus/db/queries.py`: delete the game-keyed functions, rename `game_id` → `rom_id` parameters/columns throughout, expand `upsert_rom` to accept identity fields.
3. Add forward-looking helpers (`find_sibling_metadata`, `copy_metadata`) so Session 15 has the API surface it needs.
4. Update db-layer module-level imports where the deleted symbols were re-exported.

**Carry-forward from prior sessions:**

- **`upsert_rom` is the single ROM-creation path.** Today's `RomUpsertData` ([db/queries.py:57-71](../../src/romulus/db/queries.py#L57-L71)) gets new optional fields: `title`, `canonical_name`, `region`, `revision`, `is_hack`, `is_homebrew`, `is_bios`. Caller (scanner / importer) fills them from the filename parse; Heavy Scan UPDATEs them later from `dat_match`. The path-keyed UPSERT contract from design rule #7 (tombstone, don't delete) survives unchanged.
- **`CONFIDENCE_RANK` stays.** The ordering vocabulary (`dat_verified > header > fuzzy > unmatched`) is rom-level data — already on `roms.match_confidence` — and the rank table in `db/queries.py` doesn't move.
- **`hashes` table is already rom-keyed.** No changes there.
- **`dat_entries` table is system-keyed, not game-keyed.** No changes.
- **`scan_history` is unchanged.**
- **`sync_destinations` / `sync_plans` are unchanged.** Only `dest_inventory.game_id` column gets dropped.

**Tasks:**

- [ ] Rewrite `src/romulus/db/schema.py`:
  - Drop the `games` CREATE TABLE statement entirely.
  - Expand the `roms` CREATE TABLE to include the merged columns:
    ```sql
    title           TEXT,
    canonical_name  TEXT,
    region          TEXT,
    revision        TEXT,
    is_hack         INTEGER NOT NULL DEFAULT 0,
    is_homebrew     INTEGER NOT NULL DEFAULT 0,
    is_bios         INTEGER NOT NULL DEFAULT 0,
    ```
    Keep `header_title` (used by L2 identifier).
  - Drop `roms.game_id` column.
  - Drop `idx_roms_game` index.
  - Add `idx_roms_title` index on `(system_id, title)` for the sibling-metadata-lookup query in Session 15.
  - Rewrite `metadata` CREATE: `rom_id INTEGER PRIMARY KEY REFERENCES roms(id) ON DELETE CASCADE`.
  - Rewrite `covers` CREATE: `rom_id INTEGER REFERENCES roms(id) ON DELETE CASCADE`.
  - Rewrite the join table: `collection_roms (collection_id, rom_id PRIMARY KEY (collection_id, rom_id))` with `ON DELETE CASCADE` on `rom_id`.
  - Drop `dest_inventory.game_id` column. `rom_id` remains as the anchor.
  - Delete all of `_migrate_*` helpers — no migration framework on a fresh schema.
  - Add `PRAGMA foreign_keys = ON` enforcement to `db/connection.py` if not already on (FK cascades only fire when enabled).
- [ ] Rewrite `src/romulus/db/queries.py`:
  - **Delete entirely:**
    - `upsert_game`
    - `link_rom_to_game`
    - `find_game_id_for_fuzzy_key`
    - `get_game_by_id`
    - `get_roms_for_game`
    - `prune_orphan_games` (ON DELETE CASCADE replaces it)
    - `_delete_game_dependents` (same)
    - `find_game_id_for_dat_match` (won't be created — not needed in strict 1:1)
    - `get_game_ids_for_scope` (becomes `get_rom_ids_for_scope`, already exists in similar shape — verify and merge)
  - **Rename `game_id` → `rom_id` in signatures, SQL, and docstrings:**
    - `upsert_metadata`, `get_metadata`
    - `insert_cover`, `get_covers`, `get_preferred_cover`, `set_preferred_cover`, `count_covers`, `_ensure_preferred`, `has_cover`
    - `add_game_to_collection` → `add_rom_to_collection`
    - `remove_game_from_collection` → `remove_rom_from_collection`
    - `is_game_in_collection` → `is_rom_in_collection`
    - `get_collection_games` → `get_collection_roms`
    - `get_games_needing_enrichment` → `get_roms_needing_enrichment` (now joins through roms directly)
    - `get_games_with_enrichment_status` → `get_roms_with_enrichment_status`
  - **Expand `upsert_rom`** ([db/queries.py:114-185](../../src/romulus/db/queries.py#L114-L185)):
    - Extend `RomUpsertData` TypedDict with the new identity fields (all optional).
    - INSERT/UPSERT SQL writes them when supplied; UPDATE keeps existing values when omitted (use `COALESCE(EXCLUDED.field, roms.field)` pattern).
  - **Add forward-looking helpers** (skeletons; Session 15 wires them up):
    - `find_sibling_metadata(conn, system_id, canonical_name, sha1) -> sqlite3.Row | None` — returns a metadata row from any *other* rom matching the same identity (sha1 first, then `(system_id, canonical_name)`).
    - `copy_metadata(conn, source_rom_id, dest_rom_id) -> None` — INSERTs a metadata row for `dest_rom_id` from the row at `source_rom_id`.
    - `find_sibling_covers(conn, system_id, canonical_name, sha1) -> list[sqlite3.Row]` — same pattern for covers.
    - `copy_covers(conn, source_rom_id, dest_rom_id) -> None` — copies cover rows (incl. `local_path` re-use; the on-disk file is shared between rom rows).
  - **`get_dest_inventory_*` queries** — drop the `game_id` column from selects and writes.
- [ ] Update `src/romulus/db/__init__.py` if it re-exports any of the deleted symbols.
- [ ] Update `RomRow` / equivalent dataclass(es) if any module wraps roms-table rows with typed access; new columns must be readable.
- [ ] Wipe-on-mismatch: `app.py` should refuse to open a pre-1:1 database. Detect by `PRAGMA table_info(games)` returning rows; surface a clear error dialog ("Your library DB predates v0.4.0 — delete `data/romulus.db` and rescan."). Do NOT auto-wipe.

**Test files affected** (do not fix in this session — list for Session 19 re-baseline):

- `tests/conftest.py` — `seeded_db` fixture unchanged but `_insert_rom` helpers in test files break.
- `tests/test_db.py` — touches `upsert_game`, basic CRUD.
- `tests/test_organizer.py`, `tests/test_scanner.py`, `tests/test_metadata.py`, `tests/test_covers.py`, `tests/test_collections.py`, `tests/test_exporter.py`, `tests/test_sync.py`, `tests/test_sync_preview.py`, `tests/test_sync_fixes.py`, `tests/test_library_cleanup.py`, `tests/test_local_cover_finder.py`, `tests/test_ui.py`, `tests/test_scoped_actions.py` — all touch deleted/renamed APIs.

**Acceptance criteria:**

- `create_tables()` on a fresh DB produces the new schema (verifiable with `PRAGMA table_info`).
- `games` table does NOT exist after `create_tables()`.
- `roms` table contains the merged columns (`title`, `canonical_name`, `region`, `revision`, `is_hack`, `is_homebrew`, `is_bios`).
- `metadata`, `covers`, `collection_roms`, `dest_inventory` all FK to `roms.id` with `ON DELETE CASCADE` (where applicable).
- `PRAGMA foreign_keys` is `1` on every connection returned by `db/connection.py`.
- `upsert_rom` accepts the new identity fields; supplying none leaves existing columns untouched (verified by a single new unit test in `tests/test_db.py` — this is the one test you DO write in this session, because it covers the load-bearing UPSERT contract).
- Deleting a `roms` row cascades to `metadata`, `covers`, `collection_roms` (one new unit test).
- All previously-deleted query functions are absent from `src/romulus/db/queries.py` (grep for `upsert_game`, `link_rom_to_game`, etc. returns nothing in `src/`).
- Ruff clean on `src/romulus/db/`.
- Wipe-on-mismatch dialog fires when `app.py` opens an old DB (manual smoke; tests cover via temp legacy schema if convenient).

STOP. Commit with message `refactor(db): strict 1:1 rom↔game — drop games table, expand roms, rename FKs`. Move to Session 14.
