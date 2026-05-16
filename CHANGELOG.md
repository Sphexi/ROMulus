# Changelog

All notable changes to ROMulus will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/Sphexi/ROMulous/releases/tag/v0.1.0
