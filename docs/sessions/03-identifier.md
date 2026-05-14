# Session 3: Identifier Pipeline (Headers + Hashing + DAT Parser)

**Type:** Build

**Context for this session:**

You are building Layer 2 (internal header extraction) and Layer 3 (hashing + DAT matching) of the identifier pipeline, plus the Logiqx XML DAT parser.

Internal header locations (from TECHNICAL_PLAN.md §6 — Identifier Pipeline):
- SNES: title at `0x7FC0` (LoROM) or `0xFFC0` (HiROM), 21 bytes. Strip SMC 512-byte header first if `size % 1024 == 512`.
- N64: title at `0x20`, 20 bytes. Byte-swap to z64 first. Magic: `80 37 12 40` = z64, `37 80 40 12` = v64, `40 12 37 80` = n64.
- Mega Drive: title at `0x150` (overseas) / `0x120` (domestic), 48 bytes. Check "SEGA MEGA DRIVE" / "SEGA GENESIS" at `0x100`.
- GB/GBC: title at `0x134`, 11-16 bytes. Trim at first null.
- GBA: title at `0xA0`, 12 bytes.
- DS: title at `0x00`, 12 bytes.

Header strip rules for hashing:
- `smc_512`: strip 512 bytes if `size % 1024 == 512`
- `ines_16`: strip 16 bytes if magic is `NES\x1a`
- `n64_byteswap`: convert to z64 byte order before hashing
- `lynx_64`: strip 64 bytes if magic is `LYNX\x00`

ZIP handling: if `.zip` with single inner file, hash the inner content (after header stripping). If multiple inner files (MAME romset), hash the largest.

DAT format: Logiqx XML. Parse with `xml.etree.ElementTree`. Extract: game name, rom name, size, CRC32, MD5, SHA-1.

DAT matching: look up by SHA-1 first, then CRC32+size as fallback.

**Tasks:**

- [x] Create `src/romulus/core/identifier.py`:
  - `extract_header_title(file_path, header_rule)` — read internal title based on system's header_rule
  - Returns None for systems without headers
- [x] Create `src/romulus/core/hasher.py`:
  - `hash_rom(file_path, header_rule)` — compute CRC32 + SHA-1 with header stripping and zip extraction
  - `normalize_rom_content(content, header_rule)` — apply strip/byteswap rules
  - `hash_library(conn, progress_callback, workers=8)` — parallel hash all unhashed/changed ROMs using ThreadPoolExecutor
  - Hash cache check: skip if (path, mtime, size) unchanged since last hash
- [x] Create `src/romulus/core/dat_parser.py`:
  - `parse_dat_file(filepath)` — parse single DAT, return list of DatEntry records
  - `load_all_dats(conn, dat_paths)` — parse all DATs from bundled + user folders, insert into dat_entries table
  - `match_hashes(conn)` — for all hashed ROMs, look up in dat_entries by SHA-1 then CRC32+size. Update rom.dat_match and rom.match_confidence.
  - `parse_region_from_name(game_name)` — extract region tags from canonical name
- [x] Add DAT-related queries to `db/queries.py`:
  - `insert_dat_entry(conn, entry)`, `get_dat_by_sha1(conn, sha1)`, `get_dat_by_crc_size(conn, crc32, size)`, `update_rom_match(conn, rom_id, dat_match, confidence)`
- [x] Add hash-related queries to `db/queries.py`:
  - `upsert_hash(conn, rom_id, crc32, sha1, md5)`, `get_hash(conn, rom_id)`, `get_unhashed_roms(conn)`, `get_stale_hashes(conn)` (mtime changed)
- [x] Copy bundled No-Intro DAT files into `data/dats/`. Include DATs for the ~30 systems in the system registry. (If actual DAT files aren't available at dev time, create 2-3 small test DAT files with known entries for testing.)
- [x] Write tests:
  - `tests/test_identifier.py`: test header extraction for SNES (LoROM/HiROM, with/without SMC header), N64 (z64/v64/n64 byte orders), GB, GBA, MD, DS
  - `tests/test_hasher.py`: test hash computation with header stripping, ZIP extraction, hash caching logic
  - `tests/test_dat_parser.py`: test Logiqx XML parsing, DAT matching by SHA-1, CRC32+size fallback, region parsing

**Acceptance criteria:**
- Internal header extraction works for SNES, N64, MD, GB/GBC, GBA, DS
- Hashing correctly strips headers (SMC, iNES, Lynx) and byte-swaps N64 before computing SHA-1
- ZIP files handled: single-file extracted, multi-file hashes largest
- DAT parser reads Logiqx XML and populates dat_entries table
- Hash matching links ROMs to DAT entries by SHA-1 with CRC32+size fallback
- All tests pass, ruff clean

STOP. Commit with message "Session 3: Identifier pipeline, hasher, DAT parser". Do not proceed to Session 4.

## Completion Summary
**Status:** COMPLETE
**Date:** 2026-05-14
**What was built/changed:**
- `src/romulus/core/identifier.py` — Layer 2 internal title extraction for SNES (LoROM/HiROM with SMC strip + printable-ratio tiebreaker), N64 (z64/v64/n64 byte-order detection and swap), Mega Drive (overseas/domestic at 0x150/0x120 with SEGA magic check), GB/GBC (0x134, null-trim), GBA (0xA0), DS (0x00). Returns None for systems without an internal title slot.
- `src/romulus/core/hasher.py` — `normalize_rom_content` (smc_512/ines_16/lynx_64 strip + n64_byteswap), `hash_rom` (CRC32+SHA-1+MD5 with stream path for plain files and full-read path when normalization is needed; ZIP-aware: single inner file or largest of many), `hash_library` (ThreadPoolExecutor parallelism with workers param, progress callback, mtime-based staleness check) plus internal `_rows_needing_hash` join.
- `src/romulus/core/dat_parser.py` — `DatEntry` frozen dataclass, `parse_dat_file` (Logiqx XML via stdlib ElementTree, header→system_id resolution via registry `dat_name`, region/revision extraction, isbios flag), `load_all_dats` (accepts mixed files + directories, recursive .dat/.xml glob), `match_hashes` (SHA-1 primary, CRC32+size fallback that refuses ambiguous matches, skips already-dat_verified rows), `parse_region_from_name`.
- `src/romulus/db/queries.py` — `upsert_hash`, `get_hash`, `get_unhashed_roms`, `get_stale_hashes`, `insert_dat_entry` (takes DatEntry dataclass), `get_dat_by_sha1` (case-insensitive), `get_dat_by_crc_size` (ambiguity-safe: returns None on collision), `update_rom_match`.
- `data/dats/` — two tiny synthetic Logiqx XML DATs (SNES + GB) as scaffolding stand-ins for real No-Intro bundles. Real DAT files were not available at dev time; the spec explicitly permits this.
- `tests/test_identifier.py`, `tests/test_hasher.py`, `tests/test_dat_parser.py` — 79 new tests covering every header rule, byte-order conversion, ZIP extraction path, library-level hash orchestration with staleness, DAT parsing edge cases, SHA-1 + CRC32+size matching, and ambiguity refusal.

**Tests:** 240 passed (161 baseline + 79 new). Ruff clean.
**Config changes:** None.
**Breaking changes:** None. `insert_dat_entry` is a new signature taking the `DatEntry` dataclass; the symbol is new so nothing calls it yet outside this session's code.
**Carry-forward notes:**
- The bundled `data/dats/` directory only contains placeholder DATs. The next milestone that depends on real DAT data (likely the Heavy Scan UI plumbing or the canonical-name pass in the Organizer session) will need real No-Intro files committed in.
- `update_rom_match` only writes the canonical game name (string); it does not create or link a `games` row. That linking is the Organizer's job — match_hashes intentionally stays in the "label" lane.
- The N64 byte-swap implementation pads odd/non-multiple-of-4 inputs with zero bytes before swapping. Real ROMs are always multiples of 4, so this is purely defensive against test inputs.
- `get_dat_by_crc_size` deliberately returns None when more than one DAT row shares a (CRC32, size) pair. This matches the ROM-DEDUP §5.4 "ambiguous CRC32s shouldn't be auto-applied" rule but means future code calling this helper must handle None as "ambiguous", not just "missing".
- The hasher streams plain files but reads the full payload when a `header_rule` is set (so it can apply the strip/byteswap before digesting). For very large ROMs with header rules (256+ MB N64 cartridges) this loads the whole file into RAM — acceptable for now since N64 cartridges max out at ~64 MB, but flag if we ever target Saturn/Dreamcast ISOs with header rules.

