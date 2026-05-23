# Session 14: Strict 1:1 — Scanner / Identifier / Importer

**Type:** Build (Phase 2 of 7 — strict 1:1 rom ↔ game refactor)

**Context for this session:**

With the schema flattened (Session 13), the scanner and importer no longer need a separate "create or find a games row" phase. Every ROM file writes its own identity (`title`, `region`, `revision`, `is_hack`, etc.) directly onto its `roms` row at upsert time. The post-walk grouping pass (`_group_unlinked_roms_into_games` in [core/scanner.py:550-576](../../src/romulus/core/scanner.py#L550-L576)) is **deleted entirely** — its job no longer exists.

Heavy Scan still updates identity columns *in place* on each rom row once a DAT match lands: `canonical_name = dat_match`, plus the region/revision parsed from the DAT name's parens. No row creation, no row splitting, no reconciliation pass.

Importer ([core/importer.py](../../src/romulus/core/importer.py)) mirrors the same flow for inbound enrolment.

In one sentence: **"Identity fields write to roms directly; the grouping phase is gone."**

**Workflow:**

1. Quick Scan walks the filesystem and produces, per file, a parsed identity (filename parser already does this — region/revision/hack flags come out of `_no_intro_tokens`).
2. Scanner calls `q.upsert_rom(conn, RomUpsertData{... title, region, revision, is_hack, is_homebrew, fuzzy_key, ...})`.
3. Heavy Scan computes the SHA-1, looks it up against bundled DATs via `core/dat_parser.match_hashes`, and on a hit UPDATEs the same rom row: `canonical_name`, `match_confidence='dat_verified'`, plus re-parsed region/revision from the canonical DAT name.
4. Importer's `analyse_import` + `apply_plan` ([core/importer.py](../../src/romulus/core/importer.py)) carries the same identity fields into the rom row it enrolls.

**Carry-forward from prior sessions:**

- **Single library at a time** (design rule #6). Scanner's missing-row sweep stays — any row not visited this scan is tombstoned regardless of `library_root`.
- **Tombstone, don't delete** (design rule #7). The path-keyed UPSERT contract from Session 13's `upsert_rom` change means re-scanning an absent-then-present file un-tombstones the row without losing identity columns.
- **Hash cache is sacred** (design rule #9). Hashing path is unchanged; just the post-hash UPDATE writes more columns now.
- **Importer is symmetric to sync** (design rule #21). The atomic copy + per-action SAVEPOINT shape is unchanged; only the enrolment `upsert_rom` call carries new identity fields.
- **Cooperative cancel** between scanner phases (the post-walk DB phase disables Cancel today — that constraint remains, the phase just gets shorter).
- **Filename parser is canonical.** Filename → `(title, region, revision, is_hack, is_homebrew, is_bios?)` parsing already lives in `core/scanner.py` and `core/_no_intro_tokens.py` — DO NOT duplicate it. If Heavy Scan needs to re-parse from a DAT name, factor the parens-tokenizing helper into a shared private function in `core/_no_intro_tokens.py` rather than duplicating logic.

**Tasks:**

- [ ] `src/romulus/core/scanner.py`:
  - **Delete** `_group_unlinked_roms_into_games` and its caller in the post-walk phase.
  - In the per-file upsert path (the loop inside the walker), populate the new identity fields on the `RomUpsertData` dict from the existing filename parse result.
  - The "post-walk DB phase" progress reporting referenced in CLAUDE.md current state stays — it just has fewer sub-steps now (no grouping). Reword the user-facing progress strings ("Grouping ROMs into games…" → drop) so the dialog matches reality.
  - Verify the missing-row sweep still runs as the final phase.
- [ ] `src/romulus/core/scanner.py` — Heavy Scan post-DAT-match step:
  - After `match_hashes` returns a hit, UPDATE the rom row with `canonical_name`, `region`, `revision` (parsed from the canonical name's parens), `match_confidence='dat_verified'`, `dat_match=<canonical_name>`.
  - If the DAT entry also indicates `is_bios` (some DAT files flag BIOS dumps), propagate.
  - On no-hit, leave the existing identity columns alone — fuzzy/header data set at Quick Scan time stays.
- [ ] `src/romulus/core/identifier.py`:
  - If L2 header extraction produces a `header_title`, it continues writing to `roms.header_title`. No structural change; verify the upsert still threads it through.
  - If header extraction yields a region (rare — Lynx headers carry one), set `roms.region` only if not already set by filename parse.
- [ ] `src/romulus/core/_no_intro_tokens.py`:
  - Factor out the region/revision/hack-tag parser into a public helper `parse_no_intro_tokens(name: str) -> ParsedTokens` returning a small dataclass. Used by scanner (filename input) and Heavy Scan (DAT name input).
- [ ] `src/romulus/core/importer.py`:
  - `analyse_import` populates the identity fields when constructing the planned `upsert_rom` payload — same flow as the scanner.
  - `apply_plan` already calls `upsert_rom`; threads through the new fields.
  - The "import a `missing=1` row" un-tombstone path keeps working via the path-keyed UPSERT contract from Session 13.
- [ ] `src/romulus/ui/workers.py`:
  - `ScanWorker` / `HeavyScanWorker` / `ImportAnalyseWorker` / `ImportApplyWorker` need no API changes, but the progress-string constants change with the deleted phase. Update any worker that emits "Grouping ROMs…" to skip that string.
- [ ] `src/romulus/ui/scan_progress.py`:
  - The progress dialog shows phase names; drop the grouping phase from the phase list.

**Test files affected** (Session 19 re-baseline):

- `tests/test_scanner.py` — `_group_unlinked_roms_into_games` tests delete; identity-on-upsert tests added in Session 19.
- `tests/test_importer.py` — identity threading through `upsert_rom`.
- `tests/test_identifier.py` — header-fills-region case.

**Acceptance criteria:**

- `_group_unlinked_roms_into_games` does not appear anywhere in `src/romulus/`.
- A Quick Scan walk of a fixture filesystem produces rom rows whose `title`, `region`, `revision`, `is_hack` columns are populated from filename parsing alone (verifiable in Session 19 tests).
- A Heavy Scan against a fixture DAT promotes a rom row's `canonical_name` / `match_confidence` / `region` / `revision` in place (no row created / split).
- Importer enrolment writes the same identity columns.
- Filename and DAT-name parsing share one helper in `_no_intro_tokens.py`.
- Ruff clean on `src/romulus/core/`.
- Manual smoke: Quick Scan a small fixture library, confirm rom rows have title/region in the DB. Heavy Scan one row, confirm canonical_name lands.

STOP. Commit with message `refactor(scanner): write identity fields directly to roms; drop grouping phase`. Move to Session 15.
