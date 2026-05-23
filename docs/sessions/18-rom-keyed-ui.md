# Session 18: Strict 1:1 — UI: Detail Panel + Game Table + Signals

**Type:** Build (Phase 6 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

The UI layer reskinned to match the rom-keyed model. The biggest concrete change is the **detail panel** — today it queries by `game_id` and uses `LIMIT 1` without ordering to pick "the" SHA-1 / "the" dat_match, which is what produced the user's bug report of two regional variants displaying identical metadata. With one rom per "game", the panel reads its fields directly and unambiguously from the selected rom row.

The game table reshapes too: every rom file is one row, naturally sorted to cluster variants and duplicates together so the user can see them by scrolling. The user explicitly asked for this behaviour ("I want to be able to see easily by searching or sorting if I have multiple copies of the same ROM").

Signal names rename `game_*` → `rom_*` throughout `ui/`.

In one sentence: **"Detail panel reads per-rom; table shows one row per rom; every signal/handler renames."**

**Carry-forward from prior sessions:**

- **No external CDN/JS dependencies** (design rule #2). All theme / artwork stays local.
- **Hide-when-empty grid rows** (existing detail-panel idiom). `_set_field` continues to hide both label and value when the value is empty/None. The fields that *go away entirely* (DAT name as ambiguous-across-variants, ROM files block) get removed wholesale — not hidden conditionally.
- **Match badge** stays. With one rom per "game", the badge unambiguously reflects this rom's `match_confidence`.
- **Cover viewer** keeps its prev/next/preferred controls. Cover rows are now rom-keyed; the queries swap accordingly.
- **System sidebar counts** already query `roms` directly (see CLAUDE.md current state). Verify nothing breaks; no logic change expected.
- **Tri-state group headers + right-click bulk toggle** on preview dialogs (design rule #27). Organize / Sync / Verify Library / Import preview dialogs all keep this; row models update to whatever the rom-keyed actions look like.

**Detail panel field changes:**

Today's fields:
- Region, Revision, ROM size, SHA-1, DAT name (game-keyed, LIMIT 1 ambiguity), Genre, Developer, Publisher, Released, Players, Rating + description + ROM files block.

After this session:
- Region, Revision, ROM size, SHA-1, DAT name, Path (new), Match confidence (moved from badge into grid for clarity if desired — or keep badge), Genre, Developer, Publisher, Released, Players, Rating + description.
- **ROM files block deleted.** Per the user's directive in the scope discussion. With one rom per detail panel, the block was redundant — the path moves into the grid as a regular row.
- **DAT name** now unambiguous (this rom's `dat_match` if dat_verified, else `canonical_name`, else None).
- **SHA-1** now unambiguous (this rom's hash directly via `hashes.rom_id`).

**Tasks:**

- [ ] `src/romulus/ui/detail_panel.py`:
  - Rename internal state: `self._game_id` → `self._rom_id`.
  - Rename method: `update_game(game_id)` → `update_rom(rom_id)`.
  - Rename property: `current_game_id` → `current_rom_id`.
  - `update_rom` body:
    - Fetch the rom row via `q.get_rom_by_id(conn, rom_id)` (renamed in Session 13).
    - Fetch metadata via `q.get_metadata(conn, rom_id)`.
    - Fetch covers via `q.get_covers(conn, rom_id)`.
    - No more `get_roms_for_game` call — the rom IS the unit.
  - Delete `_first_sha1_for_game`, `_best_dat_match`, `_best_confidence`, `_sum_rom_size`, `_format_rom_list`. Replace with direct reads from the rom row:
    - `sha1`: `SELECT sha1 FROM hashes WHERE rom_id = ?` (one rom, one hash).
    - `dat_name`: `rom["canonical_name"]` or `rom["dat_match"]`.
    - `match_confidence`: `rom["match_confidence"]`.
    - `size`: `rom["size_bytes"]`.
  - Delete the ROM files QTextEdit + QScrollArea (lines around [detail_panel.py:265-276](../../src/romulus/ui/detail_panel.py#L265-L276)).
  - Add a new `path` row to the metadata grid (full forward-slash path; `setTextInteractionFlags` selectable so users can copy it).
  - Favorite / collection buttons: rename `q.add_game_to_collection` calls to `add_rom_to_collection`, similar for remove / is_in / ensure_favorites.
  - Description hide-when-empty stays.
- [ ] `src/romulus/ui/game_table.py`:
  - Rename `GameRow` → `RomRow`; `game_id` field → `rom_id`.
  - `rows_from_db(conn, system_id, rom_ids, limit)`: query roms directly. Identity fields now on `roms` (no games join). For convenience, also LEFT JOIN `hashes` to surface SHA-1 in the row model (useful for the new sort behaviour below).
  - `selected_game_id` property → `selected_rom_id`.
  - `select_game(game_id)` → `select_rom(rom_id)`.
  - Signal renames:
    - `game_selected` → `rom_selected`
    - `add_to_favorites_requested` → keeps name shape but payload is rom_id
    - `remove_from_collection_requested` → payload rom_id
    - `enrich_game_requested` → `enrich_rom_requested`
    - `heavy_scan_game_requested` → `heavy_scan_rom_requested`
    - `find_local_covers_game_requested` → `find_local_covers_rom_requested`
  - **Default sort**: `(system_id ASC, title ASC, region ASC, revision ASC, path ASC)`. This naturally clusters variants and duplicates adjacent to each other — the user's explicit request.
  - Right-click context menu (Reveal in Explorer / Delete this ROM) is already rom-keyed; verify it still binds to rom_id.
  - Add a "Duplicates" indicator column (optional but recommended): a small badge or count rendered when the row's SHA-1 matches another row's. Backing query: `COUNT(*) - 1 OVER (PARTITION BY sha1)`. Adds a window function; SQLite 3.25+ supports it (Python's bundled SQLite is 3.35+ on Python 3.12). Default visible; user can hide via column header right-click.
- [ ] `src/romulus/ui/main_window.py`:
  - Handler renames:
    - `_on_game_selected(game_id)` → `_on_rom_selected(rom_id)`. Body calls `self.detail_panel.update_rom(rom_id)`.
    - `_on_add_to_favorites` payload is rom_id; calls `q.add_rom_to_collection(...)`.
    - `_on_remove_from_collection` similar.
  - Scoped action handlers:
    - `_heavy_scan_scoped(rom_id=None, ...)` — directly use rom_id, no `get_rom_ids_for_scope(game_id=...)` translation needed since the scope IS the rom.
    - `_enrich_scoped(rom_ids=None, ...)` — list of rom_ids.
    - `_find_local_covers_scoped(rom_id=None, ...)` — single rom.
  - Signal connections in `__init__` updated to new signal names.
  - Worker storage attrs that named `game_*` rename to `rom_*` where applicable.
- [ ] `src/romulus/ui/workers.py`:
  - `EnrichWorker`, `CoverFinderWorker`, `HeavyScanWorker` — scope kwarg renames done in Session 15; verify the worker signatures and emitted progress strings match.
  - `_DbWorker` base class: no changes.
- [ ] `src/romulus/ui/system_sidebar.py`:
  - System counts: confirm the count query still reads `COUNT(*) FROM roms WHERE system_id = ?` or equivalent. With variants no longer collapsed, counts naturally increase — that's correct behaviour, not a bug.
- [ ] `src/romulus/ui/organize_preview.py`, `src/romulus/ui/sync_preview.py`, `src/romulus/ui/import_dialog.py`, `src/romulus/ui/scrub_dialog.py`, `src/romulus/ui/per_system_summary_dialog.py`:
  - Any code reading `action.game_id` or `row["game_id"]` — rename to `rom_id`.
  - `_grouped_tree.py` mixin: no change (it's payload-agnostic).
- [ ] `src/romulus/ui/enrich_options_dialog.py`, `src/romulus/ui/cover_options_dialog.py`:
  - Strings only ("Enrich N games" → "Enrich N ROMs" where appropriate). No structural change.

**Test files affected** (Session 19 re-baseline):

- `tests/test_ui.py` — 57 game_id touchpoints. Most rename to rom_id. Detail panel tests assert the unambiguous per-rom SHA-1 / dat_match display.
- Smoke test for the new "Duplicates" indicator column if it ships.

**Acceptance criteria:**

- Selecting two byte-identical rom rows in the table updates the detail panel with the SAME metadata + SAME SHA-1 + SAME path-for-each-row (modulo path differs). Selecting USA-variant then Europe-variant updates with DIFFERENT region / dat_match / SHA-1 — fixing the bug the user reported.
- Detail panel has no ROM files block; the rom's path lives in the metadata grid as a regular row.
- Game table default sort clusters variants and duplicates adjacent.
- All UI signals carry `rom_id`, no `game_id` references anywhere in `src/romulus/ui/`.
- System sidebar counts reflect rom-row count per system (numbers will be higher than before — expected).
- Ruff clean on `src/romulus/ui/`.
- Manual smoke (post-Session 19): launch the app, select two regional variants from the user's library, confirm they show distinct SHA-1 / region / dat_match.

STOP. Commit with message `refactor(ui): rom-keyed detail panel + game table; signal rename; drop ROM files block`. Move to Session 19.
