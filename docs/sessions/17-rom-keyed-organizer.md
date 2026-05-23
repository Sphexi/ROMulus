# Session 17: Strict 1:1 — Organizer Rewrite + Bug 2 + Bug 3 Fixes

**Type:** Build (Phase 5 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

Three problems get fixed together in this session because the organizer is being meaningfully reshaped anyway:

1. **Strict 1:1 makes `find_cross_extension_dupes` obsolete.** Its concept ("two ROMs share game_id, propose deleting the non-canonical extension") no longer exists — there is no game_id to share. Cross-extension duplicates with identical *content* are caught structurally by `find_duplicates` (the SHA-1 detector). Cross-extension *variants* with different content are correctly NOT proposed for deletion anymore.

2. **Bug 2 — TOCTOU re-hash guard uses raw stream digest.** [organizer.py:562-598](../../src/romulus/core/organizer.py#L562-L598) calls `_digest_stream(path)` which reads raw bytes. The stored `hashes.sha1` came from `hash_rom()` which applies the system's `header_rule` (smc_512 strip, n64 byteswap, etc.) and zip-extracts the inner ROM. So legitimate same-content pairs like `.sfc` vs `.zip` or headered `.smc` vs `.sfc` always fail the guard. The fix: re-hash via `hash_rom(path, header_rule)` so the guard compares apples to apples.

3. **Bug 3 — Collision detector misses rename-vs-existing-file.** [organizer.py:460-505](../../src/romulus/core/organizer.py#L460-L505) only checks rename-against-rename collisions. Rename targets that collide with an existing non-renamed rom (different content) are caught at `_execute_rename` time as `FileExistsError`, but the preview should have flagged them so the user could resolve before applying.

All three land here because they touch the same module.

In one sentence: **"Delete cross-extension detector, fix TOCTOU guard's hash comparison, extend collision detector to cover existing-file targets."**

**Carry-forward from prior sessions:**

- **Never modify files without preview** (design rule #4). Preview semantics unchanged.
- **Atomic writes only** (design rule #5). Rename / delete paths unchanged.
- **Hacks are first-class artifacts** (design rule #8). `find_duplicates` continues excluding `is_hack=1` rows via the WHERE clause — `is_hack` now reads from `roms` directly (Session 13 moved the column).
- **Per-action SAVEPOINT rollback.** The execute loop's `try/except: ROLLBACK TO SAVEPOINT` pattern stays.
- **The "Done. Applied 0, skipped 0, failed 244" symptom** from the user's bug report is what we're killing here. After this session, applying a generated plan against the user's real library should land most actions cleanly (where today every cross-extension dupe and every header-stripped same-content dupe fails).

**Tasks:**

- [ ] `src/romulus/core/organizer.py`:
  - **Delete `find_cross_extension_dupes`** entirely. Drop its import in `analyze_library` and the corresponding `actions.extend(cross_ext)` line. The function's docstring referenced "same game/system/folder, multiple extensions" — that concept is gone now.
  - **Fix Bug 2 in `_execute_delete_duplicate`**:
    - Replace `_digest_stream(source)` / `_digest_stream(target)` with `hash_rom(path, header_rule)` calls (the function from `core/hasher.py`, NOT the bare `_digest_stream`).
    - Resolve `header_rule` via the rom's `system_id` looked up against `SYSTEM_REGISTRY` (`get_system(system_id).header_rule`). If either side's system_id isn't in the registry, fall back to `_digest_stream` (matches today's behaviour for unknown systems — at least raw equality still works for unheadered formats).
    - Compare the resulting `HashResult.sha1` values.
    - Update the error message to say "post-normalization SHA-1 no longer matches keeper" so future debug logs are unambiguous.
    - Keep the `OSError` re-raise behaviour for unreadable files.
  - **Fix Bug 3 in `detect_collisions`**:
    - In addition to the existing rename-vs-rename and rename-target-equals-rename-source checks, add a third check: for each rename target, look up `q.find_rom_by_path(conn, target)`. If a rom row exists at that path AND that rom is not itself being renamed in this plan, surface as `ACTION_COLLISION` with reason "target path already occupied by a different file in the library" and filter out the conflicting rename.
    - Path comparison uses the forward-slash normalization the rest of the organizer already applies.
    - Requires `detect_collisions` to take `conn` as a parameter — today it takes only an actions iterable. Update the one call site in `analyze_library`.
  - **Keep `find_duplicates`** unchanged. With Bug 2 fixed, this detector now correctly executes its proposed deletes for legitimate same-content pairs (e.g. `Bugs Bunny.sfc` + `Bugs Bunny.zip` — same normalized SHA-1, different raw bytes).
  - **Keep `find_alias_merges`** unchanged.
  - **Keep `find_renameable_roms`** unchanged in logic, but verify it reads identity fields from `roms` directly (Session 13 moved them off games).
- [ ] `src/romulus/core/hasher.py`:
  - `_digest_stream` is currently private (`_` prefix). Leave it private; organizer should NOT call it directly anymore. The clean import path is `from romulus.core.hasher import hash_rom`.
  - Verify `hash_rom` accepts a `Path` (it does — type hint is `str | os.PathLike[str]`).
- [ ] `src/romulus/db/queries.py`:
  - Verify `find_rom_by_path` (added in Session 12, [queries.py around find_rom_by_path]) is still present after Session 13's rewrite. If accidentally deleted, restore it.

**Test files affected** (Session 19 re-baseline):

- `tests/test_organizer.py`:
  - Delete `TestFindCrossExtensionDupes` test class entirely.
  - Update `TestDetectCollisions` to cover the new rename-vs-existing-file case.
  - Update `TestExecuteDeleteDuplicate` (or equivalent) to cover Bug 2's fix: assert a `.sfc` + `.zip` same-content pair succeeds (today it always fails); assert a different-content pair still refuses.
- `tests/test_hasher.py` — no changes; `hash_rom` is unchanged.

**Acceptance criteria:**

- `find_cross_extension_dupes` does not exist anywhere in `src/romulus/`.
- `analyze_library` produces actions only from `find_alias_merges`, `find_renameable_roms`, `find_duplicates`, and `detect_collisions` post-processing.
- `_execute_delete_duplicate` calls `hash_rom(path, header_rule)` for the TOCTOU guard; `_digest_stream` is not imported by `organizer.py`.
- Applying a fixture plan that pairs `Mario.sfc` + `Mario.zip` (same content) succeeds (action.executed=True) when today it would fail.
- Applying a fixture plan that pairs `Mario (USA).sfc` + `Mario (Europe).smc` (different content) **never proposes** the action — `find_cross_extension_dupes` is gone and `find_duplicates` requires identical SHA-1.
- `detect_collisions` flags a rename whose target equals an existing un-renamed rom row's path. The conflicting rename is filtered out and an `ACTION_COLLISION` row replaces it.
- Manual smoke (deferred to user testing post-Session 19): re-run Organize on the user's 38K-ROM library; expect the 191 bogus cross-ext "duplicates" to disappear from the preview and the 6 legitimate SHA-1 dupes to apply successfully.
- Ruff clean on `src/romulus/core/organizer.py`.

STOP. Commit with message `refactor(organizer): delete cross-ext detector; fix TOCTOU normalized hash; collision detector covers existing-file targets`. Move to Session 18.
