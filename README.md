# ROMulus

A local-first desktop ROM collection manager for retro game consoles. Scan,
identify, enrich with metadata and cover art, organize, and export your
collection to whatever device you actually play on — Anbernic handhelds,
Batocera setups, MiSTer FPGAs, Analogue Pocket, RetroPie, muOS, and Onion OS.

No server. No cloud account required. No external services to keep running.
Everything lives in a single SQLite database under the install folder
(`<install_dir>/data/`, with a `~/.romulus/` fallback if the install dir is
read-only) and a folder of cached cover art. Built with Python 3.12+,
PySide6 (Qt 6), and SQLite.

**Project status:** v0.3.0 (in development). The full
scan → identify → enrich → organize → export → **sync** pipeline works.
v0.3.0 adds destination sync (push/pull/two-way with 4-tier identity
matching), single-library cleanup (tombstone-missing + library-root
change handling), and a single-binary portable Windows build. See
[CHANGELOG.md](CHANGELOG.md) for per-release detail.

**License:** [Apache License 2.0](LICENSE).

**Built with LLM assistance.** Most of the implementation work was driven
by [Claude Code][claude-code] — visible in commit metadata (
`Co-Authored-By: Claude Opus ...` trailers), the per-session task lists
under [docs/sessions/](docs/sessions/), and the orientation file
[CLAUDE.md](CLAUDE.md) at the repo root. Architecture decisions, API
choices, and the design rules are owned by the human maintainer; the LLM
agent does the typing and the test-driven iteration. Worth knowing if
you're auditing the code or wondering why the commit history has unusual
co-author entries.

[claude-code]: https://docs.claude.com/en/docs/claude-code

---

## Why ROMulus?

If you keep a serious ROM library you've probably collected workflow scraps:
filename cleanup scripts, a half-finished ScreenScraper run, a OneDrive folder
that "almost matches" what your handheld expects. ROMulus replaces that with
a single desktop app that:

- **Stays local.** No phoning home, no upload, no required login. SQLite +
  files on disk, nothing else.
- **Bundles offline metadata.** Bundled snapshots of [libretro-database][lrdb]
  (~20 MB across 7 metadata dimensions × ~50 systems — genre, developer,
  publisher, release year, max players, ESRB, franchise) and
  [GameDB][gamedb] (~17 MB across 42 systems — canonical names, regions,
  publisher / release date for the systems that carry them) are
  consulted *before* any network call. Most cartridge-based titles fill
  out without ever touching the internet.
- **Online sources are opt-in per batch.** The Enrich Metadata dialog
  has an "Also try online metadata sources" checkbox; uncheck it and
  Hasheous, ScreenScraper, and TheGamesDB stay quiet.
- **Respects your files.** The Organizer and Exporter both show a preview
  before doing anything irreversible. Nothing is moved, renamed, or deleted
  until you explicitly confirm.
- **Treats hacks as first-class.** ROM hacks are never silently merged with
  their base titles.
- **Caches what's expensive.** SHA-1 hashes are stored in SQLite keyed by
  `(path, mtime, size)`. Rescans of unchanged files are nearly free.
- **Uses free cover art by default.** [libretro-thumbnails][libretro] needs no
  API key. ScreenScraper credentials are optional.

[libretro]: https://github.com/libretro-thumbnails/libretro-thumbnails
[lrdb]: https://github.com/libretro/libretro-database
[gamedb]: https://github.com/niemasd/GameDB

---

## Installation (portable, Windows)

The easiest way to run ROMulus on Windows is the portable ZIP:

1. Download `romulus-windows-x64.zip` from the [Releases][releases] page.
2. Extract it anywhere you like — `C:\Tools\ROMulus\`, a USB stick,
   wherever. There's no installer, no registry entry, nothing to uninstall.
3. Double-click `romulus.exe`.

After first launch the folder looks like this:

```
ROMulus\
  romulus.exe              (single self-contained binary — Python + PySide6
                            + every DLL packed inside)
  profiles\*.yaml          (destination profiles — edit freely)
  systems\*.yaml           (system registry — drop in extra YAMLs to extend)
  dats\*.dat               (bundled No-Intro DAT files, ~457k entries)
  gamedb\*.json            (bundled GameDB snapshots, 42 systems, ~17 MB)
  libretro-metadat\        (bundled libretro-database metadata DATs,
    <dimension>\*.dat       sorted by dimension — genre, developer,
                            publisher, releaseyear, maxusers, esrb,
                            franchise — ~50 systems, ~20 MB)
  data\                    (romulus.db + covers cache — everything live)
  logs\                    (rotating log file)
```

Want to back up your library? Zip the whole folder. Want to move it to
another PC? Copy the folder. Everything is local; nothing else on your
machine is touched.

You can also pin the data directory anywhere with the `ROMULUS_DATA_DIR`
env var — useful when you want the exe on a fast SSD but the SQLite DB
and cover cache on a roomier drive.

[releases]: https://github.com/Sphexi/ROMulous/releases

---

## Installation (from source)

If you'd rather run from source — required for macOS / Linux today, since
the portable binary distribution is Windows-only — clone the repo, create
a virtual environment, install in editable mode, and launch.

### Prerequisites

- **Python 3.12 or newer** (modern type hints, `match` statements)
- **Git**, to clone the repo
- A desktop environment capable of running Qt 6 apps (Windows 10/11, macOS
  12+, or a recent Linux distro with X11 or Wayland)

### Quick install

```bash
git clone https://github.com/Sphexi/ROMulous.git
cd ROMulous
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -e .

# Launch
python -m romulus
```

For development (adds pytest + ruff + PyInstaller):

```bash
pip install -e ".[dev]"
```

The first launch creates a `data/` folder under the repo root for the
SQLite DB and cover cache (falling back to `~/.romulus/` if the repo is
read-only). Nothing else on your system is touched.

---

## Quick start

The first time you run ROMulus the window is empty. The recommended workflow
is:

1. **File → Open Library...** Pick the root folder that contains your ROMs.
   ROMulus will save the path; you only need to do this once. If you've
   previously scanned a *different* folder, ROMulus prompts to wipe the
   stale entries — ROMulus treats one library folder at a time as the
   source of truth.
2. **Quick Scan** (toolbar). Walks the library, detects which console each
   ROM belongs to (via folder aliases and file extensions), and parses
   filenames for region/revision/disc/hack flags. Runs in seconds to a few
   minutes for tens of thousands of files. No hashing happens here. Files
   that have disappeared from disk since the last scan are *tombstoned*
   rather than dropped — the row is kept with `missing=1` so its metadata
   and enrichment survive a temporarily-unmounted network share.
3. **Heavy Scan** (toolbar). Computes SHA-1/CRC32 with header stripping
   and matches against No-Intro DATs for canonical naming. Bundled DATs
   cover ~106 systems out of the box. Subsequent Heavy Scans are
   nearly free thanks to the (path, mtime, size) hash cache.
4. **Enrich Metadata** (toolbar). Walks DAT-verified games and fills
   in genre / developer / publisher / release date / players / rating
   from a chain of sources, **local first**:
   - **libretro-database** (bundled, offline) — per-CRC32 metadata
     DATs from the upstream community-curated set.
   - **GameDB** (bundled, offline) — per-CRC32 JSON snapshots.
   - **Hasheous** (online, no key) — SHA-1 keyed.
   - **LaunchBox XML** (local, user-supplied via Settings → DATs).
   - **ScreenScraper** (online, opt-in via Settings → Metadata).
   - **TheGamesDB** (online, opt-in via Settings → Metadata; monthly
     quota tracked + respected).

   A pre-run dialog lets you tick "Also enrich fuzzy-matched games",
   "Re-attempt enrichment on games that already have metadata", and
   "Also try online metadata sources" (default on). Uncheck the last
   to run completely offline. The chain stops at the first source
   that has user-facing data for the game.
5. **Find Covers** (toolbar). Separate workflow from metadata. A
   dialog lets you tick "Search for local covers" (default on —
   walks the library tree for `.png/.jpg` files matching enrolled
   ROMs) and/or "Search online for covers" (default off — fetches
   libretro thumbnails for games still missing a cover). Either,
   neither, or both per run; the dialog disables OK when both are
   unchecked.
7. **Organize** (toolbar). Previews proposed library cleanups — alias folder
   merges, canonical-name renames, duplicate removal, cross-extension
   dedup. You see every action and approve them individually before
   anything moves.
8. **Export / Sync** (toolbar). Pick a destination profile (Batocera,
   RetroPie, MiSTer, Anbernic RGLauncher, etc.), pick a target folder,
   and run a one-shot **Export** (mirror the library to a fresh target)
   or a **Sync** with one of five modes:
   - **Push merge** — copy new local files to the destination, leave
     everything else alone (default).
   - **Push mirror** — make the destination match the library; orphan
     files there are deleted.
   - **Push wipe** — wipe the destination first, then push everything.
   - **Pull merge** — copy dest-only files back into the library and
     enrol them as fuzzy matches.
   - **Two-way** — the diff goes both directions; conflicts surface in
     the preview for resolution (skip / take local / take dest / newest).

   Every sync produces a preview with per-action counts and totals, a
   double-confirm before destructive actions, and per-action SAVEPOINT
   rollback if anything fails mid-run.
9. **Tools → Clean Missing Entries** removes tombstoned rows the user is
   confident are gone for good (and their dependent `hashes` /
   `dest_inventory` rows + orphan `games` rows).

Right-click any game in the table for **Add to Favorites** / **Add to
Collection...** / **Heavy Scan this game's ROMs** / **Enrich this
game** (opens the same options dialog scoped to the row) /
**Find covers for this game** (opens the cover options dialog scoped
to the row). Click a game to see its detail panel — cover art with
prev/next cycling, the platform's official logo, a compact key/value
grid of region/revision/size/SHA-1/genre/developer/publisher/release
date/etc., and the description (when one's available). The system
sidebar on the left shows each console's logo next to its name and
filters the table by console or by user-defined collection.

---

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
│  │   Settings                                           ││
│  │ Tools menu: Clean Missing Entries…                   ││
│  └──────────────────────────────────────────────────────┘│
└──────────────┬───────────────────────────────────────────┘
               │ signals/slots + QThread workers
┌──────────────┴───────────────────────────────────────────┐
│                    Core Engine                           │
│                                                          │
│  Scanner ──→ Identifier Pipeline ──→ SQLite DB           │
│  (+ missing  (L1 fuzzy, L2 header, L3 hash+DAT)          │
│   sweep +                                                │
│   self-heal)                                             │
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
│             .m3u, artwork)    two-way; 4-tier identity   │
│                               match; dest_inventory      │
│                               cache; SAVEPOINT rollback) │
│                                                          │
│  Cover Cache    (<install_dir>/data/covers/)             │
│  GameDB JSON    (<install_dir>/data/gamedb/)             │
│  libretro DATs  (<install_dir>/data/libretro-metadat/)   │
│  SQLite DB      (<install_dir>/data/romulus.db)          │
└──────────────────────────────────────────────────────────┘
```

Single-process desktop app. Long-running work (scanning, hashing, enrichment,
organize, export) runs on `QThread` workers with cooperative cancel. Each
worker opens its own SQLite connection inside `run()` — sqlite3 connections
are thread-bound by default, so the main-thread connection is never shared
across threads. All filesystem-mutating code routes through the atomic-write
helpers in `src/romulus/core/atomic.py` (`tempfile.mkstemp` + `os.replace`)
so a cancelled or killed worker can never leave a half-written file behind.

---

## Configuration reference

All configuration lives in the `config` table in
`<install_dir>/data/romulus.db` (with `~/.romulus/romulus.db` as a fallback
when the install folder isn't writable). There is no config file to
hand-edit — everything is editable through **File → Settings...** in the
app. The full set of keys, taken from `romulus.db.config.DEFAULT_CONFIG`:

| Key                              | Default                                    | Meaning                                                   |
|----------------------------------|--------------------------------------------|-----------------------------------------------------------|
| `library_path`                   | `""` (unset)                               | Root folder of your ROM library                            |
| `dat_paths`                      | `["dats"]` (JSON)                          | Folders scanned for No-Intro / Redump XML DAT files       |
| `cover_cache_path`               | `<data_dir>/covers`                        | Where libretro / Hasheous covers are cached on disk       |
| `screenscraper_username`         | `""`                                       | Optional ScreenScraper account username                    |
| `screenscraper_password`         | `""`                                       | Optional ScreenScraper account password (see Security)    |
| `thegamesdb_api_key`             | `""`                                       | Optional TheGamesDB API key (public or private); blank disables the provider |
| `thegamesdb_remaining_allowance` | `""`                                       | Diagnostic — last seen monthly quota counter from TGDB    |
| `theme`                          | `system`                                   | UI theme: `system`, `light`, `dark`, or `wbm_classic`     |
| `log_level`                      | `INFO`                                     | `DEBUG`, `INFO`, `WARNING`, or `ERROR` (live-applied)     |
| `default_view`                   | `table`                                    | Default view mode for the game list                        |
| `scan_threads`                   | `8`                                        | Worker threads used by Heavy Scan / hashing                |
| `last_scan_type`                 | `""`                                       | Diagnostic — last scan type that completed                 |
| `last_scan_time`                 | `""`                                       | Diagnostic — ISO timestamp of last scan                    |

`dat_paths` is JSON-encoded in storage. Use the **DATs** tab in Settings to
add or remove folders rather than editing the value directly.

The `ROMULUS_LOG_LEVEL` environment variable (`DEBUG` / `INFO` / `WARNING`
/ `ERROR`) takes precedence over the Settings value at startup, useful for
one-off diagnostics without touching the saved config. The
`ROMULUS_DATA_DIR` environment variable pins the data directory anywhere
on disk.

---

## Destination profiles

Profiles describe how to lay out an exported library for a specific device or
launcher. Each profile is a YAML file with a system-by-system map of folder
names and supported file extensions. Seven profiles ship in `profiles/`:

| Profile                     | Target                                                  |
|-----------------------------|---------------------------------------------------------|
| `batocera.yaml`             | Batocera (`/roms/<system>/`) with gamelist.xml          |
| `retropie.yaml`             | RetroPie (`~/RetroPie/roms/<system>/`)                  |
| `onionos.yaml`              | Onion OS for Miyoo Mini                                 |
| `muos.yaml`                 | muOS for ROCKNIX / RG-series handhelds                  |
| `mister.yaml`               | MiSTer FPGA (`/media/fat/games/<Core>/`)                |
| `analogue-pocket.yaml`      | Analogue Pocket via openFPGA cores                      |
| `anbernic-rglauncher.yaml`  | Anbernic stock OS / RGLauncher (`Roms/<system>/` + ES-DE-style gamelists) |

Profiles also specify an `artwork_filename_template` (`{stem}{ext}` by
default; `{stem}-image{ext}` for EmulationStation classic) so artwork copies
land at the filename the target expects, and a `gamelist_format`
(`emulationstation_xml`) for the per-system XML the device's frontend
consumes.

### Folder-name accuracy

The folder names baked into the bundled profiles are **best effort**. They
reflect the public docs / community conventions at the time of writing but
not every device has a single canonical naming scheme. Known judgement
calls:

- **MiSTer:** Atari 2600 and 7800 share the `ATARI7800` core folder; MAME
  uses generic `Arcade`. Verify against your specific build.
- **Analogue Pocket:** several systems (Virtual Boy, PCE-CD) assume the
  `agg23` openFPGA core layout. Reconfirm against your installed cores.
- **Onion OS:** folder casing follows the Onion docs at the time of
  writing. Onion has churned casing rules before.
- **RetroPie:** Sega Mega Drive / Genesis is `megadrive` (the RetroPie
  default), not `genesis`.

If a built-in mapping is wrong for your setup, copy the YAML file to
`~/.romulus/profiles/` and edit it — user profiles override built-ins by
filename.

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

User-supplied profiles in `~/.romulus/profiles/` are loaded alongside the
built-ins and show up in the Export dialog's profile dropdown.

---

## DAT files

ROMulus uses [No-Intro][nointro]-style Logiqx XML DAT files to match ROMs
by SHA-1/CRC32 and replace messy filenames with canonical names.

**Real No-Intro DATs ship with v0.2.0+** — 106 DAT files covering roughly
80 systems, ~457k total entries. They land in `dats/` next to the exe in
the portable build, and under `data/dats/` in a source checkout. Heavy
Scan works out of the box on common systems (Nintendo, Sega, Sony, Atari,
NEC, Sega CD, etc.).

To add more (Redump for disc-based systems, TOSEC, or newer No-Intro
revisions):

1. Visit [DAT-o-MATIC][datomatic] (the official No-Intro distribution
   site) or the Redump downloads page.
2. Download the `Standard` DAT for each system you care about.
3. Drop the `.dat` files into the `dats/` folder, OR pick a different
   folder via **Settings → DATs → Add folder...** — multiple DAT folders
   are supported and rescanned on startup.

[nointro]: https://no-intro.org/
[datomatic]: https://datomatic.no-intro.org/

---

## Metadata sources

| Source              | Cost      | Account required? | What you get                                  |
|---------------------|-----------|--------------------|-----------------------------------------------|
| libretro-thumbnails | Free      | No                 | Cover art (Named_Boxarts / Snaps / Titles)    |
| Hasheous            | Free      | No                 | Game metadata by SHA-1 / CRC32                |
| LaunchBox (offline) | Free      | No                 | Genres, descriptions, players, release dates  |
| ScreenScraper       | Free      | Yes (free account) | Extended metadata, region-specific descriptions |

The enrichment pipeline runs **libretro-thumbnails → Hasheous → LaunchBox**
on every game by default. ScreenScraper is queried only when valid
credentials exist in `config` and is purely additive — if you skip it,
enrichment still works.

To configure ScreenScraper:

1. Create a free account at <https://www.screenscraper.fr/>.
2. **Settings → Metadata** in ROMulus, enter your username and password.
3. Click **Test connection** to validate the credentials against
   ScreenScraper before saving (uses the values currently in the form,
   not the saved config).

---

## Development

### Setup

```bash
git clone https://github.com/Sphexi/ROMulous.git
cd ROMulous
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -e ".[dev]"
```

### Running tests

```bash
.venv/Scripts/python.exe -m pytest                # full suite
.venv/Scripts/python.exe -m ruff check src/ tests/
```

On Windows one test (`test_get_connection_restricts_db_file_permissions`)
is skipped because NTFS ACLs are inherited from the parent directory rather
than set via `chmod`. On Linux/macOS the same test runs and asserts that
the SQLite file lands at mode `0o600`.

### Project structure

```
ROMulous/
├── CLAUDE.md                     # Project rules + session checklist
├── CHANGELOG.md
├── README.md
├── pyproject.toml
├── romulus.spec                  # PyInstaller spec (--onefile)
├── build-portable.ps1            # Windows portable-ZIP builder
├── scripts/
│   └── generate_icon.py          # CD-ROM disc icon generator (QPainter)
├── .github/workflows/ci.yml      # Lint + test on push/PR
├── .github/workflows/release.yml # Tag-driven portable ZIP build
├── profiles/                     # 7 built-in destination profile YAMLs
├── systems/                      # System registry YAML (builtin.yaml)
├── data/
│   └── dats/                     # 106 bundled No-Intro DAT files
├── docs/
│   ├── TECHNICAL_PLAN.md         # Full design doc
│   ├── sync-design.md            # Destination sync engine spec
│   ├── ROM-FORMATS-REFERENCE.md
│   ├── ROM-DEDUP-METHODOLOGY.md
│   ├── ROM-LIBRARY-ANALYSIS-REPORT.md
│   └── sessions/                 # Per-build-session task lists + summaries
├── src/romulus/
│   ├── __main__.py               # Entry point: `python -m romulus`
│   ├── app.py                    # QApplication setup, DB init, log setup,
│   │                             # data-dir resolution, first-launch seeding
│   ├── core/
│   │   ├── scanner.py            # Filesystem walk + L1/L2 + missing sweep
│   │   ├── identifier.py         # L2 header extraction
│   │   ├── hasher.py             # SHA-1/CRC32 + header stripping
│   │   ├── dat_parser.py         # Logiqx XML DAT parser + match_hashes
│   │   ├── organizer.py          # Library reorganization (preview/commit)
│   │   ├── exporter.py           # Destination profile export engine
│   │   ├── sync.py               # 5-mode sync engine + 4-tier identity match
│   │   ├── dest_inventory.py     # Destination filesystem scanner + cache
│   │   ├── local_cover_finder.py # Disk-side cover discovery + linking
│   │   └── atomic.py             # tempfile.mkstemp + os.replace helpers
│   ├── db/                       # SQLite connection, schema, queries, config
│   ├── metadata/                 # libretro / Hasheous / LaunchBox / ScreenScraper
│   ├── models/                   # Pydantic data models + system registry loader
│   └── ui/
│       ├── main_window.py        # Main window, menu, toolbar
│       ├── system_sidebar.py     # System + Favorites + collections tree
│       ├── game_table.py         # Sortable, filterable QTableView
│       ├── detail_panel.py       # Cover, description, metadata, tags
│       ├── settings_dialog.py    # General / DATs / Metadata / Scan / Diagnostics
│       ├── scan_progress.py      # Quick / Heavy / DestScan progress dialogs
│       ├── organize_preview.py
│       ├── export_dialog.py
│       ├── sync_preview.py       # Sync preview + apply UI
│       ├── icons/cdrom.{png,ico} # CD-ROM disc app icon
│       ├── themes/*.qss          # light / dark / wbm_classic
│       └── workers.py            # QThread workers for async ops
└── tests/                        # pytest, 838 tests
```

### Continuous integration

`.github/workflows/ci.yml` runs `ruff check src/ tests/` and `pytest` on
every push to `main` and on every pull request. The workflow:

- Pins Python to 3.12.
- Installs the system libraries PySide6 wheels depend on (libegl1, libgl1,
  libxkbcommon0, libdbus-1-3, libfontconfig1, the libxcb-* stack).
- Sets `QT_QPA_PLATFORM=offscreen` so headless Qt widget tests work.
- Installs the project via `pip install -e ".[dev]"`.

Per the CI/CD Local Validation Rule in `CLAUDE.md`, the workflow's exact
commands are run locally on Windows before any release tag is pushed.
Current state: **838 tests passing, 1 skipped** (the POSIX-only chmod
test, skipped on Windows because NTFS ACLs are inherited).

### Code style

- Python 3.12+ — modern type hints (`str | None`), `match` statements.
- Type hints on every function signature, docstrings on every public
  class/method/function.
- Pydantic v2 for all data models crossing boundaries.
- `structlog` for structured logging.
- `httpx` as the only HTTP client.
- Ruff for linting (config in `pyproject.toml`).
- No global state, no scattered SQL — queries live in `db/queries.py`.

---

## Troubleshooting

**"No library configured" when I click Quick Scan.** Use **File → Open
Library...** first to point ROMulus at the root of your ROM folder.

**Quick Scan finished but the game table is empty.** The scanner only
shows files it could place in a known system folder. Check
`docs/ROM-FORMATS-REFERENCE.md` for the folder-name aliases each system
accepts. Either rename your folder to match (e.g. `SNES`, `snes`,
`Super Nintendo`) or add a YAML entry to `systems/` and restart.

**Quick Scan shows "N missing" in the status bar.** Files the scanner
expected to find (from a previous scan under the current library root)
weren't on disk this time. Reconnect the drive / remount the share and
re-scan — tombstoned rows un-tombstone automatically via the path-keyed
UPSERT. If they're really gone, **Tools → Clean Missing Entries…** drops
them along with their `hashes`, `dest_inventory`, and orphan `games`
rows.

**Switching libraries shows a "N entries from previous libraries will be
removed" prompt.** ROMulus treats one library folder at a time as the
source of truth (see [Key Design Rules](#key-design-rules) below). Pick
"Yes" to drop the previous library's rows so they don't show up as
duplicates; pick "No" to back out of the switch.

**DEBUG log level in Settings looks like nothing happens.** Set
`ROMULUS_LOG_LEVEL=DEBUG` in the environment before launching for
verbose breadcrumbs from the DAT parser, identifier, hasher, local
cover finder, exporter, organizer, sync, and every metadata client.
The env var beats the Settings value on startup. The log file is at
`<install_dir>/logs/romulus.log` (rotating, 5 MB × 3 backups).

**Heavy Scan completes but nothing got matched.** Check that the
relevant DAT file is in `dats/` — bundled DATs cover ~80 systems but
not everything (Atari Lynx, some computer platforms, etc. require user
DATs). DEBUG logs from `romulus.core.dat_parser` and
`romulus.core.identifier` show every hash compared and which DAT
entry won (or "no match found").

**Cover art doesn't appear after Enrich.** libretro-thumbnails keys
covers by the *canonical* No-Intro game name. A ROM that didn't pick
up a canonical name (no header, no hash match, no DAT entry) won't
fetch covers cleanly. Right-click → **Heavy Scan (this game)** to
upgrade the identifier confidence, then re-enrich.

**Sync preview shows nothing or shows everything as "identical".**
The sync engine's tier-2 identity matcher requires the destination
file's folder to map to the same system as the local ROM (so a Game
Boy `Pac-Man.gb` doesn't accidentally match a Game Boy Color
`Pac-Man.gbc`). If your destination uses non-standard folder names,
the profile YAML's `systems.<id>.folder` field needs to match what's
actually on disk.

**The app froze during a long operation.** Workers communicate progress
back to the UI via Qt signals at file-level granularity. If the UI
itself appears unresponsive (not just slow progress updates), please
open an issue with the worker name (Scan / Heavy Scan / Enrich /
Organize / Export / Sync), the approximate library size, and what was
happening at the time.

**ScreenScraper "Test connection" says invalid even though my
credentials work on the website.** ScreenScraper occasionally returns
non-JSON HTML during maintenance windows; the test treats that as
failure. Retry after a few minutes. If the failure persists, double-
check that your account has API access (some free tiers limit it).

**On Linux, `python -m romulus` crashes with `Qt: Session management
error` or a missing-library import error.** PySide6 wheels link
against a long list of X11 / Wayland / OpenGL libraries. Install them
via your package manager — the CI workflow in `.github/workflows/ci.yml`
has the full list for Debian/Ubuntu. (The portable Windows build is the
supported distribution for v0.3.0; Linux/macOS run from source only.)

---

## Key Design Rules

These are intentional and non-negotiable for the current architecture.
Listed here so the behavior doesn't surprise users.

1. **Single library at a time.** Switching `library_path` offers to wipe
   the previous library's rows. The scan sweep is library-agnostic — any
   row not visited this scan becomes `missing=1`. Multi-library is not
   supported.
2. **Tombstone before delete.** A vanished file becomes `missing=1`;
   the row stays in the DB so its enrichment / hash cache / metadata
   survives a temporarily-unmounted share. Re-scanning after reconnect
   flips `missing` back to 0. **Tools → Clean Missing Entries** is the
   only way to actually remove rows.
3. **Preview before mutation.** Organize and Sync both show a per-action
   preview with totals; nothing on disk changes until the user confirms.
   Destructive sync actions (mirror, wipe, two-way with deletes) require
   a second confirm prompt.
4. **Atomic writes only.** Every file write goes through
   `core/atomic.py` (`tempfile.mkstemp` + `os.replace`) so a cancelled
   or killed worker can never leave a half-written file behind.
   Per-action SAVEPOINT rollback keeps the DB consistent with the disk.
5. **Hacks are first-class.** ROM hacks are never silently merged with
   their base titles — `[h]` / `[T+]` markers anchor a distinct
   fuzzy_key suffix.

---

## Known limitations

These are intentional gaps in the current architecture, documented so
they don't surprise you.

1. **Single-library design.** Switching to a different library root
   offers to wipe the prior library's rows. There is no multi-library
   mode — by design, since the user explicitly asked for "one library
   at a time" behavior.

2. **No DB migrations for pre-v0.3.0 databases.** ROMulus is pre-1.0
   with no shipped user base; v0.3.0 dropped the migration helper for
   pre-v0.3.0 schemas in favor of "wipe `data/romulus.db` and let it
   rebuild on next launch". A real migration framework will land when
   the project gets a real user base.

3. **ScreenScraper credentials are stored in plaintext in SQLite.**
   The database file is `chmod 0o600` on POSIX (Linux/macOS), so other
   local users cannot read it. On Windows, NTFS ACLs inherited from
   the install folder provide the same protection. Moving credentials
   into the system keyring (`keyring` package) is deferred to a future
   release.

4. **Organize plan history is not displayed in the UI.** Every applied
   organize plan is persisted to the `organize_plans` table as JSON.
   The "View history / undo last plan" dialog isn't built.

5. **Sync plan history is not displayed in the UI.** Same shape as
   above — `sync_plans` rows are persisted on every apply, but no
   history view exists yet.

6. **No Heavy Scan progress estimate.** Hashing speed depends so
   heavily on the filesystem (240 GB over SMB ≈ 80 min, the same
   library on a local SSD ≈ 5 min) that we don't show an ETA. The
   per-file progress callback is wired up; only the headline ETA is
   missing.

7. **Linux / macOS distribution is source-only.** The portable Windows
   build (`romulus.exe` + side-by-side data folders, see
   [Installation](#installation-portable-windows)) is the supported
   distribution for v0.3.0. Run from source on other platforms.

---

## Credits

See [docs/CREDITS.md](docs/CREDITS.md) for the upstream services, open-source
libraries, ROM-preservation projects, console/launcher targets, and other
sources that ROMulus builds on or interoperates with.

## License

ROMulus is distributed under the [Apache License 2.0](LICENSE). All code
in this repository is original work authored by the human maintainer with
LLM assistance (see the LLM-assistance callout near the top of this file
and the `Co-Authored-By` trailers in the git history).

Third-party services and data sources used by ROMulus retain their own
licenses and usage terms — see `docs/CREDITS.md` for the full list, links,
and any usage notes.
