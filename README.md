# ROMulus

A local-first desktop ROM collection manager for retro game consoles. Scan,
identify, enrich with metadata + cover art, organize, and **sync** your
collection to whatever device you actually play on — Anbernic handhelds,
Batocera setups, MiSTer FPGAs, Analogue Pocket, RetroPie, muOS, Onion OS.

No server. No cloud account. No external services to keep running. SQLite +
files on disk, nothing else.

**Project status:** v0.3.0 (in development). The full
scan → identify → enrich → organize → export / sync pipeline works.
See [CHANGELOG.md](CHANGELOG.md) for the per-release breakdown.

**License:** [Apache License 2.0](LICENSE).

**Built with LLM assistance.** Architecture, API choices, and design rules
are owned by the human maintainer; most of the implementation typing was
driven by [Claude Code](https://docs.claude.com/en/docs/claude-code).
Commits carry `Co-Authored-By: Claude Opus ...` trailers and the historic
work breakdown lives under [docs/sessions/](docs/sessions/).

---

## Installation (portable, Windows)

The easiest way to run ROMulus on Windows is the portable ZIP:

1. Download `romulus-windows-x64.zip` from the [Releases][releases] page.
2. Extract it anywhere — `C:\Tools\ROMulus\`, a USB stick, wherever. No
   installer, no registry entry, nothing to uninstall.
3. Double-click `romulus.exe`.

After first launch the folder looks like this:

```
ROMulus\
  romulus.exe              (single self-contained binary)
  profiles\*.yaml          (destination profiles — edit freely)
  systems\*.yaml           (system registry — drop in extra YAMLs to extend)
  dats\*.dat               (bundled No-Intro DAT files)
  gamedb\*.json            (bundled GameDB metadata snapshots)
  libretro-metadat\        (bundled libretro-database metadata DATs)
  data\                    (romulus.db + covers cache — runtime state)
  logs\                    (rotating log file)
```

Backup = zip the folder. Move to another PC = copy the folder. Everything
is local; nothing else on your machine is touched.

The `ROMULUS_DATA_DIR` env var pins the data directory anywhere — useful
if you want the exe on a fast SSD but the SQLite DB and cover cache on a
roomier drive.

[releases]: https://github.com/Sphexi/ROMulous/releases

---

## Installation (from source)

Required for macOS / Linux today, since the portable build is Windows-only.

**Prerequisites:** Python 3.12+, Git, a desktop environment that can run
Qt 6 (Windows 10/11, macOS 12+, or a recent Linux distro with X11/Wayland).

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

For development (adds pytest + ruff + PyInstaller): `pip install -e ".[dev]"`.

On Linux you may need to apt-install the X11 / Wayland / OpenGL libraries
PySide6 wheels link against — see `.github/workflows/ci.yml` for the full
list of Debian/Ubuntu packages, although note that CI runs on
`windows-latest` now (see [docs/architecture.md](docs/architecture.md) for
why).

---

## Quick start

The first time you run ROMulus the window is empty. The recommended workflow:

1. **File → Open Library...** — point ROMulus at the root of your ROM
   folder. If you've previously scanned a different folder, ROMulus prompts
   to wipe the stale entries; ROMulus treats one library folder at a time
   as the source of truth.
2. **Quick Scan** (toolbar) — walks the library, detects which console
   each ROM belongs to, parses filenames for region/revision/disc/hack
   flags. Seconds to a few minutes for tens of thousands of files. No
   hashing. Files that vanished since the last scan are *tombstoned* (kept
   in the DB with `missing=1`) rather than dropped, so enrichment survives
   a temporarily-unmounted share. Right-click a system in the sidebar to
   scope the scan to just that system.
3. **Heavy Scan** (toolbar) — computes SHA-1/CRC32 with header stripping
   and matches against bundled No-Intro DATs for canonical naming.
   Re-runs of unchanged files are nearly free thanks to the hash cache.
4. **Enrich Metadata** (toolbar) — fills in genre / developer / publisher /
   release date / players / rating from a local-first chain of sources.
   The pre-run dialog has a "Also try online metadata sources" checkbox;
   uncheck it to run completely offline.
5. **Find Covers** (toolbar) — separate workflow with two independent
   checkboxes: "Search for local covers" (walks the library tree for
   `.png/.jpg` files matching enrolled ROMs) and "Search online for
   covers" (libretro-thumbnails fetch). Either, neither, or both per run.
6. **Organize** (toolbar) — previews proposed library cleanups (alias
   folder merges, canonical renames, duplicate removal). Every action is
   reviewed and approved before anything moves.
7. **Export / Sync** (toolbar) — pick a destination profile (Batocera,
   RetroPie, MiSTer, Anbernic, etc.), pick a target folder, and run a
   one-shot **Export** or a **Sync** with one of five modes (push
   merge/mirror/wipe, pull merge, two-way). Every sync produces a preview
   with per-action counts; destructive modes require a double-confirm.
8. **Tools → Clean Missing Entries** — removes tombstoned rows the user is
   confident are gone for good (and cascades to dependent rows).

**Right-click a game** in the table for: Add to Favorites / Add to
Collection / Heavy Scan this game / Enrich this game / Find covers for
this game / Reveal in Explorer / Delete this ROM. Right-click a system
in the sidebar for a system-scoped Quick Scan.

**Click a game** to see its detail panel — cover art with prev/next
cycling, platform logo, key/value metadata grid, and description.

---

## Settings

Everything is editable through **File → Settings...**. There is no config
file to hand-edit; configuration lives in the SQLite database.

Common tabs:

- **General** — library path, theme, default view, log level.
- **DATs** — DAT folders. Multiple folders supported; rescanned at startup.
- **Metadata** — ScreenScraper credentials (optional), TheGamesDB API key
  (optional). Both have **Test connection** buttons.
- **Scan** — worker thread count for Heavy Scan.
- **Diagnostics** — install dir, data dir, log path. Copy these into bug
  reports.

The `ROMULUS_LOG_LEVEL` environment variable (`DEBUG` / `INFO` / `WARNING`
/ `ERROR`) overrides the saved log level at startup — useful for one-off
diagnostics without touching Settings. The log file is at
`<install_dir>/logs/romulus.log` (rotating, 5 MB × 3 backups).

---

## Troubleshooting

**"No library configured" when I click Quick Scan.** Use **File → Open
Library...** first to point ROMulus at the root of your ROM folder.

**Quick Scan finished but the game table is empty.** The scanner only
shows files it could place in a known system folder. See
`docs/ROM-FORMATS-REFERENCE.md` for the folder-name aliases each system
accepts. Either rename your folder to match (e.g. `SNES`, `snes`,
`Super Nintendo`) or add a YAML entry to `systems/` and restart.

**Quick Scan shows "N missing" in the status bar.** Files the scanner
expected to find weren't on disk this time. Reconnect the drive / remount
the share and re-scan — tombstoned rows un-tombstone automatically. If
they're really gone, **Tools → Clean Missing Entries…** removes them.

**Switching libraries shows a "N entries from previous libraries will be
removed" prompt.** ROMulus treats one library folder at a time as the
source of truth. Pick "Yes" to drop the previous library's rows; pick "No"
to back out of the switch.

**DEBUG log level in Settings looks like nothing happens.** Set
`ROMULUS_LOG_LEVEL=DEBUG` in the environment before launching — the env
var beats the Settings value on startup.

**Heavy Scan completes but the dialog says "cache up to date".** Quick
Scan must run first to detect file changes; Heavy Scan only hashes ROMs
the cache flags as new or modified. If your library actually has new
files, run Quick Scan and then Heavy Scan again.

**Heavy Scan completes but nothing got matched.** Check that the relevant
DAT file is in `dats/` — bundled DATs cover ~80 systems but not
everything. Add user DATs via **Settings → DATs → Add folder...**.

**Cover art doesn't appear after Find Covers.** libretro-thumbnails keys
covers by the *canonical* No-Intro game name. A ROM that didn't pick up a
canonical name (no header, no hash match, no DAT entry) won't fetch
online covers cleanly. Right-click → **Heavy Scan (this game's ROMs)** to
upgrade the identifier confidence, then re-run Find Covers.

**Sync preview shows everything as "identical" or nothing matches.** The
sync engine's identity matcher requires the destination file's folder to
map to the same system as the local ROM. If your destination uses
non-standard folder names, the profile YAML's `systems.<id>.folder` field
needs to match what's actually on disk.

**ScreenScraper "Test connection" says invalid even though my credentials
work on the website.** ScreenScraper occasionally returns non-JSON HTML
during maintenance windows; the test treats that as failure. Retry after
a few minutes.

**The app froze during a long operation.** The end of Quick Scan now
disables the Cancel button while it finalises the DB (the
"Marking missing entries…" / "Linking ROMs to games…" /
"Finalising scan history…" labels are post-walk phases that can't be
safely cancelled). For genuine UI freezes, please open an issue with the
worker name, library size, and what was happening.

**Starting a second copy of ROMulus errors out about the log file.** Only
one instance can hold `logs/romulus.log` at a time. Close the first
instance, or set `ROMULUS_DATA_DIR` to a different folder for the second.

**On Linux, `python -m romulus` crashes with a missing-library import
error.** PySide6 wheels link against a long list of X11 / Wayland /
OpenGL libraries. Install them via your package manager — `ci.yml` has
the Debian/Ubuntu list (even though CI itself now runs on
`windows-latest`).

---

## Documentation

| Doc | What's in it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Architecture, design rules, schema, config reference, destination profile format, packaging, limitations |
| [docs/TECHNICAL_PLAN.md](docs/TECHNICAL_PLAN.md) | Full implementation spec — schema details, identifier pipeline, every subsystem in depth |
| [docs/sync-design.md](docs/sync-design.md) | Destination sync engine spec (modes, identity matcher, dest_inventory, sync_plans) |
| [docs/import-design.md](docs/import-design.md) | Import ROMs feature design (future) |
| [docs/forking-with-claude-code.md](docs/forking-with-claude-code.md) | How to fork this repo and continue building it with Claude Code |
| [docs/ROM-FORMATS-REFERENCE.md](docs/ROM-FORMATS-REFERENCE.md) | Extension tables, naming conventions, folder aliases |
| [docs/ROM-DEDUP-METHODOLOGY.md](docs/ROM-DEDUP-METHODOLOGY.md) | Three-layer identification pipeline methodology |
| [docs/CREDITS.md](docs/CREDITS.md) | Upstream services, open-source libraries, ROM-preservation projects, console/launcher targets, artwork sources |
| [CHANGELOG.md](CHANGELOG.md) | Per-release feature + fix history |
| [CLAUDE.md](CLAUDE.md) | Project rules and session checklist for LLM-assisted work |

---

## Contributing & development

```bash
git clone https://github.com/Sphexi/ROMulous.git
cd ROMulous
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows
pip install -e ".[dev]"

# Run tests + lint
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check src/ tests/
```

Current state: **918 tests passing, 1 skipped** (POSIX-only chmod test;
skipped on Windows because NTFS ACLs are inherited). CI runs on
`windows-latest`.

See [docs/architecture.md](docs/architecture.md) for code-style notes,
the project layout, the worker / threading model, and the design rules
that govern what changes are in-scope.

---

## License

ROMulus is distributed under the [Apache License 2.0](LICENSE). All code in
this repository is original work authored by the human maintainer with LLM
assistance.

Third-party services and data sources retain their own licenses and usage
terms — see [docs/CREDITS.md](docs/CREDITS.md) for the full list.
