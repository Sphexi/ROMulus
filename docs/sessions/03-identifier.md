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

- [ ] Create `src/romulus/core/identifier.py`:
  - `extract_header_title(file_path, header_rule)` — read internal title based on system's header_rule
  - Returns None for systems without headers
- [ ] Create `src/romulus/core/hasher.py`:
  - `hash_rom(file_path, header_rule)` — compute CRC32 + SHA-1 with header stripping and zip extraction
  - `normalize_rom_content(content, header_rule)` — apply strip/byteswap rules
  - `hash_library(conn, progress_callback, workers=8)` — parallel hash all unhashed/changed ROMs using ThreadPoolExecutor
  - Hash cache check: skip if (path, mtime, size) unchanged since last hash
- [ ] Create `src/romulus/core/dat_parser.py`:
  - `parse_dat_file(filepath)` — parse single DAT, return list of DatEntry records
  - `load_all_dats(conn, dat_paths)` — parse all DATs from bundled + user folders, insert into dat_entries table
  - `match_hashes(conn)` — for all hashed ROMs, look up in dat_entries by SHA-1 then CRC32+size. Update rom.dat_match and rom.match_confidence.
  - `parse_region_from_name(game_name)` — extract region tags from canonical name
- [ ] Add DAT-related queries to `db/queries.py`:
  - `insert_dat_entry(conn, entry)`, `get_dat_by_sha1(conn, sha1)`, `get_dat_by_crc_size(conn, crc32, size)`, `update_rom_match(conn, rom_id, dat_match, confidence)`
- [ ] Add hash-related queries to `db/queries.py`:
  - `upsert_hash(conn, rom_id, crc32, sha1, md5)`, `get_hash(conn, rom_id)`, `get_unhashed_roms(conn)`, `get_stale_hashes(conn)` (mtime changed)
- [ ] Copy bundled No-Intro DAT files into `data/dats/`. Include DATs for the ~30 systems in the system registry. (If actual DAT files aren't available at dev time, create 2-3 small test DAT files with known entries for testing.)
- [ ] Write tests:
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
