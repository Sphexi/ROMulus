# Known Issues — Investigate Later

Open bugs not yet triaged into a fix. Newest first. Once an entry is fixed,
delete it (the commit message + CHANGELOG carry the history).

_No open issues at the moment._

---

## Closed in v0.4.0 hotfix

- **`find_rom_by_path` missed on slash-direction mismatch.** Surfaced
  against UNC libraries on Windows: 594 organize failures (212 renames,
  382 delete-duplicates) where the rom row was present in the DB but
  the lookup returned None. Root cause was a path-form convention split
  — the scanner writes backslash-form paths on Windows (via `os.walk` +
  `Path.__str__`), while the organizer's rename detector and
  `_header_rule_for` normalise to forward-slash before calling
  `find_rom_by_path`. SQLite's `WHERE path = ?` is exact-string match,
  so the lookup silently MISSED, which (a) skipped collision case 3,
  letting rename actions slip through preview only to fail at apply
  time with `FileExistsError`, and (b) forced `_header_rule_for` to
  return None so `hash_rom(path, None)` ran raw-stream hashing — fatal
  for `.zip` files (hashed the container instead of the inner ROM).
  Fixed by making `find_rom_by_path` tolerant: on miss it flips slash
  directions and retries once. Architecture doc gained a new
  "Path convention" section codifying the rule for future code.
  Test gate: `test_find_rom_by_path_tolerates_slash_direction` +
  `test_find_rom_by_path_tolerates_reverse_direction`.

---

## Closed in v0.4.0 (strict 1:1 refactor)

Entries below were fixed as part of the strict 1:1 rom↔game refactor
(sessions 13–19). Kept here as a reference for anyone reading the
commit history.

- **Bug 4 — Detail panel showed identical SHA-1 / region for USA + Europe
  variants.** The panel queried by `game_id` using `LIMIT 1` without an
  ORDER BY, so whichever rom happened to sort first in the database
  determined what displayed for all variants. Fixed by the strict 1:1
  refactor: each ROM is now its own row, and the panel reads SHA-1 / DAT
  name / region directly from the selected rom. Commit: `9417977` (Session 18).

- **Bug 3 — Organizer collision detector missed rename-vs-existing-file
  conflicts.** `detect_collisions` only checked rename-against-rename
  pairs. A rename whose target path was already occupied by an existing
  un-renamed library file would pass preview silently and fail at apply
  time with `FileExistsError`. Fixed by adding a `find_rom_by_path`
  lookup per rename target; conflicts now surface as `ACTION_COLLISION`
  in the preview. Commit: `d913180` (Session 17). (Note: Bug 3's actual
  fix was only fully effective once the path-form slash hotfix above
  landed — see the "Closed in v0.4.0 hotfix" section.)

- **Bug 2 — `_execute_delete_duplicate` TOCTOU guard always failed for
  cross-format same-content pairs.** The guard compared raw `_digest_stream`
  bytes, but the stored SHA-1 in `hashes` was computed by `hash_rom`
  which applies header-stripping (smc_512, ines_16, n64_byteswap).
  A `.sfc` + `.zip` pair of the same ROM produced different raw bytes
  but identical normalized SHA-1 — so the guard always refused.
  Fixed by replacing `_digest_stream` calls with `hash_rom(path, header_rule)`
  in the TOCTOU check. Commit: `d913180` (Session 17). (Same caveat:
  full fix required the path-form slash hotfix to let
  `_header_rule_for` resolve the system's `header_rule` correctly.)

- **Cross-extension `find_cross_extension_dupes` false-positives.** The
  detector proposed deleting the "non-canonical" extension when two roms
  shared a `game_id` and differed only in extension — but with N:1 grouping,
  regional variants (`Mario (USA).sfc` and `Mario (Europe).sfc`) also shared
  a `game_id`, and the region-variant `.sfc` files got flagged as duplicates
  of each other. Removed the entire detector in v0.4.0: with strict 1:1 there
  is no `game_id` to share. Legitimate same-content cross-extension pairs
  (`.sfc` + `.zip`) are handled by `find_duplicates` (SHA-1 equality after
  normalization). Commit: `d913180` (Session 17).

- **Cover DB row count grows ~N× under strict 1:1 where N is the number of
  byte-identical copies.** The sibling-copy gate (Session 15) prevents
  redundant network fetches, but it does insert one `covers` row per rom
  row (pointing at the same on-disk image file). For libraries with many
  intentional duplicates this inflates the `covers` table relative to v0.3.0.
  The on-disk image cache is unaffected — both rows share the same
  `local_path`. Not a bug but a known trade-off of the 1:1 model; documented
  in `docs/strict-1to1-design.md` §5.
