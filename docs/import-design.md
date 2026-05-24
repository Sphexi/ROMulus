# Import ROMs — Feature Reference

**Status:** shipped in v0.3.0 (commit `d4d79e8`), updated for strict 1:1 model (v0.4.0).
**Code:** [`src/romulus/core/importer.py`](../src/romulus/core/importer.py),
[`src/romulus/ui/import_dialog.py`](../src/romulus/ui/import_dialog.py),
[`src/romulus/ui/workers.py`](../src/romulus/ui/workers.py)
(`ImportAnalyseWorker` + `ImportApplyWorker`).
**Authoritative reference:** this doc. Implementation diverging from
it must update the doc in the same commit.

This file replaces the original "future feature design notes" doc —
the import workflow ships and works. Section §11 captures
post-implementation notes for future work.

---

## 1. Goals

The user has a staging folder of ROMs sitting somewhere outside the
managed library (Downloads, a USB stick, a mounted archive). Import
should:

1. **Identify every file** via the same Quick + Heavy pipeline the
   scanner uses — no separate "import-only" identifier. Same fuzzy
   filename parsing, header sniffing, hash + DAT lookup.
2. **Surface three levels of duplicate detection** before any byte
   moves: same target path, same filename, same hash.
3. **Make conflict resolution per-row.** The user picks copy / move /
   skip / replace / keep-both per file, with a bulk-apply shortcut for
   long lists.
4. **Atomically commit** the approved plan — no half-imported files,
   no DB rows pointing at sources that never finished copying.
5. **Treat staging-into-library as a footgun.** Refuse to import if
   `staging_path` is inside `library_root` (or vice-versa).

In one sentence: **"Sync, but inbound from a staging folder into the
library."** The Sync engine's analyse → preview → apply shape applies
unchanged.

---

## 2. Workflow

Triggered via **Tools → Import ROMs…** or the toolbar button.

1. **User picks a staging folder.** The dialog also surfaces a list of
   recently-used staging paths so re-importing from the same source is
   one click.
2. **Sanity gate.** If `staging_path` is inside `library_root` (or
   vice-versa), `analyse_import` raises `ValueError` and the dialog
   surfaces the error. Prevents self-recursion footguns.
3. **Walk + identify** the staging folder. Heavy identification (SHA-1
   + DAT match) runs on **every** file in the staging area —
   unconditionally. Without it the hash-level dupe check can't fire
   and the user would get false-negative "this is new" badges for
   files that are actually already in the library under a different
   name. The dialog warns about duration up front when the staging
   folder is large.
4. **Build the import plan** — one `ImportAction` per staging file:
   - **Resolved system** + **target system folder** under `library_root`.
   - **Target path** = `<library_root>/<system_folder>/<filename>`.
   - **Status** classifying the action (see §3).
   - **Default resolution** based on status (see §4).
5. **Preview dialog** shows a per-file table grouped by system, with
   the same tri-state group headers + right-click toggle that
   Organize / Sync / Verify Library use.
6. **Apply** via `ImportApplyWorker`:
   - Per-action SAVEPOINT — failure on one row doesn't roll back the
     rest.
   - Atomic copy via `core/atomic.py` (`tempfile.mkstemp` +
     `os.replace`).
   - On `move`: unlink source only after copy succeeds (NOT before —
     partial transfers must leave the source intact).
   - New ROM rows enrolled via the same path-keyed `upsert_rom` the
     scanner uses. Identity fields (`title`, `region`, `revision`, etc.)
     are written onto the `roms` row at upsert time from filename
     parsing (and upgraded later by Heavy Scan from the DAT match).
     No separate grouping phase runs post-import. Importing a file
     whose target path matches a `missing=1` row un-tombstones the row
     rather than duplicating it.
   - **Sibling-copy gate applies post-import.** When the newly enrolled
     rom's identity matches another rom that already has metadata or
     covers (by SHA-1, then `(system_id, canonical_name)`), the next
     Enrich Metadata / Find Covers run will copy that data for free
     rather than re-fetching from remote sources.

---

## 3. Status taxonomy

Each `ImportAction` is classified with one of these statuses at
analyse time:

| Status | Trigger | Default resolution |
|---|---|---|
| `new` | No path / filename / hash match in the library | `copy` |
| `dupe_path` | A file already lives at the planned target path | `skip` |
| `dupe_filename` | Same basename, different content (size or hash differs) | `skip` (user picks `replace` / `keep_both`) |
| `dupe_hash` | Same hash exists in the library under a different filename | `skip` (already have this byte-identical ROM) |
| `multi_rom_archive` | Archive (.zip / .7z) containing multiple ROM entries | `skip` (out of scope; user unpacks manually) |

The detection order is **path → filename → hash**, mirroring the
sync engine. A file that hits multiple levels gets the strongest
classification (path > filename > hash).

---

## 4. Conflict resolution

Each row's `resolution` field is one of:

| Resolution | Effect |
|---|---|
| `copy` | Copy source → target. Source untouched. |
| `move` | Copy source → target, then unlink source. |
| `skip` | No-op. Row stays in the plan for the summary. |
| `replace` | Overwrite the existing target file with the source. |
| `keep_both` | Append a disambiguating suffix (`Mario (2).sfc`) and copy. |

`replace` and `keep_both` only apply to `dupe_*` statuses. The
preview dialog also offers an **"apply this to all remaining
conflicts"** button so a large import doesn't require N clicks.

---

## 5. System folder creation

When a staging file resolves to a system that doesn't have a folder
under `library_root` yet, the system shows up as `(new)` in the
preview header. The apply step creates the folder lazily on first
write — no separate "create N new folders?" confirm prompt (the
preview header already flags it). Created folders use the **first**
entry in the system's `folder_aliases` list, matching Organize /
Export.

If a staging file's system can't be resolved at all (extension not in
any system's allowlist, no folder-name hint), it lands in the
sentinel `_unsorted/` bucket — same fallback the Sync engine uses.

---

## 6. Implementation surface

```
src/romulus/core/importer.py
  ImportStatus       — Literal["new", "dupe_path", "dupe_filename",
                                "dupe_hash", "multi_rom_archive"]
  ImportResolution   — Literal["copy", "move", "skip", "replace",
                                "keep_both"]
  ImportAction       — source_path, target_path, system_id, status,
                       resolution, confidence, size_bytes, reason,
                       existing_rom_path, existing_rom_id, executed,
                       error
  ImportPlan         — staging_root, library_root, list[ImportAction],
                       created_systems, heavy_identify, total_bytes
                       (with to_json / from_json round-trip)
  ImportOptions      — default_resolution, heavy_identify (always True)
  ImportSummary      — files_imported, files_skipped, files_replaced,
                       files_kept_both, bytes_imported, systems_touched,
                       errors
  analyse_import(    — walks staging, builds the plan
    conn, staging,
    library, options)
  apply_plan(        — executes the plan; atomic per file; updates
    conn, plan,        the library DB rows via upsert_rom; cleans up
    progress_cb)       moved sources

src/romulus/ui/import_dialog.py
  ImportDialog       — preview table + conflict resolution + Apply

src/romulus/ui/workers.py
  ImportAnalyseWorker — runs analyse_import on a QThread
  ImportApplyWorker   — runs apply_plan on a QThread

src/romulus/ui/main_window.py
  Tools → "Import ROMs…" menu entry + toolbar button
```

### Reuses

* **Identifier pipeline** — `core/scanner._resolve_system_for_directory`,
  `core/identifier.parse_header`, `core/hasher.hash_rom`. Same code
  paths the regular Quick Scan / Heavy Scan use.
* **Atomic write helpers** — `core/atomic.atomic_copy`.
* **Sync engine's conflict-resolution UX** — `sync_preview.py`'s
  per-action dropdown + "apply to all remaining" pattern.
* **GroupedCheckboxTreeMixin** — tri-state group headers and
  right-click bulk toggle on the preview dialog.

---

## 7. Plan JSON round-trip

`ImportPlan.to_json()` / `ImportPlan.from_json()` produce a versioned
self-describing JSON document:

```json
{
  "version": 1,
  "kind": "romulus.import_plan",
  "generated_at": "2026-05-19T12:34:56+00:00",
  "staging_root": "D:/RetroDump",
  "library_root": "//nas/Retro Files/Console ROMs",
  "heavy_identify": true,
  "total_bytes": 2438217482,
  "created_systems": ["dreamcast", "saturn"],
  "actions": [
    {"source_path": "…", "target_path": "…", "system_id": "snes",
     "status": "new", "resolution": "copy", "confidence": "dat_verified",
     "size_bytes": 524288, "reason": "", "existing_rom_path": null,
     "existing_rom_id": null, "executed": false, "error": null},
    ...
  ]
}
```

The dialog doesn't expose a "save plan" button yet, but the round-trip
exists so a future audit / replay feature can use it.

---

## 8. Cancellation

Cooperative cancel works exactly like the Sync engine — the worker's
progress callback raises `_ImportCancelled` if the user clicked
Cancel since the last tick. The currently-executing action's
SAVEPOINT is rolled back; already-committed actions stay applied.

The dialog disables Cancel **during** an in-flight atomic copy
(there's no safe interruption point inside `atomic.atomic_copy`).
Cancel resumes between actions.

---

## 9. Safety properties

| Property | Enforced by |
|---|---|
| Source files are never deleted before the destination copy succeeds | `apply_plan` only unlinks on `move` after `atomic_copy` returns |
| Half-copied files never appear at the target path | `atomic.atomic_copy` (`tempfile.mkstemp` + `os.replace`) |
| DB never drifts from disk on partial failure | Per-action SAVEPOINT — failed row rolls back its DB writes too |
| Staging inside the library doesn't loop forever | `_validate_staging_outside_library` raises `ValueError` |
| `missing=1` rows are revived, not duplicated, when matching paths re-appear | Path-keyed `upsert_rom` |

---

## 10. Tests

`tests/test_importer.py` (23 tests):

* Plan analysis — dupe levels, extension fallback, multi-rom-zip
  detection, staging-inside-library refusal.
* Apply — atomic copy, move-after-copy ordering, replace, keep_both,
  SAVEPOINT rollback on per-action failure, cancel between actions,
  upsert path-revival of tombstoned rows.
* JSON round-trip — `to_json` → `from_json` preserves every field.
* `find_rom_by_path` / `find_rom_by_sha1` helpers used by the
  identifier path.

---

## 11. Open questions / future work

* **Copy vs move default.** Currently `copy` to preserve the staging
  area for re-imports. Move stays opt-in via per-row dropdown.
* **Archive containing multiple ROMs.** Currently classified as
  `multi_rom_archive` and skipped with a badge. Unpacking is out of
  scope — the user unpacks manually and re-runs Import.
* **Post-import enrichment.** Not auto-triggered today. A "post-import:
  also run Enrich Metadata / Find Covers" checklist at the top of the
  dialog would be a small extra plumb. Out of scope for v0.3.0.
* **Save plan as `.json`.** The round-trip exists in code; no UI
  button. Easy add when a real audit / replay use case shows up.
* **Streaming progress for large archives.** Heavy identification on a
  large staging folder can take minutes; the dialog warns about
  duration but doesn't show per-file progress until Apply. A
  per-file analyse progress tick would match what the scanner does.
