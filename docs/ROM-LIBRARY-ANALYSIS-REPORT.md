# ROM Library Analysis Report

**Library:** `\\nas\Retro Files\Console ROMs`
**Generated:** 2026-05-10
**Target device:** Anbernic RG556 / RG406 family (Android stock RGLauncher)
**Scope:** Inventory, deduplication analysis, per-folder action plan

This report is the canonical input for any future deduplication work on this library. Future Claude Code sessions can be pointed at this file to resume the dedup workflow without re-discovering anything.

---

## 1. Library at a glance

| Metric | Value |
|---|---:|
| Top-level folders (excluding Android system dirs) | ~150 |
| Populated folders | ~70 |
| Empty placeholders (Anbernic skeleton) | 78 |
| ROM-like files (after side-file filter) | **54,672** |
| Total disk usage | ~240 GB |
| Errors during hash phase | 5 (corrupt zips — see §10) |

### Notable population

| Range | Folders |
|---|---|
| Massive (5k+ files) | `c64`, `zxspectrum`, `atari800`, `amiga1200`, `nes` |
| Large (1k-5k) | `arcade`, `mame`, `atarist`, `snes`, `nesh`, `amstradcpc`, `megadrive`, `gba`, `psx`, `amiga`, `gb`, `genesis`, `fbneo`, `gbc` |
| Medium (200-1k) | `ports`, `mastersystem`, `msx`, `msx2+`, `atari2600`, `pcengine`, `varcade`, `psp`, `gamegear`, `fds`, `n64`, `neogeo`, `dreamcast` |
| Small | everything else |

Full per-folder counts: `_dedup_reports/summary.txt`.

---

## 2. Dedup signals (three layers)

This library was analyzed with a three-layer pipeline. Each layer's output is on disk in `_dedup_reports/` and feeds the next layer.

### Layer 1 — Filename fuzzy matching
- Reports: `duplicates_within_folder.csv`, `duplicates_cross_folder_aliases.csv`, `duplicates_cross_folder_other.csv`
- 6,156 within-folder name-collision groups (heuristic, includes false positives)
- 3,907 cross-folder alias groups (genesis↔megadrive, etc.)
- 3,788 other cross-folder name collisions (multi-platform releases)

### Layer 2 — Filename fuzzy + internal-header titles
- Reports: `rom_index_v2.csv`, `dups_within_v2.csv`, `dups_cross_alias_v2.csv`
- Adds internal-title extraction for SNES, N64, MD, GB/GBC/GBA, DS (94% header-read success on 6,583 attempts)
- 4,013 within-folder dup groups (more accurate than Layer 1)
- 4,092 cross-folder alias dup groups
- 214 SNES `.smc` ↔ `.sfc` collapses confirmed via internal title

### Layer 3 — Hash + No-Intro DAT lookup (the gold standard)
- Reports: `rom_hashes.csv`, `rom_canonical_match.csv`, `rom_unmatched.csv`, `rom_canonical_dups.csv`
- 54,667 SHA-1 hashes computed (5 errors on corrupt zips)
- 8,188 byte-identical redundant files (15.0% of the library)
- 11,741 files matched against No-Intro DATs (21% — limited by DAT coverage; see §6)
- **2,946 confirmed-redundant files** (matched and grouped to canonical entries)
- **1,040 cross-folder canonical-dup groups** ← highest-confidence merge candidates

Pre-hash normalizations applied:
- 284 SMC copier headers stripped (SNES)
- 141 N64 v64 byte-swapped to z64 form
- 19,515 `.zip` archives extracted (single-inner-file)
- 6,673 `.zip` archives with multiple inner files (MAME romsets — handled by hashing largest)
- iNES header stripping for `.nes` is implemented in the script but **not yet re-run** (see §6, NES anomaly)

---

## 3. Recommended action sequence (the actionable plan)

When you're ready to actually act on this library, work through the steps in order. Each step references the data file that drives it.

### Step 1 — Apply alias-folder consolidation

**Goal:** Merge folders that are aliases of the same console (e.g. `genesis` → `megadrive`).

**Driver file:** `_dedup_reports/rom_canonical_dups.csv` (cross-folder rows) plus the merge table below.

**Confirmed merges (DAT-validated, high confidence):**

| Source folder (delete after merge) | Target folder (keep) | Cross-folder canonical dups | Notes |
|---|---|---:|---|
| `genesis` | `megadrive` | 439 | Same console, ES-DE/Anbernic canonical name |
| `tg16` | `pcengine` | 61 | Same console, NA region alias |
| `pcenginecd` | `tg16cd` | (unmatched, alias confirmed) | Reverse direction — tg16cd has more files |
| `ss` | `saturn` | (alias confirmed) | Short alias |
| `supergrafx` | `sgfx` | (alias confirmed) | Short alias |

**MSX umbrella note:** `msx` ↔ `msx2+` shows 433 cross-folder canonical dups, plus 65 in `msx + msx2 + msx2+` and 17 in `msx + msx2`. **Do not auto-merge** — these are distinct console tiers and the user may want them split. Investigate before deciding.

**Tool:** `_matching_scripts/rom_merge_plan.py` (read-only by default; `--apply` to perform). The MERGES list at the top of the script encodes the table above.

### Step 2 — Merge populated hack folders into parents

**Goal:** RGLauncher does not recognize the `h`-suffix hack folders, so merge them into their parent system folders.

| Source folder | Target folder | Files |
|---|---|---:|
| `nesh` | `nes` | 1,978 |

All other hack folders (`gbh`, `gbah`, `gbch`, `snesh`, `gamegearh`, `genh`) are empty placeholders — see Step 5.

**Tool:** Same `rom_merge_plan.py`, MERGES entry already includes `nesh → nes`.

### Step 3 — Resolve byte-identical duplicates

**Goal:** Remove redundant copies of the same content. Two scopes:

#### Step 3a — Cross-folder canonical dups (high confidence, DAT-confirmed)

**Driver file:** `_dedup_reports/rom_canonical_dups.csv` — rows with `files_in_group > 1` AND multiple folders in the group.

For each group: keep one file (preferably the one in the canonical folder per §3 above; otherwise the smallest filename / cleanest name); delete the others. Net savings: ~2,946 files (some are within-folder).

#### Step 3b — Within-folder cross-extension matches (e.g. `.smc` and `.sfc` of same game)

**Driver file:** `_dedup_reports/rom_canonical_dups.csv` — rows with `files_in_group > 1` and a single folder in the group.

Per-folder counts of cross-extension matches:
- `megadrive`: **421** (zip + raw of same game, etc.)
- `snes`: **200** (.smc + .sfc of same ROM)
- `pcengine`: **169**
- `mastersystem`: 24
- `n64`: 8

Recommendation: keep one extension per game. Preferences typical of Anbernic Android setups:
- SNES: prefer `.sfc` over `.smc` (raw, unheadered, modern-canonical)
- N64: prefer `.z64` over `.v64`/`.n64` (big-endian, native byte order)
- Other formats: prefer raw `.md`/`.gen`/`.pce`/`.gb` etc. over `.zip` if disk space allows; some Anbernic emulators prefer raw

#### Step 3c — Same-size collisions during alias-merge

When the alias merge in Step 1 is applied, files with the same name and same byte count in source and target are extremely likely byte-identical (No-Intro fingerprint). Of 177 collisions in the original alias-merge dry run, **167 were same-size**; only 10 had differing sizes (those need manual review).

### Step 4 — Investigate the 5 zip errors

Files reported as "not a zip" during hashing — likely renamed `.bin` files or corrupted archives:

- `genesis/Toy Story.zip`
- `genesis/Ranger-X.zip`
- `genesis/Red Zone.zip`
- `genesis/Primal Rage.zip`
- `ports/data.zip`

Either fix the file, re-extension to its actual format, or delete.

### Step 5 — Decide on empty placeholder folders

**Driver file:** the "EMPTY PLACEHOLDER FOLDERS" list at the bottom of any `merge_plan_*_dryrun.txt` in `_dedup_reports/`.

**78 folders contain zero ROMs after filtering.** Most are Anbernic image skeletons. Categories:

- **Junk to clean up:** `1`, `25game`, `anbernic` (vendor folder, inspect first)
- **Empty hack-folder placeholders:** `gbh`, `gbah`, `gbch`, `snesh`, `gamegearh`, `genh`, `gb2players`, `gbc2players`, `mdh` — safe to delete (RGLauncher doesn't see them anyway)
- **Empty alias placeholders:** `sfc`, `famicom`, `gw`, `pico-8`, `tic-80`, `sg1000`, `lynx`, `colecovision`, `o2em`, `videopac`, `wonderswan`, `wonderswancolor`, `megadrive-japan`, `coleco`, `msx1`, `msxturbor`, `snes-msu1`, `snesmsu1`, `sufami` — safe to delete (the populated counterparts cover them)
- **Empty system placeholders (RGLauncher catalog entries — keep, may populate later):** `cavestory`, `dos`, `easyrpg`, `intellivision`, `scummvm`, `pcfx`, `n64dd`, `satellaview`, `atarijaguar`, `supervision`, `channelf`, `pokemini` (already covered)
- **Empty computer/non-catalog placeholders (PC archive — keep):** `c128`, `c16`, `c20`, `cplus4`, `pet`, `vic20`, `amiga500`, `amigacd32`, `amigacdtv`, `amstradgx4000`, `gx4000`, `pc88`, `pc98`, `x1`, `x68000`, `thomson`, `zx81`, `pc`, `3do`, `daphne`, `freej2me`, `lutro`, `mplayer`, `pygame`, `tyrquake`, `uzebox`, `xash3d_fwgs`, `solarus`, `cgenius`, `devilutionx`, `cannonball`, `prboom`, `mrboom`, `sdlpop`, `moonlight`, `openbor`, `sc-3000`, `hbmame`, `varcade`, `ports`, `capcom`, `neocd`

**Recommendation:** delete the "junk" + "empty hack placeholders" + "empty alias placeholders" categories (~30 folders). Keep everything else; they cost nothing and may populate.

### Step 6 — Decide on non-catalog systems for device-side

Per §6 of `ROM-FORMATS-REFERENCE.md`, ~22,000 files live in folders that the Anbernic stock launcher won't display:
- Computer systems: `c64` (5,230), `zxspectrum` (5,252), `atari800` (5,097), `amiga1200` (4,288), `atarist` (2,847), `amstradcpc` (1,955), `amiga` (1,113)
- Engines/ports: `ports` (771)
- Plus smaller folders for less-common systems

**User's chosen plan:** keep these on the share as a PC archive; only copy catalog-supported folders to the device.

This is enforced at *sync time* (when copying share → SD card / device storage), not by changing the share layout. No script-driven action needed for this report.

---

## 4. Headline findings

### 4.1 The `nes` folder NES anomaly — fixed and re-validated

Initially only 31 of 4,229 NES files (1%) matched the loaded No-Intro NES DAT (which has 2,815 entries). Diagnosed cause: the DAT references **unheadered** ROM content (modern post-2018 No-Intro convention), but most NES dumps in the wild carry the 16-byte iNES header.

**Fix implemented and validated:** `rom_hash_match.py` now strips iNES headers before hashing `.nes` files (loose or inside zip). Re-ran the hash + match phase on `nes/` and `nesh/`:

| Folder | Before | After fix |
|---|---|---|
| `nes` | 31 / 4,229 = **1%** | **2,937 / 4,229 = 69%** |
| `nesh` | 0 / 1,978 = **0%** | **947 / 1,978 = 48%** |
| Combined | 31 / 6,207 = 0.5% | **3,884 / 6,207 = 63%** |

5,982 of 6,207 files had iNES headers stripped (96%); 3 were detected as UNIF format. Residual unmatched (~2,300 files) is expected: Japanese-only releases, translations (`[T-En]`), hacks, prototypes, homebrew not in the official DAT.

**New per-folder data is in separate output files** so as not to overwrite the main full-library run:

- `_dedup_reports/rom_hashes_nes_nesh.csv`
- `_dedup_reports/rom_canonical_match_nes_nesh.csv`
- `_dedup_reports/rom_unmatched_nes_nesh.csv`
- `_dedup_reports/rom_canonical_dups_nes_nesh.csv`

When ready to fold these into the main reports, either re-run the full hash + match phases, or merge the `_nes_nesh` files into the main ones (small Python merge).

### 4.2 The genesis/megadrive overlap is huge

439 confirmed cross-folder canonical dups (DAT-validated byte-identical content). This is by far the biggest single dedup opportunity in the library.

### 4.3 Empty-byte files masquerading as content

11 zero-byte files share SHA-1 `da39a3ee5e6b4b0d3255bfef95601890afd80709` (the SHA-1 of empty input). Scattered across `anbernic`, `amiga`, `hbmame`, `moonlight`, `psx`, etc. Cleanup target.

### 4.4 MAME / FBNeo overlap is intense

Cross-folder canonical dups visible even at 0% match rate (because the hash layer doesn't need DATs to find byte-identical files):
- `kof2002` appears 9 times across `arcade` and `neogeo`
- `sfa2` / `sfz2al` appear 9 times across `arcade`, `cps1`, `fbneo`, `mame`
- One MAME zip is referenced under 6 different filenames in `mame/` (parent/clone romset confusion)

These are visible in `_dedup_reports/rom_hashes.csv` by SHA-1 grouping, even without DAT matching.

### 4.5 Mislabeled files caught by hash matching

Real example from the snes folder: `003 Street Fighter Alpha 2.smc` actually contains the bytes of *Final Fight 2*. SHA-1 matching surfaced the mislabel automatically.

---

## 5. Confidence levels of the data sources

| Signal | Confidence | Use for |
|---|---|---|
| Hash + DAT match (Layer 3, matched files) | **Highest** — byte-level + canonical-name confirmation | Authoritative dedup, naming corrections |
| Hash byte-equivalence (Layer 3, unmatched files) | High — same SHA-1 = same content, no canonical name | Deduping arcade ROMs, disc images, files not in any DAT |
| Internal title (Layer 2) | Medium-high — authoritative for headered formats; some games have wrong internal title | Cross-format collapsing within a console |
| Filename fuzzy (Layer 1) | Medium — picks up most cosmetic variants but fails on radical renames | First-cut report; useful when no DAT covers the system |

**Always prefer the highest-confidence signal available** when making a dedup decision. If a file is in `rom_canonical_match.csv`, trust the canonical name. If it's in `rom_unmatched.csv` but appears in a multi-row SHA-1 group, trust the byte equivalence. Filename-only matches need human review for final action.

---

## 6. Why DAT match rate is 21% overall

**Per-folder match rate breakdown** (counts → ratio):

| Range | Folders | Reason |
|---|---|---|
| 100% | `msx2+`, `sg-1000`, `atari5200`, `virtualboy`, `pokemini`, `sgfx`, `hbmame`, `moonlight`, `sdlpop`, `anbernic`, `odyssey` | Small + DAT loaded |
| 80-99% | `gbc` (98), `pcengine` (98), `n64` (97), `gamegear` (97), `sega32x` (97), `msx2` (99), `ngpc` (99), `gb` (92), `atarilynx` (91), `wswanc` (92), `tg16` (90), `ngp` (90), `mastersystem` (88), `msx` (87), `megadrive` (87), `nds` (85), `snes` (82) | DAT loaded, modest hack/homebrew bleed |
| 50-79% | `atari2600` (79), `gba` (68), `fds` (65), `genesis` (51), `vectrex` (45) | DAT loaded, lots of hacks/translations not in DAT |
| 5-49% | `psp` (14), `atarist` (3) | Disc-based or tape-based; standard cartridge DAT doesn't apply |
| 0-3% | `nes` (1), `c64` (0.4), `nesh` (0), all arcade folders (0), all disc folders (0), most computer folders (0) | NES iNES anomaly (fixable); needs Redump/MAME/TOSEC DATs that aren't loaded |

### Missing DAT categories

| Add this DAT family | Covers folders | Source |
|---|---|---|
| **MAME / FBNeo DATs** | `arcade`, `mame`, `fbneo`, `cps1`, `cps2`, `cps3`, `naomi`, `atomiswave`, `neogeo`, `neocd`, `varcade`, `hbmame`, `capcom` (~6,500 files) | User already has some at `mame/clrmamepro/*.dat` and `fbneo/clrmamepro/*.dat` — **just symlink/copy into `_dat_files/`** and rerun match phase |
| **Redump DATs** | `psx`, `ps2`, `dreamcast`, `saturn`, `gc`, `segacd`, `tg16cd`, `pcenginecd`, `wii&ngc`, `ss`, `3do` (~1,200 files) | http://redump.org/downloads/ — no login |
| **TOSEC DATs** | `c64`, `zxspectrum`, `atari800`, `atarist`, `amstradcpc`, `amiga`, `amiga1200`, computer folders (~22,000 files) | https://www.tosecdev.org/downloads — large bundle, ~50 MB |

Adding all three would push the overall match rate from 21% to an estimated 70-85%.

---

## 7. Top cross-folder dup pairs (DAT-confirmed)

From `rom_canonical_dups.csv`, files with the same SHA-1 spanning 2+ folders:

| Folder pair | Canonical dup groups | Action |
|---|---:|---|
| `genesis` + `megadrive` | 439 | Merge per §3 Step 1 |
| `msx` + `msx2+` | 433 | Investigate before merging — different console tiers |
| `msx` + `msx2` + `msx2+` | 65 | 3-way overlap; same caution |
| `pcengine` + `tg16` | 61 | Merge per §3 Step 1 |
| `msx` + `msx2` | 17 | Same caution as above |
| `genesis` + `mastersystem` + `megadrive` | 7 | 3-way (multi-platform releases?); review individually |
| `gb` + `gbc` | 5 | Likely GBC backward-compat copies; review |
| `genesis` + `mastersystem` | 3 | Multi-platform; review |
| `mame` + `pcengine` | 1 | Anomaly; review |
| `fds` + `snes` | 1 | Likely the FDS Mario Bros 2 case visible in samples |

---

## 8. Top within-folder dup groups

Groups with `files_in_group > 1` and a single folder (redundant copies in one place):

| Folder | Within-folder canonical dup groups |
|---|---:|
| `megadrive` | high (zip + raw of same game) |
| `snes` | ~140 (.smc + .sfc + .zip variants) |
| `pcengine` | ~110 |
| `mame` | parent/clone romset confusion |
| `nes` | will rise dramatically once iNES fix is rerun |

Detailed list: `_dedup_reports/rom_canonical_dups.csv` filtered to `files_in_group > 1` AND single distinct folder.

---

## 9. Files needing human review (will not auto-resolve)

### Files where canonical-dup group has `files_in_group=1`
Single matches — these are **not dups**, they're correctly-identified single files. Ignore for dedup; useful for renaming if filename is ugly.

### Same-name-different-content collisions
From the alias-merge dry run: 10 files where source and target had the same name but different sizes. Most likely different revisions (Rev 0 vs Rev 1) or different dumps. Examples:
- `Outlander (USA).zip` — genesis 421,652 vs megadrive 421,420
- `Shining Force (USA).zip` — genesis 1,175,666 vs megadrive 1,174,691
- `Dungeon Explorer II (USA).chd` — pcenginecd 389,179,728 vs tg16cd 389,046,423

For each: hash both, look up in DATs, choose the canonical (higher revision, or matching the official region).

### Unmatched files in folders with high DAT coverage
Look at `rom_unmatched.csv` filtered to `folder IN ('snes', 'gbc', 'gba', 'megadrive', etc.)`. These are likely:
- Hacks / translations
- Bad dumps
- Homebrew not in No-Intro

For these, the *filename* often tells you which (look for `[T+En]`, `[h]`, `(Hack)`, `(Aftermarket)`, etc.).

---

## 10. Hash phase errors to investigate

5 files failed during hashing because they're labeled `.zip` but aren't valid zip archives:

```
genesis/Toy Story.zip
genesis/Ranger-X.zip
genesis/Red Zone.zip
genesis/Primal Rage.zip
ports/data.zip
```

Likely candidates:
- Renamed `.bin` files (someone changed extension by mistake)
- Corrupted archives
- Different archive format (`.7z`, `.rar`) misnamed

Action: run `file genesis/Toy\ Story.zip` (or `Get-Item` on Windows) to identify, then either fix extension or delete.

---

## 11. Reference files in this analysis

All paths relative to the library root.

### Documentation (read these first)

- `ROM-FORMATS-REFERENCE.md` — comprehensive ROM formats / naming conventions / Anbernic-specific guidance
- `ROM-DEDUP-METHODOLOGY.md` — methodology explainer (portable, intended for cross-project use)
- `ROM-LIBRARY-ANALYSIS-REPORT.md` — **this file**

### Data files

| File | Source | Use |
|---|---|---|
| `_dedup_reports/summary.txt` | v1 dedup | Per-folder file counts |
| `_dedup_reports/duplicates_within_folder.csv` | v1 (filename) | First-cut within-folder dup heuristic |
| `_dedup_reports/duplicates_cross_folder_aliases.csv` | v1 (filename) | First-cut alias-folder dup heuristic |
| `_dedup_reports/duplicates_cross_folder_other.csv` | v1 (filename) | Cross-system filename collisions (review for missed aliases) |
| `_dedup_reports/rom_index_v2.csv` | v2 (fuzzy + headers) | Per-file index with fuzzy keys and internal titles |
| `_dedup_reports/dups_within_v2.csv` | v2 | Within-folder dups using fuzzy + header keys |
| `_dedup_reports/dups_cross_alias_v2.csv` | v2 | Cross-folder alias dups using fuzzy + header keys |
| `_dedup_reports/rom_hashes.csv` | Layer 3 hash | **Per-file CRC32 + SHA-1 (54,672 rows). The durable artifact.** |
| `_dedup_reports/rom_canonical_match.csv` | Layer 3 match | Files identified by No-Intro DAT |
| `_dedup_reports/rom_unmatched.csv` | Layer 3 match | Files no DAT entry recognized |
| `_dedup_reports/rom_canonical_dups.csv` | Layer 3 match | **Canonical-dup groups — the dedup gold** |
| `_dedup_reports/merge_plan_<ts>_dryrun.txt` | Alias-merge planner | Most recent dry-run plan |
| `_dedup_reports/merge_collisions_<ts>_dryrun.csv` | Alias-merge planner | Per-file collision detail with size match flag |

### Scripts

All scripts live in `_matching_scripts/` at the library root.

| Script | Phase | Status |
|---|---|---|
| `_matching_scripts/rom_dedup.py` | Layer 1 (filename fuzzy) | Ran; output in `_dedup_reports/` |
| `_matching_scripts/rom_dedup_v2.py` | Layers 1+2 (fuzzy + internal headers) | Ran (twice, after a tuning fix); output in `_dedup_reports/` |
| `_matching_scripts/rom_hash_match.py` | Layer 3 (hash + DAT) | Ran on full library, then re-run on nes+nesh with iNES fix. Latest version has iNES header support. |
| `_matching_scripts/rom_merge_plan.py` | Action (alias-folder merges) | Ran in dry-run mode; never `--apply`'d |
| `_matching_scripts/rom_dedup_plan.py` | Action (byte-identical dedup) | Ran in dry-run mode. v2 of plan is in `_dedup_reports/dedupe_plan_v2.csv` (uses USA/English-language preference scoring). Never `--apply`'d. |
| `_matching_scripts/redundancy_breakdown.py` | Analysis | Generates per-folder / per-platform / per-extension redundancy breakdown |
| `_matching_scripts/hash_summary.py` | Analysis helper | Generates dedup-impact summary from `rom_hashes.csv` |
| `_matching_scripts/match_summary.py` | Analysis helper | Per-folder match rate breakdown from `rom_canonical_match.csv` |
| `_matching_scripts/sample_v2.py` | Analysis helper | Spot-checks fuzzy + header matching on known game-name examples |
| `_matching_scripts/verify_snes_hashes.py` | Analysis helper | Validates SMC-header normalization on SNES test data |
| `_matching_scripts/nes_match_breakdown.py` | Analysis helper | Before/after NES match-rate comparison |

### DAT files

`_dat_files/` on the share contains 106 No-Intro DAT files (cartridge / handheld / digital). Missing categories:
- Redump (disc-based)
- MAME / FBNeo (arcade)
- TOSEC (computer)

---

## 12. How to "use this report" in a future session

When you're ready to actually act on this analysis, the workflow is:

1. **Tell Claude:** "Use `ROM-LIBRARY-ANALYSIS-REPORT.md` as the input. Apply the recommended action sequence in §3."
2. **Claude reads this file**, locates the relevant data CSVs in `_dedup_reports/`, and the existing scripts in the user's temp folder.
3. **Claude proposes an ordered action plan** based on §3 Steps 1-6, asks for confirmation before each destructive operation, and re-runs whichever scripts are needed (e.g. re-hash nes/nesh after iNES fix, re-match if DATs were added).
4. **No file is modified** without explicit user approval.

If the dedup workflow has already been partially run, the data files in `_dedup_reports/` will reflect the current state; this report should be regenerated before the next run.

### To regenerate this report

After any significant change to the library or to the DAT folder, re-run in this order:

```
SCRIPTS="//nas/Retro Files/Console ROMs/_matching_scripts"
DATS="//nas/Retro Files/Console ROMs/_dat_files"

python "$SCRIPTS/rom_dedup.py"
python "$SCRIPTS/rom_dedup_v2.py"
python "$SCRIPTS/rom_hash_match.py" --phase=hash --workers=8
python "$SCRIPTS/rom_hash_match.py" --phase=match --dats="$DATS"
```

Then ask Claude to update this analysis report with the fresh numbers.

---

## 13. What to expect from a real dedup pass

### Conservative scenario (only act on highest-confidence dups)
- Apply alias-folder merge (§3 Step 1) → ~6,700 files relocated, ~167 same-size dupes removed
- Resolve cross-folder canonical dups (§3 Step 3a) → another ~2,946 redundant matched files removed
- **Net: ~3,100 files removed** (down to ~51,500)

### Aggressive scenario (also act on within-folder cross-extension dups)
- Above, plus pick one extension per game in megadrive, snes, pcengine
- Add ~600-900 more redundant files removed
- **Net: ~4,000 files removed** (down to ~50,500)

### Full-coverage scenario (after adding Redump/MAME/TOSEC DATs and re-running)
- Match rate climbs from 21% to ~70-85%
- Many more cross-folder dups in arcade and disc folders surfacing
- Plausible to reach **8,000-12,000 files removed** (down to ~42,000-46,000) without losing any unique content

The 8,188 byte-identical-redundant figure from the hash phase (§2 Layer 3) is a hard upper bound on dedup without giving up content. The trade-off is whether to keep multiple regions/revisions of each game or pick one canonical version (the latter could remove tens of thousands more).

---

## 14. Caveats for any future session reading this report

- **Filename matching is heuristic.** Layer 1 reports are starting points only.
- **Internal-header titles are sometimes wrong** (developer typos, prototype titles, swapped numbering). Cross-check with hashes when in doubt.
- **CRC32 alone has collision risk.** Always pair with file size or SHA-1.
- **DAT matches are point-in-time.** No-Intro / Redump update; if a new dump enters the DAT, an old "unmatched" file may match later. Re-run the match phase periodically.
- **Hacks and translations are first-class artifacts.** Never silently dedupe them against originals.
- **The Anbernic device target is RGLauncher (not muOS / not ES-DE).** Folder names matter for cross-frontend compatibility but not for RGLauncher itself, which is path-driven (see `ROM-FORMATS-REFERENCE.md` §6).
- **The user's chosen plan: keep non-RGLauncher-catalog content on the share as PC archive.** Sync to device should filter, not the share itself.

---

*End of report. Data files referenced here are in `_dedup_reports/`. Scripts are in `_matching_scripts/`. DATs are in `_dat_files/`.*
