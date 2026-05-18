# Session 12: Import ROMs from a Staging Folder

**Type:** Build

**Context for this session:**

You are building the Import workflow — the inbound counterpart to the existing Sync engine. Users point at a staging folder (Downloads, a USB stick, a mounted archive) and ROMulus walks it, identifies the files via the same L1/L2 (and optional L3) pipeline the scanner already uses, builds a per-file plan, presents it for confirmation, and then atomically copies (or moves) the approved files into the configured `library_root` under the right system folders. Dupes are detected at three levels (path / filename / hash) and surface as conflicts the user resolves with the same "skip / replace / keep-both / apply to all" UX the sync preview already uses.

Full design spec — read this before starting: [`docs/import-design.md`](../import-design.md).

In one sentence: **"Sync, but inbound from a staging folder into the library."**

**Workflow:**

1. User picks a staging folder via a file-picker (must be outside `library_root`).
2. ROMulus walks + identifies the folder using the existing scanner / identifier pipeline (L1 filename fuzzy, L2 header sniff, optional L3 hash + DAT match if "Heavy identify before import" is ticked).
3. An `ImportPlan` is built — one `ImportAction` per staging file with resolved system, target path, conflict status, and proposed resolution.
4. `ImportDialog` shows the plan with totals at the top ("N new, M dupes, K conflicts"), per-action resolution dropdowns, and a system-folder-creation summary for previously-unseen systems.
5. User confirms; an `ImportWorker` on a QThread executes the plan: creates missing system folders, atomically copies (or moves) each file, enrolls new rows via `upsert_rom`, and reports per-file progress.

**Duplicate detection — three levels** (see import-design.md §"Duplicate detection"):

| Level | Check | Default action |
|---|---|---|
| **Path** | A file already lives at the planned target path | Skip |
| **Filename** | Same basename, different content (size or hash differs) | Conflict → user picks `replace` / `keep-both` / `skip` (default `skip`) |
| **Hash** | Same hash exists elsewhere in the library under a different filename | Report as "already in library (different name)" and skip |

**System folder creation** when a staging file resolves to a system that has no folder under `library_root` yet (see import-design.md §"System folder creation"):

1. The preview flags the system with a `(new)` badge.
2. Apply step shows a single confirmation listing every previously-unseen system with per-system checkboxes; opted-out systems either route to `_unsorted/` or drop from the plan.
3. New folders use the **first** entry in the system's `folder_aliases` list (matches Organize / Export's canonical folder picker).

**Carry-forward from prior sessions:**

- **Atomic writes only.** Every file copy/move MUST go through [`romulus.core.atomic`](../../src/romulus/core/atomic.py) — `atomic_copy` for `copy` actions, `atomic_replace` to swap the temp file into place. The `move` action is implemented as `atomic_copy` followed by `Path.unlink()` of the source only after the copy succeeds; never unlink before the copy completes, or a partial transfer leaves the source destroyed. This is the same pattern the Sync engine uses in `apply_plan`.
- **ImportWorker signal contract.** Mirror [`romulus.ui.workers`](../../src/romulus/ui/workers.py) — thread-local `sqlite3.Connection` opened inside `run()`, emit `progress(int, int, str)` per item (current / total / filename), `finished_ok(summary)` on success, `failed(str)` on exception, and support cooperative cancel via `_WorkerCancelled` raised from inside the progress callback (the shared base in `_DbWorker` already wires the `failed` signal). The dialog should add the worker to `MainWindow._import_worker` and `closeEvent` should `requestInterruption` + `wait(5000)` like the scan / enrich / organize / export / sync workers do.
- **Reuse the identifier pipeline.** `core.scanner._resolve_system_for_directory`, `core.identifier.parse_header`, and `core.hasher.hash_rom` are the same code paths Quick Scan / Heavy Scan use — call them directly rather than re-implementing identification logic. The L3 hash + DAT match step is optional and only runs when the user opts in via the dialog checkbox.
- **Sync engine's conflict-resolution UX is the template.** The per-action dropdown + "apply to all remaining" affordance in [`sync_preview.py`](../../src/romulus/ui/sync_preview.py) is the closest UX precedent in the codebase. Lift the row model / dropdown delegate / batch-apply button from there rather than designing fresh widgets — keeping the two preview dialogs consistent matters for muscle-memory.
- **MainWindow integration.** Add a `Tools → Import ROMs…` menu entry and a toolbar button. Both handlers must guard against double-clicks with an `isRunning()` check on `_import_worker`, the same way the existing scan / enrich / sync handlers do.
- **Single library at a time.** The import target is always the current `library_root` from config — there is no "import into a different library" mode. Refuse to import if the staging folder is `library_root` or a subdirectory of it, to prevent self-recursion footguns.
- **Upsert, don't insert.** Newly imported ROMs go through `queries.upsert_rom` (the same path the scanner uses). The path-keyed UPSERT handles the case where the user accidentally imports a file whose target path matches an existing `missing=1` row — that row is un-tombstoned rather than duplicated.

**Open questions to resolve during this session** (see import-design.md §"Open questions"):

- **Copy vs move default?** Default to `copy` (preserves the staging area for re-imports and rollback). `Move` is opt-in via a checkbox at the top of the dialog.
- **Multi-ROM archives.** The scanner already treats `.zip` / `.7z` as ROM containers per system. v1 imports archives verbatim without unpacking. If an archive plausibly contains multiple ROMs (e.g. >1 ROM extension visible at the central-directory level), badge it as `multi-rom-archive` and default to skip; full handling is out of scope.
- **Post-import auto-actions.** No automatic Enrich Metadata / Find Covers run. Add a small "Post-import: also run…" checklist at the top of the dialog so the user can opt in to either, but don't trigger by default.
- **Dry-run export of the plan.** The preview *is* a dry run. Add a "Save plan as JSON…" button on the preview dialog so the user can audit / archive / re-run an import plan later. The plan JSON file format should be self-describing (versioned, includes the staging folder path + per-file resolution).

**Tasks:**

- [ ] Create `src/romulus/core/importer.py`:
  - `@dataclass ImportAction` — `source_path: Path`, `target_path: Path`, `system_id: str | None`, `status: Literal["new", "dupe_path", "dupe_filename", "dupe_hash", "multi_rom_archive"]`, `resolution: Literal["copy", "move", "skip", "replace", "keep_both"]`, `confidence: str` (matches the scanner's confidence vocabulary).
  - `@dataclass ImportPlan` — `actions: list[ImportAction]`, `staging_root: Path`, `library_root: Path`, `created_systems: set[str]` (systems that don't yet have a folder), `heavy_identify: bool`, `total_bytes: int`.
  - `analyse_import(conn, staging_path, options) -> ImportPlan` — walks the staging folder via the existing scanner identifier pipeline, classifies each file (path / filename / hash dupes vs new), determines target paths, populates `created_systems`. Refuses with `ValueError` if `staging_path` is inside `library_root`.
  - `apply_plan(conn, plan, progress_callback) -> ImportSummary` — executes the plan with per-action SAVEPOINT, atomic copy via `core.atomic.atomic_copy`, enrolls new rows via `queries.upsert_rom`, unlinks source on `move` only after copy success, returns counts (`files_imported`, `files_skipped`, `files_replaced`, `files_kept_both`, `errors`).
  - Cooperative cancel honoured between actions (not mid-file — a half-copied ROM has no safe abort point inside `atomic_copy`).
- [ ] Add import-only queries to `src/romulus/db/queries.py`:
  - `find_rom_by_hash(conn, sha1) -> RomRow | None` — for hash-level dupe detection (no current call sites; pulled out so future callers can reuse it).
  - `find_rom_by_path(conn, abs_path) -> RomRow | None` — for path-level dupe detection.
- [ ] Create `src/romulus/ui/import_dialog.py`:
  - `ImportDialog(QDialog)`:
    - Staging-folder picker (`QLineEdit` + folder button + Recent dropdown of last 5 staging paths from config).
    - "Heavy identify before import" checkbox (default off, with the same "slow but more accurate" warning text style as the Heavy Scan dialog).
    - "Copy vs Move" radio group (Copy default).
    - "Post-import: also run…" checklist (Enrich Metadata, Find Covers — both default unchecked).
    - **Analyse** button — runs `analyse_import` on a worker, displays results.
    - Results section: totals header ("N new, M dupes, K conflicts, J new system folders"), `QTreeView` grouped by system, conflict resolution dropdown per row, "Apply to all remaining conflicts" affordance lifted from `sync_preview.py`.
    - System-folder creation summary section with per-system checkboxes for previously-unseen systems (default checked).
    - **Save plan as JSON…** button (writes the plan to a file for offline audit).
    - **Apply** button — guarded by a confirmation dialog if any `replace` resolutions are present; runs `ImportWorker`.
    - Progress bar + per-file label during apply.
    - Summary after completion: "Imported N files (X.X GB), skipped M, conflicts resolved K."
- [ ] Add `ImportWorker` to `src/romulus/ui/workers.py`:
  - Subclasses `_DbWorker`. Two modes — `analyse` (calls `analyse_import`, emits `analysis_ready(plan)`) and `apply` (calls `apply_plan`, emits per-file progress and `finished_ok(summary)`). Pick the cleaner of (a) one worker class with a mode kwarg or (b) two workers — match whatever the sync engine does.
  - `_operation_name = "Import"` so the shared base's cancel/failed messages read naturally.
- [ ] Wire UI surfaces in `src/romulus/ui/main_window.py`:
  - `Tools → Import ROMs…` menu entry and a toolbar button.
  - Handler guards against double-clicks with `_import_worker.isRunning()`.
  - `closeEvent` cancels + waits on `_import_worker` (5000 ms).
  - Refresh the system sidebar + game table after a successful import (new system folders may have been created; new ROMs are in the DB).
- [ ] Write tests in `tests/test_importer.py`:
  - Plan generation: new files routed to correct system folders.
  - Plan generation: path / filename / hash dupes correctly classified.
  - Plan generation: refuses to analyse a staging folder inside `library_root`.
  - Plan generation: `created_systems` populated for previously-unseen systems.
  - Plan generation: archive with multiple ROM extensions flagged as `multi_rom_archive`.
  - Apply: `copy` action atomically writes via `core.atomic.atomic_copy` (monkeypatch-replace contract, same shape as the exporter test).
  - Apply: `move` action unlinks source ONLY after copy succeeds — failure mid-copy must leave source intact.
  - Apply: `replace` action overwrites the existing file atomically and updates the existing rom row (no duplicate insert).
  - Apply: `keep_both` action renames the incoming file with a disambiguating suffix and inserts a new rom row.
  - Apply: progress callback fan-out + cooperative cancel between actions.
  - Apply: `upsert_rom` re-uses an existing `missing=1` row if the target path matches (path-keyed UPSERT contract).
  - Apply: SAVEPOINT rollback on a mid-plan failure leaves the DB consistent with disk for the rolled-back action.
  - Save-plan-as-JSON: round-trips through `ImportPlan` → JSON → `ImportPlan` without information loss.

**Acceptance criteria:**

- `Tools → Import ROMs…` opens the dialog and the toolbar button does the same.
- Analyse pass walks the staging folder using the existing scanner pipeline and produces a populated `ImportPlan` (totals header reflects reality).
- Three dupe levels detected (path / filename / hash) and surfaced with the correct default resolution per level.
- Per-row conflict-resolution dropdown + "apply to all remaining" works the same way it does in the Sync preview.
- System-folder creation summary surfaces previously-unseen systems with per-system checkboxes; opting out routes those files to `_unsorted/`.
- Apply step copies (or moves) files atomically via `core.atomic` and enrolls new ROM rows via `queries.upsert_rom`.
- Cooperative cancel between actions; mid-file copies complete before the worker exits.
- A successful import refreshes the sidebar + table so newly-imported games are visible without restarting the app.
- "Save plan as JSON…" writes a versioned, self-describing plan file.
- All new tests pass, full suite stays green, ruff clean.

STOP. Commit with message `feat(import): inbound ROM import from staging folder with per-file conflict resolution`. The numbered-session commit-message style is retired (per CLAUDE.md); use Conventional Commits.

## Completion Summary
**Status:** COMPLETE
**Date:** 2026-05-18
**What was built/changed:**
- New `src/romulus/core/importer.py` (~580 lines) — `ImportAction`,
  `ImportPlan`, `ImportSummary`, `ImportOptions`, `analyse_import`,
  `apply_plan`. Per-action SAVEPOINT rollback, atomic copy via
  `core.atomic.atomic_copy`, cooperative cancel between actions,
  path-keyed UPSERT for new rom rows, `move` action unlinks source
  only after copy succeeds. Three-level dupe detection (path /
  filename / hash); multi-rom-zip badge via central-directory peek.
  Refuses staging folders inside the library root.
- New `src/romulus/ui/import_dialog.py` — staging-folder picker (recent
  list of 5 from config), Copy/Move radio, mandatory heavy-identify
  note + pre-flight size warning when >100 files / >1 GiB total /
  largest file >100 MiB, per-row resolution dropdown lifted from
  `sync_preview.py`, "apply to all remaining conflicts" affordance,
  "Save plan as JSON…" button, system-folder creation summary
  ("(new)" badges + green note), destructive-confirm prompt before
  any `replace` resolution runs.
- `src/romulus/ui/workers.py` — new `ImportAnalyseWorker` and
  `ImportApplyWorker` (subclasses of `_DbWorker`, same shape as
  `SyncWorker` / `DestInventoryWorker`). Apply emits `progress(int,
  int, str)` and `finished_ok(imported, skipped, replaced, kept_both,
  bytes_imported [qint64], systems, errors)`.
- `src/romulus/ui/main_window.py` — `Tools → Import ROMs…` menu entry
  + toolbar button, `_on_import_roms` / `_on_import_analyse_requested`
  / `_on_import_apply_requested` handlers (double-click guard via
  `isRunning()`), `closeEvent` cancels + waits both import workers
  for 5000 ms.
- `src/romulus/db/queries.py` — `find_rom_by_path(conn, abs_path)`
  and `find_rom_by_sha1(conn, sha1)` for dupe detection.
- `tests/test_importer.py` — 23 new tests covering plan analysis (all
  three dupe levels, extension fallback, `_unsorted` fallback,
  `created_systems`, multi-rom zip, refusal-inside-library), apply
  (atomic-copy monkeypatch, move-after-copy unlink contract,
  replace-uses-existing-row, keep-both disambiguation, progress
  fan-out, cooperative cancel, path-keyed un-tombstone, SAVEPOINT
  rollback isolating per-action failure), JSON round-trip, and the
  two new query helpers.
- `CHANGELOG.md` + `CLAUDE.md` updated with the new
  feature + design rule #21 ("Import is symmetric to sync") + test
  count (918 → 941).
**Tests:** 941 passing, 1 skipped (POSIX-only chmod test on Windows
CI; 942 collected total). Ruff clean across `src/` + `tests/`.
**Config changes:** New config key `import_recent_paths` (JSON array of
the last 5 staging folders, written via `import_dialog.remember_staging_path`).
No schema migrations.
**Breaking changes:** None.
**Carry-forward notes:**
- Heavy identification is mandatory on every analyse pass per the
  user's mid-session direction — there is no toggle. Run-time warning
  thresholds (>100 files, >1 GiB total, any file >100 MiB) live in
  `import_dialog.py` and can be tuned without touching the engine.
- Post-import auto-actions (Enrich Metadata / Find Covers) were
  removed from the dialog per the user's direction; the existing
  toolbar buttons cover those workflows after the import returns.
- The `_unsorted` fallback skips `upsert_rom` since the row needs a
  system_id FK. Files still copy to disk; a future scoped Quick Scan
  under the `_unsorted/` folder (or manual move into a system
  folder) is what enrols them.
- `.7z` archives don't get the multi-rom-zip check (we don't depend
  on `py7zr`). They're imported verbatim like `.zip` containers.
- Pre-1.0 — no migration framework. If the `import_recent_paths`
  config key ever changes shape, just wipe `data/romulus.db`.
