# Destination Sync — Design Spec

**Status:** implemented (v0.2.0), refined (v0.3.0), updated for strict 1:1 model (v0.4.0).
**Last updated:** 2026-05-23
**Authoritative reference:** this doc. Implementation diverging from it must update the doc in the same commit. Post-implementation fixes are recorded in [§12](#12-post-implementation-notes-v030).

---

## 1. Goals

The local ROMulus library is the source of truth for the user's collection. Sync
mirrors that library (or a filtered subset) to a separate destination — a
handheld over USB, an SD card, a network share, or a local folder — and lets
the user reconcile changes in either direction. Three explicit objectives:

1. **Don't push blindly.** Always quick-scan the destination first, present a
   diff with bucketed counts, and require user confirmation before mutating.
2. **Match by identity, not just by filename.** A ROM is the same ROM whether
   it's stored as `Sonic.zip` locally or `Sonic (USA).gb` on the device; the
   sync engine should treat them as one row in the diff.
3. **Save destinations as first-class entries.** USB drives and SD cards
   reappear at the same path; named "destination profiles" make re-syncing
   one-click.

---

## 2. Operations

Four sync modes, all exposed in the Export dialog with hover-help. Each gets a
short README entry as well so a user choosing for the first time can read past
the tooltip.

| Mode | Direction | Behavior | Destructive? |
|---|---|---|---|
| **Push — merge** *(default)* | Local → Dest | Copy local-only files to dest. Leave existing dest files alone. **Don't delete** anything on dest. | No |
| **Push — mirror** | Local → Dest | Copy local-only files to dest. **Delete dest-only files.** Dest becomes a 1:1 mirror of the filtered local set. | YES (deletes) |
| **Push — fresh wipe** | Local → Dest | Empty the dest first (under the chosen base_path), then run a fresh export. Equivalent to mirror but explicit about the upfront wipe. | YES (destructive) |
| **Pull — merge** | Dest → Local | Import dest-only ROMs into the local library at `<library_path>/<system_folder>/`. Mark imported ROMs as `match_confidence=fuzzy` or `unmatched`. Don't touch dest. | No (local-only writes) |
| **Two-way** | Both | For each missing-on-one-side file, copy in the direction that has it. For conflicts (same identity, different content), apply the configured conflict policy. | Conditional — only if conflict policy resolves with overwrites |

### 2.1 Conflict policies (Two-way only)

User picks one before clicking Apply. Per-row override is available in the
preview table:

- **Skip** — leave both sides as-is, log the conflict.
- **Local wins** — overwrite dest with the local file.
- **Dest wins** — overwrite local with the dest file.
- **Newest mtime wins** — compare modification times.
- **Prompt per file** — preview dialog shows a per-row dropdown.

Default: **Skip**.

### 2.2 Always-on sub-behaviors

Regardless of mode:

- **gamelist.xml is rebuilt on the destination from scratch.** It's a derived
  artifact — there's no value in attempting to diff it. After ROMs settle, the
  exporter regenerates each system's `gamelist.xml` based on what's on the
  destination right now (matched against the local DB).
- **Cover artwork tracks ROM moves.** If a ROM is copied to dest, its
  preferred cover is copied too. If a ROM is deleted from dest, its cover is
  deleted with it.
- **Atomic-write discipline.** Every write goes through
  `romulus.core.atomic`. A cancelled or crashed sync can only leave `.part`
  tempfiles behind, never half-written ROMs.

---

## 3. Identity matching

When deciding whether a dest file represents the same ROM as a local one, the
sync engine probes four tiers in order. Tier 1+2 are always on; 3 is used when
hashes are available; 4 is opt-in per sync.

| Tier | Method | Cost | Notes |
|---|---|---|---|
| 1 | **Path equivalence** — same `rel_path` under target | µs | First check; nearly free. |
| 2 | **Fuzzy key + region** — strip tags from filename, compute `fuzzy_key`, match against local `roms.fuzzy_key`. Region tag (if present) included in the match so `Sonic (USA)` and `Sonic (Europe)` stay distinct. | ms | Default for cross-region collections. |
| 3 | **Local hash lookup** — if the dest filename matches a local ROM AND the local ROM has a known SHA-1, compare against the dest file's size as a sanity gate; trust the match when size also matches. | µs per match | Already-hashed local ROMs (post-Heavy-Scan) gain accuracy for free. |
| 4 | **Deep verify** *(opt-in)* — compute SHA-1 of every dest file, match against local `hashes.sha1`. Authoritative; only false-positive case is hash collision. | minutes-to-hours | Toggled via a "Deep verify" checkbox in the sync preview. Cached in `dest_inventory.sha1` so re-syncs reuse. |

The match function returns either a `rom_id` (matched) or `None` (orphan on
dest). Tier 4 is exposed as a button in the preview alongside the existing
quick-scan-first flow, mirroring how the main library has separate Quick Scan
and Heavy Scan toolbar buttons.

### 3.1 Why region matters

The current `fuzzy_key` strips region tags so cartridge variants of the same
game collapse to one logical entry. For sync that's wrong: pushing a USA
cartridge to a device that already has the European version shouldn't be a
"skip identical" — they're different ROMs. The sync engine composes the match
key as `fuzzy_key + region_normalized` (lowercase, empty string when absent)
so regions are respected.

---

## 4. Data model

Four new SQLite tables, all backward-compat-migratable via
`PRAGMA table_info` checks like the existing `is_preferred` migration. Schema
diff lives in `db/schema.py` with a `_migrate_<name>` helper per table.

### 4.1 `sync_destinations`

Saved destinations the user can re-pick from a dropdown.

```sql
CREATE TABLE sync_destinations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,         -- "Anbernic RG556 USB", "muOS SD"
    target_path     TEXT NOT NULL,                -- "E:\" or "\\NAS\Roms"
    profile_id      TEXT NOT NULL,                -- references a profile YAML's id
    last_synced_at  TEXT,                         -- ISO timestamp, null if never
    created_at      TEXT NOT NULL,
    last_inventory_signature TEXT                  -- see §4.5
);
```

### 4.2 `dest_inventory`

Cached file state per destination. Reused on subsequent syncs when
`(rel_path, size, mtime)` haven't changed — same staleness check as the local
`hashes` cache.

```sql
CREATE TABLE dest_inventory (
    dest_id       INTEGER NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
    rel_path      TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    sha1          TEXT,                            -- NULL unless deep-verified
    rom_id        INTEGER REFERENCES roms(id),     -- matched local rom, if any
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (dest_id, rel_path)
);
CREATE INDEX idx_dest_inventory_sha1 ON dest_inventory(sha1);
CREATE INDEX idx_dest_inventory_rom ON dest_inventory(rom_id);
```

> **v0.4.0 change:** the `game_id` column is removed. The `rom_id` column is the sole
> identity anchor. Pre-v0.4.0 inventory rows become stale on the next destination
> scan; the staleness check re-recognises them via `(rel_path, size, mtime)` as
> before and the walker rebuilds the row without `game_id`.

### 4.3 `sync_plans`

Persisted plans so the user can review history and resume interrupted syncs.
Mirrors the existing `organize_plans` model.

```sql
CREATE TABLE sync_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dest_id     INTEGER NOT NULL REFERENCES sync_destinations(id),
    mode        TEXT NOT NULL,                    -- 'push_merge' / 'push_mirror' / 'push_wipe' / 'pull' / 'two_way'
    created_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',           -- pending | applied | cancelled | partial
    summary     TEXT NOT NULL,                    -- JSON: counts by action kind
    plan_json   TEXT NOT NULL                     -- JSON: full list of SyncAction rows
);
```

### 4.4 (Optional) `sync_history`

Not required for v0.2.0. Future use for an audit-trail view. The `sync_plans`
table itself can be queried for history.

### 4.5 `last_inventory_signature` (re-recognition)

When the user mounts an SD card at `E:\` today and a different one at `E:\`
tomorrow, we need to detect that "E:\" is now a different physical device. The
inventory cache would be wrong for the new device.

Solution: when scanning a destination, compute a small signature from the
first 32 file paths sorted alphabetically (just their relative paths, hashed
with SHA-1). Store it on `sync_destinations.last_inventory_signature`. On
re-scan:

- If the signature still matches → reuse cache as before.
- If the signature has drifted significantly → treat the destination as fresh
  (clear `dest_inventory` rows for this dest_id, re-scan from scratch).
- The user can also manually "Forget cache" from the destinations dropdown.

This is fast (32 stat calls) and catches the swap-the-SD case without needing
volume serial numbers (which aren't available on network shares anyway).

---

## 5. Sync flow

1. **User picks a destination.** Either from the saved-destinations dropdown
   or "Add new destination…" which opens a folder picker + name + profile
   pair.
2. **User picks a sync mode.** Default `Push — merge`.
3. **User clicks "Scan destination".** A `DestInventoryWorker` walks the
   target path (depth-capped, respects `.romulus-ignore` files if we add
   those — v0.3.0+), populates/refreshes `dest_inventory`, and emits a diff
   against the local DB. Progress dialog shows files-walked / total.
4. **Diff preview dialog.** Tree view with four buckets:
   - **To add to dest** — local-only files, will be copied
   - **To remove from dest** — dest-only files (Push-mirror, fresh-wipe only)
   - **To pull to local** — dest-only files (Pull, Two-way only)
   - **Conflicts** — same identity, different content (Two-way only)
   - **Identical** — already present, no action

   Each row checkboxed; user can deselect specific actions. Per-row conflict
   policy dropdown (Two-way only).
5. **Apply.** Worker runs the plan. Atomic-copy per file. Plan persisted to
   `sync_plans`. Progress dialog with cancel.
6. **Post-sync gamelist rebuild.** Always run, regardless of direction. Walks
   each system folder on dest and regenerates `gamelist.xml` based on
   what's actually there now (matched against local DB metadata).
7. **Summary.** Modal showing files added / removed / pulled / skipped /
   errored, plus a "Save plan summary…" button.

---

## 6. UI

### 6.1 Dialog layout

Rename "Export" toolbar entry to **"Export / Sync"**. Same window today
becomes the entry point. Top of the dialog gains a mode selector and a
destination dropdown:

```
┌─ Export / Sync ──────────────────────────────────────────────┐
│  Mode:        [Push — merge ▾]                                │
│  Destination: [Anbernic RG556 USB ▾]  [+]  [Edit] [Forget…]   │
│  Target path: E:\Roms                                         │
│  Profile:     [Anbernic RGLauncher ▾]                         │
│  ─────────────────────────────────────────────────────────    │
│  Filters: ……  Systems: ……                                     │
│  ─────────────────────────────────────────────────────────    │
│  Options: [✓] Include artwork  [✓] Generate gamelist.xml      │
│           [✓] Generate .m3u    [ ] Deep verify (slow)         │
│  ─────────────────────────────────────────────────────────    │
│  [Scan destination first]  [Quick Sync]  [Cancel]             │
└───────────────────────────────────────────────────────────────┘
```

`Quick Sync` runs scan + apply without showing the preview — only enabled
for `Push — merge` mode (the safe default).

### 6.2 Preview dialog

A separate window after scan completes:

```
┌─ Sync preview (Anbernic RG556 USB) ──────────────────────────┐
│  234 to add (5.2 GB)   12 to remove   3 conflicts   1,892 OK  │
│  ─────────────────────────────────────────────────────────    │
│  ▼ To add (234)                                               │
│    ☑ snes/Game1.sfc       1.0 MB    "no match on dest"        │
│    ☑ snes/Game2.sfc       512 KB    "no match on dest"        │
│  ▼ To remove (12)                                             │
│    ☑ snes/OldGame.sfc     ⚠ DESTRUCTIVE                       │
│  ▼ Conflicts (3)                                              │
│    ☐ snes/Mario.sfc       same name, dif hash  [Skip ▾]       │
│  ▼ Already identical (1,892)  [hidden by default]             │
│  ─────────────────────────────────────────────────────────    │
│  [Select all] [Deselect all] [Apply] [Cancel]                 │
└───────────────────────────────────────────────────────────────┘
```

### 6.3 Destructive-action confirmation

When the user clicks **Apply** AND the plan contains any delete or overwrite
action, two confirmations:

**First dialog:**
```
⚠ Major changes to destination

This sync will:
  • Add 234 files (5.2 GB)
  • DELETE 12 files (148 MB)
  • Overwrite 0 files

These changes cannot be undone automatically.
The deleted files will NOT be moved to a trash folder.

[Continue]  [Cancel]
```

**Second dialog (only if first OK'd):**
```
Are you sure?

You're about to delete 12 files from:
  E:\Roms\

This is your last chance to cancel.

[Yes, apply the plan]  [Cancel]
```

No second dialog for non-destructive plans (Push-merge, Pull-merge with no
conflicts). Single Apply click is enough there.

---

## 7. Edge cases & decisions

| Case | Behavior |
|---|---|
| Dest path becomes unreachable mid-sync | Worker logs the error per file, plan ends `partial`. User can resume by re-running. |
| Dest is read-only (mounted ISO, etc.) | Scan succeeds; Apply is disabled with an explanatory banner. |
| User reconnects same physical device at different drive letter | Saved destination's `target_path` is stale; Edit dialog lets them update. Inventory cache is keyed on `dest_id`, not path, so it survives the rename. |
| User picks a destination that's never been synced | Empty `dest_inventory`, full scan runs. No special path. |
| Profile mismatch (dest was synced with profile X, user picks profile Y now) | Treated as a layout migration: source-paths under the old layout become "to remove", new layout entries become "to add". User reviews. |
| Sync interrupted, ROMs partially copied | `.part` tempfiles remain on dest. Re-running detects them via `dest_inventory` (they aren't in there) and they get retried OR cleaned up. Add a "Clean .part files" maintenance action. |
| Two-way conflict on a file the user never opened in the detail panel | Conflict policy applies. If "Prompt per file" set, dialog blocks the worker until user decides. Otherwise policy default applies silently. |
| Local library has 0 ROMs, user picks Pull | All dest files become "to pull". OK — they're bootstrapping. |
| Local library has ROMs, user picks Pull merge | Only dest-only files pulled. Matching local files left alone. |
| Pulled file lands in a system folder that doesn't exist locally | Create the folder under `library_path/<system_id>/` first. |
| Pulled file doesn't match any known system on dest | Use the dest's directory name to guess; if no match, drop in `library_path/_unsorted/`. |

---

## 8. Pull mode specifics

Per the user's decision: pulled ROMs land in
`<library_path>/<system_folder>/<filename>` and get enrolled as if Quick Scan
ran on them. Specifically:

1. File copied via `atomic.atomic_copy`.
2. `parse_filename` → `clean_name`, `region`, etc.
3. `generate_fuzzy_key` produces the match key (release_type included if
   detected).
4. `queries.upsert_rom` inserts/updates with `match_confidence='fuzzy'` and
   `system_id` resolved from the pull source's folder or the profile's
   reverse mapping. Identity fields (`title`, `region`, etc.) are
   populated from filename parsing at upsert time — no separate grouping
   step.
5. The user can later run Heavy Scan to upgrade the match to
   `dat_verified` and populate canonical identity fields from the DAT.

If a pulled file's identity matches an existing local ROM (by fuzzy_key OR
hash), it's a "skip identical" — already in the library.

---

## 9. Implementation plan (file by file)

### 9.1 New files

- `src/romulus/core/dest_inventory.py` — walk + cache, signature, staleness
  detection. ~250 LOC.
- `src/romulus/core/sync.py` — diff engine + apply for all five modes
  (push-merge, push-mirror, push-wipe, pull, two-way). ~500 LOC.
- `src/romulus/ui/sync_preview.py` — preview dialog with bucketed tree,
  per-row checkboxes, conflict-policy dropdowns. ~350 LOC.
- `tests/test_dest_inventory.py` — walker + cache reuse + signature logic.
  ~250 LOC.
- `tests/test_sync.py` — diff engine + each mode + edge cases.
  ~500 LOC.
- `tests/test_sync_preview.py` — UI tests for the preview dialog.
  ~200 LOC.

### 9.2 Modified files

- `src/romulus/db/schema.py` — 3 new tables + 3 migration helpers.
- `src/romulus/db/queries.py` — sync_destinations CRUD,
  dest_inventory upsert/lookup/clear, sync_plans persistence. ~250 LOC added.
- `src/romulus/ui/workers.py` — `DestInventoryWorker` + `SyncWorker`.
  Both mirror the existing `_DbWorker` contract. ~150 LOC added.
- `src/romulus/ui/export_dialog.py` — mode + destination dropdowns, scan
  button, branches into preview vs direct export. ~200 LOC modified.
- `src/romulus/ui/main_window.py` — rename toolbar/menu entry; close-event
  guard already covers new workers via the existing pattern. ~30 LOC.
- `src/romulus/core/exporter.py` — `export_collection` learns the new sync
  modes; the existing "fresh export" code path stays as-is, internally
  expressed as "fresh wipe + push". ~100 LOC modified.
- `README.md` — new "Syncing your collection" section.

### 9.3 Estimated test count

~60 new tests across `test_dest_inventory.py`, `test_sync.py`, and
`test_sync_preview.py`. Brings the suite from ~720 to ~780.

---

## 10. v0.2.0 scope confirmed

In:
- All 4 sync modes (push merge / mirror / wipe / pull) plus two-way
- Conflict resolution policies (skip / local / dest / newest / prompt)
- Identity matching tiers 1–4 with tier-4 opt-in
- Saved destinations with re-recognition via inventory signature
- Bucketed preview dialog with per-row checkboxes
- Double-confirm for destructive plans
- Always-rebuild gamelist.xml post-sync
- Plan persistence in `sync_plans`

Deferred to v0.3.0+ (and explicitly NOT in this work):
- BIOS sync
- Scheduled / triggered-on-device-mount sync
- Backup / restore as separate operation
- `.romulus-ignore` per-folder ignore files
- Sync history / audit-trail viewer UI

### Implementation clarifications (v0.2.0 implementation pass)

- **Delete actions are scoped to `profile.base_path`.** Push-mirror / push-
  wipe / two-way modes only propose delete actions for dest files that live
  under the profile's `base_path`. Sibling files outside the managed sub-
  tree (e.g. a `BIOS/` directory next to `roms/`) are never deleted, even
  if they have no local-library counterpart. This is consistent with the
  spec's intent — sync manages what the profile claims, nothing else —
  and matches the path-traversal defense baked into the exporter's
  `_system_dest_dir`.
- **Stale `dest_inventory` rows are rewritten, not preserved.** When the
  walker detects size/mtime drift for a cached row, it deletes the row
  before the upsert so the stale SHA-1 / rom_id columns are cleared.
  Without this the upsert's `COALESCE` would preserve identity columns
  that no longer describe the file.

---

## 11. Open questions for the implementer

(Answers below from the user decision pass.)

- **Heavy-scan-first requirement?** No — fuzzy_key + hash-when-available is
  the default. Deep verify is opt-in per sync via a checkbox.
- **Default mode?** Push — merge (non-destructive).
- **Pull-mode landing?** `<library_path>/<system_folder>/`, with system
  inferred from the dest folder name (or `_unsorted/` if unknown).
- **Two-way in v0.2.0?** Yes, included.
- **Always rebuild dest gamelist.xml?** Yes — derived artifact, no value in
  diffing.

---

## 12. Post-implementation notes (v0.3.0)

The sync engine landed in v0.2.0 (commit `4b61049`). Real-world testing
on the maintainer's library surfaced four classes of bug that were each
fixed with a focused commit + regression test. They're recorded here so
the spec stays canonical and the patches don't drift.

### 12.1 Tier-2 cross-platform false positives (commit `0c161d5`)

**Symptom.** Destination scans logged ~30 "tier-2 match with size drift"
warnings pairing local Game Boy `.gb` files to dest Game Boy Color
`.gbc` files (and vice versa, GB↔GBA, etc.) with massive size deltas
(32 KB vs 1 MB).

**Root cause.** The tier-2 match key was `(fuzzy_key, region)`. Both
"Pac-Man (USA).gb" and "Pac-Man (USA).gbc" produced identical
fuzzy_key + region, so the matcher cross-pollinated them. The size
drift warning fired but the match still won (per §3.3).

**Fix.** Added `system_id` to the tier-2 key:
`(fuzzy_key, region, system_id)`. Local rows contribute their
`rom.system_id`. The dest side derives its `system_id` from
`entry.rel_path` via `_system_id_from_rel_path(rel_path, profile)`
(which was already there for cover-delete grouping). Dest files
outside any known system folder return `None` rather than risk a
false positive.

`_MatchIndex` gained a `profile` field so the matcher can do the
folder→system lookup without an extra argument. `_find_tier2_inventory_entry`
got the same guard.

Regression: `tests/test_sync.py::TestTier2CrossPlatformGuard` (3 tests).

### 12.2 FK constraint storm during apply (commit `eb56c05`)

**Symptom.** A real push_merge run logged ~2000
`WARNING sync action failed: kind=copy_to_dest err=FOREIGN KEY constraint failed`
entries. Files were copied to disk but the inventory cache was never
populated — every re-run proposed the same copies.

**Root cause.** `_execute_copy_to_dest`, `_execute_delete_dest`, and
`_gamelist_rows_for_system` re-derived `dest_id` at apply time via
`_dest_id_from_target(conn, target)` → `SELECT id FROM sync_destinations
WHERE target_path = str(target)`. Path stringification can diverge from
the value stored at destination creation:
- Trailing-slash differences on UNC paths
  (`\\host\share\path` vs `\\host\share\path\`).
- Separator normalization (`//server/share/dir` vs
  `\\server\share\dir`).
- Case folding on Windows.

A mismatch returned `-1`, and the dest_inventory upsert then trip the
`dest_id REFERENCES sync_destinations(id)` FK.

**Fix.** `plan.dest_id` is now captured once at the top of `apply_plan`
and threaded into every helper as an explicit `dest_id: int` keyword
arg. When `dest_id < 0` (one-shot sync without a saved destination
row), the inventory write is silently skipped — the file copy still
succeeds, there's just no cache row.

Regression: `tests/test_sync.py::TestApplyUsesPlanDestId` (2 tests). The
path-mismatch test asserts `_dest_id_from_target` actively returns -1
in its setup before asserting `apply_plan` succeeds anyway via the
plan's dest_id.

### 12.3 Cover deletion + cleanup edge cases (commit `f5669b3`)

**Symptom.** Initial sync runs failed with FK errors on one-shot
destinations (the dropdown sentinel `dest_id=-1` reached
`upsert_dest_inventory` / `sync_plans`), the destination scan locked the
UI (no progress dialog), and the preview was unclear about what would
happen on Apply.

**Fixes.**
- Removed the `dest_id=-1` sentinel from the destination dropdown;
  added `q.ensure_sync_destination_by_path()` called before the scan
  worker spawns. (See also §12.2 for the deeper fix.)
- Added `DestScanProgressDialog` matching the pattern of other
  workers (modal QDialog, progress signals, cooperative cancel).
- Reworded the preview dialog (intro paragraph, totals label, button
  text `Apply changes to <target>` instead of generic `OK`).

### 12.4 Preview "Apply" → "Close" transition (commit `baaee64`)

**Symptom.** After the sync completed, the preview dialog still showed
only a `Cancel` button — users didn't know they were done.

**Fix.** `_enter_done_state` swaps the Apply button out, renames Cancel
to Close, rewires the slot to `accept()`. Used
`contextlib.suppress(RuntimeError, TypeError)` per ruff SIM105.

### 12.5 Behavioural spec amendments

These are *spec changes* that resulted from the fixes above. They take
precedence over earlier sections in this doc if anything reads
inconsistently:

1. **Tier-2 match key is now 3-tuple `(fuzzy_key, region, system_id)`.**
   Section [§3.2](#32-tier-2-fuzzy_key--region) describes a 2-tuple;
   the implementation uses the 3-tuple per §12.1.
2. **`plan.dest_id` is authoritative throughout apply.** §4 doesn't
   forbid re-deriving from target_path, but §12.2 makes clear it's
   forbidden in practice. New helpers must take `dest_id` as a
   parameter.
3. **One-shot syncs (`dest_id < 0`) skip dest_inventory writes
   entirely.** Not specified before; recorded now so future apply-step
   work doesn't try to make them work the same as saved destinations.

### 12.6 `build_plan` perf + worker thread (commit `e3082b4`)

**Symptom.** Push-merge against a 38 K-rom local library + 17 K-file
destination froze the UI for ~10 minutes after the destination scan
completed. The user had to kill the process or interrupt via
`Ctrl+C` from the launching shell. Traceback captured during the
freeze pointed straight at `_find_tier2_inventory_entry` →
`_fuzzy_region_key_for_entry` → `parse_filename` → `generate_fuzzy_key`
→ `re.sub` — looping forever inside a per-rom scan of the entire
inventory.

**Root causes (two, interleaved).**

1. **O(N·M) tier-2 fuzzy match.**
   `_find_tier2_inventory_entry` walked the entire destination
   inventory and recomputed every entry's `(fuzzy_key, region)` on
   every call, for every local rom that didn't have a tier-1 path
   match. On a 38 K × 17 K library that's ~600 M fuzzy-key
   computations — each one is a regex parse plus normalisation, on the
   order of microseconds. Total runtime: roughly an hour of pure CPU.

   The local side already had an O(N) pre-built index
   (`_MatchIndex.by_fuzzy_region`); the dest side just never got the
   same treatment.

2. **`build_plan` ran on the UI thread.**
   `_on_inventory_done` (the slot connected to
   `DestInventoryWorker.finished_ok`) called `build_plan(...)` inline.
   Slots fired across a queued connection from a worker thread still
   execute on the **receiving** thread — which here was the UI
   thread. So even though the dest inventory walk was correctly
   off-thread, the diff phase was synchronous on the UI.

**Fix.**

1. **Pre-built dest fuzzy index.** New helper
   `_build_inventory_fuzzy_index(inv_by_path, profile)` walks the
   inventory once at the top of `_build_push_actions` /
   `_build_twoway_actions` and returns
   `dict[(fuzzy_key, region, system_id), InventoryEntry]`. The
   tier-2 lookup becomes a single `dict.get`. Sidecar paths
   (gamelist.xml, .m3u, artwork dirs) are skipped; entries whose
   folder doesn't resolve to a profile system are skipped (can't
   safely tier-2 match without a system anchor); first-write-wins
   so re-releases don't shadow originals.

   Total fuzzy-key computations: **O(N·M) → O(M).** ~600 M → ~17 K
   in the user's scenario.

2. **`BuildSyncPlanWorker`.** New QThread worker that mirrors
   `ImportAnalyseWorker`'s shape — takes the inventory + mode +
   profile, calls `build_plan` with a progress callback, emits the
   resulting `SyncPlan` via `finished_ok`. New
   `SyncDiffProgressDialog` shows "Computing diff…" with a
   determinate bar driven by per-row progress ticks
   (every 500 rows). The dialog cancels cooperatively.

3. **`build_plan` emits enter/exit INFO logs.** A future "frozen UI"
   report can now be diagnosed from `logs/romulus.log` alone:

   ```
   INFO build_plan: start mode=push_merge dest_id=1 inventory=16555 entries
   INFO build_plan: match index built (38120 local roms)
   INFO build_plan: complete mode=push_merge actions=N (copy_to_dest=X, …)
   ```

**Regression coverage.** `tests/test_sync.py::TestInventoryFuzzyIndex`
(3 tests covering index construction, sidecar skip, end-to-end
tier-2 match preservation) plus
`TestBuildSyncPlanWorker::test_worker_produces_plan_and_emits_progress`.

### 12.7 Spec amendments from §12.6

These take precedence over earlier sections in this doc if anything
reads inconsistently:

1. **Tier-2 lookup is now O(1) per local rom.** §3.2 didn't specify
   complexity; the pre-built `dest_by_fuzzy` index is the implementation.
2. **`build_plan` is asynchronous from the UI thread.** §6 describes
   the workflow without mentioning the diff phase explicitly — it
   runs on `BuildSyncPlanWorker` between the dest scan and the
   preview dialog.
3. **`build_plan` accepts a `progress_callback`.** Signature
   `(current, total, label) -> None`. Both `_build_match_index` and
   the per-mode action builders honor it.
4. **Sidecar entries are excluded from the dest fuzzy index.** Both
   `gamelist.xml` / `.m3u` and artwork subfolder paths
   (`Imgs/`, `downloaded_media/`, `media/`) are skipped during
   index construction. Tier-2 matches against them used to be
   silently impossible due to the system-folder requirement; now
   they're explicitly excluded.

---

### 12.8 Spec amendments from v0.4.0 strict 1:1 refactor

**Shipped in commit `36f3496` (Session 16 of the strict 1:1 refactor).**

These take precedence over earlier sections in this doc if anything
reads inconsistently:

1. **`SyncAction.game_id` is removed.** `rom_id` is the sole identity
   anchor in every action and inventory write. Code that reads
   `action.game_id` must be updated to use `action.rom_id`.

2. **`dest_inventory.game_id` column is removed.** The FK to `games`
   is gone. Inventory rows are anchored on `(dest_id, rel_path)` and
   optionally `rom_id`. Existing databases are incompatible; wipe
   `data/romulus.db` and rescan.

3. **Tier-2 `region` reads from `roms.region` directly.** In v0.3.0
   the region came from a joined `games` row. In v0.4.0 it comes
   directly from `roms.region` (populated by scanner filename parsing
   and updated by Heavy Scan DAT match). The match key shape
   `(fuzzy_key, region, system_id)` is unchanged; only the source
   of `region` differs.

4. **Old `sync_plans` JSON is rejected at load time.** If a persisted
   plan's JSON payload contains `game_id` keys, `load_plan` raises
   `ValueError("plan was created against an old schema; re-run
   preview")`. Pre-v0.4.0 plans must be re-generated.
