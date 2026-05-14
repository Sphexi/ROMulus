# ROM Collection Manager — Feature Inventory & Research

> **Purpose:** Competitive research across existing ROM managers to identify common features, gaps, and design direction for a local-first desktop ROM manager. This document is a starting point for Phase 1 requirements gathering.

---

## Tools Researched

| Tool | Type | Status | Stack | Notes |
|---|---|---|---|---|
| **RomM** | Self-hosted web app | Active (v4.x, AGPL-3.0) | Python/Docker/MariaDB | Most feature-rich; requires Docker + DB |
| **Retrom** | Self-hosted client-server | Active (v0.7.x, GPL-3.0) | Rust/Docker | Desktop clients + server; "self-hosted Steam" model |
| **RomVault** | Desktop app | Active | C#/.NET (Mono on Linux) | DAT-file focused; audit/rebuild tool |
| **clrmamepro** | Desktop app | Active (v4.x) | C++ (Windows) | Gold standard for MAME/arcade; steep learning curve |
| **Wii Backup Manager** | Desktop app | Legacy (Build 78) | Delphi/.NET (Windows) | Best UX reference for your vision; single-console |
| **WBFS Manager** | Desktop app | Legacy | C# (Windows) | Simpler Wii-only tool; drive management focus |
| **RetroMultiTools** | Desktop app | Active | C#/.NET 8 (cross-platform) | Newer entrant; 46 systems; header parsing + patching |
| **TinyWiiBackupManager** | Desktop app | Active | Rust (cross-platform) | Modern Wii BM rewrite; Flatpak available |
| **Igir** | CLI tool | Active (v4.3.x, GPL-3.0) | TypeScript/Node.js/Bun | **Closest feature match** — DAT-based sort/filter/extract/patch/report with 20+ destination tokens |

---

## Feature Categories

### 1. Library Scanning & Import

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| Scan folders recursively | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Auto-detect platform/system from path | ✅ | ✅ | Via DAT | Via DAT | Wii-only | ✅ (46 systems) |
| Parse filenames for tags (region, rev, etc.) | ✅ | — | Via DAT | Via DAT | ✅ | ✅ |
| Group multi-file games (multi-disc) | ✅ | ✅ | ✅ | ✅ | ✅ (multiboot) | — |
| Extract from archives (ZIP/RAR/7z) | — | — | ✅ | ✅ | ✅ (RAR) | — |
| Incremental/delta scan | ✅ | ✅ | ✅ | ✅ | — | — |
| Drag-and-drop import | — | — | — | — | — | — |

**Key takeaway:** Platform auto-detection from folder structure (e.g., `ROMS/SNES/`, `ROMS/GBA/`) is table stakes. Filename tag parsing (`(USA)`, `(Rev 1)`, `[!]`) is critical for organizing without metadata APIs.

---

### 2. Metadata Enrichment

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| Cover art download | ✅ | ✅ | — | — | ✅ (GameTDB) | — |
| Game descriptions/synopsis | ✅ | ✅ | — | — | ✅ (GameTDB) | — |
| Genre/developer/publisher | ✅ | ✅ | — | — | ✅ (wiitdb.xml) | — |
| Region info | ✅ | ✅ | Via DAT | Via DAT | ✅ | ✅ (header) |
| Release date | ✅ | ✅ | — | — | ✅ | — |
| Age ratings | ✅ | — | — | — | ✅ (ESRB) | — |
| Screenshots | ✅ | — | — | — | — | — |
| Video previews | ✅ | — | — | — | — | — |
| Alternative cover art (SteamGridDB) | ✅ | — | — | — | — | — |
| RetroAchievements integration | ✅ | — | — | — | — | — |
| HowLongToBeat data | ✅ | — | — | — | — | — |

**Metadata Sources Available:**

| Source | Type | Auth Required | Best For |
|---|---|---|---|
| **IGDB** (Twitch) | API | Yes (Twitch OAuth) | Broad game database, descriptions, screenshots |
| **ScreenScraper** | API | Yes (free account) | Most complete retro DB, covers, wheels, manuals, videos |
| **MobyGames** | API | Yes (API key) | Detailed credits, alternate titles |
| **LaunchBox** | Downloadable DB | No | Offline-first; bulk XML download |
| **GameTDB** | Downloadable XML | No | Nintendo consoles (Wii/GC/DS/3DS/Switch) |
| **SteamGridDB** | API | Yes (Steam login) | Alternative/custom cover art |
| **Hasheous** | API | No | Free hash→IGDB proxy; no API key needed |
| **Playmatch** | API | Needs IGDB key | Hash matching via No-Intro/Redump DATs |
| **TheGamesDB** | API | Optional (rate limits) | Good for newer/less-covered games |
| **No-Intro DATs** | Downloadable files | No | ROM verification; canonical naming |
| **Redump DATs** | Downloadable files | No | Disc-based ROM verification |
| **TOSEC DATs** | Downloadable files | No | Broadest coverage; preservation focus |

**Key takeaway:** For a local-first app, the most practical approach is: (1) hash-based matching via Hasheous (free, no API key), (2) ScreenScraper for covers/metadata, (3) LaunchBox/GameTDB XML downloads for offline enrichment. Avoid hard dependency on any single API.

---

### 3. ROM Verification & Integrity

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| CRC32 checksum | — | — | ✅ | ✅ | — | ✅ |
| MD5 hash | — | — | ✅ | ✅ | ✅ | ✅ |
| SHA1 hash | — | — | ✅ | ✅ | ✅ | ✅ |
| Hash-based game identification | ✅ (via Hasheous/Playmatch) | — | ✅ (via DAT) | ✅ (via DAT) | — | ✅ |
| DAT file import/audit | — | — | ✅ | ✅ | — | — |
| Missing ROM report | — | — | ✅ | ✅ | — | — |
| Bad dump detection | — | — | ✅ | ✅ | — | ✅ |
| Set completeness tracking | — | — | ✅ | ✅ | — | — |
| Verify data integrity on-demand | — | — | ✅ | ✅ | ✅ | ✅ |

**Key takeaway:** RomVault and clrmamepro are the gold standard here but are audit-focused power tools, not collection browsers. For your use case, hash-based identification (to match metadata) plus basic integrity checks (MD5/SHA1) would be sufficient without full DAT audit capability.

---

### 4. File Management & Organization

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| Rename files to standard naming | — | — | ✅ | ✅ | ✅ (GameTDB) | — |
| Move/copy to organized folder structure | — | — | ✅ | ✅ | ✅ | — |
| Format conversion (ISO↔CISO↔WBFS etc.) | — | — | — | — | ✅ | — |
| Compress/decompress (ZIP/7z) | — | — | ✅ | ✅ | — | — |
| Batch operations | — | — | ✅ | ✅ | ✅ | — |
| Duplicate detection | — | — | ✅ | ✅ | — | — |
| Region/variant filtering | — | — | ✅ | ✅ | — | — |
| Export game lists | — | — | ✅ | ✅ | ✅ (templates) | — |
| Transfer to external drive/SD card | — | — | — | — | ✅ | — |
| Split files for FAT32 (4GB limit) | — | — | — | — | ✅ | — |

**Key takeaway:** Wii Backup Manager's drive transfer and file management model is exactly what you want — the ability to organize on desktop, then copy a curated set to a microSD or external drive. The rename-to-standard-naming feature (using metadata to produce clean filenames) is hugely useful.

---

### 5. Browsing & UI

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| Cover art grid view | ✅ | ✅ | — | — | ✅ (side panel) | — |
| List/table view with columns | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Search/filter | ✅ | ✅ | ✅ | ✅ | ✅ (incremental) | ✅ |
| Filter by platform/genre/region | ✅ | ✅ | — | — | — | ✅ |
| Sort by name/date/size/rating | ✅ | ✅ | — | — | ✅ | — |
| Game detail panel (synopsis, art, info) | ✅ | ✅ | — | — | ✅ | ✅ |
| Favorites/collections/tags | ✅ | ✅ | — | — | — | ✅ |
| Dark/light theme | ✅ | ✅ | — | — | — | — |
| Responsive/mobile UI | ✅ | ✅ (web) | — | — | — | — |
| Big Picture / fullscreen mode | — | — | — | — | — | ✅ |
| Multi-language | ✅ | — | — | — | ✅ | ✅ (20 langs) |

**Key takeaway:** The browsing experience is where RomM and Retrom excel (beautiful grid UIs) but they're web apps. Wii Backup Manager nailed the desktop UX pattern: table list with cover art sidebar, game info panel, and tabbed source/destination views. That's the model to modernize.

---

### 6. Launching & Emulation

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| Built-in browser emulation | ✅ (EmulatorJS) | ✅ | — | — | — | — |
| Launch with external emulator | — | ✅ | — | — | — | ✅ |
| Emulator profile management | — | ✅ | — | — | — | — |
| Save state management | ✅ | ✅ | — | — | — | — |
| Cheat code support | — | — | — | — | — | ✅ |

**Key takeaway:** This is explicitly out of scope for v1 (you said you want to organize, not play), but worth noting for future. A simple "launch with configured emulator" feature would be low-effort and high-value down the road.

---

### 7. Data Storage & Portability

| Feature | RomM | Retrom | RomVault | clrmamepro | Wii BM | RetroMultiTools |
|---|---|---|---|---|---|---|
| Local SQLite/file-based DB | — | — | Custom cache | Custom cache | custom-titles.txt | SQLite |
| MariaDB/PostgreSQL | ✅ (MariaDB) | ✅ (Postgres) | — | — | — | — |
| Portable (no install) | — | — | ✅ | ✅ | ✅ | ✅ |
| Export/import library data | — | — | ✅ (DAT) | ✅ (DAT) | ✅ (templates) | — |
| Config file based | — | — | ✅ | ✅ | ✅ (INI) | ✅ |
| Cover cache on disk | ✅ | ✅ | — | — | ✅ | — |

**Key takeaway:** SQLite is the obvious choice for a local desktop app. Portable single-directory deployment (app + data in one folder) is a strong pattern from the legacy tools.

---

---

### 8. Igir — Deep Dive (Closest Comparable Tool)

Igir is the most architecturally relevant tool to this project. It solves many of the same problems — DAT-based ROM identification, multi-target output formatting, filtering, patching — but is CLI-only with no GUI, no metadata enrichment, and no browsing/collection management UX.

**What Igir does exceptionally well:**

- **DAT-first architecture.** Igir treats DATs (No-Intro, Redump, TOSEC, MAME) as the source of truth. ROMs are matched by CRC32/MD5/SHA1 against DAT entries. Unmatched files are flagged, not silently dropped.
- **20+ destination tokens.** Hardware-specific output path tokens (`{batocera}`, `{mister}`, `{onion}`, `{pocket}`, `{jelos}`, `{es}`, `{adam}`, `{minui}`, `{miyoocfw}`, `{retrodeck}`, `{romm}`, `{twmenu}`, `{spruce}`, `{funkeyos}`, etc.) map each system to the correct folder name for each target. This is exactly the destination profile concept we designed — Igir has already built the mapping tables.
- **ROM header handling.** Detects and optionally strips copier headers (SMC 512-byte headers, NES iNES headers, Atari Lynx headers, etc.) before hashing. Critical for accurate DAT matching.
- **1G1R filtering.** Built-in "1 Game 1 ROM" preference engine: `--single --prefer-language EN --prefer-region USA,WORLD,EUR,JPN` picks the best version of each game based on user preference ordering. This is what Retool does as a standalone DAT preprocessor, but Igir does it inline.
- **ROM patching pipeline.** Automatically discovers `.ips`, `.bps`, `.ups`, `.aps`, `.rup`, `.ppf`, `.xdelta` patches and applies them during copy/move. No separate patching step.
- **Archive handling.** Reads from and writes to ZIP/7z. Can extract, re-archive, or pass through. Understands that the ROM inside the archive is what gets hashed, not the archive itself.
- **Multi-disc playlist generation.** Automatically creates `.m3u` playlists for multi-disc games when outputting to a directory.
- **Fixdat generation.** Produces DAT files listing what's missing from your collection — can be consumed by other tools or used to track completeness.
- **Reporting.** CSV reports on collection status: what's present, what's missing, what's unmatched.
- **Filesystem-aware.** Handles FAT32 long-filename limits, case sensitivity differences, file splitting awareness.

**What Igir does NOT do (our differentiators):**

| Gap | Our Opportunity |
|---|---|
| No GUI at all | Desktop app with grid/list views, cover art, game detail panels |
| No metadata enrichment | Cover art, descriptions, genres, ratings from Hasheous/ScreenScraper/LaunchBox |
| No persistent library database | SQLite-backed collection with scan history, favorites, tags, collections |
| No browsing experience | Search, filter, sort, browse by system/genre/region/favorites |
| No cover art or visual media | Download, cache, and display covers/screenshots/box art |
| No incremental management | Every run is a full scan-and-sort pass; no "add 3 games and update" |
| No SD card transfer UX | Export is a CLI invocation, not a guided transfer workflow |
| No gamelist.xml generation | EmulationStation targets need separate scraping after Igir sorts |
| No duplicate browsing | Reports dupes in CSV but no interactive resolution UI |
| CLI learning curve | `npx igir copy extract --dat "*.dat" --input ROMs/ --output "sorted/{batocera}"` is powerful but intimidating |

**Key architectural takeaway:** Igir's destination token mapping tables are the gold standard for system→folder mappings. Our destination profiles should be **compatible with Igir's mappings** (same folder names for the same targets) so users can validate our output against Igir's. We could even consider importing Igir's `gameConsole.ts` mapping data directly as a reference.

**Igir as complementary tool rather than competitor:** The ideal workflow might be: use our app for browsing/organizing/enriching/curating, then either (a) export via our built-in destination profiles, or (b) point Igir at our organized library for users who prefer CLI-based output. The two tools serve different user needs (visual management vs. scripted pipeline) and could coexist.

---

## Gap Analysis: What's Missing From Existing Tools

1. **No modern cross-platform desktop ROM browser exists** that combines: local-only SQLite DB + metadata enrichment + cover art + file management + SD card transfer. RomM/Retrom are great but require server infrastructure. RomVault/clrmamepro are audit tools, not collection browsers. **Igir is the closest in capability but is CLI-only — it's the sorting/filtering engine without the visual management layer.**

2. **Igir has solved the destination mapping problem** with 20+ hardware tokens, but it's locked behind a CLI interface. Our app can adopt Igir's mapping data and wrap it in a guided export workflow with visual preview, selective filtering, and progress tracking.

3. **RetroMultiTools** is the closest GUI competitor — it's a modern .NET 8 cross-platform desktop app with header parsing, ROM inspection, and launching. But it's focused on ROM analysis/patching, not collection browsing and organization with rich metadata.

4. **The Wii Backup Manager UX model** — tabbed source/destination, cover art sidebar, metadata from online DB, file transfer to drive — has never been generalized to multi-console ROM management in a desktop app.

5. **Offline-first metadata** is underserved. Most tools require API keys and live internet. LaunchBox's downloadable XML database and GameTDB's XML files are great offline sources that nobody has combined into a desktop app with on-demand API enrichment as a secondary path.

6. **The missing middle ground:** Igir handles the plumbing (DAT matching, 1G1R, patching, sorting) and RomM handles the presentation (covers, descriptions, browsing, EmulatorJS). Nobody has combined both in a local desktop app. That's the gap.

---

## Proposed Feature Tiers (for Phase 1 discussion)

### Must-Have (v1)
- Scan folder structure, auto-detect platforms
- Parse filename tags (region, revision, dump status)
- SQLite local database
- Hash-based game identification (CRC32/MD5/SHA1)
- Metadata enrichment (covers, descriptions, genre, publisher)
- Cover art grid + list views with detail panel
- Search, filter by platform/genre/region, sort
- Favorites / custom collections
- Rename files to clean naming convention
- Copy/move organized files to target directory (SD card prep)
- Duplicate detection (same game, different dumps)
- Export game list (CSV, JSON, or templated)

### Nice-to-Have (v1.x)
- DAT file import for verification (No-Intro, Redump)
- Set completeness reporting ("you have 847/1024 SNES ROMs")
- Batch compress/decompress (ZIP/7z)
- Alternative cover art search (SteamGridDB)
- Region/variant filtering ("keep only USA, discard Japan dupes")
- Dark/light theme

### Future (v2+)
- Launch with external emulator
- Emulator profile management
- Save state management
- RetroAchievements integration
- Plugin/extension system
- Big Picture / fullscreen browsing mode

---

## Metadata & Cover Art Sources (revised)

### Cover Art — Free, No Account Required

| Source | Type | Account | Coverage | Notes |
|---|---|---|---|---|
| **libretro-thumbnails** | HTTP direct download | **None** | Excellent — all No-Intro systems | `https://thumbnails.libretro.com/{System}/{Named_Boxarts\|Named_Snaps\|Named_Titles}/{Game}.png`. Three image types: box art, in-game screenshot, title screen. Files use No-Intro canonical names with character replacements (`&*/:\<>?\|"` → `_`). **Primary cover source for Romulus.** |
| **LaunchBox** | Downloadable XML + media packs | None | Very good | Bulk download. Covers, screenshots, manuals. |
| **GameTDB** | Downloadable XML + images | None | Nintendo-only | Wii/GC/DS/3DS/Switch covers, discs, 3D box art. |

### Game Metadata (descriptions, genres, publishers)

| Source | Type | Account | Coverage | Notes |
|---|---|---|---|---|
| **Hasheous** | REST API | **None** | Good (IGDB proxy) | Hash-based lookup → returns IGDB metadata (descriptions, genres, release dates). Free, no API key. |
| **LaunchBox XML** | Downloadable DB | None | Very good | Full offline metadata: descriptions, genres, developers, publishers, release dates, ratings. ~200 MB download. |
| **GameTDB XML** | Downloadable DB | None | Nintendo-only | Descriptions, genres, ratings, player counts, accessories. |

### Optional (user-prompted, not required)

| Source | Type | Account | Coverage | Notes |
|---|---|---|---|---|
| **ScreenScraper** | REST API | Free account | Most complete | Best retro DB overall. Covers, descriptions, videos, manuals, wheels. Rate-limited (1 req/sec free). App prompts user: "Do you want to configure ScreenScraper for richer metadata?" If they decline, everything still works via the free sources above. |

### Enrichment Priority (in-app flow)

1. **DAT match** → gives canonical game name (from bundled No-Intro DATs)
2. **libretro-thumbnails** → use canonical name to fetch box art, screenshots, title screens (free HTTP, no API)
3. **Hasheous** → use SHA-1 hash to look up IGDB metadata (free, no key)
4. **LaunchBox XML** → offline fallback for metadata if Hasheous misses or is down
5. **ScreenScraper** → optional enrichment for gaps, user-configured

---

## Tech Stack (Decided)

- **Language:** Python 3.12+
- **GUI:** PySide6 (Qt 6)
- **Database:** SQLite
- **HTTP client:** httpx (async)
- **Config/models:** Pydantic v2
- **Logging:** structlog, JSON to stdout
- **Testing:** pytest, ruff (Standard tier)
- **Packaging:** pyproject.toml, .venv

---

---

## Destination Profiles — The Deployment Problem

Every emulation target expects ROMs in a different folder structure with different naming conventions for the same system. This is one of the biggest pain points in ROM management and a major differentiator if we solve it.

### The Core Problem

SNES ROMs go in:
- `ROMS/SNES/` — generic / user convention
- `roms/snes/` — Batocera, RetroPie, EmuDeck
- `ROMS/SFC/` — Onion OS (Miyoo Mini)
- `games/SNES/` — MiSTer FPGA
- `ROMS/Super Nintendo Entertainment System/` — some RetroArch setups
- `ROMS/Nintendo - Super Nintendo Entertainment System/` — libretro/No-Intro naming

Every system has this problem. A ROM collection organized for Batocera won't work on a Miyoo Mini without renaming every folder.

### Target Platform Folder Conventions

| System | Batocera | RetroPie | Onion OS (Miyoo) | MiSTer FPGA | muOS (Anbernic) | EmuDeck |
|---|---|---|---|---|---|---|
| NES | `nes` | `nes` | `FC` | `NES` | `NES` (user) | `nes` |
| SNES | `snes` | `snes` | `SFC` | `SNES` | `SNES` (user) | `snes` |
| Game Boy | `gb` | `gb` | `GB` | `GameBoy` | `GB` (user) | `gb` |
| Game Boy Color | `gbc` | `gbc` | `GBC` | `GameBoy` | `GBC` (user) | `gbc` |
| Game Boy Advance | `gba` | `gba` | `GBA` | `GBA` | `GBA` (user) | `gba` |
| N64 | `n64` | `n64` | — | `N64` | `N64` (user) | `n64` |
| NDS | `nds` | `nds` | `NDS` | — | `NDS` (user) | `nds` |
| Genesis/Mega Drive | `megadrive` | `megadrive` | `MD` | `Genesis` | `MD` (user) | `megadrive` |
| Master System | `mastersystem` | `mastersystem` | `MS` | `SMS` | `MS` (user) | `mastersystem` |
| Game Gear | `gamegear` | `gamegear` | `GG` | `GameGear` | `GG` (user) | `gamegear` |
| PlayStation | `psx` | `psx` | `PS` | `PSX` | `PS` (user) | `psx` |
| TurboGrafx-16 | `pcengine` | `pcengine` | `PCE` | `TGFX16` | `PCE` (user) | `pcengine` |
| Neo Geo | `neogeo` | `neogeo` | `NEOGEO` | `NeoGeo` | `NEOGEO` (user) | `neogeo` |
| Atari 2600 | `atari2600` | `atari2600` | `ATARI` | `Atari2600` | `ATARI` (user) | `atari2600` |
| Atari Lynx | `atarilynx` | `atarilynx` | `LYNX` | `AtariLynx` | `LYNX` (user) | `atarilynx` |

*muOS note: muOS doesn't enforce specific folder names — users create their own, but most guides use uppercase abbreviations. The key is that RetroArch cores on the device don't care about folder names, they care about content scan/playlist.*

### Additional Target Considerations

| Concern | Details |
|---|---|
| **Case sensitivity** | Linux-based targets (Batocera, RetroPie, muOS) are case-sensitive. MiSTer uses exFAT. Windows is case-insensitive. |
| **File format preferences** | Some targets prefer ZIP (saves space, RetroArch can read them), others need extracted ROMs. CD-based games need CUE/BIN or CHD depending on target. |
| **FAT32 4GB limit** | SD cards are often FAT32. Games over 4GB (Wii, PS2) need splitting or the target must use exFAT. |
| **BIOS files** | Different targets expect BIOS in different locations: `bios/` (Batocera), `MUOS/bios/` (muOS), `system/` (RetroArch), `games/<core>/` (MiSTer). |
| **Gamelist XML** | EmulationStation-based frontends (Batocera, RetroPie, RetroBat, EmuDeck-ES) use `gamelist.xml` per system folder for scraped metadata. The app could generate these. |
| **Playlist files** | RetroArch uses `.lpl` JSON playlist files. Generating these would let the target immediately recognize the collection. |
| **Multi-disc handling** | PS1 multi-disc games: some targets want `.m3u` playlists pointing to each disc. Others want each disc as a separate entry. CHD format handles this differently. |
| **Artwork/media paths** | EmulationStation expects `images/` and `videos/` subdirs within each system folder. RetroArch thumbnails go in `thumbnails/<system>/Named_Boxarts/`, `Named_Snaps/`, `Named_Titles/`. |

### Proposed Architecture: Destination Profiles

```
┌─────────────────────────┐
│   Internal Library DB   │  ← canonical representation
│   (SQLite, local)       │     platform-agnostic naming
│   SNES = "snes"         │     covers cached locally
│   NES = "nes"           │     hashes stored per ROM
└────────┬────────────────┘
         │
    ┌────┴────┐
    │ Export   │  ← user selects: target profile + destination path
    │ Engine   │     + optional filters (region, favorites only, etc.)
    └────┬────┘
         │
    ┌────┴──────────────────────────────────────────┐
    │              Destination Profiles              │
    ├───────────────┬──────────────┬─────────────────┤
    │  Batocera     │  Onion OS    │  MiSTer FPGA    │
    │  ─────────    │  ─────────   │  ─────────────  │
    │  roms/snes/   │  ROMS/SFC/   │  games/SNES/    │
    │  roms/nes/    │  ROMS/FC/    │  games/NES/     │
    │  roms/gba/    │  ROMS/GBA/   │  games/GBA/     │
    │  gamelist.xml │  (none)      │  (none)         │
    │  images/      │  Imgs/       │  (none)         │
    ├───────────────┼──────────────┼─────────────────┤
    │  RetroPie     │  muOS        │  EmuDeck        │
    │  ─────────    │  ─────────   │  ─────────────  │
    │  roms/snes/   │  ROMS/SNES/  │  Emulation/     │
    │  roms/nes/    │  ROMS/NES/   │   roms/snes/    │
    │  roms/gba/    │  ROMS/GBA/   │   roms/nes/     │
    │  gamelist.xml │  (none)      │  gamelist.xml   │
    └───────────────┴──────────────┴─────────────────┘
```

### Profile Definition (data model sketch)

Each profile would be a simple config (YAML/TOML/JSON) that maps:

```yaml
profile: batocera
description: "Batocera Linux (EmulationStation)"
case_sensitive: true
base_path: "roms"
artwork_path: "downloaded_media"  # or images/ per system
gamelist_format: "emulationstation_xml"
playlist_format: null
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
  psx:
    folder: "psx"
    extensions: [".cue", ".chd", ".pbp"]
    multi_disc: "m3u"
  # ...
```

### Export Workflow (user perspective)

1. User organizes collection in the app (scan → enrich → tag favorites → remove dupes)
2. User plugs in SD card or selects a destination folder
3. User picks a destination profile (e.g., "Batocera" or "Miyoo Mini (Onion OS)")
4. Optional: filter (favorites only, specific platforms, specific regions)
5. App clones the selected ROMs to the destination in the correct folder structure
6. App optionally generates gamelist.xml / .lpl playlists / artwork folders
7. App reports: "Copied 847 games across 12 systems to /media/sdcard/"

### Key Design Decisions for Phase 1

| Decision | Options |
|---|---|
| **Ship with built-in profiles?** | Yes — ship 5-6 common ones (Batocera, RetroPie, Onion OS, muOS, MiSTer, EmuDeck). Community can contribute more. |
| **User-editable profiles?** | Yes — YAML/TOML files in a `profiles/` directory. Power users can create custom ones. |
| **Copy vs. symlink vs. move?** | Default: copy. Option for hardlink (same filesystem) to save space. Never move from library. |
| **Artwork in export?** | Optional — checkbox to include covers/screenshots in the target's expected locations. |
| **Gamelist generation?** | Optional — for EmulationStation targets, generate gamelist.xml with metadata already in the DB so the target doesn't need to re-scrape. |
| **Incremental sync?** | v1: full copy. v1.x: diff-based sync ("only copy new/changed games since last export"). |

### Existing Tools That Do Partial Export

| Tool | What It Does | Gap |
|---|---|---|
| **Igir** (CLI, TypeScript) | DAT-based ROM sorter with 20+ hardware tokens (`{batocera}`, `{mister}`, `{onion}`, `{pocket}`, etc.), 1G1R filtering, header stripping, auto-patching, playlist generation, fixdat/reporting | CLI-only, no GUI, no metadata/covers, no gamelist.xml generation, no persistent library, no browsing. **But has the best destination mapping tables in the ecosystem.** |
| **Skraper** (Windows) | Scrapes metadata + generates gamelist.xml for ES-based systems | Windows-only, scraper not organizer, no export profiles |
| **ARRM** (Windows) | Advanced ROM Renaming Manager, gamelist editing | Windows-only, renaming focused |
| **Wii Backup Manager** | Transfers to Wii-formatted drives | Single console only |
| **RomM + Playnite/muOS plugins** | Can push games to clients | Requires server infrastructure |

Nobody combines: organize + enrich + export-to-profile in a single local desktop app.

---

*Research compiled May 2026. Ready for Phase 1 requirements gathering when you are.*
