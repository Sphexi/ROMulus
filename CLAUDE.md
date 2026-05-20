# CLAUDE.md — ROMulus

## What This Project Is

ROMulus is a local-first desktop ROM collection manager for retro game consoles. It scans, identifies, enriches with metadata/cover art, organizes, **syncs** to device-specific folder structures (Anbernic, Batocera, MiSTer, RetroPie, muOS, Onion OS, Analogue Pocket), and exports collections. Built with Python + PySide6 (Qt), SQLite, and no server infrastructure. Shipped as a single-binary portable Windows ZIP.

## Project Tier

**Standard** — unit tests + ruff, code reviews every 2–3 build sessions.

## Current State (as of v0.3.0 in development)

- **1,003 tests passing, 1 skipped** (POSIX-only chmod test on Windows
  CI; 1,004 collected total). Ruff clean. CI runs on `windows-latest`.
- Full pipeline works end-to-end: Quick Scan → Heavy Scan → Enrich
  Metadata → Find Covers → Organize → Export / Sync, plus inbound
  Import ROMs from a staging folder and a reverse-direction Verify
  Library scrub.
- Enrichment is metadata-only; cover discovery is a separate "Find
  Covers" workflow with a per-run dialog (local files / online
  thumbnails / both).
- Six-source enrichment chain, **local first**: libretro-database
  (bundled clrmamepro DATs) → GameDB (bundled JSON) → Hasheous (remote)
  → LaunchBox XML (local, user-supplied) → ScreenScraper (remote, opt-in)
  → TheGamesDB (remote, monthly quota). User toggles online vs offline
  per batch.
- Single library at a time — switching library_path wipes prior rows;
  tombstone-missing rather than delete-missing for un-tombstone-on-reconnect.
- Quick Scan can be scoped per-system via sidebar right-click;
  post-walk DB phases surface progress and disable Cancel so a
  mid-rebuild cancel can't leave the DB inconsistent with disk.
- Game-table right-click adds **Reveal in Explorer** and **Delete this
  ROM (permanent)…** actions, bound to rom_id (not game_id).
- **Import ROMs** (Tools menu + toolbar) walks a staging folder,
  identifies every file via the same L1+L2+L3 pipeline the scanner
  uses, surfaces path / filename / hash dupes with per-row resolution
  dropdowns, and atomically copies (or moves) the approved files into
  the current library. Heavy identification is mandatory; the dialog
  warns about duration when the staging folder is large. Full
  reference: `docs/import-design.md`.
- **Tools → Verify Library** walks the DB and classifies every row
  against disk into four buckets (missing-on-disk, outside-current-
  library, flagged-but-present, size/mtime drift). Per-bucket
  SAVEPOINT apply; unreadable-row guard skips SMB hiccups.
- **Post-Export / post-Sync per-system summary dialog**. One row per
  system × bucket columns (copied / bytes / covers refreshed /
  unsupported / refused / errors). Auto-popups on top of the
  progress dialog when the operation completes.
- **Artwork-only export mode.** Uncheck **Include ROMs** in Export
  Options to skip the ROM copy loop entirely and only refresh
  artwork + gamelist.xml. `copy_artwork` size+mtime-compares per
  file so a re-run only republishes covers that actually changed.
- **Sync diff is O(N+M)** via pre-built `dest_by_fuzzy` index;
  `build_plan` runs on `BuildSyncPlanWorker` with a "Computing diff…"
  progress dialog. Closed a multi-minute UI freeze on 38K × 17K
  libraries. See `docs/sync-design.md` §12.6.
- **Tri-state group headers + right-click bulk toggle** on Organize,
  Sync, and Verify Library preview dialogs via shared
  `GroupedCheckboxTreeMixin`.
- **Clean Missing Entries** runs on `CleanMissingWorker` with
  try/except/rollback. `prune_orphan_games` now clears FK-dependent
  metadata / covers / collection_games rows and NULLs
  `dest_inventory.game_id` before deleting from `games`.
- 11 build sessions complete (v0.1.0); v0.2.0 added portable packaging
  + Heavy Scan UI + real DATs; early v0.3.0 added destination sync,
  library cleanup, single-binary build, DEBUG breadcrumbs; later v0.3.0
  added bundled offline metadata sources (GameDB + libretro-database),
  TheGamesDB, the metadata/covers workflow split, the redesigned
  detail panel with per-platform logos, the enrich-options dialog, and
  UX polish. Final-wave v0.3.0 shipped scoped Quick Scan + post-walk
  progress + per-game Reveal/Delete + the CI Windows switch + Import
  ROMs + Verify Library + per-system summary dialogs + the sync diff
  perf rewrite + artwork-only export mode.
- Pre-v0.3.0 schema migrations were removed — wipe `data/romulus.db`
  and rescan if you have a pre-v0.3.0 DB lying around.

See `CHANGELOG.md` for the full per-release breakdown.

## Session Start

At the start of every session:
1. Read this file for project rules, architecture, and current state.
2. Check what work is in progress. If the user has a specific task, follow it. If `docs/sessions/NN-slug.md` is being used for a new piece of work, read it.
3. Run `git log --oneline -20` to see recent commits. The 11 numbered sessions (00–11) are complete; subsequent work is committed directly via `feat:` / `fix:` / `refactor:` commits without a session file (the project is past the bootstrap phase).
4. Produce an execution plan before writing any code on non-trivial work.

## Follow the Plan

Claude Code MUST follow the tasks in the current session file (when one applies). Do not add features, refactors, or improvements not specified. Do not ask questions already answered in CLAUDE.md or the session file's Context section. If something seems missing, flag it — do not silently add unplanned work.

## Reference Documents

| Document | Purpose | When to Read |
|---|---|---|
| `docs/architecture.md` | Architecture diagram, design rules, schema overview, config reference, packaging, known limitations | When orienting on the system as a whole or making cross-cutting changes |
| `docs/sessions/NN-slug.md` | Per-session task list, context, acceptance criteria (sessions 00–11 are done) | When the user resumes a numbered session |
| `docs/TECHNICAL_PLAN.md` | Full API details, schema column-by-column, implementation pseudocode | On-demand for edge cases not covered in architecture.md |
| `docs/sync-design.md` | Destination sync engine spec (modes, identity matcher, dest_inventory, sync_plans) | When touching `core/sync.py` or `core/dest_inventory.py` |
| `docs/import-design.md` | Import ROMs feature reference (shipped) — status taxonomy, conflict resolution, plan JSON, safety properties | When touching `core/importer.py` or `ui/import_dialog.py` |
| `docs/ROM-FORMATS-REFERENCE.md` | Extension tables, naming conventions, folder aliases | When implementing scanner or system registry |
| `docs/ROM-DEDUP-METHODOLOGY.md` | Three-layer identification pipeline methodology | When implementing identifier pipeline |
| `docs/ROM-LIBRARY-ANALYSIS-REPORT.md` | Real-world library stats, test validation data | When writing tests or validating assumptions |
| `docs/forking-with-claude-code.md` | How to fork ROMulus and continue building it with Claude Code | When mentoring a fork-and-extend workflow |
| `docs/KNOWN-ISSUES.md` | Open bugs triaged for later — newest first, deleted when fixed | Check before proposing new work; flag anything new here that's deferred |
| `CHANGELOG.md` | Per-release feature + fix history with breaking-change callouts | When orienting on what shipped when |

Do not load reference documents into context every turn — read them when needed.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     PySide6 UI                           │
│  ┌─────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ System  │  │  Game Table  │  │   Game Detail Panel │ │
│  │ Sidebar │  │  (sortable,  │  │   (cover, logo,     │ │
│  │ (logos) │  │   filterable)│  │    grid, desc)      │ │
│  └─────────┘  └──────────────┘  └─────────────────────┘ │
│  ┌──────────────────────────────────────────────────────┐│
│  │ Toolbar: Quick Scan | Heavy Scan | Organize |        ││
│  │   Enrich Metadata | Find Covers | Export/Sync |      ││
│  │   Import ROMs | Settings                             ││
│  │ Tools menu: Import ROMs… | Verify Library… |         ││
│  │             Clean Missing Entries…                   ││
│  └──────────────────────────────────────────────────────┘│
└──────────────┬───────────────────────────────────────────┘
               │ signals/slots + QThread workers
┌──────────────┴───────────────────────────────────────────┐
│                    Core Engine                           │
│                                                          │
│  Scanner (+missing sweep, self-heal) ──→ Identifier ──→  │
│  SQLite DB   (L1 fuzzy, L2 header, L3 hash+DAT)          │
│                                                          │
│  DAT Parser (bundled No-Intro + user)                    │
│                                                          │
│  Metadata chain (Enrich Metadata):                       │
│    libretro-database ──→ GameDB ──→ Hasheous ──→         │
│    LaunchBox ──→ ScreenScraper ──→ TheGamesDB            │
│    (local-first, online toggleable per batch)            │
│                                                          │
│  Cover chain (Find Covers):                              │
│    local image walk (offline) + libretro thumbnails      │
│    (online), independently toggled per batch             │
│                                                          │
│  Organizer (preview/commit, atomic move, SAVEPOINT)      │
│  Export Engine (dest profiles, copy, gamelist.xml, .m3u) │
│                                                          │
│  Sync Engine  ── 5 modes: push merge/mirror/wipe,        │
│   (core/sync.py)  pull merge, two-way                    │
│                ── 4-tier identity match: path, fuzzy+    │
│                   region+system_id, hash-by-name, sha1   │
│                ── dest_inventory cache (per destination) │
│                ── sync_plans persisted JSON per apply    │
│                                                          │
│  Import Engine (core/importer.py): staging → identify    │
│                → plan → per-row resolution → atomic      │
│                copy/move/replace/keep-both into library  │
│                                                          │
│  Cover Cache    (<install_dir>/data/covers/)             │
│  GameDB JSON    (<install_dir>/data/gamedb/)             │
│  libretro DATs  (<install_dir>/data/libretro-metadat/)   │
│  SQLite DB      (<install_dir>/data/romulus.db)          │
└──────────────────────────────────────────────────────────┘
```

**Key architecture notes:**
- Single-process desktop app, no server, no Docker.
- Distributed as a single-binary portable Windows ZIP (PyInstaller
  `--onefile`); data folders (`dats/`, `gamedb/`, `libretro-metadat/`,
  `profiles/`, `systems/`) ship alongside the exe in the ZIP.
- SQLite for all persistent state (library, config, metadata, scan history, dest inventory, sync plans).
- QThread workers for scanner / heavy-scan / enricher / organizer / exporter / sync / dest-inventory-scan / local-cover-finder with cooperative cancel via private exception raised inside the progress callback.
- Quick scan (L1+L2, seconds-to-minutes) vs Heavy scan (L3, minutes-to-hours).
- Config stored in SQLite, not files — user edits everything via Settings dialog.
- **Single library at a time.** Switching `library_path` prompts to wipe prior rows; the scan sweep flags any row not visited this scan as `missing=1` regardless of its `library_root`.
- Pre-v0.3.0 schema migrations were removed; users with a pre-v0.3.0 database wipe `data/romulus.db` and rescan.

## Tech Stack

| Concern | Choice |
|---|---|
| **Language** | Python 3.12+ |
| **GUI** | PySide6 (Qt 6) |
| **HTTP client** | httpx (async for metadata fetching) |
| **Database** | SQLite via sqlite3 stdlib (no ORM) |
| **Config/models** | Pydantic v2 for data models, validation |
| **Logging** | structlog, JSON to stdout |
| **Linting** | ruff |
| **Testing** | pytest |
| **Packaging** | pyproject.toml, .venv |

## Project Structure

```
ROMulous/
├── CLAUDE.md
├── CHANGELOG.md
├── README.md
├── pyproject.toml
├── romulus.spec                  # PyInstaller spec (--onefile)
├── build-portable.ps1            # Windows portable-ZIP builder
├── scripts/
│   ├── generate_icon.py             # CD-ROM disc icon generator (QPainter)
│   ├── extract_system_logos.py      # One-shot logo extractor (Dan Patrick zip)
│   ├── download_gamedb.py           # One-shot GameDB JSON downloader
│   └── download_libretro_metadat.py # One-shot libretro DAT downloader
├── .github/workflows/
│   ├── ci.yml                    # Lint + test on push/PR
│   └── release.yml               # Tag-driven portable ZIP build
├── profiles/                     # 7 built-in destination profiles (YAML)
├── systems/                      # System registry YAML (builtin.yaml)
├── data/
│   ├── dats/                     # 106 bundled No-Intro DAT files
│   ├── gamedb/                   # 42 bundled GameDB JSON snapshots (~17 MB)
│   └── libretro-metadat/         # 294 bundled libretro DAT files (~20 MB),
│                                 # nested by dimension (genre / developer /
│                                 # publisher / releaseyear / maxusers / esrb /
│                                 # franchise)
├── docs/
│   ├── TECHNICAL_PLAN.md
│   ├── sync-design.md            # Destination sync engine spec
│   ├── CREDITS.md                # Upstream services, libraries, devices
│   ├── ROM-FORMATS-REFERENCE.md
│   ├── ROM-DEDUP-METHODOLOGY.md
│   ├── ROM-LIBRARY-ANALYSIS-REPORT.md
│   └── sessions/                 # Sessions 00-11 (done)
├── src/romulus/
│   ├── __init__.py
│   ├── __main__.py               # Entry point
│   ├── app.py                    # QApplication setup, log + DB init,
│   │                             # data-dir resolution, first-launch seeding,
│   │                             # log-file lock detection
│   ├── db/
│   │   ├── connection.py         # SQLite connection manager
│   │   ├── schema.py             # Table definitions, migration helpers
│   │   ├── queries.py            # All SQL queries
│   │   └── config.py             # Default config + accessors
│   ├── core/
│   │   ├── scanner.py            # Filesystem walk + L1/L2 + missing sweep
│   │   │                         # + self-heal for unlinked roms
│   │   ├── identifier.py         # L2 header extraction
│   │   ├── hasher.py             # SHA-1/CRC32 + header stripping + archives
│   │   ├── dat_parser.py         # Logiqx XML DAT parser + match_hashes
│   │   ├── organizer.py          # Library reorganization (preview/commit)
│   │   ├── exporter.py           # Destination profile export engine
│   │   │                         # (incl. include_roms artwork-only mode,
│   │   │                         # phase-2 sidecar progress, PerSystemExportCounts)
│   │   ├── sync.py               # 5-mode sync + 4-tier identity match
│   │   │                         # (O(N+M) tier-2 via dest_by_fuzzy index,
│   │   │                         # PerSystemSyncCounts)
│   │   ├── dest_inventory.py     # Destination filesystem scanner + cache
│   │   ├── importer.py           # Staging-folder import (analyse + apply)
│   │   ├── scrub.py              # Reverse-direction DB ↔ disk verifier
│   │   │                         # (four buckets, per-bucket SAVEPOINT)
│   │   ├── local_cover_finder.py # Disk-side cover discovery + linking
│   │   ├── atomic.py             # tempfile.mkstemp + os.replace helpers
│   │   └── _no_intro_tokens.py   # FILENAME_REGION_TOKENS, REVISION_RE
│   ├── metadata/
│   │   ├── __init__.py           # enrich_library + chain orchestrator +
│   │   │                         # fetch_online_covers_for_scope
│   │   ├── libretro_metadat.py   # Bundled libretro-database (offline,
│   │   │                         # tried first — broadest per-field coverage)
│   │   ├── gamedb.py             # Bundled GameDB JSON (offline, tried second)
│   │   ├── libretro.py           # libretro-thumbnails cover art
│   │   ├── hasheous.py           # Hasheous API client (online, hash-keyed)
│   │   ├── launchbox.py          # LaunchBox XML parser (offline,
│   │   │                         # user-supplied)
│   │   ├── screenscraper.py      # ScreenScraper API client (online, opt-in)
│   │   └── thegamesdb.py         # TheGamesDB API client (online,
│   │                             # name+platform, monthly quota)
│   ├── models/
│   │   ├── system.py             # SYSTEM_REGISTRY + YAML loader
│   │   ├── rom.py
│   │   ├── game.py
│   │   └── profile.py            # DestinationProfile + SystemMapping
│   └── ui/
│       ├── main_window.py        # Window, menu, toolbar, all workflow hooks
│       ├── system_sidebar.py     # Logo + name + count per system
│       ├── game_table.py
│       ├── detail_panel.py       # Cover + system logo + key/value grid +
│       │                         # hide-when-empty description
│       ├── settings_dialog.py    # General / DATs / Metadata / Scan / Diagnostics
│       ├── enrich_options_dialog.py # Fuzzy / re-enrich / online checkboxes
│       ├── cover_options_dialog.py  # Local-files / online-thumbnails checkboxes
│       ├── scan_progress.py      # Quick / Heavy / DestScan dialogs
│       ├── enrich_progress.py    # Enrich Metadata progress
│       ├── local_cover_progress.py # Find Covers progress (dual phase)
│       ├── organize_preview.py
│       ├── export_dialog.py      # Export / Sync dialog (incl. Include ROMs toggle)
│       ├── sync_preview.py       # Sync preview + apply UI
│       ├── sync_diff_progress.py # "Computing diff…" between dest scan + preview
│       ├── import_dialog.py      # Import ROMs preview + apply UI
│       ├── scrub_dialog.py       # Verify Library bucketed-checkbox preview
│       ├── scrub_progress.py     # Verify Library analyse phase
│       ├── clean_missing_progress.py  # Clean Missing Entries progress
│       ├── per_system_summary_dialog.py # Post-Export / post-Sync breakdown table
│       ├── _grouped_tree.py      # Tri-state header + right-click toggle mixin
│       ├── workers.py            # QThread workers (Scan / HeavyScan / Enrich /
│       │                          # Organize / Export / Sync / DestInventory /
│       │                          # BuildSyncPlan / CoverFinder / ImportAnalyse /
│       │                          # ImportApply / CleanMissing /
│       │                          # ScrubAnalyse / ScrubApply)
│       ├── artwork/              # Bundled per-platform logos (dark + light)
│       │   ├── __init__.py       # resolve_system_logo(system_id, theme)
│       │   └── systems/          # <system_id>-{dark,light}.png × 70 systems
│       ├── icons/cdrom.{png,ico}
│       └── themes/               # light, dark, wbm_classic .qss
└── tests/
    ├── conftest.py                # db / seeded_db / qapp fixtures
    ├── test_scanner.py
    ├── test_identifier.py
    ├── test_hasher.py
    ├── test_dat_parser.py
    ├── test_organizer.py
    ├── test_exporter.py
    ├── test_metadata.py
    ├── test_sync.py               # 5 modes, 4 tiers, cross-platform guard,
    │                              # path-mismatch dest_id threading
    ├── test_sync_preview.py
    ├── test_sync_fixes.py
    ├── test_library_cleanup.py    # tombstone, root-change, FK cascade,
    │                              # logging precedence
    ├── test_packaging.py          # install-dir, three-tier profile loading
    ├── test_importer.py           # 23 tests: plan analysis (dupe levels,
    │                              # extension fallback, multi-rom zip,
    │                              # refusal-inside-library), apply (atomic,
    │                              # move-after-copy, replace, keep_both,
    │                              # SAVEPOINT rollback, cancel, upsert),
    │                              # JSON round-trip, find_rom_by_path/sha1
    ├── test_scrub.py              # Verify Library: bucket classification,
    │                              # per-bucket SAVEPOINT, unreadable guard
    ├── test_per_system_summary_dialog.py  # Per-system summary dialog smoke
    └── ...                        # 1003 tests total, 1 skipped
```

## Git Policy

Claude Code handles `git add` and `git commit` at the end of each work unit (session OR feature/fix commit). `git push` is ALWAYS denied. `git merge`, `git rebase`, `git stash`, `git reset --hard`, `--no-verify` are denied.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) style: `feat(scope): ...`, `fix(scope): ...`, `refactor(scope): ...`, `docs(scope): ...`. Sessions 00–11 used `Session N: ...` style; that pattern is retired.

## License

[Apache License 2.0](LICENSE). All code in this repo is original work; LLM-assisted authorship is acknowledged in the README and via `Co-Authored-By` trailers on commits.

## Code Style & Conventions

- Python 3.12+ — modern type hints (`str | None`), match statements
- Type hints on every function signature
- Docstrings on every public class/method/function
- Pydantic v2 for all data models crossing boundaries
- structlog for structured JSON logging to stdout
- httpx as the only HTTP client
- Virtual env in `.venv` at project root
- No global state — pass dependencies explicitly
- SQL queries as plain strings in `db/queries.py`, not scattered across modules
- Constants in UPPER_SNAKE_CASE at module level
- Private methods prefixed with underscore
- `from romulus.db import queries as q` — alias `q.` is the preferred form in long files; bare `queries.` is acceptable inside `db/__init__.py` and its near neighbours

## Key Design Rules (Non-Negotiable)

1. **Local-first.** No server, no Docker, no external dependencies to run. SQLite for storage, files on disk for covers.
2. **No external CDN/JS dependencies.** All assets vendored locally if needed.
3. **Quick scan must be fast.** L1 (fuzzy filename) + L2 (internal header) run automatically during scan. L3 (hash+DAT) is a separate "Heavy Scan" action with a progress dialog and duration warning.
4. **Never modify files without preview.** The Organizer shows a before/after diff. The Exporter shows what will be copied. The Sync engine shows a per-action preview with totals + a double-confirm prompt before destructive runs. User must explicitly confirm before any filesystem changes.
5. **Atomic writes only.** Every file write goes through `core/atomic.py` (`tempfile.mkstemp` + `os.replace`). Per-action SAVEPOINT rollback in organizer + sync keeps the DB consistent with disk.
6. **Single library at a time.** ROMulus treats one `library_path` as the source of truth. Switching libraries prompts to wipe prior rows. The scanner sweep marks any row not visited as `missing=1` regardless of its `library_root` — see `core/scanner.py::scan_library` and `core/queries.py::mark_missing_under_root`.
7. **Tombstone, don't delete.** A vanished file becomes `missing=1`; the row stays in the DB so its metadata / hashes / enrichment survive a temporarily-unmounted share. Re-scanning un-tombstones via the path-keyed UPSERT. **Tools → Clean Missing Entries** is the only path that actually removes rows, and it cascades to `hashes` + `dest_inventory` + orphan `games`.
8. **Hacks are first-class artifacts.** Never silently deduplicate a hack against its original. Treat them as distinct titles.
9. **Hash cache is sacred.** Hashes are expensive. Cache in SQLite keyed by (path, mtime, size). Reuse on rescan if file hasn't changed.
10. **DATs are bundled.** 106 No-Intro DATs covering ~80 systems ship in `data/dats/` (dev) / `dats/` (portable). Users can add more to a configurable folder. Both are merged on startup.
11. **Cover art is free.** Primary source: libretro-thumbnails (HTTP, no API key). ScreenScraper is optional — app prompts user, works without it.
12. **Config lives in SQLite.** No manual config file editing. Everything through the Settings dialog.
13. **Destination profiles are YAML.** Ship 7 built-in profiles in `profiles/`. Users can create custom ones; three-tier load (user > install > package builtin).
14. **No pre-v0.3.0 DB migration support.** ROMulus is pre-1.0 with no production user base; legacy DBs get wiped, not migrated. Re-introduce migration framework when v1.0 ships.
15. **Sync identity matching anchors on system_id.** Tier-2 fuzzy match keys on `(fuzzy_key, region, system_id)` so cross-platform fuzzy-key collisions (e.g. Game Boy vs Game Boy Color "Pac-Man") never match. Tier-1 path equivalence and tier-4 SHA-1 are also gated correctly.
16. **Plan.dest_id is authoritative.** Sync apply uses `plan.dest_id` directly; do NOT re-derive from `str(target_path)` because Path stringification can diverge from the value stored at destination-creation time (UNC trailing slash, separator normalization).
17. **Metadata enrichment is local-first.** Order: libretro-database → GameDB → Hasheous → LaunchBox → ScreenScraper → TheGamesDB. The two bundled offline sources (`data/libretro-metadat/` and `data/gamedb/`) run before any network call. The "Also try online metadata sources" checkbox on `EnrichOptionsDialog` gates the three remote providers; offline-only runs commit nothing for games the local sources missed (no API quota burnt, no surprise network traffic).
18. **Metadata and cover-art are separate workflows.** `enrich_library` writes to the `metadata` table only. Cover discovery is driven by `CoverFinderWorker` via `CoverOptionsDialog`, which lets the user pick local-file walk and/or libretro-thumbnail fetch independently per batch. `fetch_online_covers_for_scope` is the orchestrator's per-game cover fetcher.
19. **Bundled offline metadata is content-addressed by CRC32.** Both `libretro_metadat` and `gamedb` index by lowercase 8-char CRC32 (stripping any `0x` prefix). `roms.hashes` populated by Heavy Scan is what unlocks them. Quick-scan-only games fall through to title-fuzzy fallback paths in both clients.
20. **TheGamesDB has a monthly quota.** ~1000 requests/month for public keys, 6000 lifetime for private keys. The client logs `remaining_monthly_allowance` per response, persists it to `thegamesdb_remaining_allowance` in config, and short-circuits future calls when it hits zero. Slot it last in the chain so we only spend on games every cheaper source missed.
21. **Import is symmetric to sync.** The Import engine (`core/importer.py`) mirrors the Sync engine in shape — analyse → preview → apply, per-action SAVEPOINT, atomic copy via `core/atomic.py`, cooperative cancel between actions. Heavy identification (SHA-1 + DAT match) runs unconditionally on every analyse pass so the three duplicate levels (path / filename / hash) all surface; the dialog warns about duration up front when the staging folder is large. Staging folder must be outside `library_root` — refused with `ValueError` to prevent self-recursion footguns. New ROMs are enrolled via the same path-keyed `upsert_rom` the scanner uses, so importing a file whose target path matches a `missing=1` row un-tombstones the row rather than duplicating it.
22. **Long-running DB writes go through a worker + rollback wrap.** `CleanMissingWorker` and `ScrubApplyWorker` (and any future analogue) run on their own QThread, open their own connection, and wrap the work in `try/except: conn.rollback(); raise`. Closes the "DB locked / silent rollback" footgun where an exception inside a UI-thread DML chain left the implicit transaction open and held the write lock for the rest of the session.
23. **`prune_orphan_games` clears FK-dependent rows first.** `metadata` / `covers` / `collection_games` are deleted; `dest_inventory.game_id` is NULLed (the dest_inventory row is anchored on `rom_id`). Required because none of those tables declare `ON DELETE CASCADE` — the games delete fails with `IntegrityError` if dependents remain. Latent bug pre-v0.3.0 because tests never attached metadata to the games they pruned.
24. **Sync diff is O(N+M), not O(N·M).** `_build_inventory_fuzzy_index` pre-computes `(fuzzy_key, region, system_id) → InventoryEntry` once at the top of `_build_push_actions` / `_build_twoway_actions`; tier-2 lookup is a single `dict.get`. `build_plan` runs on `BuildSyncPlanWorker` with a "Computing diff…" progress dialog — slots fired across a queued connection from a worker still execute on the receiving (UI) thread, so the inventory worker alone doesn't move `build_plan` off the UI thread. See `docs/sync-design.md` §12.6.
25. **Export has an artwork-only mode.** `ExportOptions.include_roms` defaults True; uncheck to skip the ROM copy loop and run only the sidecar refresh. `copy_artwork` size+mtime-compares per file (2s tolerance for FAT32/SMB rounding). The dialog disables Scan destination when Include ROMs is off — a sync-path plan would be pure `ACTION_IDENTICAL` rows and the apply step doesn't touch sidecars.
26. **Post-Export / post-Sync show a per-system summary dialog.** `ExportSummary` and `SyncSummary` carry a `per_system` field populated alongside the existing aggregates; the dialog (`PerSystemSummaryDialog`) renders one row per system with the per-bucket counts. Used to diagnose why a system was skipped (unsupported / refuse-overwrite / already-present) without grepping `logs/romulus.log`.
27. **Preview dialogs have tri-state group headers + right-click bulk toggle.** Shared `GroupedCheckboxTreeMixin` powers `OrganizePreviewDialog`, `SyncPreviewDialog`, and `ScrubPreviewDialog`. Multi-thousand-row plans become workable. Buckets whose every child is non-checkable (e.g. Organize Collisions) keep a plain non-checkable header.

## Scan Types

| Type | What runs | Speed | Trigger |
|---|---|---|---|
| **Quick Scan** | Filesystem walk + platform detection + filename parsing (L1) + internal header extraction (L2) + missing-row sweep | Seconds to minutes | "Quick Scan" button or on library import |
| **Heavy Scan** | SHA-1/CRC32 hashing + DAT matching (L3) | Minutes to hours (240 GB ≈ 80 min over SMB) | "Heavy Scan" toolbar/menu action with duration warning dialog. Can be scoped per-game via right-click |
| **Destination Scan** | Filesystem walk of a sync target + signature-drift check against cached `dest_inventory` rows | Seconds to a minute | First step of the Sync workflow |

Quick scan gives a browsable library immediately and tombstones any file that has vanished since the last scan. Heavy scan unlocks canonical naming, accurate dedup, cover art matching, and completeness reporting. Destination scan is the read-only first half of a sync.

## Sync Modes

| Mode | Direction | Dest-only files | Destructive? |
|---|---|---|---|
| `push_merge` (default) | Local → Dest | Left in place | No |
| `push_mirror` | Local → Dest | Deleted | Yes — needs double-confirm |
| `push_wipe` | Local → Dest | Wiped before push | Yes — needs double-confirm |
| `pull_merge` | Dest → Local | Copied to library + enrolled as fuzzy match | No |
| `two_way` | Both | Conflicts surface in preview (skip / local / dest / newest / prompt) | Possibly — confirm based on action mix |

See `docs/sync-design.md` for the full spec (5 modes × 4 identity tiers × dest_inventory cache × sync_plans persistence).

## Agent & Plugin Routing

Plugin agents take priority over custom agents when their domain matches.

| Task | Agent/Plugin |
|---|---|
| General Python (async, Pydantic, PySide6) | `python-pro` plugin |
| Backend architecture, data flow | `backend-architect` plugin |
| TDD, test-first development | `tdd-orchestrator` plugin |
| Test automation, pytest suites | `test-automator` plugin |
| Debugging, runtime errors | `debugger` plugin |
| Error diagnosis | `error-detective` plugin |
| Code quality, refactoring | `code-reviewer` plugin |
| Security audit | `security-auditor` plugin |
| Performance optimization | `performance-engineer` plugin |
| Database optimization | `database-optimizer` plugin |
| Database architecture, SQL | `database-architect` / `sql-pro` plugin |
| Architecture docs, tutorials | `docs-architect` / `tutorial-engineer` plugin |
| Project spec, CLAUDE.md, TECHNICAL_PLAN.md | `project-architect` custom |
| Session planning, task breakdown | `task-orchestrator` custom |
| PySide6 UI, Qt widgets | `frontend-engineer` custom |
| Shell scripting (Bash) | `bash-powershell-engineer` custom |
| REST API clients (httpx, metadata APIs) | `rest-api-engineer` custom |
| README, CHANGELOG, docstrings | `docs-writer` custom |

## Agent Callout Format

**Orchestrator — when assigning tasks:**
```
📋 task-orchestrator → Assigning Session N tasks:
  • [agent-name]  : [task description]
  • [agent-name]  : [task description]
  ([agent-a] and [agent-b] can run in parallel; [agent-c] waits for [agent-a])
```

**Each agent — when starting:** `🔧 [agent-name] → Starting: [brief task description]`
**Each agent — when finishing:** `✅ [agent-name] → Done: [what was produced] ([file paths if applicable])`
**Each agent — if blocked:** `⚠️ [agent-name] → Blocked: [reason]. Waiting on: [dependency].`

## CI/CD Local Validation Rule

Whenever a GitHub Actions workflow is created or modified that runs lint or tests — run those exact same commands locally first and resolve all failures before the workflow is committed.

## Completion Summary Template

Every session ends by appending this block to its session file:

```markdown
## Completion Summary
**Status:** COMPLETE
**Date:** {{DATE}}
**What was built/changed:** {{brief summary}}
**Tests:** {{pass/fail summary}}
**Config changes:** {{new settings, or "None"}}
**Breaking changes:** {{list or "None"}}
**Carry-forward notes:** {{anything the next session or review session needs to know}}
```
