# Session 1: Data Models, SQLite Schema & System Registry

**Type:** Build

**Context for this session:**

You are building the foundation layer: Pydantic data models, SQLite schema, and the system registry that seeds the database with all supported platforms.

SQLite schema — use the full schema from `docs/TECHNICAL_PLAN.md` §3 (SQLite Schema section). All tables: `config`, `systems`, `roms`, `hashes`, `dat_entries`, `games`, `metadata`, `covers`, `collections`, `collection_games`, `scan_history`, `organize_plans`.

System registry — define all supported systems as Python data. Each system needs: `id`, `display_name`, `short_name`, `manufacturer`, `extensions` (JSON array), `header_rule`, `libretro_name`, `folder_aliases` (JSON array), `dat_name`. Reference `docs/ROM-FORMATS-REFERENCE.md` §1 for extensions and §4 for folder aliases. Start with the ~30 most common systems (NES, SNES, N64, GB, GBC, GBA, DS, Genesis/MD, Master System, Game Gear, Saturn, Dreamcast, PSX, PSP, Atari 2600/7800/Lynx, PCE/TG16, Neo Geo, Arcade/MAME/FBNeo, MSX, Amiga, C64, Atari ST, ZX Spectrum, CPC).

The `config` table stores all app settings as key-value pairs. Default values: `library_path` = empty (user sets on first run), `dat_paths` = JSON array with `data/dats/` as default, `cover_cache_path` = `~/.romulus/covers/`, `theme` = `"system"`, `scan_threads` = `8`.

**Tasks:**

- [x] Create Pydantic models in `src/romulus/models/`:
  - [x] `system.py`: SystemDef model (id, display_name, short_name, manufacturer, extensions, header_rule, libretro_name, folder_aliases, dat_name)
  - [x] `rom.py`: RomFile model (path, filename, extension, size_bytes, mtime, system_id, fuzzy_key, header_title, match_confidence)
  - [x] `game.py`: Game model (title, system_id, canonical_name, region, revision, is_hack, is_homebrew)
  - [x] `profile.py`: DestinationProfile model (id, name, base_path, gamelist_format, systems map)
- [x] Create SQLite layer in `src/romulus/db/`:
  - [x] `connection.py`: get_connection() returns a sqlite3.Connection to `~/.romulus/romulus.db`. Creates `~/.romulus/` dir if missing. Enables WAL mode and foreign keys.
  - [x] `schema.py`: create_tables() executes all CREATE TABLE statements. Called on app startup. Uses IF NOT EXISTS.
  - [x] `queries.py`: stub file with module docstring explaining its purpose. Actual queries added in later sessions.
- [x] Create system registry in `src/romulus/models/system.py`:
  - [x] Define SYSTEM_REGISTRY: list of SystemDef instances for ~30 systems
  - [x] seed_systems(conn) function: inserts all systems into SQLite, skipping existing
- [x] Create config manager in `src/romulus/db/`:
  - [x] `config.py`: get_config(conn, key), set_config(conn, key, value), get_all_config(conn), seed_defaults(conn)
- [x] Write tests:
  - [x] `tests/test_db.py`: test schema creation, config seed, config get/set
  - [x] `tests/test_models.py`: test Pydantic model validation
  - [x] `tests/test_system_registry.py`: test system seeding, extension lookups, folder alias matching

**Acceptance criteria:**
- SQLite database created at `~/.romulus/romulus.db` with all tables
- System registry seeds ~30 systems with extensions and folder aliases
- Config table seeded with defaults
- All tests pass, ruff clean

STOP. Commit with message "Session 1: Data models, SQLite schema, system registry". Do not proceed to Session 2.

---

## Completion Summary
**Status:** COMPLETE
**Date:** 2026-05-14
**What was built/changed:**
- Pydantic models: `SystemDef`, `RomFile`, `Game`, `DestinationProfile` in `src/romulus/models/`
- SQLite layer: `connection.py` (WAL + FK), `schema.py` (12 tables with indexes), `config.py` (key-value store with defaults), `queries.py` still a stub for Session 2 to populate
- System registry: 33 systems seeded (NES/SNES/N64/GameCube/GB/GBC/GBA/DS/Virtual Boy, Mega Drive/MS/GG/Saturn/Dreamcast/32X, PSX/PSP, Atari 2600/7800/Lynx/ST, PCE/PCE-CD, Neo Geo/NGP/NGPC, MAME/FBNeo, MSX, Amiga, C64, ZX Spectrum, Amstrad CPC). Header rules wired for SNES (smc_512), NES (ines_16), N64 (n64_byteswap), Lynx (lynx_64).
- Helper lookups for the scanner: `get_systems_by_alias()` and `get_extensions_by_system()` flatten the JSON-encoded columns into dicts.
- Shared `db` pytest fixture in `tests/conftest.py`.

**Tests:** 39 passed (test_db: 11, test_models: 8, test_system_registry: 20). Ruff clean.

**Config changes:** Default config keys seeded: library_path, dat_paths, cover_cache_path, screenscraper_username, screenscraper_password, theme, default_view, scan_threads, last_scan_type, last_scan_time.

**Breaking changes:** None (first build session beyond Session 0 scaffold).

**Carry-forward notes:**
- The `roms` table has FK to `games` and `scan_history` — Session 2's `upsert_rom` must accept NULL for both initially (game_id is set in `group_into_games`).
- `roms.path` is UNIQUE; use ON CONFLICT(path) for upsert.
- Indexes on (system_id, fuzzy_key) and game_id are already in place — Session 2's `group_into_games` query can lean on them.
- The system registry uses canonical id `megadrive` (not `genesis`); `genesis` is a folder alias that resolves to `megadrive`. Scanner tests should assert this mapping.
- `get_systems_by_alias()` returns lowercase keys; the scanner should lowercase the directory name before lookup.
- Extension lookups via `get_extensions_by_system()` already return lowercase-with-dot values — scanner should compare against lowercased extensions of incoming filenames.
