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

**Project status:** v0.1.0 — first complete end-to-end release. The full
scan → identify → enrich → organize → export pipeline works. See
[CHANGELOG.md](CHANGELOG.md) for the full feature list and known limitations.

---

## Why ROMulus?

If you keep a serious ROM library you've probably collected workflow scraps:
filename cleanup scripts, a half-finished ScreenScraper run, a OneDrive folder
that "almost matches" what your handheld expects. ROMulus replaces that with
a single desktop app that:

- **Stays local.** No phoning home, no upload, no required login. SQLite +
  files on disk, nothing else.
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
  dats\*.dat               (bundled No-Intro DAT files)
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
the binary distribution is Windows-only for v0.2.0 — clone the repo,
create a virtual environment, install in editable mode, and launch.

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
   ROMulus will save the path; you only need to do this once.
2. **Quick Scan** (toolbar). Walks the library, detects which console each
   ROM belongs to (via folder aliases and file extensions), and parses
   filenames for region/revision/disc/hack flags. Runs in seconds to a few
   minutes for tens of thousands of files. No hashing happens here.
3. **Heavy Scan** *(disabled in the toolbar for v0.1.0; the underlying
   hashing engine ships but the trigger button is wired up in v0.2.0)*.
   Computes SHA-1/CRC32 with header stripping and matches against No-Intro
   DATs for canonical naming.
4. **Enrich** (toolbar). Pulls cover art from libretro-thumbnails and
   metadata from Hasheous / LaunchBox. Adds genres, descriptions, players,
   release dates, and cached PNG thumbnails. Free, no account required.
   ScreenScraper is queried only if you've supplied credentials in Settings.
5. **Organize** (toolbar). Previews proposed library cleanups — alias folder
   merges, canonical-name renames, duplicate removal, cross-extension
   dedup. You see every action and approve them individually before
   anything moves.
6. **Export** (toolbar). Pick a destination profile (Batocera, RetroPie,
   MiSTer, etc.), pick a target folder, optionally generate `gamelist.xml`
   for EmulationStation-based frontends and `.m3u` for multi-disc games,
   and ship a clean subset of your library to the device.

Right-click any game in the table for **Add to Favorites** / **Add to
Collection...** Click a game to see its detail panel (cover, description,
metadata, tags). The system sidebar on the left filters the table by
console or by user-defined collection.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     PySide6 UI                           │
│  ┌─────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ System  │  │  Game Table  │  │   Game Detail Panel │ │
│  │ Sidebar │  │  (sortable,  │  │   (cover, desc,     │ │
│  │         │  │   filterable)│  │    metadata, tags)  │ │
│  └─────────┘  └──────────────┘  └─────────────────────┘ │
│  ┌──────────────────────────────────────────────────────┐│
│  │ Toolbar: Quick Scan | Heavy Scan | Organize |        ││
│  │          Enrich | Export | Settings                  ││
│  └──────────────────────────────────────────────────────┘│
└──────────────┬───────────────────────────────────────────┘
               │ signals/slots + QThread workers
┌──────────────┴───────────────────────────────────────────┐
│                    Core Engine                           │
│                                                          │
│  Scanner ──→ Identifier Pipeline ──→ SQLite DB           │
│              (L1 fuzzy, L2 header,     │                 │
│               L3 hash+DAT)        Metadata Client        │
│                                   (libretro-thumbnails,  │
│  DAT Parser (bundled + user)       Hasheous, LaunchBox)  │
│                                                          │
│  Organizer (rename/merge/dedup     Export Engine         │
│             with preview/commit)   (dest profiles, copy, │
│                                    gamelist.xml, .m3u)   │
│                                                          │
│  Cover Cache (~/.romulus/covers/)                        │
│  SQLite DB   (~/.romulus/romulus.db)                     │
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

All configuration lives in the `config` table in `~/.romulus/romulus.db`.
There is no config file to hand-edit — everything is editable through
**File → Settings...** in the app. The full set of keys, taken from
`romulus.db.config.DEFAULT_CONFIG`:

| Key                       | Default                                    | Meaning                                                   |
|---------------------------|--------------------------------------------|-----------------------------------------------------------|
| `library_path`            | `""` (unset)                               | Root folder of your ROM library                            |
| `dat_paths`               | `["data/dats"]` (JSON)                     | Folders scanned for No-Intro / Redump XML DAT files       |
| `cover_cache_path`        | `~/.romulus/covers`                        | Where libretro / Hasheous covers are cached on disk       |
| `screenscraper_username`  | `""`                                       | Optional ScreenScraper account username                    |
| `screenscraper_password`  | `""`                                       | Optional ScreenScraper account password (see Security)    |
| `theme`                   | `system`                                   | UI theme: `system`, `light`, or `dark`                     |
| `default_view`            | `table`                                    | Default view mode for the game list                        |
| `scan_threads`            | `8`                                        | Worker threads used by Heavy Scan / hashing                |
| `last_scan_type`          | `""`                                       | Diagnostic — last scan type that completed                 |
| `last_scan_time`          | `""`                                       | Diagnostic — ISO timestamp of last scan                    |

`dat_paths` is JSON-encoded in storage. Use the **DATs** tab in Settings to
add or remove folders rather than editing the value directly.

---

## Destination profiles

Profiles describe how to lay out an exported library for a specific device or
launcher. Each profile is a YAML file with a system-by-system map of folder
names and supported file extensions. Six profiles ship in `data/profiles/`:

| Profile           | Target                                                  |
|-------------------|---------------------------------------------------------|
| `batocera.yaml`   | Batocera (`/roms/<system>/`) with gamelist.xml         |
| `retropie.yaml`   | RetroPie (`~/RetroPie/roms/<system>/`)                 |
| `onionos.yaml`    | Onion OS for Miyoo Mini                                 |
| `muos.yaml`       | muOS for ROCKNIX / RG-series handhelds                  |
| `mister.yaml`     | MiSTer FPGA (`/media/fat/games/<Core>/`)                |
| `analogue-pocket.yaml` | Analogue Pocket via openFPGA cores                 |

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

ROMulus uses [No-Intro][nointro]-style Logiqx XML DAT files to (eventually)
match ROMs by SHA-1/CRC32 and replace messy filenames with canonical
names. DAT parsing is implemented and unit-tested; what ships in
`data/dats/` for v0.1.0 is **two synthetic placeholder files** (one for
Game Boy, one for SNES, one game each). They exist to keep the parser
exercised — they are not a usable matching dataset.

**To get real DAT matching, you need to supply your own DATs.** No-Intro
DAT files are redistributed under terms that don't permit bundling in
third-party software, so you download them yourself and point ROMulus
at the folder:

1. Visit [DAT-o-MATIC][datomatic] (the official No-Intro distribution
   site).
2. Download the `Standard` DAT for each system you care about.
3. Unzip the `.dat` files into a folder anywhere on disk.
4. In ROMulus, open **Settings → DATs**, click **Add folder...**, and pick
   your DAT folder.

Multiple DAT folders are supported — ROMulus rescans them on startup and
when you confirm changes in the Settings dialog. Heavy Scan match rates
will be very low until you've added real DATs.

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
romulus/
├── CLAUDE.md
├── CHANGELOG.md
├── README.md
├── pyproject.toml
├── .github/workflows/ci.yml      # Lint + test on push/PR
├── data/
│   ├── dats/                     # Placeholder DAT files (see "DAT files")
│   └── profiles/                 # 6 built-in destination profiles
├── docs/
│   ├── TECHNICAL_PLAN.md         # Full design doc
│   ├── ROM-FORMATS-REFERENCE.md
│   ├── ROM-DEDUP-METHODOLOGY.md
│   ├── ROM-LIBRARY-ANALYSIS-REPORT.md
│   └── sessions/                 # Per-build-session task lists + summaries
├── src/romulus/
│   ├── __main__.py               # Entry point: `python -m romulus`
│   ├── app.py                    # QApplication setup, DB initialization
│   ├── core/                     # Scanner, identifier, hasher, DAT parser,
│   │                             # organizer, exporter, atomic-write helpers
│   ├── db/                       # SQLite connection, schema, queries, config
│   ├── metadata/                 # libretro / Hasheous / LaunchBox / ScreenScraper
│   ├── models/                   # Pydantic data models + system registry
│   └── ui/                       # PySide6 widgets, dialogs, QThread workers
└── tests/                        # pytest, ~415 tests
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
commands were run locally on Windows before the file was committed — they
pass with 415 tests collected, 1 skipped (the POSIX-only chmod test).

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
`Super Nintendo`) or wait for a future release to add a folder-mapping UI.

**Enrich button does nothing visible.** Enrichment only fires for games
the identifier pipeline has matched (via filename parsing in v0.1.0,
plus DAT matching once you add real DATs). Games that scanned as raw
ROM files with no system match get skipped.

**Cover art doesn't appear after Enrich.** libretro-thumbnails has a
specific naming convention — covers are keyed by the *canonical*
No-Intro game name. Without real DATs (see above) most lookups will
404. That's not an error, just a miss.

**Heavy Scan toolbar button is disabled.** Intentional for v0.1.0.
The hashing and DAT-matching engine is implemented and tested, but the
toolbar trigger is wired up in v0.2.0. The Organizer's "rename to
canonical" actions depend on real DATs being present.

**The app froze during a long operation.** Workers communicate progress
back to the UI via Qt signals at file-level granularity. If the UI
itself appears unresponsive (not just slow progress updates), please
open an issue with the worker name (Scan / Enrich / Organize / Export),
the approximate library size, and what was happening at the time.

**ScreenScraper "Test connection" says invalid even though my
credentials work on the website.** ScreenScraper occasionally returns
non-JSON HTML during maintenance windows; the test treats that as
failure. Retry after a few minutes. If the failure persists, double-
check that your account has API access (some free tiers limit it).

**On Linux, `python -m romulus` crashes with `Qt: Session management
error` or a missing-library import error.** PySide6 wheels link
against a long list of X11 / Wayland / OpenGL libraries. Install them
via your package manager — the CI workflow in `.github/workflows/ci.yml`
has the full list for Debian/Ubuntu.

---

## Known limitations

These are issues that exist intentionally in v0.1.0 and are tracked
for resolution in v0.2.0. They are documented here so they don't
surprise you.

1. **No real bundled DAT files.** `data/dats/` contains two synthetic
   placeholder files only. Real No-Intro DATs are not redistributable —
   see the [DAT files](#dat-files) section above for how to install
   them yourself. Heavy Scan match rates are low until you do.

2. **Heavy Scan trigger is disabled in the toolbar.** The hashing /
   DAT-matching engine ships and is fully tested. Wiring the toolbar
   button to it with the duration-warning dialog is a v0.2.0 task.

3. **ScreenScraper credentials are stored in plaintext in SQLite.**
   The database file is `chmod 0o600` on POSIX (Linux/macOS), so other
   local users cannot read it. On Windows, NTFS ACLs inherited from
   `~/.romulus/` (your home directory) provide the same protection.
   Moving credentials into the system keyring (`keyring` package) is
   deferred to v0.2.0 to avoid blocking v0.1.0 on a packaging
   complication.

4. **Organize plan history is not displayed in the UI.** Every applied
   organize plan is persisted to the `organize_plans` table as JSON,
   but there is no "View history / undo last plan" UI yet. The data
   model supports it; the dialog is v0.2.0.

5. **Folder-name guesses in built-in profiles.** See
   [Destination profiles → Folder-name accuracy](#folder-name-accuracy).

6. **No Heavy Scan progress estimate.** Hashing speed depends so
   heavily on the filesystem (240 GB over SMB ≈ 80 min, the same
   library on a local SSD ≈ 5 min) that we don't show an ETA. The
   per-file progress callback is wired up; only the headline ETA is
   missing.

---

## License

See repository for license terms.
