# Changelog

All notable changes to ROMulus will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] — v0.3.0 in development

The v0.3.0 cycle reshapes the project for actual real-world use. Major
themes: a destination sync engine, single-library cleanup semantics, a
single-binary portable Windows build, and a debug-logging overhaul.

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

### Removed

- Pre-v0.3.0 schema migration helper. ROMulus is pre-1.0 with no
  shipped user base; users running an earlier alpha-state database
  should wipe `data/romulus.db` and let v0.3.0 rebuild it on next
  launch.

### Breaking changes

- **Database schema:** `roms.library_root` (TEXT) and `roms.missing`
  (INTEGER NOT NULL DEFAULT 0) columns added; `sync_destinations`,
  `dest_inventory`, `sync_plans` tables added. Pre-v0.3.0 databases
  are NOT migrated — wipe and rescan.
- **Installation layout:** v0.2.0 portable build had an `_internal/`
  subfolder next to the exe. v0.3.0 collapses that into a single
  binary — re-extract the ZIP for the new layout.
- **Project name in window titles / profile YAML descriptions / etc.**
  changed `Romulus` → `ROMulus`. Has no functional effect; called out
  for completeness.

### Test suite

**838 tests passing, 1 skipped** (POSIX-only chmod test). Ruff clean.
Coverage expanded to include:
- Sync engine: all five modes, identity matching tiers 1–4,
  region-distinct match, conflict policies, atomic delete via
  tombstone, plan persistence + reload, gamelist rebuild,
  pull-mode enrolment, unknown-system `_unsorted/` fallback,
  signature-drift recognition, cross-platform tier-2 guard,
  path-mismatch dest_id threading.
- Library cleanup: scanner sweep, reconnect un-tombstone,
  library-root change detection + wipe, FK-cascade delete,
  orphan-game prune, upsert resets missing.
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
