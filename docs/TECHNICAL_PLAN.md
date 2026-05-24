# ROMulus — Full Technical Specification

> **This file is a reference document.** The concise project rules live in `CLAUDE.md` at the project root. Session definitions live in `docs/sessions/*.md` (split from this file during Session 0). Read this file when you need specific implementation details not covered in the session file's Context section. For per-release feature + fix history see `CHANGELOG.md`.

---

## Table of Contents

1. [Current State](#current-state) (added 2026-05-17)
2. [Project Overview](#project-overview)
3. [Architecture](#architecture)
4. [SQLite Schema](#sqlite-schema)
5. [System Registry](#system-registry)
6. [Scanner](#scanner)
7. [Identifier Pipeline](#identifier-pipeline)
8. [DAT Parser](#dat-parser)
9. [Metadata & Cover Art Clients](#metadata--cover-art-clients)
10. [Library Organizer](#library-organizer)
11. [Export Engine & Destination Profiles](#export-engine--destination-profiles)
12. [Destination Sync Engine](#destination-sync-engine) — full spec in `docs/sync-design.md`
13. [Library Cleanup (Tombstone-Missing)](#library-cleanup-tombstone-missing)
14. [Packaging & Distribution](#packaging--distribution)
15. [UI Components](#ui-components)
16. [Error Handling](#error-handling)
17. [Technical Guardrails](#technical-guardrails)
18. [Session Definitions](#session-definitions)

---

## Current State

**As of v0.4.0 (in development; last reviewed 2026-05-23):**

- **1,015 tests passing, 8 skipped** (1,023 collected total; 7
  platform-specific cover-UI skips + 1 POSIX chmod skip on
  `windows-latest`). Ruff clean.
- **CI runs on `windows-latest`.** ROMulus is a Windows-first desktop
  app; running CI on the same OS we ship for exercises the same
  Qt/SQLite/PySide6 stack end users will run. Also dodges a Linux
  PySide6+sqlite3 segfault in `test_worker_emits_progress_and_finishes`.
- Sessions 00–19 are complete. Sessions 00–11 built the v0.1.0–v0.3.0
  pipeline; sessions 13–19 implemented the strict 1:1 rom↔game refactor.
- Subsequent work is committed directly via Conventional Commits without
  a numbered session file.
- See `CHANGELOG.md` for the per-release feature + fix log.
- For the cross-cutting "how is this built" view, see
  `docs/architecture.md` — this file is the deeper implementation
  reference.

**v0.4.0 deltas worth knowing when reading the rest of this doc:**

- **Strict 1:1 model.** The `games` table is gone. Identity columns
  (`title`, `canonical_name`, `region`, `revision`, `is_hack`,
  `is_homebrew`, `is_bios`) merged onto `roms`. All FKs to roms have
  `ON DELETE CASCADE`. `prune_orphan_games` and `_delete_game_dependents`
  are deleted; cascade does the work atomically.
- **`metadata.game_id` → `metadata.rom_id` (PK, 1:1).** Same for
  `covers.game_id` → `covers.rom_id`.
- **`collection_games` → `collection_roms`.** New PK is
  `(collection_id, rom_id)`.
- **`dest_inventory.game_id` dropped.**
- **Scanner grouping phase deleted.** `_group_unlinked_roms_into_games`
  is gone. Identity writes directly onto roms at upsert time. Heavy
  Scan updates `canonical_name/region/revision` in place after a DAT
  match via `_update_identity_from_dat` in `core/dat_parser.py`.
- **Shared parens-token parser.** `src/romulus/core/_no_intro_tokens.py
  ::parse_no_intro_tokens` — used by both filename and DAT-name parsing.
- **Sibling-copy gate.** Before any network source runs, the metadata
  chain calls `find_sibling_metadata` / `find_sibling_covers`. On a
  hit, the row is copied and the chain is skipped. Priority: SHA-1 →
  `(system_id, canonical_name)` → `(system_id, fuzzy_key)`.
- **`ExportOptions.distinct_content_only` toggle.** Exports one rom per
  SHA-1 cluster; keeper rank: dat_verified > canonical ext > shorter
  filename > lower rom_id.
- **Organizer `find_cross_extension_dupes` deleted.** SHA-1-based
  `find_duplicates` covers the same ground post-Bug 2 fix.
- **Organizer Bug 2 fixed.** TOCTOU guard now calls
  `hash_rom(path, header_rule)` not raw `_digest_stream`.
- **Organizer Bug 3 fixed.** `detect_collisions` now also flags rename
  targets occupied by an existing un-renamed rom row.
- **Detail panel Bug 4 fixed.** `update_rom(rom_id)` reads SHA-1 /
  DAT name / region directly from the selected rom — no LIMIT 1 ambiguity.

See `docs/strict-1to1-design.md` for the full design rationale.

**v0.3.0 deltas worth knowing when reading the rest of this doc:**

- **Sync engine** ([Destination Sync Engine](#destination-sync-engine)
  below; full spec `docs/sync-design.md`) — five modes (push
  merge/mirror/wipe, pull merge, two-way), four-tier identity matcher
  keyed on `(fuzzy_key, region, system_id)`, `dest_inventory` cache,
  `sync_plans` persistence, SAVEPOINT-per-action rollback.
- **Library cleanup** ([section](#library-cleanup-tombstone-missing)
  below) — `roms.library_root` + `roms.missing` columns; single-library
  design (switching `library_path` prompts to wipe prior rows); scanner
  sweep marks any unvisited row missing; **Tools → Clean Missing
  Entries** drops them with FK cascade.
- **Single-binary portable build** ([section](#packaging--distribution))
  — PyInstaller `--onefile` produces `romulus.exe` containing Python +
  PySide6 + every transitive DLL. Data folders (`dats/`, `profiles/`,
  `systems/`, `gamedb/`, `libretro-metadat/`) ship as real folders next
  to the exe in the ZIP.
- **Logging precedence fixed:** `ROMULUS_LOG_LEVEL` env var beats
  stored `config.log_level`. DEBUG breadcrumbs added across
  `dat_parser`, `identifier`, `hasher`, `local_cover_finder`,
  `exporter`, `organizer`, and every metadata client.
- **Schema migrations removed.** Pre-v0.3.0 databases are not
  migrated; wipe `data/romulus.db` and rescan.
- **Real DATs bundled.** 106 No-Intro DATs covering ~80 systems in
  `data/dats/` (dev) and `dats/` (portable build).
- **Bundled offline metadata** — `data/gamedb/` (42 GameDB JSON
  snapshots, ~17 MB) and `data/libretro-metadat/` (294 clrmamepro DATs
  across 7 dimensions, ~20 MB). Both are tried before any network call
  in the enrichment chain.
- **Metadata / cover-art workflow split.** `enrich_library` writes to
  the `metadata` table only. Cover discovery is now driven by
  `CoverFinderWorker` via `CoverOptionsDialog`. Pre-batch
  `EnrichOptionsDialog` adds three flags (fuzzy / re-enrich /
  online-providers).
- **Detail panel redesign.** Hide-when-empty description, compact
  key/value `QFormLayout` grid, per-platform console logos in the
  sidebar + detail panel.
- **Scoped Quick Scan + post-walk progress + safe-cancel.**
  `scan_library` accepts `scope_system_id`; sidebar right-click invokes
  scoped scans. End-of-scan DB phases emit Unicode-ellipsis-suffixed
  progress labels; the dialog detects them and disables Cancel so the
  DB can't be left mid-rebuild.
- **Per-game Reveal in Explorer + Delete actions** on the game-table
  right-click menu, bound to rom_id (not game_id).
- **Rename: Romulus → ROMulus.** Python package import path `romulus`
  (lowercase) preserved; only display-name strings changed.
- **Inbound Import ROMs** (`src/romulus/core/importer.py`,
  `src/romulus/ui/import_dialog.py`) — staging-folder → library
  workflow with three-level dupe detection (path / filename / hash)
  and per-row resolution dropdowns. Full spec at
  `docs/import-design.md` (formerly a "future" doc, now
  authoritative reference for the shipped feature).
- **Reverse-direction Verify Library scrub** (`src/romulus/core/scrub.py`,
  `src/romulus/ui/scrub_dialog.py`). Walks every roms row and
  classifies against disk into four buckets (missing-on-disk,
  outside-current-library, flagged-but-present, size/mtime drift),
  with per-bucket SAVEPOINT apply.
- **Per-system summary dialog** after Export and Sync
  (`src/romulus/ui/per_system_summary_dialog.py`). One row per system,
  Copied / Bytes / Covers refreshed / Already on dest / Unsupported /
  Refused / Errors. New `per_system` field on `ExportSummary` /
  `SyncSummary` populated alongside the existing aggregates.
- **Artwork-only export mode.** New `include_roms: bool = True` on
  `ExportOptions`. Uncheck → skip the ROM copy loop entirely, run only
  the sidecar refresh (artwork + gamelist.xml). `copy_artwork` adds a
  size + mtime compare so re-runs only re-publish covers that
  actually changed.
- **Sync diff perf + threading rewrite** (commit `e3082b4`,
  `docs/sync-design.md` §12.6). `build_plan` is O(N+M) now via the
  pre-built `dest_by_fuzzy` index, and runs on `BuildSyncPlanWorker`
  with a "Computing diff…" progress dialog. Closes a multi-minute UI
  freeze on large libraries.
- **`prune_orphan_games`** (v0.3.0 fix, now superseded). Was clearing
  FK-dependent metadata / covers / collection_games before deleting
  orphan game rows. In v0.4.0 this function is deleted entirely;
  `ON DELETE CASCADE` on all rom-keyed tables replaces it.
- **`CleanMissingWorker`** wraps Clean Missing Entries on a worker
  thread with `try/except/conn.rollback()/raise` — closes the
  "DB locked / silent rollback" footgun.
- **Tri-state group headers + right-click bulk toggle** on Organize,
  Sync, and Verify Library preview dialogs via the shared
  `GroupedCheckboxTreeMixin`.

These deltas don't invalidate the sections that follow — they extend
them.

---

## Project Overview

ROMulus is a local-first desktop ROM collection manager for retro game consoles. It addresses a gap in the ecosystem: no modern cross-platform desktop app combines rich metadata browsing (covers, descriptions, genres) with file management, library organization, and device-specific export — without requiring Docker or a server.

**How it works at a high level:**
1. User points ROMulus at a folder containing ROMs
2. Quick Scan walks the filesystem, detects platforms from folder names, parses filenames for tags (region, revision, dump status), and extracts internal ROM headers
3. Results populate a SQLite database and display in a browsable table UI with system sidebar
4. Optional Heavy Scan computes SHA-1 hashes, matches against bundled No-Intro DATs for authoritative identification
5. Metadata enrichment fetches cover art (libretro-thumbnails), descriptions and genres (Hasheous/LaunchBox XML)
6. User can organize their library (rename to canonical names, merge alias folders, remove duplicates) with a preview/commit workflow
7. User can export curated sets to device-specific folder structures (Batocera, MiSTer, Anbernic, etc.) via destination profiles

**Target users:** Retro game enthusiasts who collect ROMs and want to organize them before loading onto handhelds (Anbernic RG556/RG406), SBCs (Raspberry Pi with Batocera/RetroPie), or FPGA devices (MiSTer, Analogue Pocket).

**What's in scope for v1:**
- Scan, identify, and catalog ROM collections (50,000+ files, 240+ GB)
- Three-layer identification pipeline (fuzzy filename → internal header → hash+DAT)
- Cover art from libretro-thumbnails (free, no API key)
- Metadata from Hasheous (free) and LaunchBox XML (downloadable)
- Optional ScreenScraper integration (user-prompted)
- Table view with system sidebar and game detail panel
- Library organization with before/after preview
- Export to 6+ destination profiles with gamelist.xml generation
- Bundled No-Intro DATs for ~30 common systems

**What's out of scope for v1:**
- Grid/gallery view (v1.1)
- Emulator launching
- Save state management
- RetroAchievements integration
- Plugin/extension system
- Disc-based format conversion (CHD↔BIN+CUE)
- Redump/MAME/TOSEC DAT bundling (user can add manually)

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     PySide6 UI                           │
│  ┌─────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ System   │  │  Game Table  │  │   Game Detail Panel │ │
│  │ Sidebar  │  │  (sortable,  │  │   (cover, desc,     │ │
│  │          │  │   filterable)│  │    metadata, tags)   │ │
│  └─────────┘  └──────────────┘  └─────────────────────┘ │
│  ┌──────────────────────────────────────────────────────┐│
│  │ Toolbar: Quick Scan | Heavy Scan | Organize |        ││
│  │          Enrich | Export | Settings                   ││
│  └──────────────────────────────────────────────────────┘│
└──────────────┬───────────────────────────────────────────┘
               │ signals/slots + QThread workers
┌──────────────┴───────────────────────────────────────────┐
│                    Core Engine                            │
│                                                           │
│  ┌───────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  Scanner   │  │  Identifier  │  │  Metadata Client  │  │
│  │            │  │  Pipeline    │  │                    │  │
│  │ Quick:     │  │  L1: fuzzy   │  │ libretro-thumbs   │  │
│  │  walk+parse│  │  L2: header  │  │ Hasheous (free)   │  │
│  │ Heavy:     │  │  L3: hash+DAT│  │ LaunchBox XML     │  │
│  │  hash all  │  │              │  │ ScreenScraper(opt) │  │
│  └─────┬─────┘  └──────┬───────┘  └────────┬──────────┘  │
│        │               │                    │             │
│  ┌─────┴───────────────┴────────────────────┴──────────┐  │
│  │              SQLite Database                         │  │
│  │  config | systems | games | roms | hashes |          │  │
│  │  dat_entries | metadata | covers | collections |     │  │
│  │  scan_history | organize_plans                       │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                           │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐   │
│  │  DAT Parser  │  │  Organizer   │  │ Export Engine  │   │
│  │  (bundled +  │  │  (rename,    │  │ (dest profiles,│   │
│  │   user DATs) │  │   merge,     │  │  copy, gamelist│   │
│  │              │  │   dedup,     │  │  .xml, .lpl,   │   │
│  │              │  │   preview)   │  │  progress)     │   │
│  └──────────────┘  └──────────────┘  └────────────────┘   │
│                                                           │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Cover Cache  (~/.romulus/covers/)                    │  │
│  └──────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

- **Runtime:** Single-process desktop app (PySide6 QApplication)
- **Threading:** QThread workers for scanner, hasher, metadata fetcher. Main thread owns the UI. Workers emit signals for progress updates.
- **State:** SQLite at `~/.romulus/romulus.db`. Cover cache at `~/.romulus/covers/`.
- **Config:** All settings stored in the `config` SQLite table. No config files to edit.

---

## SQLite Schema

```sql
-- App configuration (key-value)
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Default config entries:
-- library_path: path to ROM collection root
-- dat_paths: JSON array of DAT folder paths (bundled + user)
-- cover_cache_path: ~/.romulus/covers/
-- screenscraper_username: (empty if not configured)
-- screenscraper_password: (empty if not configured)
-- theme: "system" | "light" | "dark"
-- default_view: "table"
-- scan_threads: 8
-- last_scan_type: "quick" | "heavy"
-- last_scan_time: ISO timestamp

-- System/platform definitions (seeded from system registry)
CREATE TABLE systems (
    id              TEXT PRIMARY KEY,     -- canonical lowercase id: "snes", "gba", etc.
    display_name    TEXT NOT NULL,        -- "Super Nintendo Entertainment System"
    short_name      TEXT NOT NULL,        -- "SNES"
    manufacturer    TEXT,                 -- "Nintendo"
    generation      INTEGER,             -- console generation number
    extensions      TEXT NOT NULL,        -- JSON array: [".sfc", ".smc", ".fig", ".swc"]
    header_rule     TEXT,                 -- "smc_512" | "ines_16" | "n64_byteswap" | "lynx_64" | null
    libretro_name   TEXT,                 -- "Nintendo - Super Nintendo Entertainment System"
    folder_aliases  TEXT NOT NULL,        -- JSON array: ["snes", "sfc", "superfamicom", "supernes"]
    dat_name        TEXT                  -- No-Intro DAT name pattern for matching
);

-- ROM files on disk (v0.4.0: identity unit; games table removed)
CREATE TABLE roms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    path             TEXT NOT NULL UNIQUE,    -- full filesystem path
    filename         TEXT NOT NULL,           -- basename only
    extension        TEXT NOT NULL,           -- lowercase, with dot: ".sfc"
    size_bytes       INTEGER NOT NULL,
    mtime            REAL NOT NULL,           -- file modification time (for hash cache invalidation)
    system_id        TEXT REFERENCES systems(id),
    scan_id          INTEGER REFERENCES scan_history(id),
    fuzzy_key        TEXT,                    -- L1 normalized filename key
    header_title     TEXT,                    -- L2 internal header title (if extracted)
    dat_match        TEXT,                    -- canonical name from DAT match (if matched)
    match_confidence TEXT DEFAULT 'unmatched',-- "unmatched" | "fuzzy" | "header" | "dat_verified"
    -- Identity columns (formerly on games table; set by scanner, updated by Heavy Scan):
    title            TEXT,                    -- display title (parsed from filename)
    canonical_name   TEXT,                    -- No-Intro canonical name if DAT-matched
    region           TEXT,                    -- e.g. "USA", "Europe", "Japan"
    revision         TEXT,                    -- e.g. "Rev 1", "Rev A"
    is_hack          INTEGER NOT NULL DEFAULT 0,
    is_homebrew      INTEGER NOT NULL DEFAULT 0,
    is_bios          INTEGER NOT NULL DEFAULT 0,
    -- Library / tombstone columns:
    library_root     TEXT,                    -- canonical absolute path of the library root
    missing          INTEGER NOT NULL DEFAULT 0  -- 0=present, 1=tombstoned
);

-- Hash cache (expensive to compute, reused if mtime unchanged)
CREATE TABLE hashes (
    rom_id  INTEGER PRIMARY KEY REFERENCES roms(id),
    crc32   TEXT,
    sha1    TEXT,
    md5     TEXT,
    hashed_at REAL NOT NULL             -- timestamp of hash computation
);

-- DAT entries (parsed from No-Intro/Redump/TOSEC XML files)
CREATE TABLE dat_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dat_file    TEXT NOT NULL,           -- source DAT filename
    system_id   TEXT REFERENCES systems(id),
    game_name   TEXT NOT NULL,           -- canonical game name from DAT
    rom_name    TEXT NOT NULL,           -- expected ROM filename from DAT
    size_bytes  INTEGER,
    crc32       TEXT,
    md5         TEXT,
    sha1        TEXT,
    region      TEXT,                    -- parsed from game_name: "USA", "Europe", etc.
    revision    TEXT,                    -- parsed from game_name: "Rev 1", etc.
    is_bios     INTEGER DEFAULT 0
);
CREATE INDEX idx_dat_sha1 ON dat_entries(sha1);
CREATE INDEX idx_dat_crc32_size ON dat_entries(crc32, size_bytes);

-- NOTE: games table removed in v0.4.0. Identity columns merged onto roms.
-- See the roms table definition above for title, canonical_name, region,
-- revision, is_hack, is_homebrew, is_bios.

-- ROM metadata (from enrichment sources; 1:1 with roms; v0.4.0: rom_id PK)
CREATE TABLE metadata (
    rom_id      INTEGER PRIMARY KEY REFERENCES roms(id) ON DELETE CASCADE,
    description TEXT,
    genre       TEXT,
    developer   TEXT,
    publisher   TEXT,
    release_date TEXT,
    players     TEXT,                    -- "1", "1-2", "1-4"
    rating      TEXT,                    -- ESRB/PEGI/CERO
    source      TEXT NOT NULL            -- "hasheous" | "launchbox" | "screenscraper" | "gametdb"
);

-- Cover art references (v0.4.0: rom_id FK replaces game_id)
CREATE TABLE covers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rom_id      INTEGER REFERENCES roms(id) ON DELETE CASCADE,
    cover_type  TEXT NOT NULL,           -- "boxart" | "screenshot" | "title_screen"
    source_url  TEXT,                    -- original download URL
    local_path  TEXT,                    -- path in cover cache (shared across rom rows for same content)
    width       INTEGER,
    height      INTEGER
);

-- User collections (favorites, custom groups, export sets)
CREATE TABLE collections (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    description TEXT,
    is_system   INTEGER DEFAULT 0       -- 1 for built-in collections like "Favorites"
);

-- v0.4.0: renamed from collection_games; rom_id replaces game_id
CREATE TABLE collection_roms (
    collection_id INTEGER REFERENCES collections(id),
    rom_id        INTEGER REFERENCES roms(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, rom_id)
);

-- Scan history
CREATE TABLE scan_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type   TEXT NOT NULL,           -- "quick" | "heavy"
    started_at  TEXT NOT NULL,           -- ISO timestamp
    finished_at TEXT,
    root_path   TEXT NOT NULL,
    files_found INTEGER DEFAULT 0,
    files_matched INTEGER DEFAULT 0,
    files_new   INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0
);

-- Organization plans (preview before commit)
CREATE TABLE organize_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',  -- "pending" | "committed" | "cancelled"
    plan_json   TEXT NOT NULL            -- JSON array of {action, source, dest, reason}
);
```

---

## System Registry

The system registry is a Python module (`src/romulus/models/system.py`) that defines all supported systems. It seeds the `systems` SQLite table on first run.

Key fields per system:
- `id`: canonical lowercase identifier (e.g., `snes`, `gba`, `n64`)
- `extensions`: accepted file extensions
- `header_rule`: which header-stripping logic to apply before hashing
- `libretro_name`: maps to libretro-thumbnails folder names (e.g., `"Nintendo - Super Nintendo Entertainment System"`)
- `folder_aliases`: all known folder names across Anbernic, Batocera, RetroPie, Onion OS, MiSTer, etc.

The full extension table and folder alias table are in `docs/ROM-FORMATS-REFERENCE.md` §1 and §4. The system registry codifies that data.

---

## Scanner

### Quick Scan Flow

```
1. Walk library_path recursively
2. For each directory, attempt to match against system folder_aliases
   - If match: assign system_id to all ROM files in that directory
   - If no match: mark as "unknown system" (user can assign later)
3. For each file:
   a. Check extension against system's accepted extensions
   b. Skip side-files (.cue, .m3u, .sub, .txt, .nfo, .jpg, .png, etc.)
   c. Parse filename for tags using parse_no_intro_tokens (shared parser):
      - Region: (USA), (Europe), (Japan), (World), etc.
      - Revision: (Rev 1), (Rev A), (v1.1), etc.
      - Status: [!] (verified), [b] (bad dump), [h] (hack), [T+Eng], etc.
   d. Generate fuzzy_key (Layer 1 normalization — see ROM-DEDUP-METHODOLOGY.md §3)
   e. Populate identity fields directly on the rom row: title, region,
      revision, is_hack, is_homebrew (from filename parse)
   f. If system has header_rule, extract internal title (Layer 2)
   g. Insert/update rom record in SQLite via upsert_rom (path-keyed UPSERT)
      — no separate grouping phase; identity lives on the rom row
4. Missing sweep: mark all roms not visited this scan as missing=1
5. Emit progress signals: files_scanned, files_total, current_file
6. Write scan_history record
```

Note: The `_group_unlinked_roms_into_games` post-walk grouping phase
that existed in v0.3.0 is gone. There is no `games` table to link to.

### Heavy Scan Flow

```
1. Query all roms where hash is missing OR mtime has changed since last hash
2. For each file (parallel workers, configurable thread count):
   a. Read file content
   b. If file is .zip with single inner file: extract and hash inner content
   c. If file is .zip with multiple inner files (MAME romset): hash largest
   d. Apply header_rule normalization:
      - smc_512: strip first 512 bytes if size % 1024 == 512
      - ines_16: strip first 16 bytes if magic is "NES\x1a"
      - n64_byteswap: detect endianness from magic, convert to z64 byte order
      - lynx_64: strip first 64 bytes if magic is "LYNX\x00"
   e. Compute CRC32 + SHA-1 of normalized content
   f. Store in hashes table with timestamp
3. After hashing, run DAT matching:
   a. For each hash, look up in dat_entries by SHA-1 (primary) then CRC32+size (fallback)
   b. If match: call _update_identity_from_dat (dat_parser.py) which:
      - Writes rom.dat_match, rom.match_confidence = "dat_verified"
      - Writes rom.canonical_name, rom.region, rom.revision in place
      (v0.4.0: no "create/link game record" step — identity is on roms directly)
   c. If no match: rom stays at previous confidence level
4. Emit progress signals per file
5. Update scan_history record
```

---

## Identifier Pipeline

See `docs/ROM-DEDUP-METHODOLOGY.md` for the full methodology. Summary:

| Layer | Signal | Cost | Runs during |
|---|---|---|---|
| L1: Fuzzy filename | Normalized filename key | Trivial | Quick Scan |
| L2: Internal header | ROM-embedded title | ~100 bytes read | Quick Scan |
| L3: Hash + DAT | SHA-1 lookup in DAT DB | Full file read | Heavy Scan |

### Header Strip Rules

| Rule | Systems | What to strip |
|---|---|---|
| `smc_512` | SNES (.smc, .sfc, .fig, .swc) | 512-byte copier header if `size % 1024 == 512` |
| `ines_16` | NES (.nes) | 16-byte iNES header if magic is `NES\x1a` at offset 0 |
| `n64_byteswap` | N64 (.n64, .v64, .z64) | Byte-swap to z64 (big-endian) before hashing. Magic: `80 37 12 40` = z64, `37 80 40 12` = v64, `40 12 37 80` = n64 |
| `lynx_64` | Atari Lynx (.lnx) | 64-byte header if magic is `LYNX\x00` |

### Internal Header Locations

| System | Title offset | Title length | Notes |
|---|---|---|---|
| SNES | `0x7FC0` (LoROM) or `0xFFC0` (HiROM) | 21 bytes ASCII | Strip SMC header first. Try both offsets, pick higher printable-ASCII ratio. |
| N64 | `0x20` (after byte-swap to z64) | 20 bytes ASCII | Detect endianness by magic at offset 0 |
| Mega Drive | `0x150` (overseas) / `0x120` (domestic) | 48 bytes ASCII | Check for "SEGA MEGA DRIVE" or "SEGA GENESIS" at `0x100` |
| Game Boy/Color | `0x134` | 11-16 bytes ASCII | Trim at first null/non-printable |
| GBA | `0xA0` | 12 bytes ASCII | Followed by 4-byte gamecode at `0xAC` |
| DS | `0x00` | 12 bytes ASCII | Followed by 4-byte gamecode at `0x0C` |

---

## DAT Parser

Parses Logiqx-format XML DAT files. Structure:

```xml
<datafile>
  <header>
    <name>Nintendo - Super Nintendo Entertainment System</name>
    ...
  </header>
  <game name="Super Mario World (USA)">
    <rom name="Super Mario World (USA).sfc" size="524288"
         crc="A354DB25" md5="..." sha1="..." />
  </game>
</datafile>
```

Parser extracts: game name, rom name, size, CRC32, MD5, SHA-1, region (parsed from game name), revision, BIOS flag.

Use `xml.etree.ElementTree` (stdlib) for parsing — no lxml dependency. DAT files are small enough to parse in-memory.

**Bundled DATs:** ~30 No-Intro DAT files covering common cartridge/handheld systems ship in `data/dats/`. The full list matches the systems defined in the system registry.

**User DATs:** Additional DATs (Redump for disc-based, MAME for arcade, TOSEC for computers) can be placed in a user-configurable folder. The app scans both locations.

---

## Metadata & Cover Art Clients

### libretro-thumbnails (Primary — cover art)

**No API key. No account. Direct HTTP GET.**

URL pattern:
```
https://thumbnails.libretro.com/{libretro_name}/Named_Boxarts/{game_name}.png
https://thumbnails.libretro.com/{libretro_name}/Named_Snaps/{game_name}.png
https://thumbnails.libretro.com/{libretro_name}/Named_Titles/{game_name}.png
```

Where:
- `{libretro_name}` = system's `libretro_name` field, URL-encoded (e.g., `Nintendo%20-%20Super%20Nintendo%20Entertainment%20System`)
- `{game_name}` = No-Intro canonical game name with character replacements: `&*/:\<>?\|"` → `_`

Three image types: `Named_Boxarts` (box art), `Named_Snaps` (in-game screenshot), `Named_Titles` (title screen).

**Prerequisite:** ROM must be DAT-matched (L3) to have the canonical name. Unmatched ROMs fall back to fuzzy filename matching against the thumbnail server.

Download to `~/.romulus/covers/{system_id}/{cover_type}/{game_name}.png`. Cache indefinitely — these images don't change.

### Hasheous (Primary — game metadata)

**No API key. Free REST API.**

Endpoint: `https://hasheous.org/api/v1/lookup/{hash_type}/{hash_value}`

Returns IGDB metadata: title, description, genre, developer, publisher, release date, platform, cover URL.

Hash types: `sha1`, `md5`, `crc32` (SHA-1 preferred).

Rate limiting: undocumented but respectful. Use 1 request/second with exponential backoff on 429.

### LaunchBox XML (Offline fallback — metadata)

Downloadable XML database from LaunchBox's GitHub. ~200 MB. Contains game descriptions, genres, developers, publishers, release dates, ratings for thousands of retro games.

Parse once on import, store relevant fields in the `metadata` table. Match by game title + system.

### ScreenScraper (Optional — rich metadata)

**Requires free account.** Rate-limited: 1 request/second for free tier.

On first run or in Settings, app prompts: "Would you like to configure ScreenScraper for richer metadata? (Free account required)" If user declines, everything works via the free sources above.

API: `https://api.screenscraper.fr/api2/`

---

## Library Organizer

The Organizer modifies the user's library folder in place — renaming files, merging alias folders, removing duplicates. It NEVER acts without a preview.

### Workflow

```
1. User clicks "Organize" in toolbar
2. Organizer analyzes library state from SQLite:
   a. Identify alias folders to merge (genesis → megadrive)
   b. Identify files to rename (fuzzy name → canonical DAT name)
   c. Identify duplicates (same hash, different files)
   d. Identify cross-extension duplicates (.smc + .sfc of same ROM)
3. Generate an organize_plan (JSON array of actions):
   - {action: "merge_folder", source: "genesis/", dest: "megadrive/"}
   - {action: "rename", source: "zelda.smc", dest: "Legend of Zelda, The (USA).sfc"}
   - {action: "delete_duplicate", path: "snes/Game (USA).smc", keep: "snes/Game (USA).sfc", reason: "byte-identical, prefer .sfc"}
   - {action: "collision", source: "genesis/Game.zip", dest: "megadrive/Game.zip", sizes: [421652, 421420], resolution: "manual"}
4. Display in OrganizePreviewDialog:
   - Before/after tree view
   - Per-action approve/reject checkboxes
   - Collision review panel for manual decisions
   - Summary: "X files renamed, Y folders merged, Z duplicates removed"
5. User reviews, modifies selections, clicks "Apply"
6. Organizer executes approved actions:
   - Move/rename files (never overwrite without explicit approval)
   - Update SQLite paths
   - Write plan record with status "committed"
7. If any action fails: roll back that action, continue with others, report errors
```

### Organize Rules

- **Folder merges:** Only merge folders confirmed as aliases (validated against system registry folder_aliases)
- **Renames:** Only rename if DAT-matched (L3 confidence). Never rename unmatched files.
- **Duplicate removal:** Only remove if byte-identical (same SHA-1). Prefer: canonical extension (.sfc over .smc, .z64 over .v64), then smaller filename.
- **Hacks:** Never merge or deduplicate against originals. Hacks are distinct titles.
- **Collisions:** When merging folders and both have a file with the same name but different content (different size or hash), flag for manual review. Never silently overwrite.

---

## Export Engine & Destination Profiles

### Profile Format (YAML)

```yaml
id: batocera
name: "Batocera Linux"
description: "EmulationStation-based Linux distro for retro gaming"
case_sensitive: true
base_path: "roms"
gamelist_format: "emulationstation_xml"   # "emulationstation_xml" | "retroarch_lpl" | null
artwork_subdir: "downloaded_media"
multi_disc: "m3u"                         # "m3u" | null
systems:
  nes:
    folder: "nes"
    extensions: [".nes", ".zip", ".7z"]
  snes:
    folder: "snes"
    extensions: [".sfc", ".smc", ".zip", ".7z"]
  gba:
    folder: "gba"
    extensions: [".gba", ".zip"]
  n64:
    folder: "n64"
    extensions: [".z64", ".n64", ".v64", ".zip"]
  megadrive:
    folder: "megadrive"
    extensions: [".md", ".gen", ".bin", ".zip"]
  psx:
    folder: "psx"
    extensions: [".chd", ".cue", ".pbp"]
  # ... all systems
```

### Built-in Profiles

Ship 6 profiles in `data/profiles/`:
1. `batocera.yaml` — Batocera Linux / RetroBat
2. `retropie.yaml` — RetroPie
3. `onionos.yaml` — Onion OS (Miyoo Mini)
4. `muos.yaml` — muOS (Anbernic Android)
5. `mister.yaml` — MiSTer FPGA
6. `analogue-pocket.yaml` — Analogue Pocket

User custom profiles go in `~/.romulus/profiles/`.

### Export Workflow

```
1. User clicks "Export" in toolbar
2. ExportDialog opens:
   a. Select destination profile (dropdown)
   b. Select target path (folder chooser — SD card, external drive, folder)
   c. Optional filters:
      - Systems (checkboxes)
      - Collections (e.g., "Favorites only")
      - Regions (e.g., "USA + World only")
   d. Options:
      - Include artwork (checkbox)
      - Generate gamelist.xml / .lpl (checkbox, depends on profile)
      - Copy mode: "Copy" | "Hardlink" (same filesystem only)
3. Click "Preview" — shows file count, estimated size, folder tree preview
4. Click "Export" — copies files with progress bar
   a. For each ROM in the filtered set:
      - Look up system in profile's systems map → target folder name
      - Copy file to {target_path}/{profile.base_path}/{folder}/{filename}
      - If profile specifies extension preferences, convert filename extension
   b. If gamelist_format is "emulationstation_xml":
      - Generate gamelist.xml per system folder with metadata from SQLite
   c. If gamelist_format is "retroarch_lpl":
      - Generate .lpl playlist files per system
   d. If include artwork:
      - Copy covers to profile's artwork_subdir structure
   e. If multi_disc is "m3u":
      - Generate .m3u playlists for multi-disc games
5. Report: "Exported 847 games across 12 systems to /media/sdcard/ (4.2 GB)"
```

### gamelist.xml Format (EmulationStation)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<gameList>
  <game>
    <path>./Super Mario World (USA).sfc</path>
    <name>Super Mario World</name>
    <desc>Description from metadata...</desc>
    <image>./downloaded_media/Super Mario World (USA)-image.png</image>
    <releasedate>19901121T000000</releasedate>
    <developer>Nintendo EAD</developer>
    <publisher>Nintendo</publisher>
    <genre>Platform</genre>
    <players>1-2</players>
    <rating>0.9</rating>
  </game>
</gameList>
```

---

## Destination Sync Engine

Added in v0.3.0. Lives in `src/romulus/core/sync.py` and
`src/romulus/core/dest_inventory.py`. The full design spec is in
`docs/sync-design.md` — this section summarises the moving parts and
records the post-implementation fixes.

### Five sync modes

- **`push_merge`** (default) — copy local-only files to the destination,
  leave dest-only files alone, skip identical pairs.
- **`push_mirror`** — same as push_merge plus delete dest-only files so
  the target matches the local library exactly. Destructive.
- **`push_wipe`** — wipe the destination's `base_path` first, then push.
  Most destructive.
- **`pull_merge`** — copy dest-only files back into the local library and
  enrol them as fuzzy matches.
- **`two_way`** — bi-directional diff. Conflicts (same rel_path, differing
  bytes) surface in the preview with a per-action resolution dropdown
  (`skip` / `local` / `dest` / `newest` / `prompt`).

### Four-tier identity matcher

For every dest file, `_match_dest_entry` tries (in order):

1. **Tier 1 — path equivalence.** Does the dest entry's `rel_path` equal
   what this local rom *would* land at under the active profile?
2. **Tier 2 — `(fuzzy_key, region, system_id)`.** Parses the dest
   filename, computes the same fuzzy_key + region the scanner would,
   joins with the system_id resolved from the dest folder via
   `_system_id_from_rel_path(rel_path, profile)`. The system_id segment
   is the cross-platform guard — without it, Game Boy `Pac-Man.gb` and
   Game Boy Color `Pac-Man.gbc` collide on identical fuzzy keys.
3. **Tier 3 — size sanity gate.** When tier 2 matches AND the local rom
   has a known SHA-1, mismatched sizes are logged but the match still
   wins (the spec treats size as a soft hint, not a hard reject).
4. **Tier 4 — SHA-1 deep verify.** Authoritative when present. Set by
   the user toggling "Deep verify" during the destination scan.

### Persistent cache + plan

- **`sync_destinations`** — user's saved destination targets (target_path,
  profile_id, last_synced_at, last_inventory_signature).
- **`dest_inventory`** — cached filesystem state per destination
  (`dest_id, rel_path, size_bytes, mtime, sha1, rom_id, last_seen_at`).
  Primary key `(dest_id, rel_path)`. Signature-drift detection invalidates
  the cache when the user has manually moved files. The `game_id` column
  is removed in v0.4.0; `rom_id` is the sole identity anchor.
- **`sync_plans`** — persisted JSON payload of every preview + apply
  (`dest_id, mode, status, summary, plan_json, created_at`). Foundation
  for a history dialog (deferred).

### Apply semantics

- Per-action SAVEPOINT rollback. A failed copy never leaves the
  inventory cache out of sync with disk.
- `plan.dest_id` is threaded into every helper (`_execute_copy_to_dest`,
  `_execute_delete_dest`, `_rebuild_gamelists`) rather than re-derived
  from `str(target_path)` — Path stringification can diverge from the
  value stored when the destination row was created (UNC trailing slash,
  separator normalization), and a mismatch returns `-1` and breaks every
  subsequent `upsert_dest_inventory` on the FK constraint. One-shot
  syncs (no saved destination, `dest_id < 0`) silently skip the inventory
  write; the file copy still completes.
- Cover art follows the ROM on copy. `gamelist.xml` is rebuilt for every
  system touched by the sync regardless of mode.
- Cover deletion on `delete_dest` actions is keyed by filename within
  the system folder.

### Sync API surface

```python
# core/sync.py
def build_plan(conn, dest_id, profile, target, inv, mode,
               *, conflict_policy="skip", library_path=None) -> SyncPlan
def apply_plan(conn, plan, profile, target_path, *,
               library_path=None, progress_callback=None) -> SyncSummary
def persist_plan(conn, plan, status="pending") -> int
def load_plan(conn, plan_id) -> SyncPlan | None
```

`SyncPlan` carries `dest_id`, `mode`, list of `SyncAction`, and a
`conflict_policy`. Each `SyncAction` has a kind (`copy_to_dest`,
`delete_dest`, `copy_to_local`, `conflict`, `identical`), rel_path,
local_path, dest_path, size_bytes, rom_id, system_id, and an
`executed` flag set by `apply_plan` for partial-failure replay.
In v0.4.0, `game_id` is removed from `SyncAction`.

### Tests

`tests/test_sync.py`, `tests/test_sync_preview.py`,
`tests/test_sync_fixes.py` cover the five modes, all four identity
tiers, conflict policies, atomic-delete via tombstone, plan
persistence + reload, gamelist rebuild on every mode, pull-mode
enrolment, unknown-system `_unsorted/` fallback, signature-drift
re-recognition, the cross-platform tier-2 guard, and the
path-mismatch `plan.dest_id` regression.

---

## Library Cleanup (Tombstone-Missing)

Added in v0.3.0. Lives in `src/romulus/core/scanner.py` (sweep step) and
`src/romulus/db/queries.py` (`mark_missing_under_root`,
`count_missing_roms`, `delete_missing_roms`, `_delete_rom_dependents`,
`count_roms_with_other_library_root`,
`delete_roms_with_other_library_root`).

In v0.4.0, `prune_orphan_games` is deleted. All tables that referenced
`games` or carried dependent data (`metadata`, `covers`,
`collection_roms`) now declare `ON DELETE CASCADE` on their `rom_id`
FK. Deleting a rom row via `delete_missing_roms` or
`delete_roms_with_other_library_root` automatically cleans all
dependents without any explicit caller action.

### Schema additions

```sql
-- v0.3.0 columns on roms (existing columns unchanged)
library_root  TEXT,                        -- canonical absolute path
missing       INTEGER NOT NULL DEFAULT 0,  -- 0=present, 1=tombstoned

CREATE INDEX idx_roms_library_root ON roms(library_root);
CREATE INDEX idx_roms_missing ON roms(missing) WHERE missing = 1;
```

### Scan-time behavior

```python
# core/scanner.py, end of scan_library:
files_newly_missing = queries.mark_missing_under_root(
    conn, library_root_str, visited_rom_ids
)
# Note: library_root_str is captured but not used by the SQL filter —
# the sweep marks ALL unvisited rows missing (library-agnostic) under
# the single-library design. The parameter is kept for backward compat.
```

The library-agnostic sweep matches the "one library at a time" design
rule. If the user previously scanned library A, then switches to
library B and scans, A's rows all flip to `missing=1` because they
weren't visited. Re-scanning A flips them back via the path-keyed
UPSERT in `upsert_rom` (which always sets `missing=0` on conflict).

### `upsert_rom` invariants

- Uses `RETURNING id` to get the correct row id whether the UPSERT goes
  INSERT or UPDATE. `cursor.lastrowid` is unreliable for UPSERT-UPDATE
  on SQLite — the connection's `last_insert_rowid` doesn't update on
  the UPDATE branch and returns the most-recent INSERTED rowid, which
  is the wrong row in a multi-file rescan.
- Every successful upsert sets `missing = 0`. Pre-existing rows that
  were tombstoned get un-tombstoned via UPSERT-UPDATE without any
  caller action.
- `library_root` uses `COALESCE(excluded.library_root, roms.library_root)`
  so passing `None` doesn't blank out an existing value.

### Library-switch flow

```python
# ui/main_window.py::_on_open_library
chosen_canonical = str(Path(chosen).resolve())
stale_count = q.count_roms_with_other_library_root(conn, chosen_canonical)
if stale_count > 0:
    if user confirms "Switch library?":
        q.delete_roms_with_other_library_root(conn, chosen_canonical)
        # v0.4.0: ON DELETE CASCADE on metadata/covers/collection_roms handles cleanup;
        # prune_orphan_games is removed.
        conn.commit()
set_config(conn, "library_path", chosen)
```

### Clean Missing flow

```python
# ui/main_window.py::_on_clean_missing
count = q.count_missing_roms(conn)
if count == 0: show "No missing entries"
elif user confirms:
    q.delete_missing_roms(conn)
    # v0.4.0: ON DELETE CASCADE on metadata/covers/collection_roms/hashes
    # cleans all dependents; prune_orphan_games is removed.
    conn.commit()
```

### FK cascade

In v0.4.0, `metadata`, `covers`, and `collection_roms` all declare
`ON DELETE CASCADE` on their `rom_id` FK — deleting a roms row
atomically removes all three. `hashes` and `dest_inventory` do not
declare CASCADE (SQLite requires a table-recreate to add it to an
existing table with data). `_delete_rom_dependents` still handles
`hashes` and `dest_inventory` explicitly in chunks of 500 ids before
the rom delete, so the FK constraint on those tables never triggers.

### Status bar surfacing

`refresh_game_table` writes
`"{total} ROMs ({missing} missing — Tools > Clean Missing Entries)"`
when any tombstones exist, otherwise just `"{total} ROMs"`.

### Tests

`tests/test_library_cleanup.py` (24 tests): scanner sweep,
reconnect-untombstone, library-root stamping, single-library
cross-library flagging, count/delete with-other-root, FK-dependent
delete, orphan-game prune, `upsert_rom` resets missing, logging
precedence (env var > Settings > default).

---

## Packaging & Distribution

Added in v0.2.0, revised in v0.3.0.

### Portable Windows ZIP (`build-portable.ps1`)

1. PyInstaller (`--onefile` per `romulus.spec`) produces
   `dist/romulus.exe`. The exe self-extracts to `%TEMP%/_MEIxxxxxx/` on
   launch and runs from there.
2. `build-portable.ps1` moves the exe into `dist/romulus/`, then copies
   `data/dats/` → `dist/romulus/dats/`, `profiles/` → `dist/romulus/profiles/`,
   `systems/` → `dist/romulus/systems/`.
3. The `dist/romulus/` folder is ZIPed to
   `dist/romulus-windows-x64.zip`.

End-user layout:
```
romulus/
  romulus.exe       (single self-contained binary)
  dats/*.dat        (bundled No-Intro DATs)
  profiles/*.yaml   (destination profiles — user-editable)
  systems/*.yaml    (system registry — user-editable)
```

After first launch the app creates `data/` (SQLite DB + cover cache)
and `logs/` (rotating log) alongside the exe.

### Spec choices

- **`--onefile` over `--onedir`** — produces a single binary at the
  cost of ~1.5s extra startup on first launch each session
  (bootloader unpacks to %TEMP%). Acceptable for an infrequently-
  updated portable.
- **`contents_directory='.'`** would be redundant since onefile has no
  contents directory.
- **UPX disabled.** Trips Windows Defender heuristics; the savings
  are marginal vs. ZIP compression.
- **Themes + icons embedded** (loaded via `Path(__file__).parent`),
  but **profiles/systems/dats are external** (live alongside the exe
  in the ZIP) so users can edit them without launching the app.

### Install-dir resolution

`app._resolve_install_dir` returns:

1. `sys.executable.parent` when running frozen.
2. Walks up from the module looking for `pyproject.toml` when running
   from a dev clone.
3. Falls back to `~/.romulus/` if neither works.

`app.resolve_data_dir` then prefers `<install_dir>/data` if writable,
else `~/.romulus/`. `ROMULUS_DATA_DIR` env var overrides both.

### Three-tier profile + system YAML loading

`exporter.load_all_profiles` and `models.system.load_systems_from_yaml`
both consult, in order:

1. `~/.romulus/profiles/` (or `<install_dir>/profiles/` for v0.2.0+)
2. `<install_dir>/profiles/` (the bundled defaults shipped in the ZIP)
3. `importlib.resources.files(romulus.data.profiles)` (the empty stub
   left in the wheel for backward compat with editable installs)

Same pattern for systems. User-supplied YAML overrides built-in by id.

### Tests

`tests/test_packaging.py` covers `_resolve_install_dir`,
`resolve_data_dir` (env var, install dir writable, legacy fallback),
`ensure_user_editable_files` (creates folders + seeds defaults),
three-tier profile loading precedence, system YAML round-trip, and
the Settings → Diagnostics tab surfacing of install + data dirs.

---

## UI Components

### Main Window Layout

```
┌────────────────────────────────────────────────────────────┐
│  Menu Bar: File | Edit | View | Tools | Help               │
├────────────────────────────────────────────────────────────┤
│  Toolbar: [Quick Scan] [Heavy Scan] | [Organize] [Enrich] │
│           [Export] | [Settings]                             │
├──────────┬─────────────────────────┬───────────────────────┤
│ System   │  Game Table             │  Detail Panel         │
│ Sidebar  │  ┌──────────────────┐   │  ┌─────────────────┐ │
│          │  │ Search: [_______]│   │  │ [Cover Art]     │ │
│ All (N)  │  │ Filter: [Region▾]│   │  │                 │ │
│ ──────── │  ├──────────────────┤   │  │ Title           │ │
│ ■ NES    │  │ Name    │Sys│Reg │   │  │ System          │ │
│ ■ SNES   │  │─────────┼───┼───│   │  │ Region / Rev    │ │
│ ■ N64    │  │ Game 1  │SNE│USA│   │  │ Genre           │ │
│ ■ GBA    │  │ Game 2  │SNE│EUR│   │  │ Developer       │ │
│ ■ Genesis│  │ Game 3  │SNE│JPN│   │  │ Publisher       │ │
│ ■ PSX    │  │ ...     │   │   │   │  │ Description...  │ │
│ ──────── │  └──────────────────┘   │  │                 │ │
│ Favs (N) │                         │  │ [★ Favorite]    │ │
│ Custom(N)│                         │  │ [Add to...]     │ │
│          │  Status: 54,672 ROMs    │  └─────────────────┘ │
└──────────┴─────────────────────────┴───────────────────────┘
```

### Key Widgets

- **SystemSidebar:** QTreeView with system icons, ROM counts per system, special entries for collections
- **GameTable:** QTableView with sortable columns (Name, System, Region, Size, Match Status). Lazy-loads rows. Search bar filters in real time.
- **DetailPanel:** QWidget with cover art (QLabel with pixmap), metadata labels, action buttons (Favorite, Add to Collection)
- **ScanProgressDialog:** QProgressDialog with per-file updates, ETA, cancel button
- **OrganizePreviewDialog:** QDialog with before/after tree, checkboxes per action, collision review
- **ExportDialog:** QDialog with profile selector, path chooser, filter checkboxes, preview, progress
- **SettingsDialog:** QDialog with tabs: General (library path, theme), DATs (folder paths), Metadata (source toggles, ScreenScraper credentials), Export (default profile)

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Library path doesn't exist | Show error dialog, prompt to select valid path |
| Corrupt/unreadable ROM file | Log warning, skip file, increment error count in scan_history |
| Corrupt ZIP archive | Log warning, skip file, record in scan errors |
| DAT parse failure | Log error, skip DAT file, continue with others |
| Network unreachable (metadata fetch) | Queue for retry, show "offline" indicator, continue with cached data |
| libretro-thumbnails 404 | No cover for this game — show placeholder, not an error |
| Hasheous timeout/error | Fall back to LaunchBox XML, log warning |
| ScreenScraper rate limit (429) | Exponential backoff, max 3 retries, then skip |
| Export target full | Stop export, show error with bytes written / bytes remaining |
| File permission error during organize | Skip that action, log error, continue with others, report at end |
| Hash mismatch on re-scan | Update hash, re-run DAT match, log as "content changed" |

---

## Technical Guardrails

1. **No external CDN/JS dependencies** — all assets vendored locally if needed
2. **SQLite only** — no PostgreSQL, no MariaDB, no Redis
3. **No ORM** — plain SQL strings in `db/queries.py`, not scattered across modules
4. **Pydantic for boundaries only** — data models crossing module boundaries use Pydantic; internal state can use dataclasses or plain dicts
5. **httpx only** — no requests, no urllib, no aiohttp
6. **Hash cache invalidation** — keyed by (path, mtime, size). If any differ, rehash.
7. **Never overwrite without preview** — Organizer and Exporter always show a preview before modifying the filesystem
8. **DAT files are read-only** — the app never modifies DAT files
9. **Cover cache is append-only** — once downloaded, covers are never re-downloaded unless user explicitly requests refresh
10. **Profiles are YAML** — not JSON, not TOML. YAML with comments for user editability.

---

## Session Definitions

> **Note:** Sessions 0–11 below are historical bootstrap definitions that built
> v0.1.0–v0.3.0. They reference the old `games` table, `upsert_game`,
> `link_rom_to_game`, `collection_games`, and `find_cross_extension_dupes` —
> all of which were removed in the v0.4.0 strict 1:1 refactor (sessions 13–19
> in `docs/sessions/`). These session files are preserved as an audit trail.
> Do not implement them verbatim — consult the v0.4.0 schema sections above
> and `docs/strict-1to1-design.md` for the current data model.

### Session Types

**Build sessions:** Code → write new tests → run tests → completion summary → STOP.

**Review sessions:** Read previous completion summaries → review all code changed → security review → fix loop → run full test suite → documentation sync → STOP.

### Test Commands (all sessions)

```bash
# Run tests
pytest

# Lint
ruff check src/ tests/

# Full validation (must pass before session end)
pytest && ruff check src/ tests/
```

### Session Overview

| Session | Title | Type | File |
|---|---|---|---|
| 0 | Bootstrap & Scaffold | Bootstrap | `docs/sessions/00-bootstrap.md` |
| 1 | Data Models, SQLite Schema & System Registry | Build | `docs/sessions/01-data-models.md` |
| 2 | Scanner & Filename Parser | Build | `docs/sessions/02-scanner.md` |
| 3 | Identifier Pipeline (Headers + Hashing + DAT Parser) | Build | `docs/sessions/03-identifier.md` |
| 4 | Review & Docs Sync | Review | `docs/sessions/04-review.md` |
| 5 | UI Shell, System Browser & Game Table | Build | `docs/sessions/05-ui-shell.md` |
| 6 | Metadata Enrichment & Cover Art | Build | `docs/sessions/06-metadata.md` |
| 7 | Game Detail Panel, Search/Filter, Collections | Build | `docs/sessions/07-detail-panel.md` |
| 8 | Review & Docs Sync | Review | `docs/sessions/08-review.md` |
| 9 | Library Organizer | Build | `docs/sessions/09-organizer.md` |
| 10 | Export Engine & Destination Profiles | Build | `docs/sessions/10-exporter.md` |
| 11 | Final Review, README & Polish | Review | `docs/sessions/11-final-review.md` |

---

### Session 0: Bootstrap & Scaffold [NOT STARTED]

**Type:** Bootstrap

**Tasks:**

- [ ] Split sessions into individual files. Create `docs/sessions/` directory. For each session defined in this file (including this one), create a file named `docs/sessions/NN-slug.md` containing that session's full definition including the Context section. Copy verbatim — do not summarize.
- [ ] Verify toolchain:
  - `python --version` (3.12+)
  - Create `.venv` and activate
  - `pip install pyside6 httpx pydantic structlog pyyaml pytest ruff`
  - `pyside6-designer --version` or equivalent (confirm PySide6 installed)
  - `ruff --version`
  - `pytest --version`
- [ ] Project scaffolding:
  - Create `pyproject.toml` with all dependencies and project metadata
  - Create `src/romulus/__init__.py`, `__main__.py` (minimal entry point that prints "ROMulus v0.1.0")
  - Create directory structure per CLAUDE.md Project Structure
  - Create `data/dats/` directory (empty for now — DATs added in Session 3)
  - Create `data/profiles/` directory (empty for now — profiles added in Session 10)
  - Create `.gitignore` (Python defaults + .venv + __pycache__ + .romulus/)
- [ ] Verify build and lint pass:
  - `python -m romulus` runs and prints version
  - `pytest` passes (no tests yet, zero errors)
  - `ruff check src/ tests/` clean
- [ ] Git: `git init`, initial commit with all scaffolding

**Acceptance criteria:**
- All session files exist in `docs/sessions/` with correct content
- Project runs `python -m romulus` and prints version
- pytest and ruff pass clean
- `.venv` created with all dependencies installed

⛔ **STOP. Tell me Session 0 is complete. Do not proceed to Session 1.**

---

### Session 1: Data Models, SQLite Schema & System Registry [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building the foundation layer: Pydantic data models, SQLite schema, and the system registry that seeds the database with all supported platforms.

SQLite schema — use the full schema from `docs/TECHNICAL_PLAN.md` §4 (SQLite Schema section). **v0.4.0 schema note:** `games` table is removed; `collection_games` → `collection_roms`; all tables FK to `roms.id` with `ON DELETE CASCADE`. See the schema section above for the current DDL.

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

⛔ **STOP. Commit with message "Session 1: Data models, SQLite schema, system registry". Do not proceed to Session 2.**

---

### Session 2: Scanner & Filename Parser [NOT STARTED]

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

⛔ **STOP. Commit with message "Session 2: Scanner and filename parser". Do not proceed to Session 3.**

---

### Session 3: Identifier Pipeline (Headers + Hashing + DAT Parser) [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building Layer 2 (internal header extraction) and Layer 3 (hashing + DAT matching) of the identifier pipeline, plus the Logiqx XML DAT parser.

Internal header locations (from TECHNICAL_PLAN.md §6 — Identifier Pipeline):
- SNES: title at `0x7FC0` (LoROM) or `0xFFC0` (HiROM), 21 bytes. Strip SMC 512-byte header first if `size % 1024 == 512`.
- N64: title at `0x20`, 20 bytes. Byte-swap to z64 first. Magic: `80 37 12 40` = z64, `37 80 40 12` = v64, `40 12 37 80` = n64.
- Mega Drive: title at `0x150` (overseas) / `0x120` (domestic), 48 bytes. Check "SEGA MEGA DRIVE" / "SEGA GENESIS" at `0x100`.
- GB/GBC: title at `0x134`, 11-16 bytes. Trim at first null.
- GBA: title at `0xA0`, 12 bytes.
- DS: title at `0x00`, 12 bytes.

Header strip rules for hashing:
- `smc_512`: strip 512 bytes if `size % 1024 == 512`
- `ines_16`: strip 16 bytes if magic is `NES\x1a`
- `n64_byteswap`: convert to z64 byte order before hashing
- `lynx_64`: strip 64 bytes if magic is `LYNX\x00`

ZIP handling: if `.zip` with single inner file, hash the inner content (after header stripping). If multiple inner files (MAME romset), hash the largest.

DAT format: Logiqx XML. Parse with `xml.etree.ElementTree`. Extract: game name, rom name, size, CRC32, MD5, SHA-1.

DAT matching: look up by SHA-1 first, then CRC32+size as fallback.

**Tasks:**

- [ ] Create `src/romulus/core/identifier.py`:
  - `extract_header_title(file_path, header_rule)` — read internal title based on system's header_rule
  - Returns None for systems without headers
- [ ] Create `src/romulus/core/hasher.py`:
  - `hash_rom(file_path, header_rule)` — compute CRC32 + SHA-1 with header stripping and zip extraction
  - `normalize_rom_content(content, header_rule)` — apply strip/byteswap rules
  - `hash_library(conn, progress_callback, workers=8)` — parallel hash all unhashed/changed ROMs using ThreadPoolExecutor
  - Hash cache check: skip if (path, mtime, size) unchanged since last hash
- [ ] Create `src/romulus/core/dat_parser.py`:
  - `parse_dat_file(filepath)` — parse single DAT, return list of DatEntry records
  - `load_all_dats(conn, dat_paths)` — parse all DATs from bundled + user folders, insert into dat_entries table
  - `match_hashes(conn)` — for all hashed ROMs, look up in dat_entries by SHA-1 then CRC32+size. Update rom.dat_match and rom.match_confidence.
  - `parse_region_from_name(game_name)` — extract region tags from canonical name
- [ ] Add DAT-related queries to `db/queries.py`:
  - `insert_dat_entry(conn, entry)`, `get_dat_by_sha1(conn, sha1)`, `get_dat_by_crc_size(conn, crc32, size)`, `update_rom_match(conn, rom_id, dat_match, confidence)`
- [ ] Add hash-related queries to `db/queries.py`:
  - `upsert_hash(conn, rom_id, crc32, sha1, md5)`, `get_hash(conn, rom_id)`, `get_unhashed_roms(conn)`, `get_stale_hashes(conn)` (mtime changed)
- [ ] Copy bundled No-Intro DAT files into `data/dats/`. Include DATs for the ~30 systems in the system registry. (If actual DAT files aren't available at dev time, create 2-3 small test DAT files with known entries for testing.)
- [ ] Write tests:
  - `tests/test_identifier.py`: test header extraction for SNES (LoROM/HiROM, with/without SMC header), N64 (z64/v64/n64 byte orders), GB, GBA, MD, DS
  - `tests/test_hasher.py`: test hash computation with header stripping, ZIP extraction, hash caching logic
  - `tests/test_dat_parser.py`: test Logiqx XML parsing, DAT matching by SHA-1, CRC32+size fallback, region parsing

**Acceptance criteria:**
- Internal header extraction works for SNES, N64, MD, GB/GBC, GBA, DS
- Hashing correctly strips headers (SMC, iNES, Lynx) and byte-swaps N64 before computing SHA-1
- ZIP files handled: single-file extracted, multi-file hashes largest
- DAT parser reads Logiqx XML and populates dat_entries table
- Hash matching links ROMs to DAT entries by SHA-1 with CRC32+size fallback
- All tests pass, ruff clean

⛔ **STOP. Commit with message "Session 3: Identifier pipeline, hasher, DAT parser". Do not proceed to Session 4.**

---

### Session 4: Review & Docs Sync [NOT STARTED]

**Type:** Review

**Covers:** Sessions 1–3

**Tasks:**

- [ ] Read completion summaries from `docs/sessions/01-data-models.md`, `02-scanner.md`, `03-identifier.md`
- [ ] Code review all code changed in Sessions 1–3:
  - Check for error handling gaps (file I/O, corrupt files, missing directories)
  - Check for unused imports, dead code
  - Check type hints coverage on all public functions
  - Check docstrings on all public classes/methods
  - Check SQL injection risk (all queries should use parameterized placeholders)
  - Check thread safety of SQLite access (connections not shared across threads)
- [ ] Security review:
  - Path traversal risk in scanner (does it stay within library_path?)
  - SQL injection in query parameters
  - File permission handling during scan
- [ ] Fix any findings, re-run `pytest && ruff check src/ tests/`
- [ ] Update any documentation that completion summaries flagged

**Acceptance criteria:**
- All review findings addressed
- All tests pass after fixes
- ruff clean

⛔ **STOP. Commit with message "Session 4: Review sessions 1-3". Do not proceed to Session 5.**

---

### Session 5: UI Shell, System Browser & Game Table [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building the main application window with PySide6. The UI has three panels: system sidebar (left), game table (center), and a placeholder detail panel (right, built in Session 7).

Main window layout (from TECHNICAL_PLAN.md §11 — UI Components):
- Menu bar: File (Open Library, Settings, Quit), View (columns toggle), Tools (Quick Scan, Heavy Scan, Organize, Enrich, Export), Help (About)
- Toolbar: Quick Scan, Heavy Scan, Organize, Enrich, Export, Settings buttons
- System sidebar: QTreeView listing all systems with ROM counts, "All" at top, collections section at bottom
- Game table: QTableView with columns: Name, System, Region, Size, Match Status. Sortable. Search bar above.
- Status bar: total ROM count, scan status

The app entry point (`__main__.py`) creates a QApplication, initializes the database (create tables, seed systems, seed config), and shows the main window.

On first launch, if no library_path is configured, show a folder picker dialog: "Select your ROM library folder".

Quick Scan button triggers a scan in a QThread worker, with progress dialog. After scan completes, game table refreshes.

**Tasks:**

- [ ] Update `src/romulus/__main__.py`:
  - Create QApplication
  - Initialize database (create_tables, seed_systems, seed_defaults)
  - Check config for library_path — if empty, show folder picker
  - Show MainWindow
- [ ] Create `src/romulus/app.py`:
  - App initialization logic (DB setup, config loading)
- [ ] Create `src/romulus/ui/main_window.py`:
  - MainWindow(QMainWindow) with menu bar, toolbar, status bar
  - Three-panel layout using QSplitter: sidebar | game table | detail placeholder
  - Connect toolbar buttons to actions
- [ ] Create `src/romulus/ui/system_sidebar.py`:
  - SystemSidebar(QTreeView) backed by a QStandardItemModel
  - "All" entry at top showing total ROM count
  - One entry per system that has ROMs, showing count
  - "Favorites" and collections section at bottom
  - Signal: system_selected(system_id) — filters the game table
- [ ] Create `src/romulus/ui/game_table.py`:
  - GameTable(QTableView) backed by QAbstractTableModel subclass (GameTableModel)
  - Columns: Name, System, Region, Size, Match Status
  - Sortable by clicking column headers
  - Search bar (QLineEdit) above table — filters by game name in real time
  - Lazy-load rows from SQLite (paginate if >5000 games)
- [ ] Create `src/romulus/ui/workers.py`:
  - ScanWorker(QThread) — runs scan_library in background, emits progress/finished signals
  - Connect to ScanProgressDialog
- [ ] Create `src/romulus/ui/scan_progress.py`:
  - ScanProgressDialog(QProgressDialog) — shows file count, current file, cancel button
- [ ] Create `src/romulus/ui/settings_dialog.py`:
  - SettingsDialog(QDialog) with tabs:
    - General: library path (folder picker), theme selector
    - DATs: DAT folder paths (list + add/remove buttons)
    - Metadata: ScreenScraper credentials (username/password fields, test button)
    - Scan: thread count spinner
  - Save all settings to config table
- [ ] Write tests:
  - `tests/test_ui.py`: test GameTableModel data loading, sorting, filtering (can test model without showing UI)

**Acceptance criteria:**
- App launches with `python -m romulus`, shows main window
- First launch prompts for library folder
- Quick Scan button triggers scan with progress dialog
- System sidebar populates with systems that have ROMs
- Game table shows ROM list, sortable and searchable
- Settings dialog reads/writes config table
- All tests pass, ruff clean

⛔ **STOP. Commit with message "Session 5: UI shell, system sidebar, game table". Do not proceed to Session 6.**

---

### Session 6: Metadata Enrichment & Cover Art [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building the metadata fetching clients and cover art download system. Three sources, in priority order:

1. **libretro-thumbnails** (cover art only, free, no API key):
   - URL: `https://thumbnails.libretro.com/{libretro_name}/Named_Boxarts/{game_name}.png`
   - `{libretro_name}` = system.libretro_name, URL-encoded
   - `{game_name}` = No-Intro canonical name with `&*/:\<>?\|"` → `_`
   - Three types: Named_Boxarts, Named_Snaps, Named_Titles
   - Download to `~/.romulus/covers/{system_id}/{cover_type}/{game_name}.png`
   - 404 = no cover available, not an error

2. **Hasheous** (metadata, free, no API key):
   - Endpoint: `https://hasheous.org/api/v1/lookup/{hash_type}/{hash_value}`
   - Returns: title, description, genre, developer, publisher, release date
   - Use SHA-1 as hash_type
   - Rate: 1 req/sec with backoff on 429

3. **LaunchBox XML** (metadata, offline fallback):
   - Downloadable XML database, ~200 MB
   - Parse once, match by title + system
   - Store in metadata table

4. **ScreenScraper** (optional, user-prompted):
   - Only if user has configured credentials in Settings
   - API: `https://api.screenscraper.fr/api2/`
   - Rate: 1 req/sec max for free tier

Enrichment runs as a background QThread worker. User clicks "Enrich" button. Progress dialog shows per-game updates.

**Tasks:**

- [ ] Create `src/romulus/metadata/libretro.py`:
  - `fetch_cover(system, game_name, cover_type, cache_dir)` — download PNG from libretro-thumbnails
  - `build_thumbnail_url(libretro_name, game_name, cover_type)` — construct URL with character replacements
  - `sanitize_game_name(name)` — replace `&*/:\<>?\|"` with `_`
  - Handle 404 gracefully (no cover available)
- [ ] Create `src/romulus/metadata/hasheous.py`:
  - `lookup_by_hash(sha1)` — call Hasheous API, return metadata dict
  - Parse response into metadata fields (description, genre, developer, publisher, release_date)
  - Rate limiting: 1 req/sec with exponential backoff
- [ ] Create `src/romulus/metadata/launchbox.py`:
  - `parse_launchbox_xml(xml_path)` — parse LaunchBox database XML
  - `match_game(title, system_id, db)` — fuzzy match game title against LaunchBox entries
  - Store matched metadata in SQLite
- [ ] Create `src/romulus/metadata/screenscraper.py`:
  - `lookup_game(sha1, system_id, credentials)` — call ScreenScraper API
  - Only called if credentials are configured
  - Rate limiting: 1 req/sec strict
  - Stub implementation is fine if API details need more research
- [ ] Create enrichment orchestrator in `src/romulus/metadata/__init__.py`:
  - `enrich_library(conn, cache_dir, progress_callback)` — orchestrate enrichment across sources
  - For each DAT-matched game: try libretro-thumbnails for covers, Hasheous for metadata, LaunchBox as fallback
  - Skip games that already have metadata (don't re-fetch)
- [ ] Add metadata/cover queries to `db/queries.py`:
  - `upsert_metadata(conn, rom_id, metadata_dict)`, `get_metadata(conn, rom_id)`
  - `insert_cover(conn, rom_id, cover_type, source_url, local_path)`, `get_covers(conn, rom_id)`
  - `get_roms_needing_enrichment(conn)` — ROMs with match_confidence="dat_verified" but no metadata
  - **v0.4.0 addition:** `find_sibling_metadata`, `copy_metadata`, `find_sibling_covers`, `copy_covers`
    (sibling-copy gate — see architecture.md rule 29 and docs/strict-1to1-design.md §3)
- [ ] Add EnrichWorker to `src/romulus/ui/workers.py`:
  - QThread worker that runs enrich_library, emits progress signals
- [ ] Write tests:
  - `tests/test_metadata.py`: test URL construction for libretro-thumbnails, game name sanitization, Hasheous response parsing, LaunchBox XML parsing. Use mocked HTTP responses (httpx mock or responses library).

**Acceptance criteria:**
- libretro-thumbnails cover art downloads work for DAT-matched games
- Hasheous metadata lookup returns descriptions/genres for known hashes
- LaunchBox XML parser extracts metadata for matched games
- Cover art cached to `~/.romulus/covers/`
- Enrich button triggers background enrichment with progress dialog
- All tests pass, ruff clean

⛔ **STOP. Commit with message "Session 6: Metadata enrichment and cover art". Do not proceed to Session 7.**

---

### Session 7: Game Detail Panel, Search/Filter, Collections [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building the right-side detail panel, wiring up search/filter to the game table, and implementing the collections system (favorites, custom groups).

Detail panel layout:
- Cover art image (QLabel with scaled pixmap, placeholder if no cover)
- Title (bold, large font)
- System name
- Region / Revision
- Genre, Developer, Publisher (from metadata table)
- Description (scrollable QTextEdit, read-only)
- Match status indicator (color-coded: green=DAT verified, yellow=header matched, gray=unmatched)
- Action buttons: ★ Favorite toggle, "Add to Collection..." dropdown

Filter controls above the game table:
- Search bar already exists from Session 5 — wire it to filter by game title
- Region filter dropdown (All, USA, Europe, Japan, World, Other)
- Match status filter (All, Verified, Unmatched)

Collections:
- "Favorites" is a built-in system collection
- Users can create custom collections ("Export to Anbernic", "RPGs", etc.)
- Collections appear in the system sidebar under a separator
- Right-click game → "Add to Collection" context menu

**Tasks:**

- [ ] Create `src/romulus/ui/detail_panel.py`:
  - DetailPanel(QWidget) showing cover art, metadata, action buttons
  - `update_rom(rom_id)` — fetch rom, metadata, cover from SQLite, update display
    (v0.4.0: renamed from update_game; reads SHA-1 / DAT name / region directly from the selected rom)
  - Cover art: load from cache path, scale to fit panel width, show placeholder if missing
  - Match status: colored badge (green/yellow/gray)
- [ ] Wire detail panel to game table selection:
  - When user clicks a row in GameTable, emit rom_selected(rom_id) signal
    (v0.4.0: renamed from game_selected(game_id))
  - MainWindow connects signal to DetailPanel.update_rom
- [ ] Add filter controls to game table:
  - Region filter dropdown (QComboBox) — filters GameTableModel
  - Match status filter (QComboBox) — filters by match_confidence
  - Wire search bar to filter by title (already exists, may need re-wiring)
- [ ] Implement collections system:
  - Add collection queries to `db/queries.py`: create_collection, delete_collection,
    add_rom_to_collection, remove_rom_from_collection, get_collection_roms, get_collections
    (v0.4.0: renamed from add_game_to_collection, remove_game_from_collection, get_collection_games)
  - Create "Favorites" as a system collection on first run (is_system=1)
  - Add ★ Favorite toggle button in DetailPanel — adds/removes from Favorites collection
  - Add "Add to Collection..." button — shows dropdown of user collections
  - Add "New Collection..." option in dropdown
- [ ] Update SystemSidebar:
  - Add collections section below systems separator
  - Show collection names with game counts
  - Clicking a collection filters the game table to show only its games
- [ ] Add right-click context menu on game table rows:
  - "Add to Favorites"
  - "Add to Collection..." → submenu of collections
  - "Remove from Collection" (when viewing a collection)
- [ ] Write tests:
  - `tests/test_collections.py`: test create/delete collection, add/remove games, favorite toggle

**Acceptance criteria:**
- Clicking a game in the table shows its details in the right panel
- Cover art displays if cached, placeholder if not
- Search bar filters game table by title
- Region and match status dropdowns filter the table
- Favorites toggle works
- Custom collections can be created, games added/removed
- Collections appear in sidebar, clicking filters the table
- All tests pass, ruff clean

⛔ **STOP. Commit with message "Session 7: Detail panel, search/filter, collections". Do not proceed to Session 8.**

---

### Session 8: Review & Docs Sync [NOT STARTED]

**Type:** Review

**Covers:** Sessions 5–7

**Tasks:**

- [ ] Read completion summaries from `docs/sessions/05-ui-shell.md`, `06-metadata.md`, `07-detail-panel.md`
- [ ] Code review all code changed in Sessions 5–7:
  - UI thread safety: are all SQLite calls happening in workers, not the main thread? (Read-only queries for display are OK on main thread for small result sets)
  - Signal/slot connections: any disconnected signals or missing connections?
  - Memory management: are QThread workers properly cleaned up?
  - Error handling in metadata clients: network errors, malformed responses, missing fields
  - httpx usage: are connections being closed properly? Using context managers?
  - Cover cache: is the cache directory created if missing? Are file writes atomic?
- [ ] Security review:
  - ScreenScraper credentials stored in SQLite — is the DB file-permission restricted?
  - Any user input flowing into URLs without sanitization?
  - Any file paths from user input used without validation?
- [ ] Fix any findings, re-run `pytest && ruff check src/ tests/`
- [ ] Update documentation if completion summaries flagged changes

**Acceptance criteria:**
- All review findings addressed
- All tests pass after fixes
- ruff clean

⛔ **STOP. Commit with message "Session 8: Review sessions 5-7". Do not proceed to Session 9.**

---

### Session 9: Library Organizer [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building the library organization system — the workflow where users can reorganize their ROM folder in place with a preview/commit model.

Organizer workflow (from TECHNICAL_PLAN.md §9 — Library Organizer):
1. Analyze library state from SQLite
2. Generate organize_plan with actions: merge_folder, rename, delete_duplicate, collision
3. Display in OrganizePreviewDialog with before/after view
4. User reviews, approves/rejects individual actions
5. Execute approved actions, update SQLite paths, record plan status

Organize rules:
- Folder merges: only confirmed aliases (validated against system.folder_aliases)
- Renames: only DAT-matched ROMs (L3 confidence). Never rename unmatched files.
- Duplicate removal: only byte-identical (same SHA-1). Prefer canonical extension (.sfc over .smc), then smaller filename.
- Hacks: never merge or deduplicate against originals
- Collisions: same name but different content → flag for manual review, never overwrite

Action types:
- `merge_folder`: move all files from source folder to target folder (source is an alias of target)
- `rename`: rename file to canonical No-Intro name
- `delete_duplicate`: remove redundant copy (same SHA-1 as another file being kept)
- `collision`: source and target have same filename but different sizes/hashes — needs manual resolution

**Tasks:**

- [ ] Create `src/romulus/core/organizer.py`:
  - `analyze_library(conn)` — scan for organizeable actions, return OrganizePlan
  - `find_alias_merges(conn)` — identify folders that are aliases of the same system and have overlapping content
  - `find_renameable_roms(conn)` — ROMs with dat_match that differ from current filename
  - `find_duplicates(conn)` — ROMs with same SHA-1 in same or alias folders
  - `find_cross_extension_dupes(conn)` — **deleted in v0.4.0.** Was: same
    game_id, same folder, different extensions (.smc + .sfc). Relied on a
    shared game_id that no longer exists. SHA-1-based `find_duplicates`
    covers the same ground post-Bug 2 fix (TOCTOU guard now uses
    normalized hash via hash_rom). Do not re-add this function.
  - `detect_collisions(merge_pairs)` — files that would collide during a folder merge
  - `execute_plan(conn, approved_actions, progress_callback)` — execute filesystem changes and update DB
  - Rollback per-action on failure: if rename/move fails, skip it, log error, continue
- [ ] Add organize queries to `db/queries.py`:
  - `get_alias_folder_pairs(conn)` — folders sharing a system_id that aren't the canonical folder
  - `get_duplicate_groups(conn)` — groups of ROMs with identical SHA-1
  - `update_rom_path(conn, rom_id, new_path)` — after rename/move
  - `delete_rom(conn, rom_id)` — after duplicate removal
  - `insert_organize_plan(conn, plan_json)`, `update_plan_status(conn, plan_id, status)`
- [ ] Create `src/romulus/ui/organize_preview.py`:
  - OrganizePreviewDialog(QDialog):
    - Summary header: "X files to rename, Y folders to merge, Z duplicates to remove"
    - QTreeView showing proposed changes grouped by action type
    - Checkbox per action (all checked by default)
    - Collision section with side-by-side comparison (filename, size, hash)
    - "Select All" / "Deselect All" buttons
    - "Apply" button (executes checked actions)
    - "Cancel" button
    - Progress bar during execution
- [ ] Wire "Organize" toolbar button to open the preview dialog
- [ ] Write tests:
  - `tests/test_organizer.py`:
    - Test alias merge detection
    - Test duplicate finding (same hash)
    - Test cross-extension dedup (.smc + .sfc)
    - Test collision detection
    - Test plan execution with mock filesystem (use tmp_path)
    - Test rollback on failure

**Acceptance criteria:**
- Organize button opens preview dialog with proposed changes
- Before/after view shows what will change
- User can approve/reject individual actions
- Apply executes changes and updates SQLite
- Collisions flagged for manual review
- Hacks never merged with originals
- All tests pass, ruff clean

⛔ **STOP. Commit with message "Session 9: Library organizer with preview/commit". Do not proceed to Session 10.**

---

### Session 10: Export Engine & Destination Profiles [NOT STARTED]

**Type:** Build

**Context for this session:**

You are building the export engine that copies ROMs to a destination in a device-specific folder structure, and the destination profile system.

Profile format (YAML) — see TECHNICAL_PLAN.md §10 for the full YAML schema. Key fields per profile: id, name, case_sensitive, base_path, gamelist_format, artwork_subdir, multi_disc, systems map (system_id → {folder, extensions}).

Built-in profiles to create (6 YAML files in `data/profiles/`):
1. batocera.yaml — base_path: "roms", gamelist: emulationstation_xml
2. retropie.yaml — base_path: "roms", gamelist: emulationstation_xml
3. onionos.yaml — base_path: "ROMS", gamelist: null (Onion OS uses its own scraper)
4. muos.yaml — base_path: "ROMS", gamelist: null
5. mister.yaml — base_path: "games", gamelist: null
6. analogue-pocket.yaml — base_path: "Assets/{pocket_folder}/common", gamelist: null

System→folder mappings should match Igir's token output for compatibility. Reference Igir's docs for exact folder names per target.

Export workflow:
1. User selects profile + target path + optional filters
2. Preview shows file count, size, folder tree
3. Export copies files with progress
4. Optional: generate gamelist.xml per system folder
5. Optional: copy artwork to target's artwork directory
6. Optional: generate .m3u for multi-disc games

gamelist.xml format — see TECHNICAL_PLAN.md §10 for the XML structure.

**Tasks:**

- [ ] Create `src/romulus/core/exporter.py`:
  - `load_profile(yaml_path)` — parse YAML into DestinationProfile model
  - `load_all_profiles(builtin_dir, user_dir)` — load all profiles from both locations
  - `preview_export(conn, profile, target_path, filters)` — return file count, total size, folder tree without copying anything
  - `export_collection(conn, profile, target_path, filters, options, progress_callback)` — copy files to target
  - `generate_gamelist_xml(conn, system_id, system_folder, target_path)` — write gamelist.xml
  - `generate_m3u_playlists(conn, system_id, system_folder, target_path)` — write .m3u for multi-disc
  - `copy_artwork(conn, system_id, profile, target_path)` — copy covers to target artwork dir
- [ ] Create 6 built-in YAML profiles in `data/profiles/`:
  - Each profile must define system folder mappings for all ~30 systems in the registry
  - Use Igir-compatible folder names
- [ ] Create `src/romulus/ui/export_dialog.py`:
  - ExportDialog(QDialog):
    - Profile selector (QComboBox listing all loaded profiles)
    - Target path (QLineEdit + folder picker button)
    - System filter (list of checkboxes, all checked by default)
    - Collection filter (dropdown: "All games" or specific collection)
    - Region filter (checkboxes: USA, Europe, Japan, World, Other)
    - Options checkboxes: Include artwork, Generate gamelist.xml/.lpl
    - "Preview" button — shows file count, estimated size, folder tree in a QTextEdit
    - "Export" button — runs export with progress bar
    - Summary after completion: "Exported N games across M systems (X GB)"
- [ ] Add ExportWorker to `src/romulus/ui/workers.py`:
  - QThread worker that runs export_collection, emits progress signals
- [ ] Wire "Export" toolbar button to open ExportDialog
- [ ] Write tests:
  - `tests/test_exporter.py`:
    - Test profile YAML loading
    - Test export preview (file count, size calculation)
    - Test file copy to correct folder structure (use tmp_path)
    - Test gamelist.xml generation (validate XML structure)
    - Test m3u playlist generation
    - Test system filtering (export only selected systems)

**Acceptance criteria:**
- 6 built-in profiles load correctly
- Export preview shows accurate file count and size
- Files copied to correct folder structure for each profile
- gamelist.xml generated with metadata for EmulationStation targets
- .m3u generated for multi-disc games
- Artwork copied if option selected
- Progress dialog shows per-file updates
- All tests pass, ruff clean

⛔ **STOP. Commit with message "Session 10: Export engine and destination profiles". Do not proceed to Session 11.**

---

### Session 11: Final Review, README & Polish [NOT STARTED]

**Type:** Review (Final)

**Covers:** All sessions since last review (Sessions 9–10) plus full project review

**Tasks:**

- [ ] Read completion summaries from all build sessions since Session 8 review
- [ ] Code review: final review of all code
  - Consistency: naming conventions, import style, docstring format
  - Error handling: all I/O operations have try/except, user-friendly error messages
  - Type hints: complete coverage on all public functions
  - SQL: all queries parameterized, no string interpolation
  - UI: no blocking operations on main thread
  - Thread safety: SQLite connections per-thread, not shared
- [ ] Security review:
  - File path validation throughout
  - Network request error handling
  - Credential storage security
- [ ] Fix any findings, re-run `pytest && ruff check src/ tests/`
- [ ] GitHub Actions CI workflow:
  - `.github/workflows/ci.yml`: run `pytest` and `ruff check src/ tests/` on push/PR
  - Run locally first per CI/CD Local Validation Rule
- [ ] README.md:
  - Project description and screenshots/mockups
  - Installation (clone, create venv, pip install)
  - Quick start (first launch, select library, scan, enrich, organize, export)
  - Architecture overview
  - Configuration reference (all config keys)
  - Destination profiles (how to use built-in, how to create custom)
  - DAT files (what's bundled, how to add more)
  - Metadata sources (what's free, what needs accounts)
  - Development (setup, running tests, project structure)
  - Troubleshooting
- [ ] CHANGELOG.md — v0.1.0 entry with all features
- [ ] Final review: doc comments on all public types/functions, no TODO comments in production code, no dead code

**Acceptance criteria:**
- All CI checks pass locally before workflow is committed
- README covers installation, configuration, usage, and development
- All public types and functions have docstrings
- No TODO comments, no dead code, no unused imports
- `pytest && ruff check src/ tests/` clean
- App launches, scans, enriches, organizes, and exports successfully

⛔ **STOP. Tell me this session is complete and prompt me to do a final review and push.**

✅ **Project complete!**
