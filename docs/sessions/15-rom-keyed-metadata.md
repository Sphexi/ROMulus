# Session 15: Strict 1:1 — Metadata Enrichment + Covers + Sibling-Copy

**Type:** Build (Phase 3 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

Metadata and cover storage move from game-keyed to rom-keyed. Mechanically straightforward (every `game_id` becomes `rom_id`), but with one new piece of behaviour that's **critical to ship in this session**: a sibling-copy optimization that prevents TheGamesDB's monthly quota from being burned linearly by duplicate ROM copies.

The user's strict 1:1 model means 5 byte-identical copies of "Mario (USA)" are 5 distinct rom rows. Without intervention, the metadata enricher would make 5 separate API calls to fetch identical data. The fix: before any network source runs, look for an existing `metadata` row attached to any *other* rom with the same identity, and if one exists, copy it instead of fetching.

The same trick applies to covers — same `(system, canonical_name)` → same libretro-thumbnail blob. The on-disk image cache is already URL-keyed, so blob storage is unaffected; only the cover *DB rows* need to be inserted per rom.

In one sentence: **"Rename `game_id` → `rom_id` throughout enrichment; add sibling-copy so identical ROMs share metadata without re-fetching."**

**Carry-forward from prior sessions:**

- **Local-first metadata chain** (design rule #17). Order unchanged: libretro-database → GameDB → Hasheous → LaunchBox → ScreenScraper → TheGamesDB.
- **Metadata + covers are separate workflows** (design rule #18). `enrich_library` writes the `metadata` table only; cover discovery is `CoverFinderWorker`. That split stays.
- **Bundled offline sources are CRC32-keyed** (design rule #19). Lookup keys don't change. What changes is the *write* — `upsert_metadata(rom_id, …)` instead of `(game_id, …)`.
- **TheGamesDB monthly quota** (design rule #20). The new sibling-copy gate runs BEFORE any network source so a 5-copy library spends one TheGamesDB call instead of five.
- **Schema and query API from Session 13.** `find_sibling_metadata`, `copy_metadata`, `find_sibling_covers`, `copy_covers` exist as helpers — this session wires them in.

**Sibling-copy semantics:**

Sibling identity is checked in this priority order:
1. **SHA-1 match** — when both ROMs have a hash and they're equal. Highest-confidence sibling.
2. **`(system_id, canonical_name)` match** — when both ROMs are dat_verified to the same canonical name. Same variant, different paths.
3. **`(system_id, fuzzy_key)` match** — fallback for Quick-Scan-only libraries. Lower confidence; document the trade-off in `find_sibling_metadata`'s docstring.

A sibling row is copied verbatim into a new row for the destination `rom_id`. **No back-reference**, no "shared metadata" concept — each rom owns its own metadata row outright. If the user later edits one, the others are unaffected. That's deliberate; strict 1:1 means each rom is its own world.

**Tasks:**

- [ ] `src/romulus/db/queries.py` — flesh out the helpers stubbed in Session 13:
  - `find_sibling_metadata(conn, rom_id) -> sqlite3.Row | None`:
    - Look up the target rom's `(system_id, canonical_name)` and joined sha1.
    - Query: any *other* rom whose `(sha1 = ?)` OR `(system_id = ? AND canonical_name = ?)` AND has a `metadata` row. Return one such metadata row.
    - Return None when no sibling found.
  - `copy_metadata(conn, source_rom_id, dest_rom_id) -> None`:
    - INSERT a metadata row for dest from source's fields. Conflict on dest already having one: do nothing (rare race).
  - `find_sibling_covers(conn, rom_id) -> list[sqlite3.Row]`:
    - Same identity tiers as metadata. Return all cover rows from one chosen sibling rom.
  - `copy_covers(conn, source_rom_id, dest_rom_id) -> None`:
    - INSERT new cover rows for dest. Re-use the same `local_path` strings — the on-disk file is shared (the URL/file cache already dedups; both rom rows point at the same image on disk).
    - Re-establish `is_preferred` flags using `_ensure_preferred(dest_rom_id, cover_type)`.
- [ ] `src/romulus/metadata/__init__.py`:
  - **Rename throughout** (`_get_sha1_for_game` → `_get_sha1_for_rom`, etc.). `_get_sha1_for_rom` becomes a direct `SELECT sha1 FROM hashes WHERE rom_id = ?` — no join.
  - **Add the sibling-copy gate at the top of `_fetch_metadata_for_rom`**:
    ```python
    sibling = q.find_sibling_metadata(conn, rom_id)
    if sibling is not None:
        q.copy_metadata(conn, source_rom_id=sibling["rom_id"], dest_rom_id=rom_id)
        logger.debug("enrich: sibling-copied metadata rom_id=%d from=%d", rom_id, sibling["rom_id"])
        return True
    ```
    Skips the entire 6-source chain when a sibling already has metadata.
  - **`enrich_library` candidate query** (`get_roms_needing_enrichment`): joins through roms directly. Scope filters by `system_id`, `rom_ids` list, or `collection_id` (via `collection_roms`).
  - Every per-source helper (`_try_libretro_metadat`, `_try_gamedb`, `_try_hasheous`, `_try_launchbox`, `_try_screenscraper`, `_try_thegamesdb`) takes `rom_id` and calls `q.upsert_metadata(conn, rom_id, …)`.
  - `fetch_online_covers_for_scope` (the cover-side orchestrator) similarly takes rom_id and gains a sibling-cover gate.
- [ ] All six source modules under `src/romulus/metadata/`:
  - `libretro_metadat.py`, `gamedb.py`, `hasheous.py`, `launchbox.py`, `screenscraper.py`, `thegamesdb.py` — `game_id` → `rom_id` in every function signature, log line, and DB call. Pure rename.
  - `libretro.py` (cover fetcher) — same rename + sibling-cover gate.
- [ ] `src/romulus/core/local_cover_finder.py`:
  - `LocalCoverMatch.game_id` → `rom_id`.
  - `_get_roms_for_cover_scan` query: no more `WHERE game_id IS NOT NULL`. Replace with "ROMs that don't already have a `Named_Boxarts` cover" — that's the relevant filter post-refactor.
  - `_has_cover_for_path(conn, rom_id, local_path)` rename.
  - The discovery loop inserts into `covers` with `rom_id`. No code shape change beyond the rename.
- [ ] `src/romulus/ui/workers.py`:
  - `EnrichWorker`, `CoverFinderWorker` — scope kwargs rename `game_ids` → `rom_ids`. Logging strings updated.
- [ ] `src/romulus/ui/enrich_progress.py`, `src/romulus/ui/local_cover_progress.py`:
  - Progress strings update ("enriching game X" → "enriching ROM X"). Cosmetic.

**Test files affected** (Session 19 re-baseline):

- `tests/test_metadata.py` — every source-side test renames; new tests for sibling-copy gate (must short-circuit network calls on the second of two byte-identical ROMs).
- `tests/test_covers.py` — same rename + sibling-cover tests.
- `tests/test_local_cover_finder.py` — `game_id` → `rom_id` in dataclasses and fixtures.

**Acceptance criteria:**

- `metadata.rom_id` is the table's PK; no `game_id` column anywhere in metadata-side code.
- Enriching a fixture library with two byte-identical rom rows produces TWO `metadata` rows (one per rom) but makes only ONE network call to any of the three online sources. Asserted in a new Session 19 test.
- Cover finder produces per-rom `covers` rows; the on-disk image cache is unchanged in shape.
- Ruff clean on `src/romulus/metadata/` + `src/romulus/core/local_cover_finder.py`.
- Manual smoke (will be deferred to user testing after Session 19): enrich a library with known duplicates; confirm TheGamesDB allowance counter decrements by 1 per content-distinct entry, not per rom row.

STOP. Commit with message `refactor(metadata): rom-keyed enrichment + sibling-copy to preserve API quotas`. Move to Session 16.
