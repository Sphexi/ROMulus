# ROMulus — Architecture

This document is the "under the hood" reference for ROMulus. The
[README](../README.md) covers what ROMulus is and how to use it; this
file covers how it's built and why it's built that way.

For deeper implementation detail (schema column-by-column, identifier
pipeline pseudocode, every subsystem) see
[TECHNICAL_PLAN.md](TECHNICAL_PLAN.md). For the destination sync engine
spec specifically, see [sync-design.md](sync-design.md).

---

## Why ROMulus

If you keep a serious ROM library you've probably collected workflow
scraps: filename cleanup scripts, a half-finished ScreenScraper run, a
OneDrive folder that "almost matches" what your handheld expects.
ROMulus replaces that with a single desktop app that:

- **Stays local.** No phoning home, no upload, no required login. SQLite +
  files on disk, nothing else.
- **Bundles offline metadata.** Bundled snapshots of
  [libretro-database](https://github.com/libretro/libretro-database)
  (~20 MB across 7 metadata dimensions × ~50 systems) and
  [GameDB](https://github.com/niemasd/GameDB) (~17 MB across 42 systems)
  are consulted *before* any network call. Most cartridge-based titles
  fill out without ever touching the internet.
- **Online sources are opt-in per batch.** The Enrich Metadata dialog
  has an "Also try online metadata sources" checkbox; uncheck it and
  Hasheous, ScreenScraper, and TheGamesDB stay quiet.
- **Respects your files.** The Organizer, Exporter, and Sync engine all
  show a preview before doing anything irreversible. Nothing is moved,
  renamed, or deleted until you explicitly confirm.
- **Treats hacks as first-class.** ROM hacks are never silently merged
  with their base titles.
- **Caches what's expensive.** SHA-1 hashes are stored in SQLite keyed
  by `(path, mtime, size)`. Rescans of unchanged files are nearly free.
- **Uses free cover art by default.** libretro-thumbnails needs no API
  key. ScreenScraper credentials are optional.

---

## Architecture diagram

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
│  │ Tools menu: Verify Library… | Clean Missing Entries… ││
│  └──────────────────────────────────────────────────────┘│
└──────────────┬───────────────────────────────────────────┘
               │ signals/slots + QThread workers
┌──────────────┴───────────────────────────────────────────┐
│                    Core Engine                           │
│                                                          │
│  Scanner ──→ Identifier ──→ SQLite DB                    │
│  (+ missing  (L1 fuzzy,                                  │
│   sweep +    L2 header,                                  │
│   self-heal) L3 hash+DAT)                                │
│                                                          │
│  DAT Parser (bundled No-Intro DATs, ~106 systems)        │
│                                                          │
│  Enrich Metadata chain (local-first):                    │
│    libretro-database ──→ GameDB ──→ Hasheous ──→         │
│    LaunchBox ──→ ScreenScraper ──→ TheGamesDB            │
│    (online providers gated by per-batch checkbox)        │
│                                                          │
│  Find Covers (independent per-batch toggles):            │
│    local image walk + libretro-thumbnails fetch          │
│                                                          │
│  Organizer  Export Engine    Sync Engine                 │
│  (preview/  (dest profiles,  (5 modes: push merge/       │
│   commit)   gamelist.xml,     mirror/wipe, pull merge,   │
│             .m3u, artwork,    two-way; 4-tier identity   │
│             include_roms      match; dest_inventory      │
│             toggle for        cache; O(N+M) tier-2 via   │
│             artwork-only      pre-indexed dest_by_fuzzy; │
│             refreshes)        SAVEPOINT rollback)        │
│                                                          │
│  Importer  (staging → identify → analyse → preview →     │
│   commit)  per-action SAVEPOINT, three-level dupe        │
│            detection (path / filename / hash)            │
│                                                          │
│  Scrub Engine (Tools → Verify Library)                   │
│   walks DB ↔ disk, classifies into four buckets,         │
│   per-bucket SAVEPOINT apply                             │
│                                                          │
│  Per-system summary dialog (post-Export, post-Sync) ─────┤
│   one row per system × Copied / Bytes / Covers / etc.    │
│                                                          │
│  Cover Cache    (<install_dir>/data/covers/)             │
│  GameDB JSON    (<install_dir>/data/gamedb/)             │
│  libretro DATs  (<install_dir>/data/libretro-metadat/)   │
│  SQLite DB      (<install_dir>/data/romulus.db)          │
└──────────────────────────────────────────────────────────┘
```

Single-process desktop app, no server, no Docker. Distributed as a
single-binary portable Windows ZIP (PyInstaller `--onefile`); data folders
ship alongside the exe.

---

## Threading model

Long-running work runs on `QThread` workers. The UI thread owns the main
window and dispatches work via signals.

| Worker | Triggered by | Work |
|---|---|---|
| `ScanWorker` | Quick Scan toolbar / sidebar right-click | Filesystem walk + L1/L2 + missing sweep |
| `HeavyScanWorker` | Heavy Scan toolbar / per-game right-click | SHA-1/CRC32 + DAT matching |
| `EnrichWorker` | Enrich Metadata toolbar / per-game right-click | Metadata chain orchestrator |
| `CoverFinderWorker` (alias `LocalCoverFinderWorker`) | Find Covers toolbar / per-game right-click | Local cover walk + libretro-thumbnails fetch |
| `OrganizeWorker` | Organize toolbar | Library reorganization |
| `ExportWorker` | Export dialog | One-shot export to a profile (two-phase: ROM copy + sidecar refresh) |
| `SyncWorker` | Sync dialog | 5-mode sync apply |
| `DestInventoryWorker` | Sync dialog first step | Destination filesystem walk + cache refresh |
| `BuildSyncPlanWorker` | Between DestInventory and SyncPreview | Runs `build_plan` on a worker thread (was on UI thread; froze on large libs) |
| `ImportAnalyseWorker` | Import ROMs dialog (analyse phase) | Walks staging folder + builds ImportPlan |
| `ImportApplyWorker` | Import ROMs dialog (apply phase) | Executes the approved ImportPlan with per-action SAVEPOINT |
| `CleanMissingWorker` | Tools → Clean Missing Entries | Drops tombstoned rows + FK dependents + orphan-game prune |
| `ScrubAnalyseWorker` | Tools → Verify Library (analyse phase) | Walks every roms row, classifies vs disk |
| `ScrubApplyWorker` | Verify Library (apply phase) | Applies per-bucket fixes (SAVEPOINT-per-bucket) |

Worker conventions:

- **Each worker opens its own SQLite connection inside `run()`.** sqlite3
  connections are thread-bound by default; the main-thread connection is
  never shared across threads.
- **Cooperative cancellation** via a private exception raised inside the
  progress callback. Post-walk DB phases (`Marking missing entries…`,
  `Linking ROMs to games: <system>…`, `Finalising scan history…`) ignore
  cancel requests — interrupting mid-rebuild would leave the DB
  inconsistent with disk.
- **Atomic file writes** route through `core/atomic.py`
  (`tempfile.mkstemp` + `os.replace`). A cancelled or killed worker can
  never leave a half-written file on disk.
- **Per-action SAVEPOINT rollback** in Organize and Sync apply paths.

---

## Key design rules (non-negotiable)

These are intentional and govern what changes are in-scope. Listed here
so the behavior doesn't surprise users or future contributors.

1. **Local-first.** No server, no Docker, no external dependencies to
   run. SQLite for storage, files on disk for covers.
2. **Quick scan must be fast.** L1 (fuzzy filename) + L2 (internal
   header) run automatically. L3 (hash+DAT) is the separate "Heavy
   Scan" action with a progress dialog and duration warning.
3. **Never modify files without preview.** Organizer, Exporter, and
   Sync engine all show a per-action preview with totals before
   anything irreversible. Destructive sync modes (mirror, wipe, two-way
   with deletes) require a double-confirm prompt.
4. **Atomic writes only.** Every file write goes through
   `core/atomic.py`. Per-action SAVEPOINT rollback keeps the DB
   consistent with disk on partial failure.
5. **Single library at a time.** ROMulus treats one `library_path` as
   the source of truth. Switching libraries prompts to wipe prior rows.
   The scanner sweep marks any row not visited as `missing=1`
   regardless of its `library_root`.
6. **Tombstone, don't delete.** A vanished file becomes `missing=1`;
   the row stays so its metadata / hashes / enrichment survive a
   temporarily-unmounted share. Re-scanning un-tombstones via the
   path-keyed UPSERT. **Tools → Clean Missing Entries** is the only
   path that actually removes rows.
7. **Hacks are first-class.** ROM hacks are never silently
   deduplicated against their base titles. `[h]` / `[T+]` markers
   anchor a distinct fuzzy_key suffix.
8. **Hash cache is sacred.** Hashes are expensive. Cached in SQLite
   keyed by `(path, mtime, size)`. Reused on rescan if file hasn't
   changed.
9. **DATs are bundled.** 106 No-Intro DATs covering ~80 systems ship in
   `data/dats/` (dev) / `dats/` (portable). Users can add more via a
   configurable folder.
10. **Cover art is free.** Primary source: libretro-thumbnails (HTTP,
    no API key). ScreenScraper is optional.
11. **Config lives in SQLite.** No manual config file editing.
    Everything through the Settings dialog.
12. **Destination profiles are YAML.** 7 built-in profiles in
    `profiles/`. User profiles in `~/.romulus/profiles/` override
    built-ins by id (three-tier load: user > install > package
    builtin).
13. **No pre-v0.3.0 DB migration support.** ROMulus is pre-1.0 with no
    production user base; legacy DBs get wiped, not migrated.
14. **Sync identity matching anchors on system_id.** Tier-2 fuzzy match
    keys on `(fuzzy_key, region, system_id)` so cross-platform fuzzy
    collisions (Game Boy vs Game Boy Color "Pac-Man") never match.
15. **Plan.dest_id is authoritative.** Sync apply uses `plan.dest_id`
    directly; do not re-derive from `str(target_path)` because Path
    stringification can diverge from the value stored at
    destination-creation time.
16. **Metadata enrichment is local-first.** Order: libretro-database
    → GameDB → Hasheous → LaunchBox → ScreenScraper → TheGamesDB. The
    two bundled offline sources run before any network call.
17. **Metadata and cover-art are separate workflows.**
    `enrich_library` writes to the `metadata` table only. Cover
    discovery is driven by `CoverFinderWorker`.
18. **Bundled offline metadata is content-addressed by CRC32.** Both
    `libretro_metadat` and `gamedb` index by lowercase 8-char CRC32.
    Heavy Scan is what unlocks them; quick-scan-only games fall through
    to title-fuzzy fallback paths.
19. **TheGamesDB has a monthly quota.** ~1000 requests/month for public
    keys, 6000 lifetime for private keys. Slot it last in the chain so
    we only spend on games every cheaper source missed.
20. **Import is symmetric to sync.** The Import engine
    (`core/importer.py`) mirrors the Sync engine in shape — analyse →
    preview → apply, per-action SAVEPOINT, atomic copy, cooperative
    cancel. Three duplicate levels are detected (path / filename /
    hash); staging folder must be outside `library_root`.
21. **Long-running DB writes go through a worker + rollback wrap.**
    Clean Missing Entries and Verify Library both run on dedicated
    QThread workers that call `conn.rollback()` on any exception before
    re-raising. Closes the "DB locked / silent rollback" footgun where
    a stray exception in a UI-thread DML chain left the implicit
    transaction open and held the write lock for the rest of the
    session.
22. **`prune_orphan_games` clears FK-dependent rows first.** `metadata`
    / `covers` / `collection_games` are deleted before the orphan game
    rows themselves; `dest_inventory.game_id` is NULLed rather than
    deleted (the inventory row is anchored on `rom_id`). Required
    because none of those tables declare `ON DELETE CASCADE` — the
    games delete fails with `IntegrityError` if dependents remain.
23. **Sync diff is O(N+M), not O(N·M).** `_build_inventory_fuzzy_index`
    pre-computes `(fuzzy_key, region, system_id) → InventoryEntry` once
    at the top of `_build_push_actions` / `_build_twoway_actions`. The
    tier-2 lookup is a single `dict.get`. Prior naive form
    re-scanned the entire inventory + recomputed every fuzzy key per
    local rom — froze the UI for tens of minutes on 38K × 17K libraries.
24. **`build_plan` runs on a worker thread.** `BuildSyncPlanWorker` sits
    between `DestInventoryWorker` and `SyncPreviewDialog` with its own
    progress dialog ("Computing diff…"). Required because slots fired
    across a queued connection from a worker still run on the receiving
    (UI) thread — so calling `build_plan` from `_on_inventory_done`
    froze the UI even though the inventory walk itself was off-thread.
25. **Export has an artwork-only mode.** `ExportOptions.include_roms`
    defaults True; uncheck to skip the ROM copy loop entirely and run
    only the sidecar refresh. `copy_artwork` does a size + mtime
    compare so a re-run only republishes covers that actually changed
    (2 s mtime tolerance for FAT32/SMB rounding).
26. **Post-Export and post-Sync show a per-system summary dialog.**
    `ExportSummary` and `SyncSummary` carry a `per_system` field
    populated alongside the existing aggregates; the dialog
    (`PerSystemSummaryDialog`) renders one row per system with the
    per-bucket counts. Used to diagnose why a system was skipped
    (unsupported / refuse-overwrite / already-present) without grepping
    `logs/romulus.log`.
27. **Preview dialogs have tri-state group headers + right-click bulk
    toggle.** Shared `GroupedCheckboxTreeMixin` powers
    `OrganizePreviewDialog`, `SyncPreviewDialog`, and
    `ScrubPreviewDialog`. Multi-thousand-row plans become workable —
    flip a bucket with one click. Buckets whose every child is
    non-checkable (e.g. Organize Collisions) keep a plain non-checkable
    header.

---

## Scan types

| Type | What runs | Speed | Trigger |
|---|---|---|---|
| **Quick Scan** | Filesystem walk + platform detection + filename parsing (L1) + internal header extraction (L2) + missing-row sweep | Seconds to minutes | "Quick Scan" toolbar button, sidebar right-click (per-system scope), or on library import |
| **Heavy Scan** | SHA-1/CRC32 hashing + DAT matching (L3) | Minutes to hours (240 GB ≈ 80 min over SMB, ≈ 5 min on local SSD) | "Heavy Scan" toolbar/menu action with duration warning. Can be scoped per-game via right-click |
| **Destination Scan** | Filesystem walk of a sync target + signature-drift check against cached `dest_inventory` rows | Seconds to a minute | First step of the Sync workflow |

Scoped Quick Scan: right-click a system in the sidebar → "Quick Scan:
<system>". `scan_library` accepts `scope_system_id` and the missing sweep
is system-restricted so rows from other systems aren't tombstoned by a
scoped rescan.

Quick scan gives a browsable library immediately and tombstones any file
that has vanished since the last scan. Heavy scan unlocks canonical
naming, accurate dedup, cover art matching, and bundled offline metadata
(both libretro-database and GameDB are CRC32-keyed). Destination scan is
the read-only first half of a sync.

---

## Identifier pipeline

Three layers. See [ROM-DEDUP-METHODOLOGY.md](ROM-DEDUP-METHODOLOGY.md)
for the full methodology.

| Layer | Signal | Cost | Runs during |
|---|---|---|---|
| L1: Fuzzy filename | Normalized filename key | Trivial | Quick Scan |
| L2: Internal header | ROM-embedded title | ~100 bytes read | Quick Scan |
| L3: Hash + DAT | SHA-1 lookup in DAT DB | Full file read | Heavy Scan |

Header strip rules (SNES SMC, NES iNES, N64 byteswap, Atari Lynx) are
applied before hashing so DAT lookups hit canonical bytes.
[TECHNICAL_PLAN.md](TECHNICAL_PLAN.md#identifier-pipeline) has the
per-system header offset tables.

---

## Metadata enrichment chain

`enrich_library` walks games in scope (DAT-verified by default; fuzzy
and re-enrich are opt-in via `EnrichOptionsDialog`) and tries each
source in order, stopping at the first one with user-facing data:

1. **libretro-database** (bundled, offline) — per-CRC32 clrmamepro DATs
   across 7 metadata dimensions (genre, developer, publisher, release
   year, max players, ESRB, franchise) × ~50 systems. Tried first
   because the per-field coverage is the richest of the local sources.
2. **GameDB** (bundled, offline) — per-CRC32 JSON snapshots, 42 systems.
   Provides canonical names + regions + publisher / release date for
   the systems libretro-database doesn't reach (PSX, GameCube, Wii).
3. **Hasheous** (online, no key) — SHA-1 keyed.
4. **LaunchBox XML** (local, user-supplied via Settings → DATs).
5. **ScreenScraper** (online, opt-in via Settings → Metadata).
6. **TheGamesDB** (online, opt-in via Settings → Metadata; monthly
   quota tracked + respected).

The `EnrichOptionsDialog` exposes three flags per batch:

- **Also enrich fuzzy-matched games** — drops the
  `match_confidence='dat_verified'` filter.
- **Re-attempt enrichment on games that already have metadata** —
  drops the `m.game_id IS NULL` filter.
- **Also try online metadata sources** (default on) — gates Hasheous /
  ScreenScraper / TheGamesDB.

Cover discovery is a separate workflow driven by `CoverFinderWorker`
via `CoverOptionsDialog`. Two independent checkboxes per run:

- **Search for local covers** (default ON) — walks the library tree for
  `.png/.jpg` files and matches them to enrolled ROMs by fuzzy name.
- **Search online for covers** (default OFF) — fetches libretro
  thumbnails for games still missing a cover.

---

## Sync modes

| Mode | Direction | Dest-only files | Destructive? |
|---|---|---|---|
| `push_merge` (default) | Local → Dest | Left in place | No |
| `push_mirror` | Local → Dest | Deleted | Yes — needs double-confirm |
| `push_wipe` | Local → Dest | Wiped before push | Yes — needs double-confirm |
| `pull_merge` | Dest → Local | Copied to library + enrolled as fuzzy match | No |
| `two_way` | Both | Conflicts surface in preview (skip / local / dest / newest / prompt) | Possibly — confirm based on action mix |

Four-tier identity matcher (in order): path equivalence →
`(fuzzy_key, region, system_id)` → size sanity gate → SHA-1 deep verify.
The `system_id` segment of tier 2 is the cross-platform guard.

**Perf:** the tier-2 lookup is O(1) per local rom via the
pre-built `dest_by_fuzzy` index — see
[sync-design.md §12.6](sync-design.md#126-on-buildplan-perf-+-worker-thread-commit-e3082b4)
for the full story. `build_plan` runs on `BuildSyncPlanWorker` with a
"Computing diff…" progress dialog so the UI stays responsive on
multi-tens-of-thousands-of-ROM libraries.

See [sync-design.md](sync-design.md) for the full spec.

---

## SQLite schema (overview)

All persistent state lives in `<install_dir>/data/romulus.db` (with
`~/.romulus/romulus.db` as a fallback when the install folder isn't
writable). WAL mode, foreign keys on, POSIX `0o600` file permissions.

Tables:

- `config` — key/value app configuration.
- `systems` — system registry (seeded from `systems/builtin.yaml`).
- `roms` — ROM files on disk. Includes `library_root` and `missing`
  columns for the single-library / tombstone-missing design.
- `hashes` — hash cache, keyed by rom_id. Reused on rescan via
  `(path, mtime, size)` invalidation.
- `dat_entries` — parsed No-Intro DAT entries; indexed on SHA-1 and
  CRC32+size.
- `games` — logical games (one game may have multiple ROMs across
  regions, revisions, formats).
- `metadata` — enriched metadata per game (description, genre,
  developer, publisher, release_date, release_year, players, rating,
  source).
- `covers` — cover art records (boxart / screenshot / title_screen)
  pointing into the cover cache.
- `collections` + `collection_games` — user collections (Favorites
  seeded built-in).
- `scan_history` — scan run records.
- `organize_plans` — JSON-persisted Organize plans.
- `sync_destinations` — user's saved destination targets.
- `dest_inventory` — cached per-destination filesystem state.
- `sync_plans` — JSON-persisted Sync plans.

[TECHNICAL_PLAN.md](TECHNICAL_PLAN.md#sqlite-schema) has the column-by-
column DDL.

---

## Configuration reference

All configuration is stored in the `config` SQLite table and editable
via **File → Settings...**. The full set of keys from
`romulus.db.config.DEFAULT_CONFIG`:

| Key | Default | Meaning |
|---|---|---|
| `library_path` | `""` (unset) | Root folder of your ROM library |
| `dat_paths` | `["dats"]` (JSON) | Folders scanned for No-Intro / Redump DAT files |
| `cover_cache_path` | `<data_dir>/covers` | Where libretro / Hasheous covers are cached |
| `screenscraper_username` | `""` | Optional ScreenScraper username |
| `screenscraper_password` | `""` | Optional ScreenScraper password |
| `thegamesdb_api_key` | `""` | Optional TheGamesDB API key |
| `thegamesdb_remaining_allowance` | `""` | Diagnostic — last seen monthly quota counter |
| `theme` | `system` | UI theme: `system` / `light` / `dark` / `wbm_classic` |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (live-applied) |
| `default_view` | `table` | Default view mode for the game list |
| `scan_threads` | `8` | Worker threads used by Heavy Scan |
| `last_scan_type` | `""` | Diagnostic — last scan type that completed |
| `last_scan_time` | `""` | Diagnostic — ISO timestamp of last scan |

`dat_paths` is JSON-encoded in storage; use **Settings → DATs** rather
than editing the raw value.

**Environment variable overrides:**

- `ROMULUS_LOG_LEVEL` (`DEBUG` / `INFO` / `WARNING` / `ERROR`) takes
  precedence over the stored value at startup.
- `ROMULUS_DATA_DIR` pins the data directory anywhere on disk.

---

## Destination profiles

Profiles describe how to lay out an exported library for a specific
device or launcher. Each profile is a YAML file with a system-by-system
map of folder names and supported file extensions. Seven profiles ship
in `profiles/`:

| Profile | Target |
|---|---|
| `batocera.yaml` | Batocera (`/roms/<system>/`) with gamelist.xml |
| `retropie.yaml` | RetroPie (`~/RetroPie/roms/<system>/`) |
| `onionos.yaml` | Onion OS for Miyoo Mini |
| `muos.yaml` | muOS for ROCKNIX / RG-series handhelds |
| `mister.yaml` | MiSTer FPGA (`/media/fat/games/<Core>/`) |
| `analogue-pocket.yaml` | Analogue Pocket via openFPGA cores |
| `anbernic-rglauncher.yaml` | Anbernic stock OS / RGLauncher with ES-DE-style gamelists |

Profiles also specify an `artwork_filename_template` (`{stem}{ext}` by
default; `{stem}-image{ext}` for EmulationStation classic) and a
`gamelist_format` (`emulationstation_xml`).

### Folder-name accuracy

Built-in folder names are best-effort and reflect public docs at the
time of writing. Known judgement calls:

- **MiSTer:** Atari 2600 and 7800 share the `ATARI7800` core folder;
  MAME uses generic `Arcade`. Verify against your specific build.
- **Analogue Pocket:** several systems (Virtual Boy, PCE-CD) assume the
  `agg23` openFPGA core layout. Reconfirm against your installed cores.
- **Onion OS:** folder casing follows the Onion docs at the time of
  writing. Onion has churned casing rules before.
- **RetroPie:** Sega Mega Drive / Genesis is `megadrive` (the RetroPie
  default), not `genesis`.

If a built-in mapping is wrong for your setup, copy the YAML to
`~/.romulus/profiles/` and edit it — user profiles override built-ins
by id.

### Creating a custom profile

```yaml
# ~/.romulus/profiles/my-handheld.yaml
id: my-handheld
name: "My Handheld"
description: "Layout for my specific firmware"
case_sensitive: true
base_path: "Roms"

systems:
  snes:
    folder: "SNES"
    extensions: [".sfc", ".smc", ".zip"]
  megadrive:
    folder: "MEGADRIVE"
    extensions: [".md", ".gen", ".bin", ".smd", ".zip"]
  # Mark systems your device does not support so ROMulus skips them cleanly:
  gamecube:
    folder: ""
    supported: false
```

---

## DAT files

ROMulus uses [No-Intro][nointro]-style Logiqx XML DAT files to match
ROMs by SHA-1/CRC32 and replace messy filenames with canonical names.

**Real No-Intro DATs ship with v0.2.0+** — 106 DAT files covering ~80
systems, ~457k total entries. They land in `dats/` next to the exe in
the portable build, and under `data/dats/` in a source checkout. Heavy
Scan works out of the box on common systems.

To add more (Redump for disc-based systems, TOSEC, newer No-Intro
revisions):

1. Visit [DAT-o-MATIC][datomatic] or the Redump downloads page.
2. Download the `Standard` DAT for each system you care about.
3. Drop the `.dat` files into the `dats/` folder, OR pick a different
   folder via **Settings → DATs → Add folder...**.

[nointro]: https://no-intro.org/
[datomatic]: https://datomatic.no-intro.org/

---

## Packaging & distribution

**Portable Windows ZIP** (`build-portable.ps1`):

1. PyInstaller (`--onefile` per `romulus.spec`) produces
   `dist/romulus.exe`. The exe self-extracts to `%TEMP%/_MEIxxxxxx/` on
   launch and runs from there.
2. `build-portable.ps1` moves the exe into `dist/romulus/`, then copies
   `data/dats/`, `profiles/`, `systems/`, `data/gamedb/`, and
   `data/libretro-metadat/` alongside it.
3. The `dist/romulus/` folder is ZIPed to `dist/romulus-windows-x64.zip`.

End-user layout: see [README → Installation](../README.md#installation-portable-windows).

**Spec choices:**

- **`--onefile` over `--onedir`** — single binary at the cost of ~1.5s
  extra startup on first launch each session.
- **UPX disabled.** Trips Windows Defender heuristics; the savings are
  marginal vs. ZIP compression.
- **Themes + icons embedded** (loaded via `Path(__file__).parent`), but
  **profiles / systems / dats / gamedb / libretro-metadat are external**
  (live alongside the exe in the ZIP) so users can edit them without
  launching the app.

**Install-dir resolution** (`app._resolve_install_dir`):

1. `sys.executable.parent` when running frozen.
2. Walks up from the module looking for `pyproject.toml` when running
   from a dev clone.
3. Falls back to `~/.romulus/` if neither works.

`app.resolve_data_dir` then prefers `<install_dir>/data` if writable,
else `~/.romulus/`. `ROMULUS_DATA_DIR` env var overrides both.

**Three-tier YAML loading** (profiles + systems): user >
`<install_dir>/<dir>/` > package builtin stub. User-supplied YAML
overrides built-in by id.

---

## CI

GitHub Actions runs `ruff check src/ tests/` and `pytest` on every push
to `main` and on every pull request.

Runner: **`windows-latest`**. ROMulus is a Windows-first desktop app and
running CI on the same OS we ship for means lint + tests exercise the
same Qt/SQLite/PySide6 stack end users will run. It also sidesteps a
flaky Linux + PySide6 + sqlite3 segfault in
`test_worker_emits_progress_and_finishes` that couldn't be pinned to a
specific Python-level cause (deepest visible frame was inside the
C-level `conn.close()` of the worker thread).

Workflow specifics:

- Python pinned to 3.12.
- `QT_QPA_PLATFORM=offscreen` so headless Qt widget tests work.
- Project installed via `pip install -e ".[dev]"`.
- All actions SHA-pinned per security audit v0.1.0 finding #10.

Per the **CI/CD Local Validation Rule** in `CLAUDE.md`, the workflow's
exact commands are run locally on Windows before any release tag is
pushed.

Current state: **1,003 tests passing, 1 skipped** (POSIX-only chmod
test, skipped on Windows because NTFS ACLs are inherited).

---

## Code style & conventions

- Python 3.12+ — modern type hints (`str | None`), `match` statements.
- Type hints on every function signature.
- Docstrings on every public class / method / function.
- Pydantic v2 for all data models crossing boundaries.
- `structlog` for structured JSON logging to stdout.
- `httpx` as the only HTTP client.
- Virtual env in `.venv` at project root.
- No global state — pass dependencies explicitly.
- SQL queries as plain strings in `db/queries.py`, not scattered across
  modules. Use `from romulus.db import queries as q` in long files.
- Constants in `UPPER_SNAKE_CASE` at module level.
- Private methods prefixed with underscore.
- Ruff for linting (config in `pyproject.toml`).

---

## Project structure

```
ROMulous/
├── CLAUDE.md                     # Project rules + session checklist
├── CHANGELOG.md
├── README.md
├── pyproject.toml
├── romulus.spec                  # PyInstaller spec (--onefile)
├── build-portable.ps1            # Windows portable-ZIP builder
├── scripts/
│   ├── generate_icon.py             # CD-ROM disc icon generator
│   ├── extract_system_logos.py      # One-shot logo extractor
│   ├── download_gamedb.py           # One-shot GameDB JSON downloader
│   └── download_libretro_metadat.py # One-shot libretro DAT downloader
├── .github/workflows/
│   ├── ci.yml                    # Lint + test on push/PR (windows-latest)
│   └── release.yml               # Tag-driven portable ZIP build
├── profiles/                     # 7 built-in destination profiles
├── systems/                      # System registry YAML (builtin.yaml)
├── data/
│   ├── dats/                     # 106 bundled No-Intro DAT files
│   ├── gamedb/                   # 42 bundled GameDB JSON snapshots
│   └── libretro-metadat/         # 294 bundled libretro DATs (7 dimensions)
├── docs/
│   ├── architecture.md           # This file
│   ├── TECHNICAL_PLAN.md         # Full implementation spec
│   ├── sync-design.md            # Destination sync engine spec
│   ├── import-design.md          # Import ROMs feature design (future)
│   ├── CREDITS.md
│   ├── ROM-FORMATS-REFERENCE.md
│   ├── ROM-DEDUP-METHODOLOGY.md
│   ├── ROM-LIBRARY-ANALYSIS-REPORT.md
│   └── sessions/                 # Per-build-session task lists (00–11 done)
├── src/romulus/
│   ├── __main__.py               # Entry point
│   ├── app.py                    # QApplication setup, DB init, log setup
│   ├── db/                       # SQLite connection, schema, queries, config
│   ├── core/
│   │   ├── scanner.py            # FS walk + L1/L2 + missing sweep + self-heal
│   │   ├── identifier.py         # L2 header extraction
│   │   ├── hasher.py             # SHA-1/CRC32 + header stripping
│   │   ├── dat_parser.py         # Logiqx XML DAT parser
│   │   ├── organizer.py          # Library reorganization
│   │   ├── exporter.py           # Destination profile export engine (incl. include_roms)
│   │   ├── sync.py               # 5-mode sync + 4-tier identity + O(N+M) tier-2 index
│   │   ├── dest_inventory.py     # Destination FS scanner + cache
│   │   ├── importer.py           # Staging-folder import (analyse + apply)
│   │   ├── scrub.py              # Reverse-direction DB ↔ disk verifier
│   │   ├── local_cover_finder.py # Disk-side cover discovery + linking
│   │   └── atomic.py             # tempfile.mkstemp + os.replace helpers
│   ├── metadata/
│   │   ├── __init__.py           # enrich_library + chain orchestrator
│   │   ├── libretro_metadat.py   # Bundled libretro-database (1st in chain)
│   │   ├── gamedb.py             # Bundled GameDB (2nd)
│   │   ├── hasheous.py           # Hasheous (3rd)
│   │   ├── launchbox.py          # LaunchBox XML (4th)
│   │   ├── screenscraper.py      # ScreenScraper (5th, opt-in)
│   │   ├── thegamesdb.py         # TheGamesDB (6th, quota-bound)
│   │   └── libretro.py           # libretro-thumbnails cover art
│   ├── models/                   # Pydantic data models + system registry
│   └── ui/
│       ├── main_window.py        # Main window, menu, toolbar
│       ├── system_sidebar.py     # Logo + name + count per system
│       ├── game_table.py         # Sortable, filterable QTableView
│       ├── detail_panel.py       # Cover, logo, metadata grid, description
│       ├── settings_dialog.py
│       ├── enrich_options_dialog.py
│       ├── cover_options_dialog.py
│       ├── scan_progress.py      # Quick / Heavy / DestScan progress
│       ├── enrich_progress.py
│       ├── local_cover_progress.py
│       ├── organize_preview.py
│       ├── export_dialog.py
│       ├── sync_preview.py
│       ├── sync_diff_progress.py    # "Computing diff…" between dest scan + preview
│       ├── import_dialog.py         # Import ROMs preview + apply
│       ├── scrub_dialog.py          # Verify Library bucketed-checkbox preview
│       ├── scrub_progress.py        # Verify Library analyse phase
│       ├── clean_missing_progress.py # Clean Missing Entries determinate progress
│       ├── per_system_summary_dialog.py # Post-Export / post-Sync breakdown table
│       ├── _grouped_tree.py         # Tri-state group header + right-click toggle mixin
│       ├── workers.py            # QThread workers (every long op)
│       ├── artwork/              # Bundled per-platform logos
│       ├── icons/                # CD-ROM disc app icon
│       └── themes/               # light / dark / wbm_classic .qss
└── tests/                        # pytest, 1004 collected (1003 + 1 skipped)
```

---

## Known limitations

These are intentional gaps in the current architecture, documented so
they don't surprise you.

1. **Single-library design.** Switching to a different library root
   offers to wipe the prior library's rows. There is no multi-library
   mode — by design.

2. **No DB migrations for pre-v0.3.0 databases.** ROMulus is pre-1.0
   with no shipped user base; users running an earlier alpha-state
   database should wipe `data/romulus.db` and let v0.3.0 rebuild it. A
   real migration framework will land when the project gets a real
   user base.

3. **ScreenScraper credentials are stored in plaintext in SQLite.** The
   database file is `chmod 0o600` on POSIX, so other local users
   cannot read it. On Windows, NTFS ACLs inherited from the install
   folder provide the same protection. Moving credentials into the
   system keyring is deferred to a future release.

4. **Organize plan history is not displayed in the UI.** Every applied
   organize plan is persisted to `organize_plans` as JSON. The "View
   history / undo last plan" dialog isn't built.

5. **Sync plan history is not displayed in the UI.** Same shape as
   above — `sync_plans` rows are persisted on every apply, but no
   history view exists yet.

6. **No Heavy Scan progress estimate.** Hashing speed depends so
   heavily on the filesystem (240 GB over SMB ≈ 80 min, the same
   library on a local SSD ≈ 5 min) that we don't show an ETA. The
   per-file progress callback is wired up; only the headline ETA is
   missing.

7. **Linux / macOS distribution is source-only.** The portable Windows
   build is the supported distribution for v0.3.0. Run from source on
   other platforms.

8. **Sync engine still walks the full destination for an artwork-only
   refresh.** The `Include ROMs` checkbox shortcut lives in the Export
   dialog (synchronous, no diff phase). If you reach for **Scan
   destination → Sync** with Include ROMs unchecked, the dialog
   actively disables that button — but a fully-symmetric "Sync
   artwork only" mode isn't implemented yet. Workaround: use Export
   with Include ROMs unchecked. The destination doesn't need pre-
   walking because `copy_artwork` size+mtime-compares per file.

9. **Per-system summary doesn't drill down to filenames.** The dialog
   shows counts (Copied: 30, Refused: 1, …) but not which specific
   files landed in which bucket. For diagnostics, grep
   `logs/romulus.log` — the exporter logs every skip with reason
   (`skip-unsupported`, `skip-already-present`, `refuse-overwrite`)
   and source/dest paths at DEBUG level.
