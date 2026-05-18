# Credits & References

ROMulus is original work but it sits on top of, talks to, or interoperates
with a lot of other projects. This file lists everything ROMulus depends
on, fetches data from, ships data from, or targets — with links so you can
go check those projects out and respect their own licenses, usage terms,
and attribution requirements.

Last updated: 2026-05-17 (v0.3.0 in development).

---

## Upstream services (data ROMulus fetches at runtime)

These are the metadata and cover-art services ROMulus's enrichment pipeline
talks to. Every one is free at the tier ROMulus uses; ScreenScraper is the
only one that requires an account, and it's opt-in.

| Service | Used for | License / Terms | Link |
|---|---|---|---|
| **libretro-thumbnails** | Cover art (Named_Boxarts, Named_Snaps, Named_Titles) | CC0 / Public Domain for the thumbnail collection itself; consult the upstream repo for individual contributor terms | <https://github.com/libretro-thumbnails/libretro-thumbnails> |
| **Hasheous** | Game metadata by SHA-1 / CRC32 | Free, no account; community-run | <https://hasheous.org/> |
| **LaunchBox Games Database (offline XML)** | Genres, descriptions, players, release dates | Free download; consult [LaunchBox terms](https://forums.launchbox-app.com/terms/) for redistribution | <https://gamesdb.launchbox-app.com/> |
| **ScreenScraper** | Extended metadata, region-specific descriptions, additional artwork (opt-in) | Free account required; rate-limited; see their [terms](https://www.screenscraper.fr/) | <https://www.screenscraper.fr/> |
| **TheGamesDB (TGDB)** | Name+platform metadata fallback when every cheaper source (libretro-database, GameDB, Hasheous, LaunchBox, ScreenScraper) missed. Tried last because of the strict monthly quota. | Free with user-supplied API key — public keys cap at ~1000 requests/month/IP, private lifetime keys cap at 6000 total. See [terms](https://thegamesdb.net/) | <https://thegamesdb.net/> |

ROMulus uses `httpx` for every outbound HTTP request, never logs request
or response bodies (URL + status only), and never sends user credentials
to any party other than the service they belong to.

---

## ROM-preservation projects (data ROMulus consumes)

These are the dump preservation communities whose DAT files ROMulus reads
and ships. ROMulus bundles 106 Standard No-Intro DATs for the systems it
recognizes; users can add Redump, TOSEC, or other sources via Settings →
DATs → Add folder.

| Project | What ROMulus uses | Link |
|---|---|---|
| **No-Intro** | Standard Logiqx XML DATs for cartridge-based systems (Nintendo, Sega, Atari, NEC, etc.) | <https://no-intro.org/> · <https://datomatic.no-intro.org/> |
| **Redump** | Logiqx XML DATs for disc-based systems (PS1, Saturn, Dreamcast, PC Engine CD) | <http://redump.org/> |
| **TOSEC** | Optional supplementary DATs for older / less-mainstream platforms | <https://www.tosecdev.org/> |
| **Logiqx XML DAT format** | The DAT schema ROMulus parses | <https://www.logiqx.com/> |
| **libretro-database** (Libretro / RetroArch project) | clrmamepro DAT files providing per-CRC32 genre, developer, publisher, release year, max players, and ESRB rating across ~50 systems. **First in the enrichment chain** — the broadest per-field coverage of any local source. Bundled under `data/libretro-metadat/<dimension>/` in dev clones and `libretro-metadat/` in portable releases. | <https://github.com/libretro/libretro-database> |
| **GameDB** (Niema Moshiri) | Per-console JSON snapshots used as the offline second-pass enrichment source (publisher, release date, canonical release name, CRC32). Bundled under `data/gamedb/` in dev clones and `gamedb/` in portable releases. Covers consoles libretro-database doesn't reach (PSX, GameCube, Wii, etc.). | <https://github.com/niemasd/GameDB> |

DAT files describe ROM dumps by SHA-1 / CRC32 hash; ROMulus uses them for
canonical naming and Heavy Scan identity matching. The DAT files
themselves are catalog data — they don't contain ROM bytes.

---

## Supported destination platforms (export / sync targets)

ROMulus ships destination profiles for the following devices and frontends.
Folder layouts and `gamelist.xml` conventions come from each project's
public documentation. Where mappings are best-effort, the README's
"Folder-name accuracy" section flags the known judgement calls.

| Target | Profile | Link |
|---|---|---|
| **Batocera** | `profiles/batocera.yaml` | <https://batocera.org/> |
| **RetroPie** | `profiles/retropie.yaml` | <https://retropie.org.uk/> |
| **Onion OS** (Miyoo Mini / Mini+) | `profiles/onionos.yaml` | <https://onionui.github.io/> |
| **muOS** (RG handhelds, ROCKNIX) | `profiles/muos.yaml` | <https://muos.dev/> |
| **MiSTer FPGA** | `profiles/mister.yaml` | <https://misterfpga.org/> |
| **Analogue Pocket** (openFPGA cores) | `profiles/analogue-pocket.yaml` | <https://www.analogue.co/pocket> |
| **Anbernic RGLauncher** (stock OS) | `profiles/anbernic-rglauncher.yaml` | <https://anbernic.com/> |
| **EmulationStation** (`gamelist.xml` format) | All ES-based profiles | <https://emulationstation.org/> · <https://es-de.org/> |

These are the targets ROMulus knows how to *write* output for. If you run
a different frontend, you can write a YAML profile in
`<install_dir>/profiles/` and it'll show up in the Export / Sync dropdown
(see README → Destination profiles → Creating a custom profile).

---

## Open-source libraries (bundled at runtime)

ROMulus's portable Windows build embeds these via PyInstaller's
`--onefile` mode. The source-only distribution pulls them via pip per
`pyproject.toml`.

| Library | Version pin | Used for | License | Link |
|---|---|---|---|---|
| **Python** | 3.12+ | Runtime | PSF | <https://www.python.org/> |
| **PySide6** (Qt 6) | ≥ 6.6 | GUI (windows, widgets, threading, painter) | LGPL v3 | <https://wiki.qt.io/Qt_for_Python> |
| **httpx** | ≥ 0.27 | HTTP client (sync + async) | BSD-3-Clause | <https://www.python-httpx.org/> |
| **Pydantic v2** | ≥ 2.5 | Data models + validation | MIT | <https://docs.pydantic.dev/> |
| **structlog** | ≥ 24.1 | Structured logging | Apache 2.0 / MIT | <https://www.structlog.org/> |
| **PyYAML** | ≥ 6.0 | YAML parser (profiles, system registry) | MIT | <https://pyyaml.org/> |
| **defusedxml** | ≥ 0.7 | XML billion-laughs protection (DAT + LaunchBox parsers) | PSF | <https://github.com/tiran/defusedxml> |
| **SQLite** | stdlib (sqlite3) | All persistent storage | Public Domain | <https://sqlite.org/> |
| **PyInstaller** | ≥ 6.0 (dev only) | Portable Windows build | GPL-with-exception | <https://pyinstaller.org/> |
| **pytest** | dev only | Test runner | MIT | <https://docs.pytest.org/> |
| **ruff** | dev only | Linter | MIT | <https://docs.astral.sh/ruff/> |

---

## Research & reference material

These are documents, articles, and forum posts that informed the design of
specific subsystems. They're not dependencies — they're sources we
consulted.

- **No-Intro naming convention** — the canonical "Region, Language,
  Revision, Status" tag scheme is the basis of `core/scanner.py`'s
  filename parser. <https://datomatic.no-intro.org/stuff/The%20Official%20No-Intro%20Convention%20(20071030).pdf>
- **GoodTools tag reference** — `[!]`, `[b]`, `[h]`, `[T+Eng]`, etc.
  GoodTools is deprecated but its tag conventions persist in user
  libraries. The scanner handles both No-Intro paren tags and GoodTools
  bracket tags.
- **TOSEC naming convention** — additional bracket tags the scanner
  recognizes (`[demo]`, `[proto]`, regional `[de]`/`[fr]`).
- **EmulationStation `gamelist.xml`** schema reference. ROMulus generates
  this format for Batocera / RetroPie / Anbernic / ES-DE targets.
- **libretro thumbnails naming convention** — covers are keyed by
  *canonical No-Intro name*, which is why Heavy Scan + DATs are needed
  for high cover hit rates.
- **`docs/ROM-DEDUP-METHODOLOGY.md`** in this repo — the three-layer
  identification pipeline (filename fuzzy → header → hash+DAT) is
  ROMulus's original synthesis of these conventions.

---

## LLM-assisted authorship

Most of the implementation work was driven by [Claude Code][claude-code]
under direction from the human maintainer. The session checklists under
[docs/sessions/](sessions/) reflect that workflow. Commit messages carry
`Co-Authored-By: Claude Opus ... <noreply@anthropic.com>` trailers as
attribution.

Technical spec, architecture decisions, API choices, scope calls, and the [design
rules](../CLAUDE.md#key-design-rules-non-negotiable) are owned by the
human maintainer.

[claude-code]: https://docs.claude.com/en/docs/claude-code

---

## Artwork & visual assets

| Asset | Source | License |
|---|---|---|
| **CD-ROM disc app icon** (`src/romulus/ui/icons/cdrom.{png,ico}`) | Original work — rendered via `scripts/generate_icon.py` using PySide6 `QPainter` | Apache 2.0 (with the rest of this repo) |
| **Theme stylesheets** (`src/romulus/ui/themes/{light,dark,wbm_classic}.qss`) | Original work | Apache 2.0 |
| **Console / handheld / computer platform logos** (`src/romulus/ui/artwork/systems/*.png`) | Extracted from the v2.1 *Recommended Versions (Normal, 1 Per Platform)* set in **Console Logos — Professionally Redrawn + Official Versions** by **Dan Patrick**, hosted on Internet Archive ([archive.org/details/console-logos-professionally-redrawn-plus-official-versions](https://archive.org/details/console-logos-professionally-redrawn-plus-official-versions)). Dark + Light Color variants only; renamed to `<system_id>-{dark,light}.png` by `scripts/extract_system_logos.py`. | Redraws by Dan Patrick; please credit the source above when redistributing. The original wordmarks and logos are trademarks of their respective platform holders (Nintendo, Sega, Sony, Microsoft, Atari, NEC, SNK, Bandai, etc.) and remain the property of those companies. |

When additional third-party artwork is added to the repo, each asset
will get an entry here noting its source, license, and whether it's
bundled or fetched at runtime.

---

## Contributing additions

If you spot a missing credit, a stale link, or a license note that needs
correcting, open an issue or PR. Particular care for:

- Re-distributable bundled assets (DATs, profiles, icons) where ROMulus's
  ship state needs to track the upstream license accurately.
- Service endpoints that have changed API terms.
- Destination platforms that have changed their folder conventions
  (Batocera's directory layout, MiSTer core renames, etc.).
