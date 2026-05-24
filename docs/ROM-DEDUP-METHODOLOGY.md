# ROM Identification & Deduplication — Methodology

A self-contained artifact describing how to identify retro-console ROM files reliably, group them by logical game, and detect duplicates across regions, formats, and naming conventions.

This document is portable — it describes a **layered methodology** that any ROM-management project (or AI agent working on one) can adopt. A reference implementation in Python lives alongside this file in the source project; the design choices here apply regardless of language.

> **Why this matters.** A typical multi-console ROM library has 50,000+ files spread across 70+ system folders, with the same logical game often appearing 3-10 times under different names, formats, regions, and revisions. Filename-only matching is too brittle to dedupe reliably; binary hashing alone is too expensive to apply blindly. The right approach is a layered pipeline that uses cheap heuristics first and reserves the expensive operations for cases that need them.

---

## 1. The core problem

Two files might be:

- **Byte-identical but named differently** — `Zelda.zip` and `The Legend of Zelda (USA).nes` could be the exact same data inside.
- **Identical in content but with format-level wrapping** — `Game.smc` (with 512-byte SMC copier header) vs `Game.sfc` (raw, no header). Content is the same; bytes-on-disk differ by exactly 512.
- **Differently encoded but same logical ROM** — N64 `.z64` (big-endian), `.v64` (byte-pair-swapped), `.n64` (word-swapped). All three decompress to the same content under different byte orders.
- **Same name, different content** — two files both called `Final Fantasy.smc` could be NTSC and PAL releases (different ROMs, same logical game).
- **Different name, different content, but same logical game** — `Sonic the Hedgehog (USA, Europe).zip` and `Sonic the Hedgehog (Japan).zip` are different ROMs of the same game.
- **Same name, different game** — `Addams Family.smc` could refer to the original game, the Pugsley's Scavenger Hunt sequel, or the Values game, all of which have been called "Addams Family" by lazy renaming.

A robust pipeline must handle all of these cases without false positives (treating different games as duplicates) or false negatives (missing actual duplicates).

---

## 2. Three-layer architecture

| Layer | Identity signal | Cost | What it catches | What it misses |
|---|---|---|---|---|
| **1. Fuzzy filename** | Normalized filename | Trivial (microseconds) | Most variants where names are merely formatted differently | Anything where on-disk name is wrong (`zelda.zip`, `LinkAwakening_DX.gbc`); same-name-different-game cases |
| **2. Internal header** | Title field embedded in the ROM | Modest (a few KB read per file) | Headered/byte-swapped variants of the same ROM; renamed files where the on-cart title is correct | Formats with no title field (NES, PCE, MS/GG, raw arcade); CD-based games |
| **3. Hash + DAT** | CRC32 / SHA-1 lookup against a community-curated database | Expensive (full file read, ~80 min for 240 GB over SMB) | Authoritative identification of every byte-identical match against a known-good catalog | Bad dumps, hacks, homebrew, translations not in any DAT |

Run them in order. Each layer's output augments the previous layer's, and you can stop early if the answer is good enough for your use case.

---

## 3. Layer 1 — Fuzzy filename matching

### 3.1 Goal

Reduce a filename to a comparison key that's stable across cosmetic differences. If two files refer to the same logical game, their keys should match.

### 3.2 Normalization steps (apply in order)

1. **Drop the extension.** `Sonic.smc` → `Sonic`.
2. **Strip parenthesized and bracketed tag groups, recursively.** `Sonic (USA, Europe) (Rev 1) [!]` → `Sonic`. Tag groups frequently chain; iterate until no more match.
3. **Move trailing articles to the front, then strip.** `Addams Family, The` → `The Addams Family` → `Addams Family`. Apply to common articles in multiple languages: `The`, `A`, `An`, `Le`, `La`, `Les`, `El`, `Los`, `Las`, `Der`, `Die`, `Das`, `Il`, `Lo`, `Gli`. Strip both leading and trailing forms — both collapse to the bare title.
4. **Convert Roman numerals to Arabic.** `Final Fantasy VI` → `Final Fantasy 6`. Be conservative: only convert multi-letter Roman numerals (`II`, `III`, `IV`, `VI`-`XV`). Skip single letters (`I`, `V`, `X`, `L`, `C`, `D`, `M`) — they collide with common words.
5. **Strip trailing version suffixes — but only obvious ones.** Patterns like `v1.1`, `v2.0a`, `Rev 02`, `1.0a` should be removed. **Do NOT strip bare integers** at the end (`Final Fantasy 6`, `Aero the Acro-Bat 2`) — those are sequel numbers, not versions. The acceptable regex is something like:
   ```
   (v\d+(\.\d+[a-z]?)?     # v1, v1.1, v2.0a
   |\d+\.\d+[a-z]?         # 1.1, 2.0A — must have a dot
   |rev\s*\d+)\s*$
   ```
6. **Lowercase.** Case-insensitive comparison.
7. **Strip all non-alphanumerics.** Collapses separator drift (`Acro-Bat`, `Acro_Bat`, `Acro Bat`, `AcroBat` all become `acrobat`).

Result: a compact alphanumeric key like `aerotheacrobat2` or `addamsfamily`.

### 3.3 What this catches

- "Addams Family, The" / "The Addams Family" / "Addams Family" → same key
- "Aero the Acro-Bat 2" / "Aero The Acro-bat II" / "Aero-the-Acro_Bat_II" → same key
- "Sonic the Hedgehog (USA, Europe).md" / "Sonic the Hedgehog.zip" → same key

### 3.4 What it misses

- A file randomly renamed `zelda.smc` (loses too much info to recover)
- Ambiguity between sequels with mistitled files (`Addams Family.smc` could be game 1 or game 2)
- Cases where the canonical name itself differs from the filename (e.g. `BoF3.smc` vs `Breath of Fire III`)

---

## 4. Layer 2 — Internal-header title extraction

### 4.1 Goal

Read the title embedded inside the ROM file itself, which is more authoritative than any filename. Use it (when present) as the comparison key instead of the filename.

### 4.2 Header locations by format

| Format | Title offset | Title length | Notes |
|---|---|---|---|
| **SNES (.smc/.sfc/.fig/.swc)** | `0x7FC0` (LoROM) or `0xFFC0` (HiROM) | 21 bytes ASCII | Strip 512-byte SMC copier header first if `len(file) % 1024 == 512`. Try both LoROM and HiROM offsets; pick the one with higher printable-ASCII ratio. |
| **N64 (.z64/.n64/.v64)** | `0x20` (after byte-swap to z64) | 20 bytes ASCII | Detect endianness by magic at offset 0: `80 37 12 40` = z64, `37 80 40 12` = v64 (halfword-swapped), `40 12 37 80` = n64 (word-swapped). Byte-swap to z64 form before reading. |
| **Mega Drive / Genesis (.md/.gen/.bin)** | `0x150` (overseas) and `0x120` (domestic) | 48 bytes ASCII | Magic "SEGA MEGA DRIVE" or "SEGA GENESIS" appears near `0x100` — check that first. `.smd` is interleaved (Super Magic Drive copier format) and needs deinterleaving before this works. |
| **Game Boy / Color (.gb/.gbc)** | `0x134` | 11-16 bytes ASCII | Newer cartridges use 11 title bytes + 4 manufacturer + 1 CGB flag. Trim at first null/non-printable byte. |
| **GBA (.gba)** | `0xA0` | 12 bytes ASCII | Followed by 4-byte gamecode at `0xAC`. |
| **DS (.nds)** | `0x00` | 12 bytes ASCII | Followed by 4-byte gamecode at `0x0C`. |
| **PSP / PS3** | inside `PARAM.SFO` | varies | Parse the SFO key-value blob, read the `TITLE` field. |
| **PSX / Saturn / Dreamcast** | inside `IP.BIN` / `SYSTEM.CNF` at sector 0 of the disc image | varies | Read the first 32 KB of the disc; the title is in a fixed-offset section of the ISO9660 boot record area. |

### 4.3 Formats without internal titles

These need to fall back to filename or hash:

- **NES (.nes)** — iNES header has mapper/PRG/CHR sizes only; no title.
- **PC Engine (.pce)** — no standard title.
- **Master System / Game Gear** — TMR SEGA magic but no title.
- **Atari 2600/5200/7800** — no title in cartridge.
- **Most arcade systems** — title comes from the romset name (filename), not from inside.

### 4.4 Implementation notes

- Read only the bytes you need (the first 64 KB is enough for every cartridge format above). Don't read entire files — over a network share, that turns a 30-second pass into a 30-minute one.
- Wrap each header read in a try/except — corrupted files, partial dumps, or unusual variants will fail; treat as "no internal title" and fall back to filename.
- Decode as ASCII with `errors="replace"`; trim whitespace, nulls, and control characters; collapse internal whitespace runs.
- For the comparison key, run the extracted internal title through the same fuzzy normalizer from Layer 1. This way `THE ADDAMS FAMILY` (internal) and `Addams Family, The (USA)` (filename) end up with the same key.

### 4.5 What this catches that Layer 1 missed

- **SMC vs SFC of same ROM:** byte-different on disk (header), same internal title → same key.
- **N64 byte-order variants:** different bytes on disk, same internal title after byte-swap → same key.
- **Renamed files where the on-cart title is preserved:** a file mislabeled `random.smc` reveals its true identity from the header.

### 4.6 Caveats

- Some games legitimately have the same internal title for sequels or revisions (the game studio reused the title field). Internal-title matching can over-collapse in those cases.
- Internal titles are sometimes wrong (developer typos, prototype titles). Real example: SNES Addams Family ROMs in one collection had swapped internal titles ("Addams Family 1.smc" reported "ADDAMS FAMILY 2"; "Addams Family 2.smc" reported "ADDAMS FAMILY"). Trust hashes, not headers, when both disagree.
- For headered-but-not-detected formats, you'll silently fall back to filename; instrument your code so you can see the header-extraction success rate.

---

## 5. Layer 3 — Hash + DAT lookup (the gold standard)

### 5.1 What a DAT file is

A **DAT** (DAT file, originally from RomCenter/clrmamepro) is an XML database describing every known-good dump for a console. The dominant schema is **Logiqx XML**:

```xml
<datafile>
  <header>
    <name>Nintendo - Super Nintendo Entertainment System</name>
    <description>...</description>
    <version>20240501-091532</version>
  </header>
  <game name="Aero the Acro-Bat (USA)">
    <description>Aero the Acro-Bat (USA)</description>
    <rom name="Aero the Acro-Bat (USA).sfc"
         size="1048576"
         crc="3D0B7FFC"
         md5="8D27B1F1F0CC1F8C1A9F0B6D6E27C1F1"
         sha1="F23A8B9C72A3B5D8E1F2A3B4C5D6E7F8A9B0C1D2"/>
  </game>
  ...
</datafile>
```

Every entry carries:

- **Filename** — the canonical filename per the naming spec (No-Intro / Redump / TOSEC).
- **Size** — exact byte count of the canonical dump.
- **CRC32** — fast 32-bit hash; collision-prone above ~64K entries, sufficient as a first index.
- **MD5** — 128-bit; effectively collision-free for ROM databases.
- **SHA-1** — 160-bit; the modern standard. Used by tools like igir.

### 5.2 The major DAT sources

| Source | Coverage | URL |
|---|---|---|
| **No-Intro** | Cartridge consoles, handhelds, digital store dumps | https://datomatic.no-intro.org/ |
| **Redump** | Optical-media consoles (CD, DVD, GD-ROM, BD); track-level | http://redump.org/ |
| **TOSEC** | Home computers, obscure systems, magazine cover-disks | https://www.tosecdev.org/ |
| **MAME** | Arcade | Built into MAME (`-listxml`) |
| **FBNeo** | Arcade subset | https://github.com/finalburnneo/FBNeo |

No-Intro's DATs are typically per-system XML files of 100 KB - 5 MB. A complete cartridge-console set is ~50 MB. Update cadence is days to weeks per system.

### 5.3 The hashing pipeline

For each ROM in your collection:

1. **Apply per-format normalization BEFORE hashing.** If you skip this, your hashes won't match the DAT entries:
   - **SNES**: detect 512-byte SMC copier header (`len(file) % 1024 == 512`), strip it, hash the rest. Now both `.smc` and `.sfc` of the same ROM hash identically.
   - **N64**: detect endianness by magic, byte-swap to z64. Now `.z64`, `.v64`, `.n64` of same ROM all hash identically.
   - **NES**: detect 16-byte iNES header (magic `4E 45 53 1A` = `"NES\x1a"`), strip it before hashing. Modern (post-2018) No-Intro NES DAT entries reference *unheadered* ROM content; most dumps in the wild carry the header, so without stripping the match rate is catastrophically low (we measured ~1% match before the fix, expected ~80%+ after).
   - **`.zip` archives**: extract the inner ROM file and hash THAT, not the zip. (Zip's stored CRC32 also works for matching but only if the compression method is store/deflate; safer to extract.) **Apply the per-format normalization above to the *inner* file's bytes** — a zipped headered NES ROM still needs the iNES strip; a zipped `.smc` still needs the SMC strip.
   - **`.7z` archives**: same — extract and hash content.
   - **Multi-track CDs (.cue + .bin)**: hash each .bin track separately; Redump DATs are per-track.
   - **`.chd`**: a MAME-specific compressed CD/HDD format. Either decompress with `chdman extractcd` and hash the resulting tracks, or skip CHD-format files for DAT matching (they require their own toolchain).
2. **Stream the file through CRC32 + SHA-1 in a single pass.** Don't read it twice. In Python:
   ```python
   crc = 0
   sha1 = hashlib.sha1()
   with open(path, "rb") as f:
       while chunk := f.read(1 << 20):
           crc = zlib.crc32(chunk, crc)
           sha1.update(chunk)
   ```
3. **Parallelize.** Hashing is I/O-bound, especially over SMB/NAS. A `ThreadPoolExecutor` with 8-24 workers gives a substantial speedup on networked storage; on local SSDs the speedup is more modest.
4. **Write results to an intermediate CSV** as they complete. This way a long run is restartable: if the network drops or you cancel, you don't lose what you already hashed.

### 5.4 The matching pipeline

Build two lookup tables from the DATs:

```python
by_sha1: dict[str, DatEntry]      # SHA-1 -> entry (effectively unique)
by_crc32: dict[str, list[DatEntry]]  # CRC32 -> list (collisions possible)
```

Walk your hash CSV:

1. Look up by SHA-1 first. If found, you have an authoritative `(canonical_name, region, language, revision)` identification.
2. If SHA-1 lookup fails, try CRC32. If exactly one entry matches, use it (`match_method=crc32_unique`). If multiple entries share the CRC32, treat as ambiguous — fall back to filename heuristics.
3. If neither matches, write the file to an "unmatched" report. Unmatched usually means: hack, homebrew, translation patch, bad dump, or DAT not loaded for that system.

### 5.5 Cross-format duplicate detection

Once every file has a SHA-1 (and potentially a canonical name), grouping by SHA-1 finds **byte-identical duplicates regardless of folder, filename, format, or container**. Group sizes > 1 are dups in the strictest sense.

For a slightly broader notion of "duplicate", group by `dat_game_name` instead — that catches the same game across regions/revisions.

### 5.6 Cost

For a ~240 GB cartridge+disc collection on a gigabit LAN over SMB:

- Sequential: 60-120 minutes (network throughput-limited).
- 16 threads parallel: 30-60 minutes (can saturate a 2.5 Gbps link — be considerate of other clients of the share).
- 8 threads parallel: 45-75 minutes (sweet spot for "don't hammer the SMB server" — observed network usage ~150 MB/s on a 2.5 Gbps link).
- Run on the storage host directly (not via SMB): 5-15 minutes.

The match phase after hashing is fast — XML parsing + dict lookups, completes in seconds for a million-entry DAT set.

---

## 6. What this stack DOES NOT solve

- **Region/revision rollup.** Two files matching No-Intro entries `Sonic (USA)` and `Sonic (Europe)` are byte-different but the same logical game. Picking a "canonical one" (e.g. "USA > Europe > Japan, newest revision") is a separate decision layer. Tools like [Retool](https://github.com/unexpectedpanda/retool) preprocess DATs to apply such rules and produce a "best-pick-per-game" filtered DAT.
- **Multi-disc games.** A 3-disc PSX game shows up as 3 DAT entries with 3 hashes. Treat them as one logical game by generating an `.m3u` playlist file.
- **CD audio tracks.** Redump DATs hash each track individually. A `.bin`+`.cue` set is "the same as" a `.chd` only if every track hashes the same; assert track-level equivalence, not just disc-level.
- **Hacks, homebrew, translations.** These are first-class artifacts but rarely in the official DATs. Use the [Hack-DAT-base](https://github.com/HoraceAndTheSpider/Hack-DAT-Base) project or a community-maintained hack DAT for those. Treat them as *distinct titles*, not duplicates of the originals.
- **Bad dumps.** No-Intro and Redump exclude bad dumps from their DATs by policy. If a file doesn't match any DAT entry it could be a bad dump; flag, don't delete.

---

## 7. Established tooling alternatives

If you don't need a custom pipeline, several mature tools implement most of the above:

| Tool | Strength |
|---|---|
| **[igir](https://igir.io/)** | Cross-platform (Node.js) CLI. Modern, scriptable, handles DAT download, hashing, and library reorganization in one pass. Best modern choice for automation. |
| **[clrmamepro](https://mamedev.emulab.it/clrmamepro/)** | The original. Proprietary Windows GUI. Universal but steep learning curve. |
| **[RomVault](https://www.romvault.com/)** | Fast, large-collection-friendly, TOSEC-compatible. Commercial license for advanced features. |
| **[Retool](https://github.com/unexpectedpanda/retool)** | Pre-processes DATs to produce "best per game" filtered DATs (region/revision/language preferences). Output then fed to clrmamepro/RomVault. |
| **[SabreTools](https://github.com/SabreTools/SabreTools)** | DAT manipulation library that powers many other tools. |

Use these when you want hash-based identification but don't need to embed it in your own workflow.

---

## 8. Reference implementation in this project

The project this document originated from contains four scripts implementing the layered pipeline. Each is read-only by default and writes its output to a `_dedup_reports/` folder.

| Script | Layer | Purpose |
|---|---|---|
| `rom_dedup.py` | Layer 1 (filename) | Initial heuristic dedup using basic fuzzy filename normalization. Produces within-folder and cross-folder-alias duplicate reports. |
| `rom_dedup_v2.py` | Layers 1+2 | Adds internal-header title extraction for SNES, N64, Mega Drive, GB/GBC/GBA, DS. Catches headered/byte-swapped variants that Layer 1 misses. |
| `rom_hash_match.py` | Layer 3 | Two-phase tool: `--phase=hash` walks the collection and writes CRC32 + SHA-1 with per-format normalization. `--phase=match` reads No-Intro / Redump / TOSEC DATs and produces canonical-name match reports. |
| `rom_merge_plan.py` | Action layer | Given a list of duplicate folder pairs (e.g. `genesis` → `megadrive`), generates a dry-run move plan with collision detection. Never overwrites; never deletes folders. |

Output schemas and CSV columns are documented in each script's docstring.

### Integration into ROMulus (v0.4.0+)

The three-layer pipeline is embedded in the ROMulus scanner and Heavy Scan pipeline. Some implementation notes specific to the ROMulus integration:

- **Identity fields live on `roms` directly.** In earlier versions (v0.1.0–v0.3.0), the pipeline's output fed a separate `games` table that grouped N roms per logical game. In v0.4.0 the `games` table was removed. Every ROM file owns its own `title`, `canonical_name`, `region`, `revision`, `is_hack`, `is_homebrew`, and `is_bios` directly on the `roms` row. Two byte-identical files at different paths are two rows; hacks are never silently collapsed into their originals.

- **Post-identification grouping phase is deleted.** The v0.3.0 scanner ran `_group_unlinked_roms_into_games` after the filesystem walk to link newly scanned rom rows to `games` rows by `(system_id, fuzzy_key)`. That phase is gone in v0.4.0. Identity writes happen at `upsert_rom` time; Heavy Scan then updates identity fields in-place via `_update_identity_from_dat` when a DAT match is found.

- **Hacks are first-class.** This is unchanged. `is_hack = 1` is set by the filename parser when `[h]` or similar markers appear. The Organizer's `find_duplicates` excludes hacks from its SHA-1 dedup proposals — they are never treated as duplicates of their base titles.

- **Cross-extension dedup.** The old `find_cross_extension_dupes` detector (which relied on shared `game_id` to link `.sfc` and `.smc` rows for the same logical game) was deleted in v0.4.0. Its role is fully covered by `find_duplicates`, which groups by SHA-1 — a byte-identical `.sfc` and `.zip` of the same ROM share a normalized SHA-1 after header stripping, so `find_duplicates` catches them. The TOCTOU re-hash guard in `_execute_delete_duplicate` was fixed simultaneously to call `hash_rom(path, header_rule)` (normalized) rather than `_digest_stream(path)` (raw bytes), so legitimate same-content pairs now apply cleanly.

### Tiered detector ordering in `analyze_library`

`analyze_library` assembles the Organize plan in four phases, each narrowing the candidate set for the next:

1. `find_alias_merges` — non-canonical folder names only; no per-rom logic.
2. `find_duplicates` — SHA-1 equality groups. Content equality is the strongest claim and wins first. The rom IDs scheduled for deletion here are collected into `deleted_rom_ids`.
3. `find_renameable_roms(conn, exclude_rom_ids=deleted_rom_ids)` — DAT-verified filename renames, skipping any rom already marked for deletion. Without the exclusion set, a rom about to be deleted as a hash duplicate would also receive a rename proposal; when `detect_collisions` later found the rename target occupied by the keeper, it would emit a false collision instead of the correct delete-duplicate.
4. `detect_collisions` — post-processing pass over the combined action list. Rename-vs-rename conflicts and rename-vs-existing-file conflicts are resolved here.

### Collision sub-cases (case 3)

When a rename target path matches an existing `roms` row that is not itself being renamed, `detect_collisions` inspects SHA-1 and `is_hack` to decide the outcome:

| Sub-case | Condition | Result |
|---|---|---|
| **3a** | Both sides have a stored SHA-1, they match, neither is a hack | Upgrade to `ACTION_DELETE_DUPLICATE`. The canonical-named existing file becomes the keeper; the rename source is the file to delete. Catches pairs `find_duplicates` missed (e.g. one side not yet Heavy-Scanned when `find_duplicates` ran). |
| **3b** | Both sides have a stored SHA-1 and they differ, neither is a hack | `ACTION_COLLISION` — "target path already occupied by a different file in the library". Two distinct ROMs with the same canonical name. |
| **3c** | One or both sides lack a stored SHA-1 | `ACTION_COLLISION` — "target path already occupied; Heavy Scan both files to determine if duplicate". Equality cannot be proven without hashes. |
| **3d** | Either side has `is_hack = 1` | `ACTION_COLLISION` — "target path already occupied by a hack/non-hack pair; manual review required". Hacks are first-class; they are never auto-merged with an original. |

Cases 1 and 2 (rename-vs-rename and rename-chain conflicts) produce `ACTION_COLLISION` with `target_rom_id = None` — only the "Do nothing" resolution is offered in the UI because there is no existing DB row to work with.

### Per-row collision resolution

`OrganizePreviewDialog` renders a "Resolution" column (4th column) with a `QComboBox` per collision row. The options depend on which rom IDs the collision captured:

| Resolution | `available_resolutions` condition | Concrete actions via `resolve_collision` |
|---|---|---|
| **Do nothing** (default) | Always offered | Zero actions — the collision is dropped from the approved plan. |
| **Delete source** | `action.rom_id is not None` | One `ACTION_DELETE_FILE` for the rename source. Use when the canonical-named existing file is the authoritative copy. |
| **Delete target and rename source** | Both `action.rom_id` and `action.target_rom_id` are set (case 3 only) | Two actions in order: `ACTION_DELETE_FILE` for the existing target, then `ACTION_RENAME` for the source. The execute loop applies them sequentially under per-action SAVEPOINTs so the rename sees a clear path. |

`ACTION_DELETE_FILE` is distinct from `ACTION_DELETE_DUPLICATE`: it does not run the TOCTOU SHA-1 re-hash before unlinking. That guard exists to catch post-plan file edits, but a collision file is known to differ from the other party's content — re-hashing would always refuse. The user's explicit dropdown selection is the authorization for the unconditional delete.

---

## 9. Recommended order of operations

For a new ROM collection:

1. **Inventory pass.** Walk the tree, count files per folder, identify obvious skeleton placeholder folders. Output: per-folder file count.
2. **Layer 1 dedup pass.** Catches the gross-obvious duplicates (alias folders, format variants where filenames are honest). Output: within-folder and cross-folder candidate-dup reports.
3. **Decide on consolidations.** From Layer 1 results, identify alias folders to merge (e.g. `genesis` ↔ `megadrive`). Don't apply yet.
4. **Layer 2 dedup pass.** Re-run with internal-header extraction on the cartridge formats. The SNES `.smc`/`.sfc` and N64 `.z64`/`.v64` cases will collapse here.
5. **Acquire DATs.** Download No-Intro + Redump XMLs for the systems you have. Store in a `_dat_files/` folder.
6. **Layer 3 hash pass.** Hash everything (run on the storage host if possible). Save the hash CSV — this is your durable artifact, expensive to recompute.
7. **Layer 3 match pass.** Look up hashes against DATs. Files matching go to `canonical_match.csv`; misses go to `unmatched.csv` for inspection.
8. **Decide on rollup policy** (region/revision priorities) and run a tool like Retool, OR custom-script the rollup if you have specific preferences.
9. **Generate a move/merge plan** from the rollup output. Review thoroughly; apply with explicit confirmation.

Each layer is independently useful. You can stop at Layer 2 and accept "good-enough" dedup, or run the full pipeline for authoritative library curation.

---

## 10. Common pitfalls

- **Hashing `.zip` directly.** A `.zip` containing the same ROM as a loose file will have a totally different hash from the file. Always extract or hash the inner content. *Also: apply per-format normalization to the inner bytes — a zipped headered NES ROM still needs the iNES strip.*
- **Forgetting to strip the SMC header before SNES hashing.** Same ROM, +/- 512 bytes; hashes won't match the No-Intro DAT entry.
- **Forgetting to strip the iNES header before NES hashing.** Modern No-Intro NES DATs reference unheadered ROM content; most NES dumps in the wild are headered. Without stripping, NES match rate plummets (real-world: 1% match observed before the fix).
- **Treating CRC32 as collision-free.** It isn't. With 4.29 billion possible values, a database of ~100K ROM entries has a non-negligible collision probability. Always pair CRC32 with file size, or use SHA-1.
- **Trusting filename matching across regions.** "Final Fantasy III" in Japan is "Final Fantasy VI" in the US. Filename match collapses them; hash match keeps them distinct.
- **Treating a hack as a duplicate of the original.** Always preserve the hack as a distinct artifact; users explicitly want both.
- **Auto-deleting the smaller of two files.** Smaller often means *bad* (truncated dump). Use hashes plus DAT membership to decide which to keep.
- **Running over SMB without parallelism.** Single-threaded hashing of a 240 GB collection over SMB takes hours; throwing 16 threads at it cuts the wall-clock time dramatically.
- **Hashing on every run.** Hashes are expensive but stable — file content doesn't change unless you change it. Cache hash results in a CSV keyed by `(path, mtime, size)` and reuse.

---

## 11. Glossary

- **CHD** — Compressed Hunks of Data. MAME's lossless CD/HDD compression format. Convertible to/from `.cue`+`.bin` via `chdman`.
- **Copier header (SMC)** — A 512-byte prefix added by SNES copier hardware (Super Magic Drive) to dumps. Some `.smc` files have it; `.sfc` typically doesn't. Detect by `size % 1024 == 512`.
- **DAT** — A Logiqx-XML-format database of ROM hashes and metadata, published by No-Intro/Redump/TOSEC.
- **Headered/Unheadered** — Whether a copier header is present. Affects hashes by exactly 512 bytes for SNES.
- **iNES header** — A 16-byte header at the start of `.nes` files describing mapper/PRG/CHR sizes. Does not contain a title.
- **Logiqx** — The XML schema used by clrmamepro and adopted as the de facto DAT standard.
- **No-Intro** — The most prominent cartridge-ROM preservation group. Their naming convention is widely cited.
- **Redump** — The disc-based-game preservation group; track-level hashes for CD/DVD.
- **Romset** — In MAME parlance, the set of ROM files (often multi-file, packaged in a `.zip`) representing one arcade game.
- **TOSEC** — The Old School Emulation Center; broad coverage of home computers and obscure systems. Per-system DATs.

---

## 12. References

- No-Intro Naming Convention wiki — https://wiki.no-intro.org/
- Redump wiki — http://wiki.redump.org/
- TOSEC Naming Convention (2015-03-23) — https://www.tosecdev.org/tosec-naming-convention
- SNES dev wiki, ROM file formats — https://snes.nesdev.org/wiki/ROM_file_formats
- N64 ROM byte order — http://n64dev.org/romformats.html
- Logiqx schema — https://www.logiqx.com/Dats/dtd.htm
- igir documentation — https://igir.io/
- Retool — https://github.com/unexpectedpanda/retool
- Hash-DAT-Base (community ROM hacks) — https://github.com/HoraceAndTheSpider/Hack-DAT-Base
