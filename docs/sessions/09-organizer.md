# Session 9: Library Organizer

**Type:** Build

**Context for this session:**

You are building the library organization system — the workflow where users can reorganize their ROM folder in place with a preview/commit model.

Organizer workflow (from TECHNICAL_PLAN.md §9 — Library Organizer):
1. Analyze library state from SQLite
2. Generate organize_plan with actions: merge_folder, rename, delete_duplicate, collision
3. Display in OrganizePreviewDialog with before/after view
4. User reviews, approves/rejects individual actions
5. Execute approved actions, update SQLite paths, record plan status

Organize rules:
- Folder merges: only confirmed aliases (validated against system.folder_aliases)
- Renames: only DAT-matched ROMs (L3 confidence). Never rename unmatched files.
- Duplicate removal: only byte-identical (same SHA-1). Prefer canonical extension (.sfc over .smc), then smaller filename.
- Hacks: never merge or deduplicate against originals
- Collisions: same name but different content → flag for manual review, never overwrite

**Carry-forward from prior sessions (sessions 5–8):**

- **Atomic file moves.** When renaming or moving ROM files inside `execute_plan`, use the `tempfile.mkstemp` + `os.replace` pattern established in [src/romulus/metadata/libretro.py](../../src/romulus/metadata/libretro.py) `fetch_cover` (Session 6, hardened in Session 8). Write to a temp file in the destination directory, then `os.replace` to the final path — atomic on both POSIX and Windows, survives a mid-operation crash, and avoids leaving half-renamed ROMs behind.
- **OrganizeWorker signal contract.** Any new QThread worker MUST mirror [src/romulus/ui/workers.py](../../src/romulus/ui/workers.py) `ScanWorker` (Session 5) and `EnrichWorker` (Session 6): open a thread-local `sqlite3.Connection` inside `run()` (never share the main thread's connection), emit `progress(int, str)` per item, `finished_ok(...)` on success with a result struct, `failed(str)` on exception, and support cooperative cancel via a private exception raised from inside the progress callback.
- **MainWindow integration.** Add an `isRunning()` guard to the Organize toolbar handler so a double-click can't race two workers on the same DB, and extend `closeEvent` to `requestInterruption` + `wait(5000)` on the live worker before the window closes — both patterns landed in Session 8 for the scan/enrich workers and the organize worker must follow the same rules.

Action types:
- `merge_folder`: move all files from source folder to target folder (source is an alias of target)
- `rename`: rename file to canonical No-Intro name
- `delete_duplicate`: remove redundant copy (same SHA-1 as another file being kept)
- `collision`: source and target have same filename but different sizes/hashes — needs manual resolution

**Tasks:**

- [x] Create `src/romulus/core/organizer.py`:
  - `analyze_library(conn)` — scan for organizeable actions, return OrganizePlan
  - `find_alias_merges(conn)` — identify folders that are aliases of the same system and have overlapping content
  - `find_renameable_roms(conn)` — ROMs with dat_match that differ from current filename
  - `find_duplicates(conn)` — ROMs with same SHA-1 in same or alias folders
  - `find_cross_extension_dupes(conn)` — same game, same folder, different extensions (.smc + .sfc)
  - `detect_collisions(merge_pairs)` — files that would collide during a folder merge
  - `execute_plan(conn, approved_actions, progress_callback)` — execute filesystem changes and update DB
  - Rollback per-action on failure: if rename/move fails, skip it, log error, continue
- [x] Add organize queries to `db/queries.py`:
  - `get_alias_folder_pairs(conn)` — folders sharing a system_id that aren't the canonical folder
  - `get_duplicate_groups(conn)` — groups of ROMs with identical SHA-1
  - `update_rom_path(conn, rom_id, new_path)` — after rename/move
  - `delete_rom(conn, rom_id)` — after duplicate removal
  - `insert_organize_plan(conn, plan_json)`, `update_plan_status(conn, plan_id, status)`
- [x] Create `src/romulus/ui/organize_preview.py`:
  - OrganizePreviewDialog(QDialog):
    - Summary header: "X files to rename, Y folders to merge, Z duplicates to remove"
    - QTreeView showing proposed changes grouped by action type
    - Checkbox per action (all checked by default)
    - Collision section with side-by-side comparison (filename, size, hash)
    - "Select All" / "Deselect All" buttons
    - "Apply" button (executes checked actions)
    - "Cancel" button
    - Progress bar during execution
- [x] Wire "Organize" toolbar button to open the preview dialog
- [x] Write tests:
  - `tests/test_organizer.py`:
    - Test alias merge detection
    - Test duplicate finding (same hash)
    - Test cross-extension dedup (.smc + .sfc)
    - Test collision detection
    - Test plan execution with mock filesystem (use tmp_path)
    - Test rollback on failure

**Acceptance criteria:**
- Organize button opens preview dialog with proposed changes
- Before/after view shows what will change
- User can approve/reject individual actions
- Apply executes changes and updates SQLite
- Collisions flagged for manual review
- Hacks never merged with originals
- All tests pass, ruff clean

STOP. Commit with message "Session 9: Library organizer with preview/commit". Do not proceed to Session 10.

## Completion Summary
**Status:** COMPLETE
**Date:** 2026-05-14
**What was built/changed:**
- `src/romulus/core/organizer.py` — full library organizer: `OrganizeAction` / `OrganizePlan` dataclasses, four detectors (`find_alias_merges`, `find_renameable_roms`, `find_duplicates`, `find_cross_extension_dupes`), `detect_collisions`, `analyze_library`, and `execute_plan` with per-action SAVEPOINT rollback and atomic file moves via `tempfile.mkstemp` + `os.replace` (mirrors `libretro.fetch_cover`).
- `src/romulus/db/queries.py` — new organize helpers: `get_alias_folder_pairs`, `get_duplicate_groups`, `get_dat_matched_roms`, `update_rom_path`, `delete_rom`, `insert_organize_plan`, `update_plan_status`, plus a small `datetime_now_iso` helper.
- `src/romulus/ui/organize_preview.py` — `OrganizePreviewDialog` with a grouped `QTreeView`, per-action checkboxes (collisions read-only), Select/Deselect All, progress bar, and `actions_approved` signal.
- `src/romulus/ui/workers.py` — `OrganizeWorker` (mirrors ScanWorker/EnrichWorker contract: thread-local connection, `progress(int, int, str)`, `finished_ok(int, int, int, list)`, `failed(str)`, cooperative cancel via `_OrganizeCancelledError`).
- `src/romulus/ui/main_window.py` — wired Organize toolbar + menu actions with `isRunning()` guard, plumbed the worker through the preview dialog, and added `_organize_worker` to `closeEvent`'s cancel/wait loop.
- `tests/test_organizer.py` — 31 tests covering detection, execution, atomic moves, rollback on mid-plan failure, queries, the preview dialog, and the MainWindow concurrency guard.

**Tests:** 387 passed, 1 skipped (POSIX-only chmod test in `test_db.py`). Net delta: +31 tests vs the 356/1 baseline.
**Config changes:** None.
**Breaking changes:** None.
**Carry-forward notes:**
- **Atomic move helper:** `romulus.core.organizer._atomic_replace(Path, Path)` is the shared rename/move primitive — Session 10's exporter should reuse it (or factor it out to a shared module) instead of writing a new copy.
- **Plan persistence:** `insert_organize_plan` writes JSON into `organize_plans.plan_json` but the UI does not yet display history. Session 11 (review) may want to surface that.
- **Connection sharing during organize:** `OrganizeWorker` opens a fresh sqlite3 connection on the worker thread (WAL mode is enabled). The main-thread `MainWindow._conn` stays open during execution — relies on WAL mode for concurrency, same as ScanWorker/EnrichWorker.
- **Cross-extension dedup keeper preference:** `_EXTENSION_PREFERENCE` is the single source of truth for "canonical" file extensions. Session 10's exporter should reuse this when picking which ROM to emit per game.
- **Reusable query:** `get_dat_matched_roms(conn)` returns Layer-3-verified ROMs — Session 10 will likely want this for export filtering.
- **UI behaviour not unit-tested:** progress bar visual updates during a live worker run, drag-to-resize column behaviour, and the visual rendering of the QTreeView's collision section (asserted only via the model, not pixels). The end-to-end "click Apply -> worker runs -> dialog closes" round-trip would need pytest-qt to drive properly.
