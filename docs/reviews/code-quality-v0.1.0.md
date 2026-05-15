# Code Quality Review — Romulus v0.1.0 RC

**Reviewer:** comprehensive-review:code-reviewer agent
**HEAD reviewed:** 8b903ff
**Date:** 2026-05-14

## Executive summary

Romulus is in unusually good shape for a v0.1.0 release candidate built across 11 sessions. The architecture is coherent and the layering is respected: every SQL query lives in `db/queries.py`, every filesystem mutation routes through `core/atomic.py`, every long-running operation goes through a QThread worker with a uniform `progress` / `finished_ok` / `failed` signal contract, and Session 10's `core/atomic.py` extraction was a genuinely valuable factoring. Docstring coverage on public functions and classes is essentially complete and the style is consistently terse-but-informative. Ruff is clean, the test suite covers the load-bearing behaviours (atomic-write rollback, match-confidence monotonicity, hack-vs-original dedup safety, scan idempotency), and HTTP traffic is universally mocked.

The biggest risks are concentrated in three places: (1) the worker layer, which duplicates the same ~30-line cooperative-cancel scaffolding four times in `ui/workers.py`; (2) a small cluster of cross-module duplication around N64 byteswap helpers and No-Intro region/revision tokens between `core/scanner.py`, `core/identifier.py`, `core/hasher.py`, and `core/dat_parser.py`; and (3) the match-confidence rank table, which is encoded three times — once as a Python dict in `db/queries.py`, once as a SQL `CASE` expression a few lines below it, and once inline in `ui/detail_panel.py`. These are all maintenance hazards rather than correctness bugs; they will bite the moment the rank table grows a fifth level.

The biggest strengths to preserve are the discipline around atomic writes, the per-action `SAVEPOINT` rollback in `core/organizer.execute_plan`, the strict containment of SQL inside `db/queries.py`, and the worker concurrency guards (`_organize_worker.isRunning()` checks plus `closeEvent` `cancel + wait(5000)`) which are tested with a fake worker rather than mocked away. The data-modelling choices are also good: hacks are first-class artifacts at every layer, `match_confidence` is monotonic on rescan, and `DestinationProfile` forces every built-in profile to make an explicit `supported: true/false` decision for every system in the registry. Overall verdict: ready to tag v0.1.0 as-is; the items below are housekeeping for v0.2.0 with a small number worth fixing before the tag.

## Findings

### Critical / High

#### 1. `cursor.lastrowid` returned from `-> int` functions can be `None` (high — type-safety)
**Files / lines:**
- `src/romulus/db/queries.py:147` (`upsert_game`)
- `src/romulus/db/queries.py:206` (`insert_scan_history`)
- `src/romulus/db/queries.py:330` (`insert_dat_entry`)
- `src/romulus/db/queries.py:448` (`insert_cover`)
- `src/romulus/db/queries.py:536` (`ensure_favorites_collection`)
- `src/romulus/db/queries.py:554` (`create_collection`)
- `src/romulus/db/queries.py:777` (`insert_organize_plan`)

Every one of these functions ends `return cursor.lastrowid` and is annotated `-> int`. Python's `sqlite3.Cursor.lastrowid` is typed `int | None` in the stdlib stubs and is documented to be `None` if the cursor's last operation did not produce an INSERT into a rowid table. In practice these always-INSERT call sites do return an int, but the type annotation is a lie that mypy / pyright would flag in strict mode. The downstream callers (e.g. `MainWindow._on_new_collection` at `src/romulus/ui/main_window.py:251` and `ensure_favorites_collection` returning into `DetailPanel._favorites_id` at `detail_panel.py:55`) then index off the returned value as if it were guaranteed int.

`upsert_rom` at `src/romulus/db/queries.py:90-96` has the explicit `if cursor.lastrowid: return cursor.lastrowid; else fall back to a SELECT` shape and is the right pattern. Suggested fix: either `assert cursor.lastrowid is not None` and let the type narrow, or wrap every insert in the same `if cursor.lastrowid else SELECT` form `upsert_rom` uses. The latter is more defensive but the assertion is the smallest change.

#### 2. Match-confidence rank duplicated in three places (high — single source of truth)
**Files / lines:**
- `src/romulus/db/queries.py:19` — `_CONFIDENCE_RANK` dict
- `src/romulus/db/queries.py:64-69` — same rank encoded as a SQL `CASE` expression inside `upsert_rom`
- `src/romulus/ui/detail_panel.py:292` — `rank = {"unmatched": 0, "fuzzy": 1, "header": 2, "dat_verified": 3}` re-declared inline inside `DetailPanel._best_confidence`

If the rank ever grows a fifth value (e.g. `"hash_only"`), all three sites have to be updated in lockstep. The SQL `CASE` form is unavoidable for the upsert (you can't bind a Python dict into a SQL expression), but the Python-side duplication is gratuitous: `_best_confidence` should import `_CONFIDENCE_RANK` from `db/queries.py` (or both should live in a shared module). Worth fixing before v0.1.0 tag — it's a five-line change with no test impact.

#### 3. Four near-identical QThread workers in `ui/workers.py` (high — duplication)
**Files / lines:** `src/romulus/ui/workers.py:28-291`

`ScanWorker`, `EnrichWorker`, `OrganizeWorker`, and `ExportWorker` are structurally identical:
- Each owns a private `_CancelledError` exception subclass (lines 79, 143, 209, 289)
- Each has an identical `cancel()` method setting `self._cancel_requested = True`
- Each has a `run()` method with the same 5-step body: open thread-local DB connection, build progress-callback that raises the private exception on cancel, try/except the work function, close connection, emit the right `finished_ok` / `failed` signal

The only differences are (a) the work function called and (b) the `finished_ok` signal shape. This is the canonical case for a base class:

```python
class _CancelRequested(Exception):
    """Shared cooperative-cancel marker for every worker."""

class _DbWorker(QThread):
    failed = Signal(str)
    def __init__(self, db_path): ...
    def cancel(self): self._cancel_requested = True
    def run(self):
        try: conn = get_connection(self._db_path)
        except Exception as exc:
            self.failed.emit(f"Failed to open database: {exc}"); return
        try: self._do_work(conn)
        except _CancelRequested: ...
        except Exception as exc: ...
        finally: conn.close()
    def _do_work(self, conn): raise NotImplementedError
```

The four `_*CancelledError` subclasses serve no functional purpose — they could be a single `_WorkerCancelled` in the module. Suggested for v0.2.0 (it touches every worker test, so not urgent but very high impact on maintainability).

### Medium

#### 4. N64 byteswap helpers duplicated between identifier and hasher
**Files / lines:**
- `src/romulus/core/identifier.py:54-72` — `_byteswap_v64_to_z64` / `_byteswap_n64_to_z64`
- `src/romulus/core/hasher.py:48-64` — identical bodies, identical function names

Plus the three N64 magic constants (`_N64_MAGIC_Z64`, `_N64_MAGIC_V64`, `_N64_MAGIC_N64`) at `identifier.py:14-16` and again at `hasher.py:26-28`. These are bit-for-bit copies. Suggested fix: move both helpers and the three magic constants to a private `core/_n64.py` (or even into `core/atomic.py`'s neighbour `core/_rom_normalization.py`), import from both call sites. Same pattern would apply to `_INES_MAGIC` and `_LYNX_MAGIC` if they ever sprout a second user. Low risk because the test suite covers both copies separately, but worth factoring before either grows a divergence.

#### 5. `_REVISION_RE` and `_TAG_GROUP_RE` duplicated between scanner and dat_parser
**Files / lines:**
- `src/romulus/core/scanner.py:150-151` — `_TAG_GROUP_RE`, `_REVISION_RE`
- `src/romulus/core/dat_parser.py:52-53` — same regexes

The DAT parser also keeps a separate `_DAT_REGION_TOKENS` (`dat_parser.py:24`) that is a subset of `scanner._REGION_TOKENS` (`scanner.py:96`). The scanner's set includes language codes (`en`, `ja`, `fr`...) because filenames carry them; the DAT set doesn't because DAT names don't. Both are valid, but the divergence is undocumented at the source level — only the comment at `dat_parser.py:21-23` flags it. Suggested fix: put both sets in `core/_no_intro_tokens.py` with a short explanation of why DAT names use the smaller set. Same applies to `_REVISION_RE`.

#### 6. `core/organizer.py._atomic_replace` is a no-op wrapper around `atomic.atomic_replace`
**Files / lines:** `src/romulus/core/organizer.py:419-426`

This is a one-line aliasing function added in Session 10 to ease the migration to the shared `atomic` module. It no longer carries weight — every caller could just call `atomic.atomic_replace` directly. Suggested fix: delete the wrapper, replace the two call sites at `organizer.py:439` and `organizer.py:475` with `atomic.atomic_replace(source, dest)`. Pure cleanup, no behaviour change.

#### 7. `execute_plan` returns `dict[str, Any]` for a fixed-shape result
**Files / lines:** `src/romulus/core/organizer.py:486-553`

The summary has exactly four keys (`applied`, `skipped`, `failed`, `errors`) but is typed as `dict[str, Any]`. Callers like `OrganizeWorker.run` at `ui/workers.py:200-206` then access `summary.get("applied", 0)` defensively as if the keys might be missing. `ExportSummary` (a dataclass at `core/exporter.py:101-112`) is the right pattern. Suggested fix: introduce an `OrganizeSummary` dataclass and stop using `dict.get` at the call sites.

#### 8. Repeated `httpx.Client` ownership boilerplate in metadata clients
**Files / lines:**
- `src/romulus/metadata/libretro.py:78-88`
- `src/romulus/metadata/hasheous.py:93-133`
- `src/romulus/metadata/screenscraper.py:101-129`, `156-182`

Each client has the same `owns_client = client is None; if client is None: client = httpx.Client(...); try: ... finally: if owns_client: client.close()` pattern. Adding a private helper in `metadata/__init__.py`:

```python
@contextmanager
def _http_client(client: httpx.Client | None, timeout: float):
    if client is not None:
        yield client; return
    with httpx.Client(timeout=timeout) as c:
        yield c
```

…would replace ~12 lines across four files. Low risk because the existing tests inject mock clients via the `client=` kwarg.

#### 9. Module-level rate-limit state shared by name only, not implementation
**Files / lines:**
- `src/romulus/metadata/hasheous.py:31` (`_last_request_ts: float = 0.0`) + `_respect_rate_limit` at line 40
- `src/romulus/metadata/screenscraper.py:23` (`_last_request_ts: float = 0.0`) + `_respect_rate_limit` at line 26

Both modules have a module-level mutable global and an identical function with `global _last_request_ts`. They throttle independently (good) but the function body is duplicated. The thread-safety story is also a bit fuzzy — `_respect_rate_limit` is called from the enrich worker thread, but only that single thread, so the lack of a lock is fine. Worth a one-line comment ("called only from EnrichWorker — no lock needed") in both files, or pulling the throttle into a tiny `_Throttle` class shared by both clients. Lower priority because the behaviour is correct.

#### 10. `ExportFilters.regions` and `ExportFilters.collection_id` are completely untested
**Files / lines:** `src/romulus/core/exporter.py:67-79` (filter definition) and `_build_rom_query` at `exporter.py:168-204`

`tests/test_exporter.py` constructs `ExportFilters` 14 times but only ever populates `systems=`. Lines `exporter.py:181-194` (region clause and the special `"Other" in filters.regions` branch, plus the `collection_id` subquery branch) are unreachable from the test suite. The `_REGION_OPTIONS` UI tuple at `ui/export_dialog.py:53` includes `"Other"` so production code can reach that branch. Suggested test cases:

- A region filter that excludes a ROM (region="USA" vs region="Japan").
- The `"Other"` special case — assert NULL-region games and games whose region is not in the explicit list both pass.
- A collection filter that excludes ROMs in other collections.

#### 11. `TestM3uGeneration.test_multi_disc_playlist_written` mislabels the system
**Files / lines:** `tests/test_exporter.py:588-620`

The test inserts two `.cue` discs but tags them `system_id="snes"` (line 603) with the comment "snes used so the minimal profile copies the file". This works but produces a misleading setup — the test would equally pass without exercising the multi-disc logic specific to PSX/PCE-CD, and a future change to `_build_minimal_profile` could mask a real regression. Suggested fix: extend `_build_minimal_profile` to map `psx`/`pcenginecd` to a folder, then use the right `system_id`. Cosmetic but improves the test's signal-to-noise ratio.

#### 12. `MainWindow` reaches into `GameTable._selected_game_id`
**Files / lines:** `src/romulus/ui/main_window.py:240, 249`

`MainWindow._on_add_to_collection` and `_on_new_collection` call `self.game_table._selected_game_id()` — a private method. The leak is small (it's the same module's UI layer) but two read sites within a single window class is exactly when a method should be promoted to public. Suggested fix: rename to `selected_game_id` at `game_table.py:319` and drop the underscore, or expose it as a `@property`.

#### 13. Two near-identical byte-size formatters
**Files / lines:**
- `src/romulus/ui/game_table.py:60` — `_format_size`
- `src/romulus/ui/export_dialog.py:56` — `_format_bytes`

Both walk `("B", "KB", "MB", "GB", "TB")` and divide by 1024. `_format_size` special-cases the "B" suffix to show an integer rather than `0.0 B`; `_format_bytes` always shows one decimal. The 0.5-line difference doesn't justify two functions. Suggested fix: pick one, expose it from a shared module (e.g. `ui/_formatting.py` or even `core/_formatting.py` if non-UI ever needs it).

#### 14. `system_sidebar.get_collections` is a compatibility shim that should be deleted
**Files / lines:** `src/romulus/ui/system_sidebar.py:41-49`

The docstring explicitly says "Thin compatibility shim around `queries.get_collections` so the sidebar tests can keep using positional tuples." Test compatibility is a poor reason to leave a wrapper in production code; the test at `tests/test_ui.py:255-267` should be updated to consume `sqlite3.Row` objects (or be deleted in favour of the underlying `queries.get_collections` test in `tests/test_collections.py`). Suggested fix: remove the shim, update one test.

#### 15. Two slightly different progress-callback signatures across workers
**Files / lines:**
- `ScanWorker.progress = Signal(int, str)` — `(count, filename)` — `ui/workers.py:31`
- `EnrichWorker.progress = Signal(int, int, str)` — `(current, total, title)` — `workers.py:86`
- `OrganizeWorker.progress = Signal(int, int, str)` — `(current, total, source)` — `workers.py:158`
- `ExportWorker.progress = Signal(int, int, str)` — `(current, total, filename)` — `workers.py:224`

Scan is the odd one out because the scanner doesn't know the total file count up front (it's discovered while walking). That's a legitimate reason for the signature divergence but it forces `ScanProgressDialog` (`ui/scan_progress.py:9`) to be an indeterminate `QProgressDialog` and forces the dialog code to fork on which kind of worker drove it. Cleaner alternative: have the scanner emit `(count, None, filename)` or do a cheap pre-walk to estimate total. Not urgent; documented as accepted divergence is also fine.

#### 16. `find_alias_merges` picks the canonical target folder by `sorted(canonical_folders)[0]`
**Files / lines:** `src/romulus/core/organizer.py:187`

If a library has *both* `/lib/megadrive` and `/lib/MegaDrive` (different cases on macOS / case-insensitive filesystems), only one will be picked as the merge target and the other will be merged into it — which is correct, but the choice between "merge MegaDrive into megadrive" vs "merge megadrive into MegaDrive" is decided by sort order (`/lib/MegaDrive` sorts before `/lib/megadrive` ASCIIbetically). The user gets no say. Worth a comment explaining the tie-break, or surfacing this as a collision for manual review.

#### 17. `DetailPanel._render` puts inline HTML/CSS in a stylesheet f-string
**Files / lines:** `src/romulus/ui/detail_panel.py:234-238`

```python
self.match_badge.setStyleSheet(
    f"QLabel {{ background-color: {bg}; color: {fg}; "
    "border-radius: 6px; padding: 2px 6px; }}"
)
```

The colours come from `_MATCH_BADGES` (`detail_panel.py:31-36`) — they're constants and Romulus only ships them, so there's no injection vector. Still, mixing string interpolation with stylesheets is fragile (a future "themable" colour from a config field would have to scrub commas/braces). Worth a small wrapper that builds the stylesheet from a typed input rather than two arbitrary strings.

### Low / Nit

#### 18. `Path(__file__).resolve().parents[3]` to find data dir
**Files / lines:** `src/romulus/core/exporter.py:53-55`

```python
BUILTIN_PROFILES_DIR: Path = (
    Path(__file__).resolve().parents[3] / "data" / "profiles"
)
```

`parents[3]` is brittle: any move of `exporter.py` to a different nesting depth silently breaks the constant at import time, and the failure mode is "profile dialog shows zero profiles" rather than a tracebacks. `importlib.resources` would be the correct way to package this, and would also fix the inevitable broken-`data/profiles/`-after-`pip install` problem (the wheel doesn't currently carry `data/` because the package is `src/romulus/` and `data/` is sibling). Worth checking whether `pip install .` produces a working app — if not, this is a real bug rather than a nit.

#### 19. `models/game.py` and `models/rom.py` are tiny — consider folding them
**Files / lines:** `src/romulus/models/game.py` (24 lines), `src/romulus/models/rom.py` (31 lines)

Both are pydantic models with no logic, sitting next to the much larger `models/system.py` (518 lines) and `models/profile.py` (61 lines). One model per file is fine, but they're imported only via `models/__init__.py` and there's no test that imports them individually. Either pattern is defensible; flagging as a low-priority "do you actually want this many one-class modules?" question.

#### 20. Inconsistent import alias for `queries`
**Files / lines:** several

`queries` is imported as `q` in `core/scanner.py`, `core/organizer.py`, `core/dat_parser.py`, `ui/main_window.py`, `ui/detail_panel.py`, `ui/export_dialog.py`, `ui/game_table.py`; as `queries` in `core/hasher.py`, `metadata/__init__.py`, `db/__init__.py`; and tests use both. Either is fine, but the project would benefit from a one-line convention in `CLAUDE.md`. The `q.` form is more common and slightly more readable in long files.

#### 21. `get_dat_by_crc_size` silently returns `None` for ambiguous matches
**Files / lines:** `src/romulus/db/queries.py:345-361`

The docstring says it "Returns None if either argument is missing OR if more than one entry matches — ambiguous CRC32s shouldn't be auto-applied (ROM-DEDUP §5.4)". That's the right policy, but from the caller's perspective (`dat_parser.match_hashes` at `core/dat_parser.py:261-272`) "no match" and "ambiguous match" are indistinguishable. The ambiguous case is a useful diagnostic — surfacing it as a separate return value (or logging) would help users understand why a known-good ROM isn't being marked DAT-verified. Low priority unless users start filing "why isn't my ROM verified?" issues.

#### 22. `_pick_duplicate_keeper` sort tuple isn't documented
**Files / lines:** `src/romulus/core/organizer.py:236-253`

The sort key is `(ext_rank, filename_len, rom_id)`. The docstring at line 240 explains the rationale clearly, but the magic number `1000` at line 247 (sentinel for unranked extensions) and the assumption that "shorter filename" is canonical-ish (Mario.sfc < Mario (USA).sfc) aren't motivated. Minor — readers can infer this from the surrounding code.

#### 23. `app.py` imports `MainWindow` lazily inside `run()`
**Files / lines:** `src/romulus/app.py:60`

```python
def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    conn = initialize_database(DEFAULT_DB_PATH)
    from romulus.ui.main_window import MainWindow  # late import
```

The comment is missing — readers might assume this is dead code or a circular-import workaround. In fact, importing `MainWindow` triggers `QWidget` construction prereqs which require a live `QApplication`, so the late import is intentional. A one-line comment would prevent future "let's clean this up" PRs from breaking the app's startup.

#### 24. `screenscraper.test_connection` ignores the global rate limiter
**Files / lines:** `src/romulus/metadata/screenscraper.py:74-129`

Unlike `lookup_game` (line 161), `test_connection` doesn't call `_respect_rate_limit`. This is probably fine — it's a one-off button click, not a bulk operation — but if a user rage-clicks the button it'll spam the API. Adding the call (or rate-limiting via the UI by disabling the button — which the dialog already does at `ui/settings_dialog.py:142`) would close the gap. UI-side throttle is in place, so this is mostly defense-in-depth.

#### 25. `DetailPanel._best_confidence` shadows `db.queries._CONFIDENCE_RANK` (related to #2)
**Files / lines:** `src/romulus/ui/detail_panel.py:289-298`

Already called out as #2; listing it again here because the static method also computes a per-call dict — moving it out of `_best_confidence`'s body and into a module-level constant (or importing from `db/queries`) would let the JIT cache it. Micro-optimization, mostly cosmetic.

#### 26. `_SANITIZE_CHARS` deduplication note in `libretro.py`
**Files / lines:** `src/romulus/metadata/libretro.py:25-28`

The comment says "After de-duplicating the `\` that appears twice in the spec, the unique set is the 10 chars below." This is helpful, but the constant itself is a string with 10 characters and a reader has to count them to verify. Suggested fix: store as a `frozenset` and assert `len(_SANITIZE_CHARS) == 10` in tests. Small but improves auditability.

#### 27. `parse_filename` silently swallows unknown bracket tags
**Files / lines:** `src/romulus/core/scanner.py:371`

`# Unknown bracket tags are dropped silently.` is correct behaviour for filenames containing weird tags like `[Cracked]` or `[CT-Mod]`, but the asymmetry with "unknown paren tags also dropped" (line 290) and "but unknown DAT tokens log a warning" is not justified anywhere. Either both should log a debug-level event or the silence should be explicit policy. Low impact — these are filenames not user input.

#### 28. `__init__.py` re-exports include some unused symbols
**Files / lines:**
- `src/romulus/core/__init__.py:31-45` — exports every public org-y symbol (`ACTION_COLLISION`, etc.)
- Most consumers (`ui/organize_preview.py:30-37`) import from `romulus.core.organizer` directly rather than `romulus.core`

The package-level re-exports are useful for downstream API users but no internal code uses them. Either remove the re-exports (smaller `__init__`) or rely on them everywhere (consistent style). Pure style choice.

#### 29. `_resolve_system_for_directory` resolves the path under `try`, falls back silently
**Files / lines:** `src/romulus/core/scanner.py:242-247`

```python
try:
    library_root = library_root.resolve()
    current = directory.resolve()
except OSError:
    current = directory
```

If `.resolve()` fails on either side, `library_root` may be the un-resolved original while `current` becomes the un-resolved version of `directory`. The loop termination at line 251 (`if current == library_root or current.parent == current`) then compares a possibly-resolved root against an unresolved directory, which can loop one extra time. Probably harmless because the next iteration's `current.name` lookup will not match anything but it's worth a defensive `library_root = directory.parents[-1]` fallback or moving both resolves out of the same try.

#### 30. Test for `test_failure_on_second_action_does_not_abort_subsequent` uses substring path matching
**Files / lines:** `tests/test_organizer.py:567-577`

```python
def flaky_replace(s, d):
    if "b.sfc" in str(s) or "GameB.sfc" in str(d):
        raise OSError("simulated second-action failure")
    return real_replace(s, d)
```

This works for the current test layout but couples the test to the literal substring `"b.sfc"`. If someone renames the test files to `"sample-b.sfc"`, the test silently stops exercising the rollback path because the `flaky_replace` no longer triggers for action B. Worth tightening to a path equality (`str(s) == str(b_src)`) or capturing the targets explicitly in a closure.

#### 31. Pydantic `id: str` field on `SystemDef` and `DestinationProfile` shadows builtin
**Files / lines:**
- `src/romulus/models/system.py:28`
- `src/romulus/models/profile.py:53`

`id` is a Python builtin but pydantic models use it idiomatically because the field name often matches a database column. This is conventional, but pyright in strict mode warns. Not worth changing; flagged for completeness.

#### 32. `_resolve_cache_dir` falls back to `~/.romulus/covers` in two places
**Files / lines:**
- `src/romulus/db/config.py:21` — `DEFAULT_CONFIG["cover_cache_path"]` default
- `src/romulus/metadata/__init__.py:47` — `_resolve_cache_dir` fallback if nothing configured

Same magic path expressed twice. If the user wipes the config row, the metadata module silently re-creates the directory under the same default — which is correct, but the duplication means a future "move covers to XDG_CACHE_HOME" change has to touch two files. Pull the default into a single constant.

#### 33. `OrganizePreviewDialog` builds a tree from `OrganizePlan` without any feedback if `actions=[]`
**Files / lines:** `src/romulus/ui/organize_preview.py:122`

The summary correctly shows "Library is already organized — no changes needed." and disables the Apply button, but the empty tree view still occupies most of the dialog. A small "All clean, nothing to do" placeholder would be friendlier. Pure UX.

#### 34. `dat_parser._iter_dat_files` traverses both `*.dat` and `*.xml`
**Files / lines:** `src/romulus/core/dat_parser.py:201`

This is correct (some DAT publishers use `.xml`), but the comment doesn't explain why. Future readers may try to "fix" the `.xml` glob by removing it. One-line comment justifying the dual extension.

#### 35. `Hasheous` "MAX_RETRIES" and "BACKOFF_BASE" not on a class
**Files / lines:** `src/romulus/metadata/hasheous.py:19-22`

Module-level constants are fine for a single-file client, but the test patches them via `monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)` at `tests/test_metadata.py:201`. That mutates module state across the whole test session; pytest's fixture-scoped patch should work because each test gets its own `monkeypatch` instance, but it's brittle if the patch order or test parallelisation changes. Lifting these into a `class HasheousConfig` would be cleaner; minor.

## What's done well

- **`core/atomic.py` extraction (Session 10).** This is the single best architectural decision in the codebase. Three writers (organizer, exporter, libretro cover cache) now share one tested implementation of "write to a sibling tempfile, then `os.replace` into place." The cross-filesystem fallback in `atomic_replace` is handled and tested. The remaining direct `os.replace` call in `libretro.fetch_cover` is justified in the Session 11 carry-forward notes — keep it as-is.

- **Per-action `SAVEPOINT` rollback in `execute_plan`.** `src/romulus/core/organizer.py:522-545` wraps each action in its own SAVEPOINT. A failed rename never leaves the DB out of sync with the disk, and `tests/test_organizer.py::TestAtomicAndRollback::test_failure_on_second_action_does_not_abort_subsequent` is a load-bearing test that genuinely exercises the cross-action rollback.

- **Monotonic `match_confidence`.** `src/romulus/db/queries.py:43-89` (the `CASE` expression in `upsert_rom`) and the test at `tests/test_scanner.py::TestRescanPreservesMatchConfidence` make it impossible for a Quick rescan to downgrade a prior Heavy Scan result. This is exactly the kind of invariant that ages well — keep it tested.

- **Worker concurrency guards.** Every menu/toolbar handler in `MainWindow` checks `if self._foo_worker is not None and self._foo_worker.isRunning()` before spawning a fresh worker (`main_window.py:296-302, 340-346, 380-387, 438-445`), and `closeEvent` (`main_window.py:512-531`) cancels every running worker and bounded-waits 5 s. The fake-worker tests at `tests/test_organizer.py:732-783` and `tests/test_ui.py:582-656` exercise both the guard and the cancel path without spinning a real QThread. Good pattern.

- **`hash_rom` and `hash_library` separation.** `hash_rom` is a pure file-level function that takes a header rule and a path, returns a result. `hash_library` is the orchestration layer that decides what to hash (`_rows_needing_hash`), does it in parallel, and writes results back. The split makes both individually testable and the streaming `_digest_stream` keeps memory bounded on large ROMs.

- **Built-in profile coverage tests.** `tests/test_exporter.py::TestBuiltInProfileCoverage` asserts that all six shipped profiles declare an *explicit* `supported: true/false` decision for every system in `SYSTEM_REGISTRY`. This is the right kind of structural test — adding a new system to the registry breaks the test until every profile is updated, which is exactly what you want.

- **HTTP transport injection for testability.** Every metadata client accepts an optional `client: httpx.Client | None` and uses `httpx.MockTransport` in tests. The standalone `test_module_does_not_smuggle_real_network_calls` at `tests/test_metadata.py:759` is paranoid but cheap insurance.

- **Hacks as first-class artifacts at every layer.** `is_hack` propagates through the scanner (`parse_filename.is_hack`), the games table (`games.is_hack` column), and the organizer's duplicate query (`get_duplicate_groups` filters `AND COALESCE(g.is_hack, 0) = 0`). The test at `tests/test_organizer.py::TestFindDuplicates::test_hack_never_deduped_against_original` proves the contract. Don't let this regress.

- **Docstring discipline.** Every public function and class has at least a one-line docstring; many include rationale ("Caller is responsible for committing the surrounding transaction…", "Per-action rollback: each action runs inside its own SAVEPOINT…"). The terse-but-informative style is consistent and easy to grep.

## Recommended follow-ups

### Before tagging v0.1.0 (small, low-risk)
1. **Fix `cursor.lastrowid` type narrowing in `db/queries.py`** (finding #1). Add explicit `assert` or copy `upsert_rom`'s defensive pattern. ~7 functions, ~14 lines.
2. **Deduplicate the match-confidence rank** (finding #2). Import `_CONFIDENCE_RANK` from `db/queries.py` into `ui/detail_panel.py`. ~5-line change.
3. **Delete `system_sidebar.get_collections` compatibility shim** (finding #14). Update one test. ~15-line change.
4. **Delete `organizer._atomic_replace` wrapper** (finding #6). 5-line change, no behaviour delta.
5. **Verify `importlib.resources` packaging for `data/profiles/`** (finding #18). Either confirm `pip install .` ships the YAML files via the existing `parents[3]` path or move to `importlib.resources` before users hit the issue.

### Before tagging v0.2.0 (medium effort, high value)
6. **Refactor `ui/workers.py` to a `_DbWorker` base class** (finding #3). Removes ~120 lines of duplication and four redundant exception classes. Touches every worker test.
7. **Extract shared helpers** (findings #4, #5, #8, #13, #32). Pull N64 byteswap helpers + magic constants into `core/_n64.py`; pull `_REVISION_RE` and region tokens into `core/_no_intro_tokens.py`; pull `_http_client` context manager into `metadata/__init__.py`; consolidate the byte-size formatter; centralize cover-cache default path.
8. **Convert `execute_plan` summary to a dataclass** (finding #7). Mirrors `ExportSummary`.
9. **Close the test coverage gaps in `ExportFilters`** (finding #10). Add region-filter and collection-filter tests (the latter would also exercise the `INTERSECT` semantics with the collection_games table).
10. **Promote `GameTable._selected_game_id` to public API** (finding #12).

### Longer-term (v0.3.0+, design-level)
11. **Reconsider the four-worker progress signature divergence** (finding #15). Either standardize on `(current, total | None, label)` or accept the current state and document it in `CLAUDE.md`.
12. **Surface ambiguous DAT matches as a separate signal** (finding #21) — useful for users debugging "why isn't my ROM verified?" support questions.
13. **Decide whether `models/game.py` and `models/rom.py` justify their own files** (finding #19). Folding them into `models/__init__.py` or a single `models/_basic.py` reduces module sprawl; keeping them is defensible if pydantic models are expected to grow.
