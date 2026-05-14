# Session 2: Scanner & Filename Parser

**Type:** Build

**Context for this session:**

You are building the filesystem scanner that walks a ROM library, detects platforms from folder names, and parses filenames into structured data. This is the Quick Scan's first phase (L1 fuzzy filename matching).

Scanner flow (from TECHNICAL_PLAN.md §5 — Quick Scan Flow):
1. Walk library_path recursively
2. Match directories against system folder_aliases (from the systems table seeded in Session 1)
3. For each file: check extension, skip side-files, parse filename tags, generate fuzzy_key
4. Insert/update rom records in SQLite
5. Group into logical games by fuzzy_key + system_id

Filename parsing — extract these tag groups from No-Intro/GoodTools/TOSEC naming:
- Region: `(USA)`, `(Europe)`, `(Japan)`, `(World)`, `(USA, Europe)`, etc.
- Revision: `(Rev 1)`, `(Rev A)`, `(v1.1)`, etc.
- Status flags: `[!]` verified, `[b]` bad dump, `[h]` hack, `[T+Eng]` translation, `(Unl)` unlicensed, `(Proto)` prototype, `(Beta)`, `(Demo)`, `(Sample)`
- Disc number: `(Disc 1)`, `(Disc 2)`, etc.

Fuzzy key normalization (from ROM-DEDUP-METHODOLOGY.md §3.2):
1. Drop extension
2. Strip parenthesized and bracketed tag groups recursively
3. Move trailing articles to front, then strip (The, A, An, Le, La, etc.)
4. Convert Roman numerals to Arabic (II→2, III→3, IV→4, VI→6, etc. — skip single letters)
5. Strip trailing version suffixes (v1.1, Rev 02, etc. — NOT bare sequel numbers)
6. Lowercase
7. Strip all non-alphanumerics

Side-files to skip: `.cue`, `.m3u`, `.sub`, `.txt`, `.nfo`, `.jpg`, `.png`, `.xml`, `.dat`, `.sav`, `.srm`, `.state`, `.oops`.

**Tasks:**

- [x] Create `src/romulus/core/scanner.py`:
  - [x] `scan_library(conn, library_path, progress_callback)` — main entry point
  - [x] `detect_system(dirname, systems)` — match folder name against folder_aliases
  - [x] `is_rom_file(filename, system)` — check extension against system's accepted extensions
  - [x] `is_side_file(filename)` — skip non-ROM companion files
  - [x] `parse_filename(filename)` — extract region, revision, status, disc_number, clean_name
  - [x] `generate_fuzzy_key(clean_name)` — L1 normalization producing alphanumeric comparison key
  - [x] `group_into_games(conn, system_id)` — create/update game records by grouping on fuzzy_key
- [x] Add query functions to `src/romulus/db/queries.py`:
  - [x] `upsert_rom(conn, rom_data)` — insert or update by path
  - [x] `get_roms_by_system(conn, system_id)` — fetch all ROMs for a system
  - [x] `upsert_game(conn, game_data)` — insert or update game record
  - [x] `link_rom_to_game(conn, rom_id, game_id)`
  - [x] `insert_scan_history(conn, scan_data)` — write scan history record
- [x] Write tests:
  - [x] `tests/test_scanner.py`:
    - [x] Test folder-to-system detection (including aliases: "genesis" → megadrive system)
    - [x] Test filename parsing with various naming conventions
    - [x] Test fuzzy key generation (articles, Roman numerals, version suffixes)
    - [x] Test side-file filtering
    - [x] Test full scan against a mock directory tree (use tmp_path fixture)
  - [x] `tests/test_filename_parser.py`:
    - [x] Extensive tag parsing tests: regions, revisions, status flags, disc numbers
    - [x] Edge cases: multiple regions `(USA, Europe)`, combined tags `(Rev 1) [!]`

**Acceptance criteria:**
- Scanner walks a directory tree and populates SQLite with ROM records
- Platform detection works for all ~30 system folder aliases
- Filename parser correctly extracts region, revision, status, disc number
- Fuzzy key collapses "Addams Family, The" / "The Addams Family" / "Addams Family (USA) (Rev 1)" to the same key
- All tests pass, ruff clean

STOP. Commit with message "Session 2: Scanner and filename parser". Do not proceed to Session 3.

---

## Completion Summary
**Status:** COMPLETE
**Date:** 2026-05-14
**What was built/changed:**
- `src/romulus/core/scanner.py` — full Quick Scan implementation:
  - `parse_filename()` returns a `ParsedFilename` dataclass with region / revision / disc_number / status flags / hack / homebrew / unlicensed / prototype / beta / demo / bad_dump / verified / translation, plus `clean_name` and `display_title` (trailing-article-fronted, e.g. "Addams Family, The" → "The Addams Family").
  - `generate_fuzzy_key()` — seven-step normalization from ROM-DEDUP-METHODOLOGY.md §3.2.
  - `is_side_file()` — skips `.cue`, `.m3u`, `.sub`, `.txt`, `.nfo`, `.jpg`, `.jpeg`, `.png`, `.gif`, `.xml`, `.dat`, `.sav`, `.srm`, `.state`, `.oops`.
  - `detect_system()` + `_resolve_system_for_directory()` — walks up from any nested file to find the first system-named ancestor.
  - `group_into_games()` — groups ROMs by `(system_id, fuzzy_key)` into logical games, reusing existing games when found via `find_game_id_for_fuzzy_key`.
  - `scan_library()` — full walk: opens a `scan_history` row, walks the tree, upserts ROMs, calls `group_into_games` per touched system, finalizes scan history. Returns a `ScanResult`.
- `src/romulus/db/queries.py` populated with: `upsert_rom`, `get_roms_by_system`, `upsert_game`, `link_rom_to_game`, `find_game_id_for_fuzzy_key`, `insert_scan_history`, `update_scan_history`. All use Connection cursors and rely on the caller to wrap in transactions where needed.
- `seeded_db` pytest fixture added to `tests/conftest.py` (schema + system registry pre-seeded).

**Tests:** 161 passed in 2.12s (test_scanner: 70, test_filename_parser: 52, plus the 39 from Session 1). Ruff clean.

**Config changes:** None.

**Breaking changes:** None.

**Carry-forward notes:**
- The scanner currently writes `match_confidence = "fuzzy"` whenever a fuzzy_key is produced; Session 3's identifier pipeline will overwrite this to `"header"` (L2) or `"dat_verified"` (L3) as it finds stronger evidence.
- `is_side_file()` skips `.cue`/`.m3u` even though the system registry lists them as accepted extensions for PSX/Saturn/PCE-CD. This matches the Session 2 spec. Disc-grouping (associating cue/m3u with their underlying bin/iso/chd) is intentionally deferred — it'll need its own session.
- ROM grouping uses `(system_id, fuzzy_key)` via `find_game_id_for_fuzzy_key`. Hacks/homebrew currently still collapse into the same game as the original if they share a fuzzy_key — the parser sets the `is_hack` / `is_homebrew` flags but `group_into_games` does NOT use them to keep hacks separate yet. Per the project rule "hacks are first-class artifacts", a later session should add hack/homebrew partitioning to the grouping logic.
- `parse_filename().extension` returns the *last* suffix only (e.g. `.iso` for `Game.nkit.iso`). If we ever need to round-trip compound extensions for naming, treat the registry's extension list as authoritative rather than re-deriving from filename suffix.
- `_resolve_system_for_directory` resolves symlinks via `Path.resolve()`. On Windows this can fail with `OSError` for paths longer than MAX_PATH; we fall back to the unresolved path silently.
- Idempotent rescans rely on `roms.path` UNIQUE constraint + `ON CONFLICT(path) DO UPDATE`. Tests confirm a second scan over the same tree leaves row counts unchanged.
