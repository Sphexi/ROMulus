# Session 19: Strict 1:1 — Test Re-baseline + Verification

**Type:** Build (Phase 7 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

Sessions 13-18 reshaped the schema, scanner, metadata, exporter, sync, organizer, and UI. The full test suite (~1003 tests pre-refactor) is broken at the end of Session 18 by design — the user opted to defer test fixes until everything was in place, so this session brings the suite back to green in one focused pass.

This session is mostly **mechanical fixture and assertion updates**. The per-phase sessions listed which test files they touched; this session executes those updates plus adds the few **new** tests the refactor specifically requires:

- Sibling-copy metadata + cover gates (Session 15) — must short-circuit network calls on the second of two byte-identical ROMs.
- Distinct-content export toggle (Session 16) — keeper selection per SHA-1 group; no-SHA-1 rows always pass; integration with `include_roms=False`.
- Organizer Bug 2 fix (Session 17) — `.sfc` + `.zip` same-content pair now applies; different-content pair still refuses.
- Organizer Bug 3 fix (Session 17) — rename-target vs existing-file collision detection.
- Cascade-deletion verification (Session 13) — deleting a roms row clears metadata / covers / collection_roms via `ON DELETE CASCADE`.
- Detail panel disambiguation (Session 18) — two regional variants display distinct per-rom data.

In one sentence: **"Bring the suite green; add the handful of new tests that validate the new behaviour."**

**Carry-forward from prior sessions:**

- **Tier: Standard** (CLAUDE.md). Unit tests + ruff; code reviews every 2-3 sessions. This session is one of those review milestones.
- **Test fixtures** in `tests/conftest.py` (`db`, `seeded_db`, `qapp`) are unchanged — schema differences are absorbed at the `create_tables` call.
- **The `_insert_rom` helper pattern** that lives in multiple test files (most prominently `tests/test_organizer.py:39-70`) loses its `game_id` parameter. Helper becomes simpler: insert a rom row with identity fields directly.
- **POSIX-only chmod test** in the suite is skipped on Windows CI per CLAUDE.md current state. Leave that skip alone.
- **Ruff clean** must hold across the whole repo at the end of this session.

**Workflow:**

1. Update shared test helpers (`_insert_rom` and friends across files).
2. Run the suite; fix failures file by file.
3. Add the new behaviour tests listed below.
4. Verify ruff cleanly across the repo.
5. Update CLAUDE.md current-state section + CHANGELOG.md.

**Test files to update:**

| File | Estimated touchpoints | Notes |
|---|---|---|
| `tests/test_db.py` | small | upsert_game tests deleted; the cascade-deletion test from Session 13 already added |
| `tests/test_scanner.py` | ~5 | _group_unlinked_roms_into_games tests deleted; add identity-on-upsert tests |
| `tests/test_identifier.py` | small | header-fills-region case |
| `tests/test_hasher.py` | none | no changes |
| `tests/test_dat_parser.py` | none | no changes |
| `tests/test_metadata.py` | ~54 | rom-keyed; add sibling-copy gate tests |
| `tests/test_covers.py` | ~80 | rom-keyed; add sibling-cover gate tests |
| `tests/test_local_cover_finder.py` | ~22 | dataclass + query renames |
| `tests/test_exporter.py` | ~25 | per-rom <game> entries; distinct_content_only toggle tests |
| `tests/test_sync.py` | ~2 | drop game_id assertions; verify tier-2 still correct |
| `tests/test_sync_preview.py` | ~3 | similar |
| `tests/test_sync_fixes.py` | ~2 | similar |
| `tests/test_organizer.py` | ~13 | TestFindCrossExtensionDupes deleted; Bug 2 + 3 fix tests added |
| `tests/test_library_cleanup.py` | ~40 | bulk of these get deleted — cascade does the work; keep representative ones verifying cascade |
| `tests/test_collections.py` | ~32 | pure rename game_id → rom_id |
| `tests/test_importer.py` | small | identity threading through upsert |
| `tests/test_scrub.py` | none | rom-keyed already |
| `tests/test_ui.py` | ~57 | detail panel disambiguation; signal renames |
| `tests/test_scoped_actions.py` | ~43 | scope by rom_id works the same as by game_id; pure rename |
| `tests/test_packaging.py` | none | no changes |
| `tests/test_per_system_summary_dialog.py` | small | distinct_duplicates column smoke if shipped |

**New tests to add:**

- [ ] `tests/test_metadata.py` — class `TestSiblingMetadataCopy`:
  - Two roms with identical SHA-1 in fixture DB.
  - One has a metadata row; second triggers enrichment.
  - Mock all six metadata sources to track call counts.
  - Assert: second rom ends up with a metadata row (copied) AND zero calls to any source.
  - Variant: same `(system_id, canonical_name)` but no SHA-1 — sibling-copy still triggers.
  - Variant: no sibling found — falls through to the source chain.
- [ ] `tests/test_covers.py` — class `TestSiblingCoverCopy`:
  - Same shape as above for cover-row copying.
  - Asserts the on-disk `local_path` is reused (same string in both cover rows).
  - Asserts `_ensure_preferred` runs for the dest rom.
- [ ] `tests/test_exporter.py` — class `TestDistinctContentOnly`:
  - 3 byte-identical roms (same SHA-1, different paths) + 1 distinct rom.
  - `distinct_content_only=False`: 4 `<game>` entries.
  - `distinct_content_only=True`: 2 `<game>` entries (one keeper from the 3-set + the distinct rom).
  - Keeper rule: dat_verified beats fuzzy; canonical extension beats non-canonical; shorter filename wins.
  - Rom row with no SHA-1: always exports regardless of toggle.
  - Compose with `include_roms=False`: artwork-only export still skips covers for non-keeper roms.
- [ ] `tests/test_organizer.py` — class `TestExecuteDeleteDuplicateNormalizedHash` (Bug 2):
  - Fixture: real `.sfc` file + real `.zip` of same content; both have the same SHA-1 in `hashes` table because `hash_rom` normalizes.
  - `_execute_delete_duplicate` against the pair: succeeds, source file is unlinked.
  - Fixture: real `.sfc` + a `.zip` of *different* content; same path layout. Action proposed manually; guard correctly refuses with the new "post-normalization SHA-1 no longer matches keeper" error.
- [ ] `tests/test_organizer.py` — class `TestDetectCollisionsExistingFile` (Bug 3):
  - Fixture: rom A at `/lib/snes/657 Igo.nes` is dat_verified to "Igo - Kyuu Roban Taikyoku (Japan)".
  - Fixture: rom B at `/lib/snes/Igo - Kyuu Roban Taikyoku (Japan).nes` exists with different SHA-1, fuzzy match.
  - `analyze_library` proposes renaming A to B's path.
  - `detect_collisions(conn, actions)` returns: the rename action filtered out, an `ACTION_COLLISION` row replaces it with reason "target path already occupied by a different file".
- [ ] `tests/test_db.py` — class `TestCascadeDelete` (already added in Session 13, verify expansion):
  - Delete a rom row.
  - Assert: `metadata` row gone, `covers` rows gone, `collection_roms` rows gone, no orphans left.
- [ ] `tests/test_ui.py` — class `TestDetailPanelDisambiguation` (Bug 4 fix):
  - Fixture: two rom rows with same fuzzy_key but different SHA-1 + dat_match (USA / Europe).
  - Select rom 1, assert detail panel shows USA's SHA-1, region, dat_match.
  - Select rom 2, assert detail panel shows Europe's distinct SHA-1, region, dat_match.
  - The exact bug the user reported is the regression test gate.

**Tasks:**

- [ ] Walk each affected test file in order; apply renames + delete obsolete classes. Run the file's tests after each major edit so failures stay localized.
- [ ] Add the new behaviour tests in `test_metadata.py`, `test_covers.py`, `test_exporter.py`, `test_organizer.py`, `test_db.py`, `test_ui.py`.
- [ ] Verify the suite is green: `pytest -q`. Expected: ~1000 passing, 1 skipped (POSIX chmod), plus whatever net new tests this session adds — minus the deleted obsolete tests. The final total may land slightly below or above the pre-refactor 1003.
- [ ] Ruff clean across `src/` + `tests/`: `ruff check src tests`.
- [ ] Update `CLAUDE.md` current-state section:
  - Bump the test count.
  - Add design rules for the new model:
    - **#28: One rom = one game.** Identity unit is the rom file; metadata, covers, and collection memberships are rom-keyed.
    - **#29: Sibling-copy preserves API quotas.** `enrich_library` short-circuits when an identical-identity rom already has metadata/covers.
    - **#30: Distinct-content export is opt-in.** `ExportOptions.distinct_content_only` defaults False; when True, only one rom per SHA-1 cluster is exported.
  - Mention the deleted concepts: no more games table, no more `find_cross_extension_dupes`, no more grouping pass in scanner.
- [ ] Update `CHANGELOG.md` under a new `v0.4.0` (or whatever the next version is — confirm with current state):
  - "Reshaped data model to strict 1:1 rom ↔ game. Each ROM file is its own row with its own metadata and covers. Byte-identical copies are surfaced as duplicates instead of being silently collapsed. Fixes the regional-variant display bug and the cross-extension dedup false positives."
  - "Added `Export distinct content only` toggle to the Export dialog."
  - "Fixed organizer TOCTOU guard to compare normalized hashes; fixed collision detector to flag rename targets occupied by existing un-renamed files."
- [ ] Manual smoke against the user's library (the user said they'll handle this after the session lands):
  - Wipe `data/romulus.db`, Quick Scan, Heavy Scan, Enrich Metadata, Find Covers.
  - Verify Woody Woodpecker USA + Europe show as two distinct rows with distinct detail-panel data.
  - Verify Organize preview no longer proposes the 191 cross-extension false-positive dupes.
  - Verify Organize Apply lands the 6 legitimate SHA-1 dupes successfully.

**Acceptance criteria:**

- `pytest -q` exits 0; exactly 1 test skipped (the POSIX chmod skip).
- `ruff check src tests` exits 0.
- New behaviour tests listed above all pass and are tagged to their respective bugs/features in the test docstrings.
- `CLAUDE.md` reflects the new model — design rules updated, current-state section bumped, project structure section updated to drop `games` references.
- `CHANGELOG.md` has the new version block.
- No `game_id` references remain in `src/romulus/` (grep returns nothing meaningful — at most stale comments to clean up).
- No `upsert_game` / `link_rom_to_game` / `find_game_id_for_fuzzy_key` references in `src/` or `tests/`.

STOP. Commit with message `refactor(tests): rebaseline suite for strict 1:1 model; add sibling-copy / distinct-export / bug-2 / bug-3 / disambiguation tests`. The strict 1:1 refactor is complete after this session.

## Post-refactor verification (user-driven, not part of this session):

- Manual smoke on the user's 38K-ROM library per the steps above.
- If the smoke surfaces issues, file follow-up `fix:` commits — sessions are not the unit of work past this point per the project's session-retirement convention.
