# Strict 1:1 ROM ↔ Game Data Model — Design Reference

**Version:** v0.4.0
**Implemented in:** sessions 13–19 (commits `00c2e2b` through `83e74f2`)
**Status:** shipped

---

## 1. Background — why we moved from N:1 to 1:1

In v0.1.0–v0.3.0 ROMulus used a "1 game : N ROMs" model: a separate
`games` table held identity columns (`title`, `canonical_name`, `region`,
`revision`, `is_hack`, `is_homebrew`, `is_bios`) and multiple `roms` rows
could share one `games` row. The intent was to give the user a
"one logical game" view over variant copies.

In practice the grouping introduced several problems:

1. **Variants were silently collapsed.** USA and Europe versions of the
   same title shared a `games` row. The detail panel, which queried by
   `game_id` with `LIMIT 1`, showed whichever rom sorted first in the
   database — so clicking the Europe variant could display USA metadata.

2. **Cross-extension false-positive deduplication.** `find_cross_extension_dupes`
   proposed deleting the "wrong" extension for any two roms that shared
   a `game_id`, which included regional variants that happened to have
   the same filename stem but different content. Users saw proposals to
   delete `Zelda (USA).sfc` because `Zelda (Europe).sfc` shared the
   game row.

3. **Duplicate visibility required joining through games.** To see that
   a library held five copies of the same ROM, the user had to know to
   look at the SHA-1 column. There was no natural "sort by content" view.

4. **FK complexity.** Every downstream table (`metadata`, `covers`,
   `collection_games`, `dest_inventory`) had to carry `game_id` alongside
   `rom_id`, and cleanup helpers like `prune_orphan_games` and
   `_delete_game_dependents` existed to manually maintain referential
   integrity because cascade deletes weren't declared.

The v0.4.0 refactor removes the `games` table entirely. The ROM file
is the identity unit. Byte-identical copies at two paths are two rows.
Duplicates surface by sorting on SHA-1 or by running Organize.

---

## 2. Model

### 2.1 The `roms` table is the identity unit

All columns that used to live on `games` are now columns on `roms`:

```sql
-- Identity columns on roms (v0.4.0+):
title           TEXT,         -- display title (parsed from filename by scanner)
canonical_name  TEXT,         -- No-Intro canonical name (set by Heavy Scan / DAT match)
region          TEXT,         -- e.g. "USA", "Europe", "Japan"
revision        TEXT,         -- e.g. "Rev 1", "Rev A"
is_hack         INTEGER NOT NULL DEFAULT 0,
is_homebrew     INTEGER NOT NULL DEFAULT 0,
is_bios         INTEGER NOT NULL DEFAULT 0
```

The `game_id` column on `roms` is gone. The `games` table does not exist.

### 2.2 Downstream tables FK to `roms`, not `games`

| Table | Old PK/FK | New PK/FK | Cascade |
|---|---|---|---|
| `metadata` | `game_id INTEGER PRIMARY KEY REFERENCES games(id)` | `rom_id INTEGER PRIMARY KEY REFERENCES roms(id)` | `ON DELETE CASCADE` |
| `covers` | `game_id INTEGER REFERENCES games(id)` | `rom_id INTEGER REFERENCES roms(id)` | `ON DELETE CASCADE` |
| `collection_roms` (formerly `collection_games`) | `game_id REFERENCES games(id)` | `rom_id REFERENCES roms(id)` | `ON DELETE CASCADE` |
| `dest_inventory` | had `game_id` column | column dropped; `rom_id` is sole anchor | no CASCADE (explicit delete helper) |

### 2.3 `ON DELETE CASCADE` replaces manual cleanup

Deleting a `roms` row automatically deletes its `metadata`, `covers`, and
`collection_roms` rows. `prune_orphan_games` and `_delete_game_dependents`
are deleted from the codebase.

`hashes` and `dest_inventory` do not declare CASCADE (adding CASCADE to an
existing SQLite table with data requires a table recreate). The existing
`_delete_rom_dependents` helper handles these two tables explicitly in
chunks of 500 ids before the rom delete.

---

## 3. Identity rules

### 3.1 Quick Scan populates identity at upsert time

The scanner parses the filename using `parse_no_intro_tokens` (a shared
parens/bracket token parser in `src/romulus/core/_no_intro_tokens.py`) and
writes the extracted fields onto the `roms` row at `upsert_rom` time:

- `title` — filename with tags stripped
- `region` — from `(USA)`, `(Europe)`, `(Japan)` etc.
- `revision` — from `(Rev 1)`, `(Rev A)` etc.
- `is_hack` — set when `[h]` or `[hH]` is detected
- `is_homebrew` — set when `(Homebrew)` or `(Unl)` is detected (combined with other signals)

The `COALESCE(EXCLUDED.field, roms.field)` pattern in the UPSERT SQL means
that passing `None` for an identity field on rescan preserves any value
written by a previous scan or Heavy Scan.

### 3.2 Heavy Scan updates identity in place

After a DAT match, `_update_identity_from_dat` in `core/dat_parser.py`
writes:

- `rom.canonical_name` — canonical filename from the DAT entry
- `rom.region` — parsed from the DAT game name
- `rom.revision` — parsed from the DAT game name
- `rom.match_confidence = "dat_verified"`

This is an in-place UPDATE on the `roms` row. No separate `games` row is
created or linked. The UPSERT contract means a re-run of Heavy Scan on an
unchanged file is a no-op (the hash cache is reused).

---

## 4. Sibling-copy gate

### 4.1 Rule definition

Before any network metadata source runs for a given `rom_id`, the
enrichment orchestrator checks whether another rom already has a
`metadata` row that shares the same identity. If so, it copies the row
rather than fetching from the network.

This is critical for the 1:1 model: without it, five byte-identical copies
of "Super Mario World (USA)" would each trigger a TheGamesDB call,
multiplying API quota consumption by the number of duplicate copies.

### 4.2 Identity tier priority

`find_sibling_metadata(conn, rom_id)` tries three tiers in order:

1. **SHA-1 match** — joins `hashes` on `sha1 = ?`. Highest confidence.
   Fires when both roms have been through Heavy Scan.
2. **`(system_id, canonical_name)` match** — requires both roms to be
   `dat_verified`. Same canonical name means the same logical content.
3. **`(system_id, fuzzy_key)` match** — fallback for Quick-Scan-only
   libraries. Lower confidence; documented in the function's docstring.

The same three tiers apply to `find_sibling_covers`.

### 4.3 When it fires

The gate fires at the top of `_fetch_metadata_for_rom` before the six-source
chain is attempted:

```python
sibling = q.find_sibling_metadata(conn, rom_id)
if sibling is not None:
    q.copy_metadata(conn, source_rom_id=sibling["rom_id"], dest_rom_id=rom_id)
    return True   # skip the entire chain
```

On a cache miss the chain proceeds normally (libretro-database → GameDB →
Hasheous → LaunchBox → ScreenScraper → TheGamesDB).

### 4.4 Cover rows share the on-disk file

`copy_covers` inserts new `covers` rows for the destination rom but reuses
the same `local_path` string as the source rom. Both rows point at the
same cached image file on disk. This is safe: the cover cache is
append-only; cover files are never deleted or modified in place.

---

## 5. Distinct-content export toggle

`ExportOptions.distinct_content_only` (default `False`) exists because with
strict 1:1 a library that has five byte-identical copies of the same ROM
would normally produce five `<game>` entries in `gamelist.xml` and copy five
files to the destination.

When the toggle is ON, the exporter groups candidate roms by SHA-1 and
exports only the keeper from each group. The keeper rank:

1. `match_confidence = 'dat_verified'` wins over unverified.
2. Canonical extension (`.sfc` over `.smc`, `.z64` over `.v64`, etc.) per
   the `_EXTENSION_PREFERENCE` table in `core/organizer.py`.
3. Shorter filename.
4. Lower `rom_id` (deterministic tiebreak).

ROMs with no SHA-1 (Quick-Scan-only) always export regardless of the toggle
— we cannot prove equality without a hash.

The toggle is per-export and persisted in the export dialog's recent-options
memory. It is not a profile-level default.

---

## 6. Trade-offs accepted

| Trade-off | Accepted because |
|---|---|
| `covers` table row count grows ~N× where N = duplicate copies | On-disk images are shared; only DB rows multiply. The sibling-copy gate prevents N× network fetches. Manageable at the scale of real-world libraries. |
| `gamelist.xml` on the destination lists all copies (not just one) | `distinct_content_only` toggle mitigates this when the user wants compact device-side lists. |
| Pre-v0.4.0 databases are incompatible | Consistent with the project's stated policy (no migrations before v1.0). The app detects old DBs via `PRAGMA table_info(games)` and surfaces a clear "wipe and rescan" dialog. |
| `sync_plans` JSON from pre-v0.4.0 is rejected | `load_plan` raises `ValueError("plan was created against an old schema; re-run preview")` when it finds `game_id` keys in the payload. |

---

## 7. Future work

- **Per-rom region-specific cover fetching.** The sibling-copy gate uses the
  first matching metadata row regardless of region. A future enhancement
  could prefer region-matched covers (e.g. the Europe version gets the PAL
  box art rather than the USA NTSC art).
- **"Browse by base title" UI grouping.** A collapsible row group in the
  game table that clusters `(system_id, canonical_name)` siblings would
  let the user see "Super Mario World (USA) × 3" collapsed and expand to
  see the individual copies. This is a display layer on top of the existing
  1:1 DB — the data model already supports it.
- **Cover deduplication pass.** A maintenance tool that finds `covers` rows
  pointing at the same `local_path` and consolidates the DB rows (keeping
  one per `(rom_id, cover_type)`) could reduce the covers table back toward
  N:1 storage without changing the on-disk layout.

---

## 8. Cross-references

For the implementation detail of each phase of the refactor:

| Session | Scope | File |
|---|---|---|
| 13 | Schema + queries layer (drop games, expand roms, cascade FKs) | `docs/sessions/13-rom-keyed-schema.md` |
| 14 | Scanner (write identity fields onto roms; drop grouping phase) | `docs/sessions/14-rom-keyed-scanner.md` |
| 15 | Metadata + covers (rom-keyed enrichment + sibling-copy gate) | `docs/sessions/15-rom-keyed-metadata.md` |
| 16 | Exporter + sync (one `<game>` per rom; distinct-content toggle; drop game_id from sync payload) | `docs/sessions/16-rom-keyed-exporter-sync.md` |
| 17 | Organizer (delete cross-ext detector; Bug 2 TOCTOU fix; Bug 3 collision fix) | `docs/sessions/17-rom-keyed-organizer.md` |
| 18 | UI (rom-keyed detail panel; signal rename; drop ROM Files block) | `docs/sessions/18-rom-keyed-ui.md` |
| 19 | Test re-baseline (1,015 passing, 8 skipped) | `docs/sessions/19-rom-keyed-tests.md` |
