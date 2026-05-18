# Changelog

All notable changes to ROMulus will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] — v0.3.0 in development

The v0.3.0 cycle reshapes the project for actual real-world use. Major
themes: a destination sync engine, single-library cleanup semantics, a
single-binary portable Windows build, a debug-logging overhaul, and —
in the later wave — bundled offline metadata sources, a metadata /
cover-art workflow split, and a redesigned detail panel.

### Added

**Destination sync engine (`src/romulus/core/sync.py`,
`src/romulus/core/dest_inventory.py`, `src/romulus/ui/sync_preview.py`):**
- Five sync modes: `push_merge`, `push_mirror`, `push_wipe`, `pull_merge`,
  `two_way`. Mode picker in the Export / Sync dialog with per-mode
  tooltip + double-confirm prompt before any destructive run.
- Four-tier identity matcher: tier 1 path equivalence, tier 2
  `(fuzzy_key, region, system_id)`, tier 3 hash-by-name sanity gate,
  tier 4 SHA-1 deep verify. System_id is part of the tier-2 key so
  cross-platform collisions (Game Boy `Pac-Man.gb` vs Game Boy Color
  `Pac-Man.gbc`) never get matched together.
- `dest_inventory` cache (FK to `sync_destinations`) holds the
  destination's filesystem state between scans; signature-drift
  detection invalidates the cache when the user has manually moved
  files on the device.
- Persisted `sync_plans` rows for every preview + apply (JSON
  payload, status `pending`/`completed`/`failed`). Foundation for a
  history dialog deferred to a later release.
- Per-action SAVEPOINT rollback so a mid-sync failure on one file
  leaves the rest of the plan applicable.
- Cover art follows the ROM on copy; gamelist.xml is rebuilt for every
  system touched by the sync regardless of mode.
- `_dest_id_from_target` lookups replaced with direct `plan.dest_id`
  threading after a UNC-path-normalization mismatch caused thousands
  of FK errors during apply.

**Library cleanup (single-library design):**
- `roms.library_root` column stamps every row with the canonical path
  it was scanned under. `roms.missing` tombstones files that vanish
  from disk rather than dropping the row, so enrichment / hash cache
  / metadata survive a temporarily-unmounted share.
- Scan sweep at the end of `scan_library` marks every row not visited
  this scan as `missing=1` — library-agnostic, matching the
  "one library at a time" design rule. Re-scanning a reconnected
  library un-tombstones rows via the path-keyed UPSERT.
- **File → Open Library...** prompts before switching if rows from a
  different library exist; the wipe deletes the rows AND their
  FK-dependent `hashes` + `dest_inventory` rows + orphan `games`
  rows (chunked at 500 ids/batch to stay under SQLite's 999-parameter
  limit).
- **Tools → Clean Missing Entries...** drops every `missing=1` row
  with the same cascading cleanup. Status bar shows
  `N ROMs (M missing — Tools > Clean Missing Entries)` when stale
  rows exist.

**Logging & diagnostics:**
- `ROMULUS_LOG_LEVEL` env var now takes precedence over the
  Settings-stored `log_level`. The old behavior clobbered the env var
  back to INFO immediately after `setup_logging` applied it.
- DEBUG-level breadcrumbs added across 10 files: `dat_parser`,
  `identifier`, `hasher`, `local_cover_finder`, `exporter`,
  `organizer`, `libretro`, `hasheous`, `launchbox`, `screenscraper`.
  HTTP clients log URL / status only — never bodies, never auth.
- Settings dialog's log-level combo applies live via
  `app.set_log_level` instead of waiting for restart.
- Rotating log file at `<install_dir>/logs/romulus.log`, 5 MB × 3
  backups.

**Portable Windows build:**
- Single-binary `--onefile` PyInstaller build. `romulus.exe` contains
  Python, PySide6, every Qt plugin, themes, icons, and every transitive
  DLL. No `_internal/` subfolder, no loose `.pyd` files.
- Build script (`build-portable.ps1`) assembles a flat layout:
  ```
  romulus/
    romulus.exe          (single binary)
    dats/*.dat           (bundled No-Intro DATs)
    profiles/*.yaml      (destination profiles)
    systems/*.yaml       (system registry)
  ```
  ZIPed as `romulus-windows-x64.zip` and attached to the GitHub release.
- CD-ROM disc app icon generated via PySide6 `QPainter` (no Pillow
  dependency). PNG (256×256 RGBA) for `QApplication.setWindowIcon`,
  multi-resolution ICO for the Windows shell icon.

**Other features:**
- Anbernic RGLauncher destination profile (`profiles/anbernic-rglauncher.yaml`)
  with ES-DE-style gamelists under `Imgs/`.
- Real No-Intro DAT files bundled — 106 files, ~457k entries covering
  ~80 systems. Previous releases shipped synthetic placeholders only.
- System registry externalised to YAML (`systems/builtin.yaml`) with a
  three-tier load: user → install → package builtin.
- Recursive local-cover discovery walks subfolders (`media/`,
  `downloaded_images/`, etc.) and links 1:N covers per game, with
  prev/next cycling and per-cover-type "Make preferred".
- Right-click on a game in the table: Add to Favorites, Add to
  Collection, Heavy Scan (this game), Enrich (this game), Find Local
  Covers (this game).
- Status-bar surfacing of install dir + data dir + log path in
  Settings → Diagnostics tab.
- WBM Classic theme.

**Offline metadata layer (later-wave v0.3.0):**
- **GameDB JSON snapshots bundled** (`data/gamedb/<system_id>.json`,
  42 consoles, ~17 MB). Pulled by the one-shot
  `scripts/download_gamedb.py` from <https://github.com/niemasd/GameDB>.
  Provides offline CRC32 → (canonical release_name, region,
  publisher, release_date) mappings for cartridge-based systems
  where the upstream snapshots carry that data. Tried second in the
  enrichment chain (after libretro-database).
- **libretro-database bundled** (`data/libretro-metadat/<dim>/*.dat`,
  294 clrmamepro DAT files across 7 metadata dimensions, ~20 MB).
  Pulled by `scripts/download_libretro_metadat.py` from
  <https://github.com/libretro/libretro-database>. Per-CRC32 genre,
  developer, publisher, release year, max players, and ESRB rating
  across ~50 systems. Tried *first* in the enrichment chain — its
  per-field coverage is the richest of the local sources.
- **TheGamesDB online client** (`src/romulus/metadata/thegamesdb.py`).
  Name + platform matching against
  <https://api.thegamesdb.net/v1/Games/ByGameName> with the user's
  own API key (set via Settings → Metadata). Tries hard to map: strips
  parenthesised No-Intro tags before normalising titles, falls back
  to "candidate contains query" substring matching for series-prefix
  cases (`"007 - Everything or Nothing"` ↔ `"James Bond 007 - Everything
  or Nothing"`), resolves integer-id genre / developer / publisher
  lists to names via the `include=Genres,Developers,Publishers`
  parameter. Tracks the monthly request allowance and short-circuits
  when it hits zero. Slotted last in the chain — quota-bound, so we
  only spend it on games every cheaper source missed.
- **Enrich chain ordering** (in `_fetch_metadata_for_game`):
  libretro-database → GameDB → Hasheous → LaunchBox → ScreenScraper
  → TheGamesDB. Identifier-only local hits intentionally fall through
  so a game isn't locked out of richer follow-up data.
- **`metadata.release_year`** column added to the schema. Populated
  either directly from a year-only source or extracted from a full
  ISO date. Detail panel "Released" row prefers `release_date` when
  both are set, else falls back to `release_year`.

**Workflow split — Enrich Metadata + Find Covers (later-wave v0.3.0):**
- **Enrich** is now **Enrich Metadata**. `enrich_library` no longer
  touches the cover cache or libretro thumbnails — that work belongs
  to the separate Find Covers workflow.
- **Find Local Covers** is now **Find Covers**, with a per-run
  `CoverOptionsDialog` whose two checkboxes (`Search for local
  covers` default ON, `Search online for covers` default OFF) let
  the user pick either mode or both. Cover discovery is now driven
  by the `CoverFinderWorker` (alias `LocalCoverFinderWorker` kept
  for back-compat); the new `fetch_online_covers_for_scope` helper
  walks distinct game_ids in scope and issues one libretro lookup
  per missing cover type per game.
- **`EnrichOptionsDialog`** — pre-run prompt for every batch enrich
  entry point (global, system, collection, single-game). Three
  checkboxes:
  - `Also enrich fuzzy-matched games` (default off) — drops the
    `match_confidence='dat_verified'` filter so fuzzy / header
    matches reach the providers.
  - `Re-attempt enrichment on games that already have metadata`
    (default off) — drops the `m.game_id IS NULL` filter so the
    user can top up partial enrichments after configuring a new
    provider.
  - `Also try online metadata sources` (default on) — gates
    Hasheous / ScreenScraper / TheGamesDB. Offline-only runs use
    libretro-database + GameDB + LaunchBox XML; games with no
    offline match are reported as processed-but-not-enriched.
- Single-game right-click "Enrich this game" uses the same dialog
  scoped to one game id.
- Per-game right-click "Find covers for this game" uses
  `CoverOptionsDialog` scoped to one game.

**Detail panel redesign (later-wave v0.3.0):**
- Description is now a hide-when-empty `QLabel`, not a fixed-height
  scrollable text box — it used to reserve a third of the panel even
  on un-enriched libraries.
- Metadata fields render through a compact key/value `QFormLayout`
  grid: Region, Revision, ROM size, SHA-1, DAT name, Genre, Developer,
  Publisher, Released, Players, Rating. Empty rows hide so the grid
  stays tight.
- Per-platform console logo replaces the small text system indicator
  under the cover-nav row. 48 px tall, dark / light variants swapped
  on theme change. Display-name text fallback for the ~10 systems
  with no bundled logo file.

**Bundled per-platform console logos (later-wave v0.3.0):**
- 140 PNG files under `src/romulus/ui/artwork/systems/<id>-{dark,light}.png`,
  extracted from the v2.1 Recommended Versions (Normal) set of Dan
  Patrick's *Console Logos — Professionally Redrawn + Official
  Versions* via `scripts/extract_system_logos.py`. Credited in
  `docs/CREDITS.md`.
- `SystemDef.logo_dark` / `logo_light` fields in `systems/builtin.yaml`
  + the in-code fallback registry point each system at its bundled
  paths.
- `romulus.ui.artwork.resolve_system_logo` returns the absolute path
  per (system_id, theme).
- System sidebar shows a 22 px logo next to each row, composited onto
  a fixed-width 120 × 22 canvas so the text column aligns regardless
  of source aspect ratio (narrow logos like MSX get transparent
  padding; ultra-wide logos like Super Cassette Vision shrink on the
  long axis to fit).
- Detail panel shows a 48 px logo where the text system indicator used
  to live.
- `PyInstaller` `romulus.spec` bundles the artwork directory.

**UX polish (later-wave v0.3.0):**
- **Selection preserved across `refresh_all`.** Every worker-finished
  signal funnels through `refresh_all`; model resets in
  `sidebar.populate` and `game_table.set_rows` used to clear the
  current row in both widgets, so selecting a game, clicking Enrich,
  and waiting for the run to finish always landed users back at "All"
  with the detail panel blank. `refresh_all` now captures
  (`_selected_system`, `_selected_collection`,
  `detail_panel.current_game_id`) before the refresh and restores
  them after via new `SystemSidebar.select_system` /
  `select_collection` and `GameTable.select_game` helpers.
- **Heavy Scan "cache up to date" messaging.** When the hash cache
  is fully warm (no pending hashes, no unverified ROMs), the
  progress dialog now reads `Heavy Scan complete — cache up to date.
  No ROMs needed re-hashing and every existing hash is already
  DAT-matched.` plus a note explaining that Quick Scan must run first
  to detect file changes. INFO-level log lines around DAT-load and
  hash result give visibility even at default log level.
- **Log-file lock detection.** Starting a second copy of ROMulus
  while the first is still open used to dump a `PermissionError`
  traceback on every log rotation attempt and continue running with
  silently-dropped log messages. `setup_logging` now probes for the
  lock via `rename(p, p)` and raises `LogFileLockedError`; the
  entry point prints a friendly stderr message and exits with code 1.

### Renamed

- Project: `Romulus` → `ROMulus` across 34 user-facing files (window
  titles, profile YAML descriptions, settings dialog header, theme
  stylesheets, README, CHANGELOG, build scripts, docs). Python package
  import path `romulus` (lowercase) preserved.

### Fixed

- FK constraint failures in Clean Missing Entries — dependent
  `hashes` and `dest_inventory` rows are now deleted before the rom
  rows themselves.
- FK constraint failures during sync apply — every dest_inventory
  write now uses `plan.dest_id` (authoritative) instead of re-deriving
  via `str(target_path)` lookup, which broke on UNC trailing-slash and
  separator-normalization mismatches.
- Tier-2 cross-platform false positives — match key now includes
  `system_id` so Game Boy / Game Boy Color titles with identical
  fuzzy_key+region don't collide.
- `upsert_rom` switched to `RETURNING id`. Previously `cursor.lastrowid`
  for an UPSERT-UPDATE returned the connection's most-recent INSERTED
  id (not the row that was actually upserted), making the scanner's
  visited-rom-id set incoherent on multi-file rescans.
- Export reported negative bytes for transfers ≥ 2 GB — Qt's
  `Signal(int)` marshals through C int (32-bit signed) and wrapped.
  Switched to `Signal("qint64", ...)`.
- Export artwork wasn't copied on per-game basis; gamelist `<image>`
  element pointed at non-existent paths. Added
  `artwork_filename_template` to profiles (`{stem}{ext}` default,
  `{stem}-image{ext}` for EmulationStation classic) and rewrote the
  per-game artwork copier.
- Scanner now accepts `.zip` / `.7z` archive containers for every
  system regardless of the registry's native extension list — most
  retro libraries store cartridge ROMs zipped.
- Various UI fixes: Name column resizable + self-filling, sidebar
  startup width, Path column truncation, default A→Z sort, sync
  preview "Apply" → "Close" button after completion, collapsed
  destination row in sync preview.

**Later-wave v0.3.0 fixes:**
- **`mark_missing_under_root` ran out of SQL variables** on libraries
  larger than 999 ROMs. The naive `NOT IN (?, ?, ?, ...)` template
  bound one parameter per visited ROM and tripped SQLite's stock
  Windows variable limit. Replaced with a temp-table strategy
  (`CREATE TEMP TABLE _visited_rom_ids` + `executemany` insert +
  subquery diff). Scales arbitrarily.
- **Scanner self-heal for unlinked ROMs.** A scan that crashed at the
  pre-fix `mark_missing_under_root` step left ROMs inserted but with
  `game_id IS NULL` forever (game-grouping runs AFTER the missing
  sweep). `scan_library` now finalises with an idempotent self-heal
  pass that runs `group_into_games` for any system with at least one
  rom matching `game_id IS NULL AND fuzzy_key IS NOT NULL`. Logged
  at INFO when it kicks in.
- **TheGamesDB title-match was too strict** — exact normalised
  comparison missed series-prefix differences (`"007 - Everything or
  Nothing"` vs TGDB's `"James Bond 007 - Everything or Nothing"`).
  `_normalise_title` now strips parenthesised tags first, and a
  substring fallback fires when normalised query length ≥ 12.
- **TheGamesDB list-id fields were stored as raw integers.** Genre /
  developer / publisher came back as `"8, 12"` instead of
  `"Platformer, Adventure"`. The request now includes
  `include=Genres,Developers,Publishers` and the parser resolves IDs
  via the include block's lookup table.
- **Right-click on rows with unlinked game_id silently killed the
  menu.** The handler now shows a disabled placeholder
  `This ROM isn't linked to a game yet — re-run Quick Scan` so the
  click is visibly acknowledged.
- **Right-click on rows that weren't previously left-clicked
  silently killed the menu.** The handler now resolves the row
  under the cursor via `indexAt(point)` rather than the previous
  selection, and promotes that row to current so subsequent actions
  bind to it.

### Removed

- Pre-v0.3.0 schema migration helper. ROMulus is pre-1.0 with no
  shipped user base; users running an earlier alpha-state database
  should wipe `data/romulus.db` and let v0.3.0 rebuild it on next
  launch.

### Breaking changes

- **Database schema:** `roms.library_root` (TEXT) and `roms.missing`
  (INTEGER NOT NULL DEFAULT 0) columns added; `sync_destinations`,
  `dest_inventory`, `sync_plans` tables added. Later-wave: also
  `metadata.release_year` (INTEGER) and per-system YAML carrying
  `logo_dark` / `logo_light` / `gamedb_file` fields. Pre-v0.3.0
  databases are NOT migrated — wipe and rescan.
- **Installation layout:** v0.2.0 portable build had an `_internal/`
  subfolder next to the exe. v0.3.0 collapses that into a single
  binary, and ships two new sibling folders alongside `dats/` /
  `profiles/` / `systems/`:
  - `gamedb/*.json` — bundled GameDB snapshots
  - `libretro-metadat/<dim>/*.dat` — bundled libretro-database metadata
  Re-extract the ZIP for the new layout.
- **Project name in window titles / profile YAML descriptions / etc.**
  changed `Romulus` → `ROMulus`. Has no functional effect; called out
  for completeness.

### Test suite

**908 tests passing, 1 skipped** (POSIX-only chmod test). Ruff clean.
Coverage expanded to include:
- Sync engine: all five modes, identity matching tiers 1–4,
  region-distinct match, conflict policies, atomic delete via
  tombstone, plan persistence + reload, gamelist rebuild,
  pull-mode enrolment, unknown-system `_unsorted/` fallback,
  signature-drift recognition, cross-platform tier-2 guard,
  path-mismatch dest_id threading.
- Library cleanup: scanner sweep, reconnect un-tombstone,
  library-root change detection + wipe, FK-cascade delete,
  orphan-game prune, upsert resets missing, `mark_missing_under_root`
  scaling past the 999 SQLite variable limit, scanner self-heal
  for unlinked roms.
- Metadata clients: libretro-metadat clrmamepro parser + per-system
  dimension merge + chain placement, GameDB CRC32 + fuzzy-title
  lookup + identifier-only fall-through, TheGamesDB title
  normalisation + substring fallback + include-block ID resolution
  + monthly-allowance gating, online-vs-offline flag gating on the
  remote providers, `fetch_online_covers_for_scope` walking a scope
  → game_id list.
- UI: log-file lock detection, selection preservation across
  `refresh_all`, sidebar fixed-canvas logo composition, Heavy Scan
  cache-up-to-date messaging, CoverOptionsDialog default state +
  OK-disabled-when-both-unchecked, EnrichOptionsDialog three-flag
  plumbing.
- Logging precedence (env var vs Settings vs default).
- Packaging: install-dir resolution, three-tier profile loading,
  system YAML round-trip, ensure_user_editable_files.

---

## [0.2.0] — 2026-05-15

Packaging-focused release. The v0.1.0 source-only distribution was
turned into a portable Windows ZIP and the system registry was
externalized to YAML so users can drop in extras without rebuilding.
Heavy Scan toolbar trigger landed and the Anbernic RGLauncher profile
joined the built-in set.

### Added

- Portable Windows ZIP distribution via PyInstaller. v0.2.0 used
  `--onedir` mode with everything under `_internal/`; v0.3.0
  flattened that to a single-binary build.
- `<install_dir>/data/` for the SQLite DB + cover cache (with
  `~/.romulus/` fallback). `ROMULUS_DATA_DIR` env var pins the
  location regardless of where the exe lives.
- First-launch seeding of `profiles/`, `systems/`, and `dats/` from
  the bundled payload into the user-editable folders alongside the
  exe.
- System registry externalized to `systems/builtin.yaml` with a
  three-tier load: user > install > package builtin.
- Heavy Scan toolbar button + `HeavyScanWorker` + duration-warning
  dialog. The hashing engine had been complete since v0.1.0; v0.2.0
  wired it into the UI.
- Anbernic RGLauncher destination profile.
- Bundled real No-Intro DAT files (106 files, ~457k entries).
- WBM Classic theme; expanded filters; Path column in the game table;
  region filter "None" option.
- Local cover discovery: recursive walk + fuzzy matching, 1:N covers
  per game with prev/next cycling and per-cover-type preferred
  selection.
- Right-click context menu on game rows: Heavy Scan, Enrich, Find
  Local Covers — all scoped to the selected game.
- Settings → Diagnostics tab surfacing install dir + data dir + log
  path for copy-into-bug-report.

### Fixed

- Export reported negative bytes for transfers ≥ 2 GB (Qt `Signal(int)`
  wrapping at 2^31). Switched to `qint64`.
- Per-profile `artwork_filename_template` so artwork copies land at
  the filename the target's gamelist expects. EmulationStation
  classic gets `{stem}-image{ext}`, modern launchers get
  `{stem}{ext}`.
- Gamelist `<image>` element + artwork sidecar wiring on the
  Anbernic profile.
- Sync FK errors with one-shot destinations (`dest_id=-1`) — the
  sentinel is now caught before reaching `upsert_dest_inventory`.
- Sync scan-destination lockup — added `DestScanProgressDialog`
  matching the pattern of other progress dialogs.
- Cover preferred-selection bug where the "Make preferred" button
  visibly did nothing (was already preferred for the cover_type;
  filtered cycle to `Named_Boxarts` to make the action meaningful).
- Various UI polish: Name column resize behaviour, sidebar startup
  width, default A→Z sort.

---

## [0.1.0] — 2026-05-14

First public release. The full scan → identify → enrich → organize → export
pipeline ships end-to-end. v0.1.0 is the result of 11 build sessions
(numbered 00 through 11; see `docs/sessions/`).

### Added

**Foundations (Sessions 0–1):**
- Python 3.12+ project skeleton under `src/romulus/`, packaged via
  `pyproject.toml` with `dev` extras (`pytest`, `ruff`).
- SQLite database under `~/.romulus/romulus.db`, WAL mode, foreign keys on,
  POSIX `0o600` file permissions to protect credentials.
- Schema: `systems`, `roms`, `games`, `dat_entries`, `metadata`, `covers`,
  `collections`, `collection_games`, `scans`, `hash_cache`, `organize_plans`,
  `config`. All schema changes migrated forward.
- 33-system registry (`models/system.py`) covering Nintendo, Sega, Sony,
  Atari, NEC, SNK, and Bandai consoles + handhelds, each with folder
  aliases, file-extension hints, libretro / Hasheous / LaunchBox / ScreenScraper
  identifiers, and No-Intro DAT names.
- Pydantic v2 models for systems, ROMs, games, destination profiles.

**Scanner (Session 2):**
- Filesystem walker with per-folder system inference via folder-alias
  matching, then per-file extension matching.
- Filename parser extracting region, language, revision, version, disc,
  release-group, demo / proto / sample / beta flags, and ROM-hack markers
  (`[h]`, `[h1]`, `[T+Eng]`, etc.).
- `scan_library(conn, root, progress_callback)` API returning a
  `ScanResult` with file counts and the set of systems seen.

**Identifier pipeline (Session 3):**
- Layer 1: fuzzy filename matching (`identifier.identify_by_filename`).
- Layer 2: internal-header extraction for SNES, N64, NES, PCE, Atari 2600,
  Master System (`hasher.read_internal_header`).
- Layer 3: SHA-1 / CRC32 + No-Intro DAT lookup
  (`hasher.compute_hashes`, `dat_parser.load_dat_directory`,
  `identifier.identify_by_dat`).
- Hash cache keyed by `(path, mtime, size)` so unchanged files are never
  rehashed.
- DAT parser supports Logiqx XML; user-supplied DAT folders are merged
  with the bundled `data/dats/` placeholders.

**UI shell (Session 5):**
- PySide6 main window: menu bar, toolbar, three-panel layout (system
  sidebar | sortable game table | game detail panel).
- `ScanProgressDialog` + `ScanWorker` (QThread) running scans off the UI
  thread with a thread-local SQLite connection and cooperative cancel.
- Settings dialog with **General / DATs / Metadata / Scan** tabs writing
  back to the `config` table.

**Metadata enrichment (Session 6):**
- libretro-thumbnails cover-art client (`metadata/libretro.py`). Free,
  no API key. Atomic-write download to `~/.romulus/covers/`.
- Hasheous metadata client (`metadata/hasheous.py`). Free, no account,
  game metadata by SHA-1 / CRC32.
- LaunchBox offline metadata parser (`metadata/launchbox.py`). Free,
  no account, ships as XML.
- ScreenScraper client (`metadata/screenscraper.py`). Optional, requires
  a free account; rate-limited; short-circuits when credentials are absent.
- `enrich_library` orchestrator running libretro → Hasheous → LaunchBox
  → ScreenScraper for every matched game.
- `EnrichWorker` + `EnrichProgressDialog` wired into the **Enrich**
  toolbar button.

**Detail panel, collections, search (Session 7):**
- Game detail panel: cover image, title, canonical name, system,
  description, metadata grid, region/revision/disc/version/hack tags,
  Favorites toggle.
- User-defined collections: create, rename, delete, add/remove via
  right-click on the game table or via the detail panel.
- Built-in `Favorites` collection seeded on first launch.
- Search and filter on the game table.

**Library organizer (Session 9):**
- `core/organizer.py` — detects four classes of cleanup: alias-folder
  merges, canonical-name renames (Layer-3 verified), exact-hash
  duplicates, cross-extension duplicates (e.g. `.smc` + `.sfc`).
- Collision detection — refuses to overwrite an unrelated file.
- `OrganizePreviewDialog` — grouped QTreeView of proposed actions, per-
  action checkboxes, Select / Deselect All, progress bar.
- `OrganizeWorker` mirrors the ScanWorker/EnrichWorker contract:
  thread-local connection, cooperative cancel, progress signals.
- `execute_plan` uses per-action SQLite SAVEPOINTs so a mid-plan
  failure leaves the DB in a known state, plus atomic file moves via
  `tempfile.mkstemp` + `os.replace`.
- Hacks (`[h]`, `[T+]`, etc.) are never silently merged with their
  base titles.

**Export engine (Session 10):**
- `core/exporter.py` — `load_profile`, `preview_export`,
  `export_collection`, `generate_gamelist_xml`, `generate_m3u_playlists`,
  `copy_artwork`.
- 6 built-in YAML destination profiles in `data/profiles/`: Batocera,
  RetroPie, Onion OS, muOS, MiSTer FPGA, Analogue Pocket. Every profile
  covers all 33 registry systems explicitly (a system either gets a
  folder mapping or is marked `supported: false`).
- User profiles in `~/.romulus/profiles/` override built-ins by ID.
- ExportDialog with profile selector, target picker, system / region /
  collection filters, artwork + gamelist toggles, preview pane, progress
  bar, completion summary.
- `ExportWorker` — same contract as the other workers.
- `gamelist.xml` for EmulationStation-based targets; `.m3u` for multi-
  disc games; optional artwork sidecar copy.
- Atomic-write helpers extracted into a shared module
  (`core/atomic.py`): `atomic_replace`, `atomic_copy`,
  `atomic_write_bytes`, `atomic_write_text`. All filesystem-mutating
  code in the app routes through this module.

**Polish & CI (Session 11 — this release):**
- `.github/workflows/ci.yml` — runs `ruff check src/ tests/` and
  `pytest` on every push and pull request. Pinned to Python 3.12,
  PySide6 system libraries installed via `apt-get`, `QT_QPA_PLATFORM=offscreen`
  set so headless Qt widget tests pass.
- ScreenScraper **Test connection** button in the Settings dialog
  (`ui/settings_dialog.py`). Validates the current form values
  against `ssuserInfos.php` before saving — clear success / failure
  messaging for network errors, HTTP 401/403, non-JSON bodies, and
  unexpected status codes.
- `screenscraper.test_connection(username, password)` helper plus 6
  new unit tests covering its happy path and every failure mode.
- README, CHANGELOG, and CI documentation.

### Test suite

- **415 tests passing, 1 skipped** (POSIX-only chmod test runs on the
  Linux CI runner; skipped on Windows because NTFS ACLs are inherited,
  not set via `chmod`).
- Coverage spans scanner, identifier pipeline, hasher, DAT parser,
  metadata clients (with `httpx.MockTransport` — no real network in
  tests), organizer (detection, execution, rollback, atomic moves),
  exporter (profile loading, preview math, folder-structure copies,
  gamelist.xml validation, .m3u grouping, artwork copy), UI widgets
  (constructed off-screen with `QT_QPA_PLATFORM=offscreen`), and the
  full DB schema / queries surface.

### Known limitations (deferred to v0.2.0)

1. **Bundled DATs are placeholders.** `data/dats/` contains two
   synthetic Logiqx XML files (Game Boy + SNES, one game each). Real
   No-Intro DATs are not redistributable; users supply their own. See
   the README's *DAT files* section. Heavy Scan match rates are low
   until you install real DATs.
2. **Heavy Scan toolbar button is disabled.** The hashing and DAT-
   matching engine is fully implemented and tested; only the toolbar
   trigger + duration-warning dialog wiring is deferred.
3. **ScreenScraper credentials stored in plaintext.** Mitigated by
   `0o600` permissions on the SQLite file (POSIX) and inherited
   NTFS ACLs on Windows. Moving credentials into the system keyring
   (`keyring` package) is deferred to v0.2.0 to keep packaging
   simple for v0.1.0.
4. **Organize plan history isn't surfaced in the UI.** Plans are
   persisted to `organize_plans` as JSON; the "View history / undo
   last plan" dialog is v0.2.0.
5. **Folder-name guesses in built-in profiles.** MiSTer Atari 2600
   and 7800 share a core folder; Analogue Pocket assumes `agg23`
   core layout for some systems; Onion OS casing follows the docs at
   time of writing; RetroPie uses `megadrive` rather than `genesis`.
   See README's *Folder-name accuracy* section for the full list.
   User profiles in `~/.romulus/profiles/` override built-ins by ID.
6. **No Heavy Scan ETA.** Per-file progress is wired up; the
   headline ETA is not, because hashing throughput swings too widely
   (local SSD vs SMB).

### Breaking changes

None. v0.1.0 is the first release.

[Unreleased]: https://github.com/Sphexi/ROMulous/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Sphexi/ROMulous/releases/tag/v0.2.0
[0.1.0]: https://github.com/Sphexi/ROMulous/releases/tag/v0.1.0
