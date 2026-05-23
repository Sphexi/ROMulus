# Session 16: Strict 1:1 — Exporter + Sync + Distinct-Content Toggle

**Type:** Build (Phase 4 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

The exporter and sync engine drop the `game_id` aggregation layer. Every ROM file becomes its own `<game>` element in `gamelist.xml`; sync actions stop carrying a `game_id` field; `dest_inventory` no longer tracks one.

For users with many byte-identical duplicates (the "I copied my library twice and want to see it" use case), gamelist.xml gets longer. That's correct per the strict 1:1 model — each rom shows up as its own entry on the device. Users who want a compact gamelist on the device side get a new toggle: **Export distinct content only**.

In one sentence: **"Exporter writes one `<game>` per rom; sync drops game_id from its payload; add a per-export 'distinct content only' filter."**

**Distinct-content export — semantics:**

- Default: OFF. Every rom row exports as its own `<game>` entry — exactly the user's existing behaviour, preserved.
- ON: For each set of rom rows sharing a SHA-1, only one is exported. The chosen rom prefers (a) `match_confidence='dat_verified'`, then (b) canonical extension (`.sfc` over `.smc`, etc., via the same `_EXTENSION_PREFERENCE` table the organizer uses), then (c) shorter filename, then (d) lower rom_id (deterministic tiebreak — same shape as `_pick_duplicate_keeper` in [core/organizer.py:312-340](../../src/romulus/core/organizer.py#L312-L340)).
- ROMs with no SHA-1 (Quick-Scan-only) are always exported (each is "distinct" by definition because we can't prove equality).
- Toggle is per-export, persisted in the export dialog's recent-options memory but not as a profile default.

**Carry-forward from prior sessions:**

- **Never modify files without preview** (design rule #4). Sync preview / export dialog shape unchanged; distinct-content toggle is just one more checkbox.
- **Atomic writes only** (design rule #5). No change to file-write paths.
- **Plan.dest_id is authoritative** (design rule #16). Sync apply still keys on `plan.dest_id`; only the per-action payload loses game_id.
- **Sync identity matcher tier 2** (design rule #15) keys on `(fuzzy_key, region, system_id)`. Region now reads from `roms.region` directly instead of joined-games. Same key, different table.
- **Sync diff is O(N+M)** (design rule #24). The `dest_by_fuzzy` index logic is unchanged — just sources its region column from a different place.
- **Artwork-only export mode** (design rule #25). `ExportOptions.include_roms` stays. Distinct-content toggle composes with it (artwork-only + distinct-content = refresh covers for one rom per content-cluster).
- **Per-system summary dialog** (design rule #26). `PerSystemExportCounts` / `PerSystemSyncCounts` gain a `skipped_as_duplicate` bucket only when distinct-content toggle is ON.

**Pre-1.0 plan-invalidation:** persisted `sync_plans` JSON rows in old databases reference `game_id` — those plans become invalid as the JSON shape changes. Per the user's "fresh libraries" directive, no compat shim. Worst case a user re-runs preview before apply; document in the apply path's load-error message.

**Tasks:**

- [ ] `src/romulus/core/exporter.py`:
  - `_build_rom_query` ([exporter.py:310-319](../../src/romulus/core/exporter.py#L310-L319)): drop the LEFT JOIN games. All identity columns (`title`, `canonical_name`, `region`, `revision`) read from `roms` directly.
  - Collection-scoped queries: `r.game_id IN (SELECT game_id FROM collection_games WHERE ...)` → `r.id IN (SELECT rom_id FROM collection_roms WHERE ...)`.
  - Gamelist generation loop ([exporter.py:660-756](../../src/romulus/core/exporter.py#L660-L756)):
    - Drop the `seen_game_ids` set and its dedup logic.
    - Cover lookup: `SELECT local_path FROM covers WHERE rom_id = ?`.
    - Metadata lookup: `SELECT ... FROM metadata WHERE rom_id = ?`.
    - Write one `<game>` per rom row.
  - Artwork copy loop ([exporter.py:835-883](../../src/romulus/core/exporter.py#L835-L883)): rename `game_id` → `rom_id`; drop `seen_game_ids`.
  - **Add `ExportOptions.distinct_content_only: bool = False`.**
  - **Implement distinct-content filtering** before the gamelist write loop:
    - Group candidate rows by `hashes.sha1` (a one-time `_select_distinct_keepers` helper that mirrors `_pick_duplicate_keeper`'s rank logic).
    - When toggle is ON, exclude rows that are not the keeper.
    - Rows with no SHA-1 always pass through.
  - **`PerSystemExportCounts`**: add a `skipped_duplicates` counter bucket; per-system summary dialog will surface it (Session 18 wires the dialog).
- [ ] `src/romulus/ui/export_dialog.py`:
  - Add a checkbox: "Export distinct content only (skip byte-identical duplicates)".
  - Tooltip explains the rule (per-system, prefers dat_verified + canonical extension + shorter filename).
  - Persist last value in the dialog's recent-options shape (whatever mechanism existing toggles use).
  - When the toggle is on AND a preview is running, display the projected skip count in the totals row.
- [ ] `src/romulus/core/sync.py`:
  - `LocalRom` ([sync.py:217-229](../../src/romulus/core/sync.py#L217-L229)): drop `game_id` field.
  - `_row_to_local_rom` ([sync.py:232-243](../../src/romulus/core/sync.py#L232-L243)): drop the `game_id` extract.
  - `SyncAction` ([sync.py:105-118](../../src/romulus/core/sync.py#L105-L118)): drop `game_id` field.
  - Every `_build_*_actions` function that populated `game_id=rom.game_id`: delete those assignments.
  - Tier-2 fuzzy match: `(fuzzy_key, region, system_id)` continues working — region reads from `roms.region` (set by scanner / Heavy Scan).
  - `dest_inventory` writes: drop the `game_id` field everywhere ([sync.py:1123, 1243, 1262](../../src/romulus/core/sync.py)).
  - `copy_artwork` ([sync.py:436](../../src/romulus/core/sync.py#L436)): cover lookup now `WHERE rom_id = ?` — `LocalRom.rom_id` is the key.
  - Persisted `sync_plans` JSON: shape change is inherent. On load, if a plan's JSON references `game_id` keys, raise a clear `ValueError("plan was created against an old schema; re-run preview")`.
- [ ] `src/romulus/core/dest_inventory.py`:
  - Drop the `game_id` column from inserts / selects.
- [ ] `src/romulus/ui/sync_preview.py`:
  - Any code that reads `action.game_id` — remove. The preview already displays per-row file information; nothing user-visible changes.
- [ ] `src/romulus/ui/workers.py`:
  - `SyncWorker`, `DestInventoryWorker`, `BuildSyncPlanWorker` — rename internal vars and signal payloads.
- [ ] `src/romulus/ui/per_system_summary_dialog.py`:
  - Add the `skipped_duplicates` column conditionally (only renders when count > 0 across any system — keeps the dialog tight for non-distinct exports).

**Test files affected** (Session 19 re-baseline):

- `tests/test_exporter.py` — gamelist row count changes; new tests for `distinct_content_only` toggle (true keeper picked per SHA-1 group; rows-without-SHA-1 all pass through; integration with `include_roms=False`).
- `tests/test_sync.py`, `tests/test_sync_preview.py`, `tests/test_sync_fixes.py` — drop `game_id` assertions; verify tier-2 still keyed on `(fuzzy_key, region, system_id)` correctly.
- `tests/test_per_system_summary_dialog.py` — new `skipped_duplicates` column smoke.

**Acceptance criteria:**

- Exporting a fixture library with 3 byte-identical roms and `distinct_content_only=False` produces 3 `<game>` entries in gamelist.xml.
- Same fixture with `distinct_content_only=True` produces 1 `<game>` entry — the one ranked highest (dat_verified > canonical ext > shorter name > lower rom_id).
- A rom row without a SHA-1 always exports regardless of the toggle.
- `LocalRom`, `SyncAction`, `dest_inventory` have no `game_id` field/column.
- Sync push_merge against a fixture destination still resolves tier-2 fuzzy matches correctly (regional variants stay distinct).
- An old `sync_plans` JSON row produces a clear "re-run preview" error on apply load.
- Ruff clean on `src/romulus/core/{exporter,sync,dest_inventory}.py` and `src/romulus/ui/export_dialog.py`.

STOP. Commit with message `refactor(exporter,sync): rom-keyed gamelist + distinct-content toggle; drop game_id from sync payload`. Move to Session 17.
