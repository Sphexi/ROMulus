# Import ROMs — design notes

**Status:** future feature, not started. Captured here so the next
session has the requirements + an obvious implementation path in
front of it instead of re-deriving them.

---

## What the user wants

Quoting the request verbatim:

> If I download more ROMs, I want to be able to import them into the
> library, meaning scan them wherever they are (like a download folder
> or staging area), identify them via fuzzy/dat matching, then match
> them to a system, and copy them to the correct location in the
> existing library based on the setup. Some logic around if a system
> folder doesn't exist to create it (maybe prompt for it), otherwise
> to put them into the correct folders, but also check for existing
> dupes and provide feedback on that (skip all dupes or replace local
> with new versions, all or nothing).

In one sentence: **"Sync, but inbound from a staging folder into the
library."** The shape of the workflow rhymes with the existing
Export / Sync engine — preview → confirm → apply, with per-action
conflict handling.

---

## Workflow

1. **User chooses a staging folder** (Downloads, USB stick, mounted
   ZIP, etc.). Sources outside the current library_root only.
2. **Walk + identify** the staging folder using the existing scanner
   + identifier pipeline:
   - L1 fuzzy filename → tentative system from folder aliases OR
     extension lookup.
   - L2 header sniff (for systems with a `header_rule`).
   - Optional L3 hash + DAT match if the user ticks "Heavy identify
     before import" (slower, but disambiguates lookalikes).
3. **Build the import plan** — one row per staging file:
   - **Resolved system** + **target system folder name** under
     `library_root`. Missing system_id → goes to a fallback
     `_unsorted/` bucket like the sync engine already does.
   - **Target path** = `<library_root>/<system_folder>/<filename>`.
     When DAT-matched, optionally use the canonical name (toggle:
     "Rename to DAT canonical on import").
   - **Action**: `copy` / `move` / `skip` / `replace` / `keep-both`.
4. **Preview dialog** — table of planned actions with totals at the
   top ("N new, M dupes (skip), K conflicts (resolve)"). Conflict
   resolution columns:
   - Skip (default for `dupe-same-content`)
   - Replace (default for `dupe-different-content` + user confirmed)
   - Keep both with disambiguating suffix
5. **Apply** via a QThread worker:
   - Create system folders as needed (prompt once at the top of the
     run: "create N new folders for previously-unseen systems?" with
     a per-system checklist).
   - Atomic copy via `core/atomic.py` (`tempfile.mkstemp` + `os.replace`).
   - Enroll the new rom rows into the library DB (via the same
     `upsert_rom` path the scanner uses).
   - On `move`: unlink the source after the copy succeeds (NOT
     before — partial transfers must leave the source intact).

---

## Duplicate detection — three levels

| Level | Check | Default action |
|---|---|---|
| **Path** | A file already lives at the planned target path | Skip (it's literally already there) |
| **Filename** | Same basename, different content (size or hash differs) | Conflict → user picks `replace` / `keep-both` / `skip`. Default `skip`. |
| **Hash** | Same hash exists somewhere in the library under a different filename | Report as "already in library (different name)" and skip. Don't auto-replace — the user may have a renamed copy on purpose. |

Conflict resolution should also be available as **"apply this to all
remaining conflicts"** so a large import doesn't require N clicks.
The Sync engine has the same UX pattern in `sync_preview.py`; reuse
the components.

---

## System folder creation

When a staging file resolves to a system that doesn't have a folder
under `library_root` yet:

1. The preview dialog flags the system with a `(new)` badge.
2. The Apply step shows a one-shot confirmation: "Create N new
   system folders: atari7800, dreamcast, segacd?" with checkboxes
   per system so the user can opt out of specific ones (those files
   then either move to `_unsorted/` or get dropped from the plan).
3. The created folders use the **first** entry in the system's
   `folder_aliases` list (matches how Organize / Export pick a
   canonical folder name).

---

## Implementation surface

New modules + glue:

```
src/romulus/core/importer.py
  ImportPlan         — list[ImportAction]
  ImportAction       — source_path, target_path, system_id, status,
                       resolution (skip/copy/move/replace/keep_both)
  analyse_import(    — walks staging path, builds the plan against
    conn, staging,     the current library state
    options)
  apply_plan(        — executes the plan; atomic per file; updates
    conn, plan,        the library DB rows via upsert_rom; cleans
    progress_cb)       up moved sources
src/romulus/ui/import_dialog.py
  ImportDialog       — preview table + conflict resolution + Apply
src/romulus/ui/workers.py
  ImportWorker       — runs analyse / apply on a QThread with
                       the existing cooperative-cancel pattern
src/romulus/ui/main_window.py
  Tools → "Import ROMs..." menu entry + toolbar button
```

Reuse:

* **Identifier pipeline** — `core/scanner._resolve_system_for_directory`,
  `core/identifier.parse_header`, `core/hasher.hash_rom`. Same code
  paths the regular Quick Scan / Heavy Scan use.
* **Atomic write helpers** — `core/atomic.atomic_copy` (already
  exists, used by sync apply).
* **Sync engine's conflict-resolution UX** — `sync_preview.py`'s
  per-action dropdown + "apply to all remaining" pattern is the
  closest thing the codebase already has.

---

## Open questions

* **Copy vs move default?** Probably `copy` — preserves the staging
  area for re-imports / rollback. Move is opt-in via checkbox.
* **What about archives (.zip / .7z) in the staging folder?** The
  scanner already accepts archive containers as ROMs for every
  system. Likely the right thing is to import the archive verbatim
  (don't unpack). But what if the archive contains MULTIPLE ROMs?
  Probably out of scope for v1 — flag and skip with a "this looks
  like a multi-ROM archive" badge.
* **Should import auto-trigger Enrich Metadata + Find Covers on the
  imported ROMs?** Probably no auto, but a "post-import: also run..."
  checklist at the top of the dialog would be a small extra plumb.
* **Dry-run mode for the apply step?** The preview *is* a dry run.
  An optional "save the import plan as a .json file" would let the
  user audit, then re-run later.
* **Tier on staging-folder source identity to avoid re-importing the
  same file twice across runs.** Probably file hash → skip if already
  in the library. Already covered by the hash-level dupe check.
