# Session 1: Data Models, SQLite Schema & System Registry

**Type:** Build

**Context for this session:**

You are building the foundation layer: Pydantic data models, SQLite schema, and the system registry that seeds the database with all supported platforms.

SQLite schema — use the full schema from `docs/TECHNICAL_PLAN.md` §3 (SQLite Schema section). All tables: `config`, `systems`, `roms`, `hashes`, `dat_entries`, `games`, `metadata`, `covers`, `collections`, `collection_games`, `scan_history`, `organize_plans`.

System registry — define all supported systems as Python data. Each system needs: `id`, `display_name`, `short_name`, `manufacturer`, `extensions` (JSON array), `header_rule`, `libretro_name`, `folder_aliases` (JSON array), `dat_name`. Reference `docs/ROM-FORMATS-REFERENCE.md` §1 for extensions and §4 for folder aliases. Start with the ~30 most common systems (NES, SNES, N64, GB, GBC, GBA, DS, Genesis/MD, Master System, Game Gear, Saturn, Dreamcast, PSX, PSP, Atari 2600/7800/Lynx, PCE/TG16, Neo Geo, Arcade/MAME/FBNeo, MSX, Amiga, C64, Atari ST, ZX Spectrum, CPC).

The `config` table stores all app settings as key-value pairs. Default values: `library_path` = empty (user sets on first run), `dat_paths` = JSON array with `data/dats/` as default, `cover_cache_path` = `~/.romulus/covers/`, `theme` = `"system"`, `scan_threads` = `8`.

**Tasks:**

- [ ] Create Pydantic models in `src/romulus/models/`:
  - `system.py`: SystemDef model (id, display_name, short_name, manufacturer, extensions, header_rule, libretro_name, folder_aliases, dat_name)
  - `rom.py`: RomFile model (path, filename, extension, size_bytes, mtime, system_id, fuzzy_key, header_title, match_confidence)
  - `game.py`: Game model (title, system_id, canonical_name, region, revision, is_hack, is_homebrew)
  - `profile.py`: DestinationProfile model (id, name, base_path, gamelist_format, systems map)
- [ ] Create SQLite layer in `src/romulus/db/`:
  - `connection.py`: get_connection() returns a sqlite3.Connection to `~/.romulus/romulus.db`. Creates `~/.romulus/` dir if missing. Enables WAL mode and foreign keys.
  - `schema.py`: create_tables() executes all CREATE TABLE statements. Called on app startup. Uses IF NOT EXISTS.
  - `queries.py`: stub file with module docstring explaining its purpose. Actual queries added in later sessions.
- [ ] Create system registry in `src/romulus/models/system.py`:
  - Define SYSTEM_REGISTRY: list of SystemDef instances for ~30 systems
  - seed_systems(conn) function: inserts all systems into SQLite, skipping existing
- [ ] Create config manager in `src/romulus/db/`:
  - `config.py`: get_config(conn, key), set_config(conn, key, value), get_all_config(conn), seed_defaults(conn)
- [ ] Write tests:
  - `tests/test_db.py`: test schema creation, config seed, config get/set
  - `tests/test_models.py`: test Pydantic model validation
  - `tests/test_system_registry.py`: test system seeding, extension lookups, folder alias matching

**Acceptance criteria:**
- SQLite database created at `~/.romulus/romulus.db` with all tables
- System registry seeds ~30 systems with extensions and folder aliases
- Config table seeded with defaults
- All tests pass, ruff clean

STOP. Commit with message "Session 1: Data models, SQLite schema, system registry". Do not proceed to Session 2.
