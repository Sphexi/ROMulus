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

- [ ] Create `src/romulus/core/scanner.py`:
  - `scan_library(conn, library_path, progress_callback)` — main entry point
  - `detect_system(dirname, systems)` — match folder name against folder_aliases
  - `is_rom_file(filename, system)` — check extension against system's accepted extensions
  - `is_side_file(filename)` — skip non-ROM companion files
  - `parse_filename(filename)` — extract region, revision, status, disc_number, clean_name
  - `generate_fuzzy_key(clean_name)` — L1 normalization producing alphanumeric comparison key
  - `group_into_games(conn, system_id)` — create/update game records by grouping on fuzzy_key
- [ ] Add query functions to `src/romulus/db/queries.py`:
  - `upsert_rom(conn, rom_data)` — insert or update by path
  - `get_roms_by_system(conn, system_id)` — fetch all ROMs for a system
  - `upsert_game(conn, game_data)` — insert or update game record
  - `link_rom_to_game(conn, rom_id, game_id)`
  - `insert_scan_history(conn, scan_data)` — write scan history record
- [ ] Write tests:
  - `tests/test_scanner.py`:
    - Test folder-to-system detection (including aliases: "genesis" → megadrive system)
    - Test filename parsing with various naming conventions
    - Test fuzzy key generation (articles, Roman numerals, version suffixes)
    - Test side-file filtering
    - Test full scan against a mock directory tree (use tmp_path fixture)
  - `tests/test_filename_parser.py`:
    - Extensive tag parsing tests: regions, revisions, status flags, disc numbers
    - Edge cases: multiple regions `(USA, Europe)`, combined tags `(Rev 1) [!]`

**Acceptance criteria:**
- Scanner walks a directory tree and populates SQLite with ROM records
- Platform detection works for all ~30 system folder aliases
- Filename parser correctly extracts region, revision, status, disc number
- Fuzzy key collapses "Addams Family, The" / "The Addams Family" / "Addams Family (USA) (Rev 1)" to the same key
- All tests pass, ruff clean

STOP. Commit with message "Session 2: Scanner and filename parser". Do not proceed to Session 3.
