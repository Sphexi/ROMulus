# Python Craft Review — Romulus v0.1.0 RC

**Reviewer:** python-development:python-pro agent
**HEAD reviewed:** 8b903ff
**Date:** 2026-05-14
**Scope:** all .py under `src/romulus/` and `tests/`. Pure Python-craft lens — sibling reviews `code-quality-v0.1.0.md` and `security-v0.1.0.md` cover general engineering and security respectively. Findings the other two reviewers already flagged (worker duplication, lastrowid typing, match-rank duplication, etc.) are deliberately omitted here unless I have a Python-language angle to add.

## Executive summary

Romulus is **mostly-idiomatic-with-some-gaps**. The codebase reads like one written by an experienced Python developer who consistently picks the safe, readable form: every public callable carries a docstring, type hints are present on every signature, every long-running operation streams (`_digest_stream`, `iterparse`), every filesystem mutation is atomic, every SQL query uses `?` placeholders, frozen dataclasses are used for value objects (`HashResult`, `LaunchBoxEntry`, `DatEntry`), `match` statements appear in exactly the right places (`normalize_rom_content`, `extract_header_title`, `execute_plan`'s action dispatch), and the `frozenset` literals for the side-file / region / status token sets are textbook. Modern 3.10+/3.12+ syntax (`X | None`, walrus, structural pattern matching) is used pragmatically rather than performatively.

The gaps are concentrated in three areas. First, the **boundary types** are looser than they should be: `dict[str, Any]` is the lingua franca between the scanner, the DB layer, and the metadata clients even though every one of those dicts has a known, fixed shape that a `TypedDict` or pydantic model would catch. The `execute_plan` summary is the most visible offender (already flagged by the code-quality reviewer as #7) but the same pattern appears in `upsert_rom`, `upsert_game`, `insert_scan_history`, `update_scan_history`, `_fetch_metadata_for_game`, and `enrich_library`'s return value. Second, **stdlib opportunities** are missed: `collections.defaultdict(list)` would replace 10 `setdefault(k, []).append(v)` sites; `collections.Counter` would replace `OrganizePlan.counts_by_kind`'s hand-rolled `get(k, 0) + 1`; `functools.cached_property` would fit `DetailPanel._favorites_id`; `bytes.startswith((m1, m2, m3))` would replace the four `magic_slice.startswith` chains in `identifier._extract_md_title`. Third, **Pydantic v2 features are present but underused** — every model uses v2 BaseModel and `Field(default_factory=...)`, but there are no `@field_validator`s anywhere (so the path-traversal hole in `DestinationProfile` flagged by the security reviewer is a place where validators would have helped), no `model_config = ConfigDict(...)` for any model, and no `@computed_field` (the one place where it would shine, `SystemMapping.is_supported`, is a plain `@property`).

The biggest **Python-language strengths** worth preserving are: (1) consistent use of `frozen=True` dataclasses for results that cross module boundaries (`HashResult`, `DatEntry`, `LaunchBoxEntry`); (2) `httpx.MockTransport` as the universal HTTP test seam, with the paranoid `test_module_does_not_smuggle_real_network_calls` guard; (3) the `match`/`case _` exhaustive dispatch in `normalize_rom_content` and `extract_header_title` — these are the most readable form for the kind of "system-id → handler" routing the rom layer needs; (4) consistent use of `frozenset` for immutable lookup sets at module level. Don't let any of these regress.

## Findings

### High

#### 1. `dict[str, Any]` boundaries hide fixed-shape contracts (high — type expressiveness)
**Files / lines:**
- `src/romulus/db/queries.py:31, 113, 182, 210, 395` — `upsert_rom`, `upsert_game`, `insert_scan_history`, `update_scan_history`, `upsert_metadata` all take `dict[str, Any]` arguments with a well-defined set of required + optional keys, documented in the docstring rather than the type.
- `src/romulus/metadata/hasheous.py:49, 85` — `parse_hasheous_response` and `lookup_by_hash` produce/return `dict[str, Any]` with the same 8 fixed keys.
- `src/romulus/metadata/screenscraper.py:42, 138` — `parse_screenscraper_response` likewise.
- `src/romulus/metadata/launchbox.py:138` — `entry_to_metadata` returns `dict[str, str | None]` with 7 fixed keys.
- `src/romulus/metadata/__init__.py` — the entire `enrich_library` orchestrator passes these around and returns `dict[str, int]` with three fixed keys.

These are all `TypedDict` candidates. The `MetadataPayload` shape (`title, description, genre, developer, publisher, release_date, players, rating`) appears in **five** modules — `hasheous`, `launchbox`, `screenscraper`, `metadata/__init__`, and the DB layer's `_METADATA_FIELDS` tuple. The right Python-craft move is one `TypedDict` shared across all of them:

```python
# src/romulus/metadata/_types.py
from typing import TypedDict

class MetadataPayload(TypedDict, total=False):
    title: str | None
    description: str | None
    genre: str | None
    developer: str | None
    publisher: str | None
    release_date: str | None
    players: str | None
    rating: str | None
```

Same treatment fits `RomUpsertData`, `GameUpsertData`, and `ScanHistoryData` in `db/queries.py`. Once mypy/pyright is wired up (it isn't yet), these would catch the `cursor.lastrowid` `None` problem at the call site without any runtime change. Worth doing before adding any type checker, otherwise every `dict[str, Any]` is invisible to it.

#### 2. `launchbox_index: dict | None` and `LaunchBox index` typed as bare `dict` (high — type precision)
**Files / lines:**
- `src/romulus/metadata/__init__.py:73, 166` — `launchbox_index: dict | None` (no key/value types)
- `src/romulus/metadata/launchbox.py:116-118, 121` — `build_index` returns `dict[tuple[str | None, str], LaunchBoxEntry]` (correct, well-typed); the caller in `metadata/__init__.py` strips it back to bare `dict`

`dict` without parameters is `dict[Any, Any]` to a type checker. The full type from `build_index` is already there — drop it through. Two-line fix:

```python
# in metadata/__init__.py
from romulus.metadata.launchbox import LaunchBoxEntry

LaunchBoxIndex = dict[tuple[str | None, str], LaunchBoxEntry]

def _fetch_metadata_for_game(..., launchbox_index: LaunchBoxIndex | None, ...) -> bool: ...
```

The PEP 695 `type LaunchBoxIndex = ...` alias works too and reads cleaner.

#### 3. `setdefault(k, []).append(v)` repeated 10× where `defaultdict(list)` is canonical (high — idiom)
**Files / lines:**
- `src/romulus/core/scanner.py:496` — `groups.setdefault(fuzzy, []).append((rom_id, filename))`
- `src/romulus/core/organizer.py:171, 265, 309, 363` — alias-folder grouping, duplicate grouping, cross-ext grouping, collision target grouping
- `src/romulus/core/exporter.py:265, 344, 354, 479` — `folder_tree`, `by_system` (twice), m3u `groups`
- `src/romulus/ui/organize_preview.py:145` — `by_kind` action grouping

The `setdefault(k, []).append(v)` form is legal and correct, but `collections.defaultdict(list)` is the canonical idiom for "group items by key into a list", documented in the stdlib for exactly this case. The setdefault form does an extra dict lookup *and* allocates a fresh empty list every call to `setdefault` (it's discarded if the key already exists). Surgical example, `organizer.py:262-266`:

```python
# before
groups: dict[str, list[sqlite3.Row]] = {}
for row in rows:
    groups.setdefault(str(row["sha1"]), []).append(row)

# after
from collections import defaultdict
groups: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
for row in rows:
    groups[str(row["sha1"])].append(row)
```

Counter argument: `setdefault` keeps the result as a plain `dict` (no `defaultdict` factory bleeding through to downstream code). If that's a real concern, `dict(groups)` at the end of the build is one extra line. Not a correctness issue — pure idiom — but it's the single most reproducible "C-flavored Python" pattern in the codebase.

#### 4. `OrganizePlan.counts_by_kind` re-implements `collections.Counter` (high — stdlib opportunity)
**Files / lines:** `src/romulus/core/organizer.py:110-115`

```python
def counts_by_kind(self) -> dict[str, int]:
    out: dict[str, int] = {}
    for action in self.actions:
        out[action.kind] = out.get(action.kind, 0) + 1
    return out
```

This is literally what `collections.Counter` does:

```python
from collections import Counter

def counts_by_kind(self) -> dict[str, int]:
    return dict(Counter(a.kind for a in self.actions))
```

(Or just return `Counter[str]` directly — it's a `dict` subclass so existing call sites at `organize_preview.py:117-121` keep working.)

#### 5. `dict.update` mistake waiting to happen in `parse_hasheous_response` (medium-high — pythonicity)
**Files / lines:** `src/romulus/metadata/hasheous.py:49-77`

The `_first(*keys)` helper walks a fixed list of synonym keys looking for the first non-empty value. The function then hand-constructs a literal `dict` with eight `_first(...)` calls. This is fine, but it's also a textbook case for a small declarative table:

```python
_FIELD_SYNONYMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title",        ("title", "name")),
    ("description",  ("description", "summary", "overview")),
    ("genre",        ("genre", "genres")),
    ("developer",    ("developer", "developers")),
    ("publisher",    ("publisher", "publishers")),
    ("release_date", ("release_date", "first_release_date", "released")),
    ("players",      ("players", "max_players")),
    ("rating",       ("rating", "esrb")),
)

def parse_hasheous_response(payload: dict[str, Any]) -> MetadataPayload:
    game = _unwrap(payload)  # collapses payload['game']/['data']/['result']
    return {key: _first(game, *syns) for key, syns in _FIELD_SYNONYMS}
```

Same pattern fits `parse_screenscraper_response`'s 8 calls to `_localized(game.get(...))`. The table form makes the "alias list per metadata field" relationship explicit and 1-edit-friendly when a new synonym appears.

### Medium

#### 6. Pydantic v2 features unused — no `@field_validator`, no `ConfigDict`, no `@computed_field` (medium — Pydantic specifically)
**Files / lines:** `src/romulus/models/system.py`, `src/romulus/models/profile.py`, `src/romulus/models/rom.py`, `src/romulus/models/game.py`

Every model uses `BaseModel` and `Field(default_factory=...)` (v2 idioms, correct). But:

- **`@field_validator`** is missing everywhere. The security reviewer's #1 finding (profile path traversal via `base_path` / `folder`) is exactly what a `@field_validator` is for. Same for `SystemDef.extensions` (should reject extensions missing a leading dot — the test `test_registry_extensions_have_leading_dot` enforces this externally instead of letting Pydantic enforce it on construction) and `SystemDef.header_rule` (should be `Literal["smc_512", "ines_16", "n64_byteswap", "lynx_64"] | None` instead of free `str | None`).
- **`ConfigDict`** never appears. Defaults are fine here, but if anyone wants `frozen=True` for `SystemDef` (the registry is intended to be immutable post-construction — `SYSTEM_REGISTRY` is exposed as a `list[SystemDef]` and any caller can mutate `.folder_aliases.append(...)`), `model_config = ConfigDict(frozen=True)` is the v2 way.
- **`@computed_field`** is the v2 replacement for "field whose value is derived from others, but should show up in `.model_dump()`". `SystemMapping.is_supported` (`profile.py:34-37`) is the textbook case — currently a `@property`, which means `model_dump()` does NOT include it. If the value is meant to be part of the model's surface (and the JSON tests at `tests/test_models.py` suggest it is), `@computed_field` is correct.

Recommended minimum (no behaviour change at runtime):

```python
from typing import Literal
from pydantic import Field, field_validator

HeaderRule = Literal["smc_512", "ines_16", "n64_byteswap", "lynx_64"]

class SystemDef(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9]+$")
    ...
    header_rule: HeaderRule | None = None

    @field_validator("extensions", mode="after")
    @classmethod
    def _ensure_dot_prefix(cls, v: list[str]) -> list[str]:
        if any(not e.startswith(".") for e in v):
            raise ValueError("extensions must include a leading dot")
        return [e.lower() for e in v]
```

#### 7. `httpx.Client | None` + `owns_client` boilerplate is a context-manager case (medium — resource discipline)
**Files / lines:** `src/romulus/metadata/libretro.py:78-88`; `src/romulus/metadata/hasheous.py:93-133`; `src/romulus/metadata/screenscraper.py:101-129, 156-182`

The code-quality reviewer already flagged this as #8 with a `@contextmanager` suggestion. The Python-craft angle worth adding: the pattern is **dangerous** if anyone ever puts an early `return` inside the `try` *without* a `finally`. `libretro.fetch_cover:81-88` only has the `client = client.close()` cleanup inside `finally`; the closer the cleanup gets to the `return`, the more chance of a future contributor accidentally introducing a leak. `contextlib.ExitStack` or a small dedicated context manager is the safest form. Also: when `client` IS provided by the caller, we currently never call `close()` on it (correct — the caller owns it), but there's no test that verifies a caller-supplied mock client is NOT closed. Cheap test to add when factoring out the helper.

#### 8. `DetailPanel._best_confidence` reconstructs the rank dict on every call (medium — pythonicity)
**Files / lines:** `src/romulus/ui/detail_panel.py:289-298`

```python
@staticmethod
def _best_confidence(roms: list[sqlite3.Row]) -> str:
    rank = {"unmatched": 0, "fuzzy": 1, "header": 2, "dat_verified": 3}
    best = "unmatched"
    for rom in roms:
        conf = rom["match_confidence"] or "unmatched"
        if rank.get(conf, 0) > rank.get(best, 0):
            best = conf
    return best
```

Already flagged at the duplication level by code-quality #2/#25. Python-craft angle: the loop body is literally `max(roms, key=...)`. With the rank table imported (or as a module-level constant alongside `_MATCH_BADGES`):

```python
_CONFIDENCE_RANK = {"unmatched": 0, "fuzzy": 1, "header": 2, "dat_verified": 3}

@staticmethod
def _best_confidence(roms: list[sqlite3.Row]) -> str:
    if not roms:
        return "unmatched"
    return max(
        (rom["match_confidence"] or "unmatched" for rom in roms),
        key=lambda c: _CONFIDENCE_RANK.get(c, 0),
    )
```

One expression. Reads top-down. Same complexity. The hand-rolled "track current best" loop is C-flavored.

#### 9. `_iter_dat_files` uses `+ sorted(...)` for what `itertools.chain` does (medium — stdlib)
**Files / lines:** `src/romulus/core/dat_parser.py:194-212`

```python
files = sorted(p.rglob("*.dat")) + sorted(p.rglob("*.xml"))
```

This materializes both lists, concatenates them, then iterates. `itertools.chain(sorted(p.rglob("*.dat")), sorted(p.rglob("*.xml")))` does the same thing without the intermediate concatenation. Minor — both are fine — but `chain` makes the intent ("walk both globs in order") explicit. More importantly, the function then deduplicates by `f.resolve()` into a list, but uses a manual `seen: set[Path]` and a separate `found: list[Path]`. That's the canonical case for `dict.fromkeys` (which preserves order, deduplicates, and is one line):

```python
def _iter_dat_files(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    def _expand(p: Path) -> Iterable[Path]:
        if p.is_dir():
            return chain(sorted(p.rglob("*.dat")), sorted(p.rglob("*.xml")))
        if p.is_file():
            return [p]
        return []
    candidates = chain.from_iterable(_expand(Path(p)) for p in paths)
    return list({f.resolve(): f for f in candidates}.values())
```

(Or use the new `dict.fromkeys` over resolved paths — the dict-comprehension form keeps the original `Path` rather than the resolved one, which matches current behaviour.)

#### 10. `_extract_md_title` has a four-way `startswith` chain that `bytes.startswith(tuple)` handles natively (medium — pythonicity)
**Files / lines:** `src/romulus/core/identifier.py:121-128`

```python
if not (
    magic_slice.startswith(_MD_MAGIC_OVERSEAS)
    or magic_slice.startswith(_MD_MAGIC_DOMESTIC)
    or magic_slice.startswith(_MD_MAGIC_32X)
    or magic_slice.startswith(_MD_MAGIC_PICO)
):
    return None
```

`bytes.startswith` accepts a tuple of prefixes natively:

```python
_MD_MAGICS = (_MD_MAGIC_OVERSEAS, _MD_MAGIC_DOMESTIC, _MD_MAGIC_32X, _MD_MAGIC_PICO)
if not magic_slice.startswith(_MD_MAGICS):
    return None
```

Same applies to the N64 magic detection in `_normalize_n64_to_z64` (`identifier.py:75-84`) if it ever grows past three forms.

#### 11. `_clean_ascii_title` builds a list-comp then `"".join`s — `bytes.translate` is the stdlib equivalent (medium — stdlib opportunity, performance)
**Files / lines:** `src/romulus/core/identifier.py:35-43`

```python
cleaned_chars = [c for c in text if c == " " or (c.isprintable() and ord(c) >= 0x20)]
cleaned = "".join(cleaned_chars).strip()
cleaned = " ".join(cleaned.split())
```

For the ASCII filter that's already in the function, `bytes.translate(None, delete=bytes(range(0x20)) + bytes(range(0x7F, 0x100)))` does the same work at C speed on the raw bytes — relevant because this runs once per ROM in `extract_header_title`. The current form is correct and readable; flagging because if hashing-time profiling ever surfaces this, the answer is at the stdlib level.

Less invasive idiom-improvement: `c.isprintable()` already implies `ord(c) >= 0x20` for ASCII (and is False for newlines/tabs/etc.) — the second clause is redundant. The whole comprehension collapses to `[c for c in text if c.isprintable() or c == " "]`. `c == " "` is needed because `" ".isprintable()` is True, so actually the `or c == " "` is also redundant. The whole comprehension is just `text.translate(...)` or `"".join(c for c in text if c.isprintable())`.

#### 12. `_pick_duplicate_keeper`'s magic `1000` sentinel reads as a smell (medium — readability)
**Files / lines:** `src/romulus/core/organizer.py:236-253`, also `find_cross_extension_dupes` at `:317-332`

```python
ext_rank = _EXTENSION_PREFERENCE.get(ext, 1000)
```

`1000` appears four times across the file as "sort to the end" sentinel. Python idiom for "sort unknown last": `float("inf")` or `sys.maxsize`. Both make the intent self-documenting. `1000` requires the reader to verify no real rank can exceed it (true today, but fragile). One-line fix at module level:

```python
_UNRANKED_SORT_KEY: int = sys.maxsize
```

Then every `1000` becomes `_UNRANKED_SORT_KEY`.

#### 13. `Path(__file__).resolve().parents[3] / "data" / "profiles"` is brittle (medium — packaging idiom)
**Files / lines:** `src/romulus/core/exporter.py:53-55`

Code-quality flagged this as #18 from a packaging-correctness angle. Python-craft angle: `importlib.resources` (stdlib since 3.7, refined in 3.9) is the *idiomatic* way to find packaged data and it doesn't require any directory math:

```python
from importlib.resources import files

BUILTIN_PROFILES_DIR: Path = Path(str(files("romulus").joinpath("../../data/profiles")))
```

…except that won't work because `data/` is a sibling of the package, not inside it. The right Python-craft move is to **move the YAMLs into the package** (`src/romulus/data/profiles/`) and use `files("romulus.data.profiles")` directly. That eliminates `parents[3]`, makes `pip install` work without any `MANIFEST.in` ceremony, and gives `tests/` an import path it can call directly. It's a packaging change, not just a code change — but it's the only stable Python-craft answer here.

#### 14. `_text` and `_normalize_title` in `launchbox.py` are good — `_text` should support a default (medium — API consistency)
**Files / lines:** `src/romulus/metadata/launchbox.py:68-74`

```python
def _text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None
```

This is the right shape. Nit: every call site at `:93-110` does `_text(element, "X")` and uses the result with `or None` already baked in. The function does its job. **However**: there's an asymmetry — `_normalize_title` (`:77-79`) takes a bare `str` and returns `str`, while `_text` returns `str | None`. The downstream call `_normalize_title(title)` at `:131` requires `title: str` (non-None), and the upstream filter at `:94-96` ensures `title` is non-None — but the type system sees `title` as plain `str` there. This is fine for now but the kind of place where a `tags: TypedDict` would catch a future refactor.

#### 15. `_classify_paren_tag`'s 8-way return is a strong `match` candidate (medium — Python 3.10+ feature usage)
**Files / lines:** `src/romulus/core/scanner.py:261-290`

The function returns `(kind, value)` where `kind` is one of nine string literals dispatched by a series of `if/elif` checks. The dispatch is exactly the kind of thing 3.10's `match` was added for, except the conditions involve regex matches and set membership rather than literal equality. A `match` here would be more performative than helpful — the if-chain is actually clearer for the mixed-predicate case. Flagging because it's tempting to "modernize" but **don't**; the current form is the right Python-craft answer. Adding it as a positive contrast to finding #16 below.

#### 16. `parse_filename` has a `for match in _TAG_GROUP_RE.finditer(...)` with a 30-branch if-chain — extracting per-status handlers would help (medium — function length)
**Files / lines:** `src/romulus/core/scanner.py:324-423` (100 lines)

`parse_filename` does too much in one function:
1. Splits stem/extension
2. Loops over every parenthesized + bracketed tag group
3. Per-group: classifies it AND mutates eight local boolean flags AND appends to `status`
4. After the loop, strips tag groups again
5. Trims separators
6. Picks a `display_title`

The body is correct and well-tested, but a single function with 14 local variables and a 30-line if/elif chain inside a `for` loop is the canonical "consider extracting helpers" smell. Two small handler dicts would do it:

```python
_PAREN_STATUS_HANDLERS: dict[str, str] = {
    "prototype": "prototype",
    "beta": "beta",
    "demo": "demo",
    "sample": "sample",
    "unlicensed": "unlicensed",
    "homebrew": "homebrew",
}

_BRACKET_STATUS_HANDLERS: dict[str, str] = {
    "!": "verified",
    # _BAD_DUMP_RE etc. dispatched separately
}
```

…then `parse_filename` becomes a dispatch-and-collect that's mechanically obvious. Not urgent — current form has 100% test coverage and parses correctly — but the file will grow when new TOSEC tokens get added.

#### 17. `ParsedFilename` is a regular `@dataclass` (mutable) but is never mutated post-construction (medium — immutability)
**Files / lines:** `src/romulus/core/scanner.py:169-193`

```python
@dataclass
class ParsedFilename:
    ...
```

Compare to `HashResult` (`hasher.py:33`: `@dataclass(frozen=True)`), `DatEntry` (`dat_parser.py:56`: `frozen=True`), `LaunchBoxEntry` (`launchbox.py:53`: `frozen=True`). `ParsedFilename` is constructed once at the end of `parse_filename` and never mutated. Inconsistent. Same applies to `ScanResult` (`scanner.py:532`), `OrganizeAction` (`organizer.py:77`), `OrganizePlan` (`organizer.py:104`), `ExportFilters` / `ExportOptions` / `ExportPreview` / `ExportSummary` (`exporter.py:67-112`).

`OrganizeAction` actually *is* mutated by `execute_plan` (`organizer.py:518-541`: `action.executed = ...`, `action.error = ...`), so it must stay mutable. The rest are all candidates for `frozen=True`. `slots=True` is a free win for the immutable ones (3.10+, smaller per-instance memory).

#### 18. `Iterable[X]` vs `list[X]` for read-only parameters (medium — type precision)
**Files / lines:** several

- `src/romulus/core/dat_parser.py:194` — `_iter_dat_files(paths: Iterable[str | os.PathLike[str]])` — correct, good example
- `src/romulus/core/dat_parser.py:217` — `load_all_dats(..., dat_paths: Iterable[...])` — correct
- `src/romulus/core/organizer.py:237, 256, 285, 311, 347, 357, 488` — every detector takes `list[sqlite3.Row]` or `list[OrganizeAction]`. Most only iterate. Most could be `Iterable[X]` or (better) `Sequence[X]` (you index into the list in a couple of places).
- `src/romulus/core/exporter.py:400, 466, 502` — `rows: list[sqlite3.Row]` for `generate_gamelist_xml`, `generate_m3u_playlists`, `copy_artwork` — these all iterate only. `Iterable[sqlite3.Row]` is correct.
- `src/romulus/ui/game_table.py:70, 305` — `game_ids: list[int] | None`, `rows: list[GameRow]` — same.

The annotation `list[X]` says "I want a mutable list and reserve the right to mutate it"; `Iterable[X]` says "I only need to iterate". Hand a generator to a `list[X]` parameter and a type checker complains. This is what the code-quality reviewer touched on at the high level but the per-file fix list is here.

### Low / Nit

#### 19. `from __future__ import annotations` everywhere — keep or drop, but be deliberate (low — style consistency)
**Files / lines:** every file under `src/romulus/`

Project targets py312 and uses `X | None` syntax throughout. On py312, `from __future__ import annotations` is no longer needed for those — runtime `X | None` works natively. The only thing the future-import buys on 3.12 is "all annotations become strings, never evaluated at runtime", which can matter for `Pydantic` (and `pydantic` v2 handles both fine).

Two valid stances: (a) keep it everywhere as "future-proof in case we ever target 3.10-", or (b) drop it everywhere since the floor is 3.12. The current state is "keep it everywhere", which is fine — flagging because if anyone runs `ruff --select FA` they'll see `FA100`/`FA102` suggestions and should decide once.

#### 20. `dict()` and `list()` returns when callers expect `Mapping` / `Sequence` (low — type narrowing)
**Files / lines:**
- `src/romulus/db/queries.py:99-105` `get_roms_by_system`: `rows = conn.execute(...).fetchall(); return list(rows)` — `fetchall()` already returns a list. The `list(...)` wrap is a no-op.
- Same pattern at `:274-285, 288-299, 451-456, 510-514, 599-607, 617-627, 663-684, 695-715, 725-736`.

Either the `list(...)` is needed (sqlite3 row iterators are list-typed but conceptually iterators) or it isn't. If it's defensive, a one-line comment. If it isn't, drop the wrap. Pure micro-clean — but 12 call sites doing the same thing is a "decide once" moment.

#### 21. `match_filter == "Verified": return row.match_confidence in {"dat_verified", "header"}` is great — but the set is allocated per-call (low — perf nit)
**Files / lines:** `src/romulus/ui/game_table.py:234-237`

```python
if self._match_filter == "Verified":
    return row.match_confidence in {"dat_verified", "header"}
if self._match_filter == "Unmatched":
    return row.match_confidence in {"unmatched", "fuzzy"}
```

Sets are constructed every call to `filterAcceptsRow`, which Qt invokes once per row times once per filter change. Hoist to module-level `frozenset` constants and the comparison is unchanged but allocation-free. Two extra lines, no behavior change.

#### 22. `_field` static method on `DetailPanel` returns `str` — could be `LiteralString` or a small helper (low — nit)
**Files / lines:** `src/romulus/ui/detail_panel.py:283-287`

```python
@staticmethod
def _field(label: str, value: object) -> str:
    if value is None or value == "":
        return ""
    return f"{label}: {value}"
```

`value: object` is too loose — every caller passes `str | int | None` from a `sqlite3.Row`. `value: str | int | None` would be more honest. Also: this is a candidate for a `match` since "value is empty" covers `None`, `""`, `0`, `[]` (which aren't expected here but the current `is None or == ""` chain is brittle to a value of `0` or `False` slipping through — those would correctly fail the equality check, but the chain is the kind of code that grows a third condition next session).

#### 23. `_format_size`'s "B vs decimal" special case is a one-off (low — duplication)
**Files / lines:** `src/romulus/ui/game_table.py:60-67`

Code-quality finding #13 already covered the cross-file duplication. Python-craft angle: even within this function, the special case for `"B"` makes the loop body non-uniform:

```python
return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
```

The intent is "no decimal for bytes". One cleaner form is to early-return for the byte case:

```python
def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"  # unreachable, satisfies type checker
```

Nit.

#### 24. `conn.execute(...).fetchone()` then `if row is not None: return row[0] else None` pattern (low — pythonicity)
**Files / lines:** `src/romulus/db/queries.py:155-174, 333-342, 494-498, 501-505, 630-636`

The shape is consistent — good. One spot that could use the walrus:

```python
# at queries.py:333-342
def get_dat_by_sha1(conn, sha1):
    if not sha1:
        return None
    return conn.execute(
        "SELECT * FROM dat_entries WHERE sha1 = ? LIMIT 1",
        (sha1.lower(),),
    ).fetchone()
```

…is the right shape. But `find_game_id_for_fuzzy_key` (`:155-174`) has:

```python
row = conn.execute(...).fetchone()
return row[0] if row else None
```

`row[0] if row else None` is the right idiom for `sqlite3.Row` (Row is falsy when None). Consistent across files. Walrus wouldn't help here — flagging only because the `:if row is not None` form appears in `:494-498`/`:501-505`/`:630-636` (returning the row directly) while `:155-174` uses `if row` (returning a column). Mixed predicates for the same null check. Cosmetic.

#### 25. `_HEADER_READ_BYTES: int = 64 * 1024` — `_HEADER_READ_BYTES = 64 * 1024` is sufficient (low — type hint over-specification)
**Files / lines:** `src/romulus/core/identifier.py:24`; same pattern at `hasher.py:30` and many other module-level constants

Every module-level int/bytes constant has a type annotation. Annotations on module-level constants are useful when the inferred type would be wrong (`x: int = some_function()` where `some_function` returns `Any`) or for `Final`/`ClassVar` semantics. For `_HEADER_READ_BYTES: int = 64 * 1024`, the type is obvious and the annotation is noise. Same for `_SNES_TITLE_LEN: int = 21`, `_INES_MAGIC: bytes = b"NES\x1a"`, `_CHUNK: int = 1 << 20`, etc.

If the project wants module-level constants to be `typing.Final`, that's a different discussion — `from typing import Final; _CHUNK: Final = 1 << 20`. Otherwise, drop the annotations. Style choice.

#### 26. `time.time()` for `hashed_at` while `started_at`/`finished_at` use ISO-8601 strings (low — consistency)
**Files / lines:** `src/romulus/db/queries.py:263` (`hashes.hashed_at` = `time.time()`); `src/romulus/db/queries.py:790-794` (`organize_plans.created_at` = ISO-8601 string); `src/romulus/db/queries.py:200` (`scan_history.started_at` = ISO-8601 string from caller)

Three different timestamp conventions across three tables. None of this is wrong (epoch float compares directly with the `mtime` REAL column, ISO-8601 sorts alphabetically), but the mix is invisible to anyone reading just one of the three. A one-line comment in each call site documenting why "this one is epoch / this one is ISO" would help. Also: `datetime.now(UTC).isoformat()` for the ISO case is correct, but `datetime.now(UTC).timestamp()` for the epoch case would be more consistent than `time.time()` (both are UTC, both come from the same module).

#### 27. `set_config` always commits — `seed_defaults` calls `INSERT OR IGNORE` in a loop without batching (low — sqlite pattern)
**Files / lines:** `src/romulus/db/config.py:37-44, 59-68`

```python
def set_config(...):
    conn.execute(...)
    conn.commit()  # commits every single call

def seed_defaults(conn):
    for key, value in DEFAULT_CONFIG.items():
        cursor.execute(...)
    conn.commit()
```

`seed_defaults` correctly batches the commit. `set_config` doesn't, which is fine for a 10-key config table touched at human speed. Flagging because a future "import settings from file" feature that loops over `set_config` would hit the WAL flush per call. Tiny perf concern; readability concern is that the project has two slightly different transaction-discipline conventions (`upsert_rom` doesn't commit; `set_config` commits; `add_game_to_collection` commits; `update_rom_path` doesn't commit). Each docstring says which, so it's manageable, but the inconsistency makes "who's responsible for the transaction boundary?" hard to skim.

#### 28. `cursor: sqlite3.Cursor = conn.cursor()` then `cursor.execute(...)` then `cursor.rowcount` — `conn.execute(...).rowcount` works (low — sqlite idiom)
**Files / lines:** `src/romulus/db/queries.py:469-493` (`seed_systems`), `src/romulus/db/config.py:59-68` (`seed_defaults`)

`conn.execute` returns a cursor; you can chain off the return value. Only `seed_systems` and `seed_defaults` use the explicit-cursor form, and only because they accumulate `cursor.rowcount` across iterations. The explicit form is fine; flagging because the rest of `queries.py` uses the inline form, and consistency aids skim-reading.

#### 29. `__all__` in `core/__init__.py` is 50+ symbols; `metadata/__init__.py`'s `__all__` is 5 (low — public API surface)
**Files / lines:** `src/romulus/core/__init__.py:59-105`; `src/romulus/metadata/__init__.py:218-224`

Code-quality flagged this at #28. Python-craft angle: `core/__init__.py`'s re-exports include every symbol the package needs to expose. Future-proof, but invites import-from-the-top-of-the-package use which then makes `ui/main_window.py` look like it's pulling from `romulus.core` and `romulus.core.organizer` interchangeably (it is). Decide one: either `from romulus.core import OrganizePlan` is the canonical form (rebuild every UI module's imports), or `from romulus.core.organizer import OrganizePlan` is (delete most of `__all__`). Currently both work; both appear.

#### 30. Tests use `monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)` to defeat rate limiting (low — testability)
**Files / lines:** `tests/test_metadata.py:201, 233, 405, 619, 672, 709, 738`

Seven test paths monkey-patch module-level constants. This works, but the brittleness is that any code that captures `MIN_REQUEST_INTERVAL` *at import time* (none does today, but the next refactor might do `MIN_REQUEST_INTERVAL = MIN_REQUEST_INTERVAL` somewhere) would break the patch. The robust Python-craft form is to make `_respect_rate_limit` take its interval as a parameter (or read it from a `RateLimiter` instance), tested by direct injection rather than monkey-patching:

```python
class _RateLimiter:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._last = 0.0
    def wait(self) -> None: ...
```

Already mentioned at code-quality #9 from a "duplication" angle. The testability angle is independent and stronger.

#### 31. `tests/conftest.py:18-32` — `db` fixture replicates `get_connection` instead of calling it (low — DRY)
**Files / lines:** `tests/conftest.py:18-32`

```python
@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    create_tables(conn)
    yield conn
    conn.close()
```

This is structurally identical to `get_connection` from `romulus.db.connection`. The intent (per the docstring) is to skip the `_restrict_db_permissions` chmod step in tests. That's a one-condition difference — `_restrict_db_permissions` already no-ops on Windows and suppresses OSError on read-only filesystems, so calling `get_connection(db_path)` directly should be safe in tests. Reusing it would automatically pick up future connection-setup changes (e.g. PRAGMA additions). Two-line fixture instead of seven.

#### 32. `tests/test_organizer.py:514` and similar — `def _always_raise(*_a, **_kw) -> None:` (low — test idiom)
**Files / lines:** `tests/test_organizer.py:514-515, 569-575`; `tests/test_metadata.py:144-145, 719-720`

The pattern of "monkeypatch a function with a stub that ignores args and raises" appears 4× across tests. Each writes its own anonymous stub. The pytest idiom for this is a tiny fixture in `conftest.py`:

```python
@pytest.fixture
def always_raise():
    def _raiser(exc: type[Exception] = OSError, msg: str = "test"):
        def _f(*_a, **_kw):
            raise exc(msg)
        return _f
    return _raiser
```

Probably not worth the abstraction at four sites. Flagging because pytest fixtures are exactly the seam for this kind of cross-test setup share.

#### 33. `tests/test_metadata.py:759-765` — `test_module_does_not_smuggle_real_network_calls` doesn't actually test what its name says (low — test naming)
**Files / lines:** `tests/test_metadata.py:759-765`

```python
def test_module_does_not_smuggle_real_network_calls() -> None:
    callers = [libretro.fetch_cover, hasheous.lookup_by_hash, screenscraper.lookup_game]
    assert all(callable(c) for c in callers)
```

This asserts that three functions are callable. It does NOT verify that they refuse to make real network calls. The docstring explains the intent ("if this ever fails, somebody added a test that bypassed MockTransport") but the test body has no causal link to the intent. A real version would set `httpx.Client(transport=httpx.MockTransport(...))` as the default at module level via monkey-patch in a `conftest.py` `autouse=True` fixture, OR would use the `socket` module's `socket.create_connection = pytest.raises(...)` trick to make any unmocked outbound call fail loudly.

#### 34. `tests/test_filename_parser.py` and `test_scanner.py` use `@pytest.mark.parametrize` well — `test_models.py` doesn't (low — test idiom)
**Files / lines:** `tests/test_models.py:39-59, 73-97`

`TestRomFile` and `TestDestinationProfile` each have multiple test methods that exercise the same model with different inputs. The `parametrize` form would compact `test_required_fields`/`test_full_construction`/`test_required_fields` into one parametrized test per class. Pure consistency nit — current form is more readable for the four-case-per-class size.

#### 35. `sqlite3.Connection.row_factory = sqlite3.Row` then accessing rows via `row[0]` instead of `row["id"]` (low — readability)
**Files / lines:** `src/romulus/db/config.py:33-34, 50` (`row[0]`); `src/romulus/db/queries.py:96, 174, 530, 540, 565, 568, 607` (`row[0]`); `src/romulus/ui/system_sidebar.py:32` (`row[0]/row[1]/row[2]`).

`row_factory = sqlite3.Row` makes named-column access work; the positional `row[0]` then works too but loses the documentation value of the column name. Picking one is a five-edit cleanup; either is correct. The mix appears in the same module (`queries.py` uses both forms across functions) which is the smell.

## Patterns done well

These are the Python-craft patterns the codebase gets *right* and should not regress as it grows.

- **Module-level `frozenset` for immutable lookup sets.** `scanner.py:34-52, 56-74, 96-138, 142-147`; `dat_parser.py:24-50`; `game_table.py:43`; `models/__init__.py` exports. Constants stay immutable, allocation is one-time at import. Textbook.
- **`@dataclass(frozen=True)` for value objects that cross boundaries.** `HashResult` (`hasher.py:33`), `DatEntry` (`dat_parser.py:56`), `LaunchBoxEntry` (`launchbox.py:53`). These are the right shape — pickle/repr/equality for free, immutability prevents the "did this get mutated by the caller?" question. Extend to the rest of the dataclasses per finding #17.
- **`match`/`case _` for system-id dispatch.** `hasher.normalize_rom_content` (`:76-99`), `identifier.extract_header_title` (`:184-201`), `organizer.execute_plan` (`:525-533`). These are the canonical case for 3.10's structural pattern matching — fixed set of string discriminators, per-case handler body, exhaustive default. Better than chained `if/elif` for this exact shape.
- **`httpx.MockTransport` as the universal HTTP test seam.** `tests/test_metadata.py` and friends inject a `MockTransport`-backed `httpx.Client` rather than `monkey-patch httpx.get`. This is the cleanest possible pattern for testing async/sync HTTP with full control over response shape. Don't regress.
- **`with path.open("rb") as f:` over `open(str(path), "rb")`.** Uses the `Path.open` method directly. Three places (`hasher.py:146, 186`, `identifier.py:176`); zero `open(...)`. Idiomatic for code that's already using `pathlib`.
- **`pytest.fixture` composition.** `seeded_db` (`conftest.py:35-39`) is built atop `db` rather than re-implementing the schema-creation step. Tests then use whichever level they need.
- **`from collections.abc import Callable` (not `from typing`).** `scanner.py:16`, `hasher.py:16`, `organizer.py:34`, `metadata/__init__.py:12`. The `typing.Callable` is deprecated as of 3.9 in favor of `collections.abc.Callable`. Project uses the right one consistently.
- **Walrus used sparingly and only where it helps.** Approximately zero gratuitous walruses in the codebase. When `:=` would actually save a line it doesn't appear (mostly because the SQL-fetch-then-narrow pattern uses the `row[0] if row else None` form, which is already a one-liner). The lack of cleverness here is itself a Python-craft virtue.
- **`@property` for derived values that don't need persistence.** `SystemMapping.is_supported` (`profile.py:34-37`) and `DetailPanel.current_game_id` (`detail_panel.py:176-179`). The right level of indirection — neither is a method, neither needs to be settable.

## Recommended quick wins

Ranked by **impact / effort**. Each is small enough to land in a 1–2 hour session.

1. **Define a single `MetadataPayload` TypedDict** (`metadata/_types.py`, ~10 lines) and use it as the return type of `parse_hasheous_response`, `parse_screenscraper_response`, `entry_to_metadata`, and the `metadata` parameter of `upsert_metadata`. Closes finding #1's biggest case. **Five-file diff, no runtime change, catches future field drift the moment a type checker is added.**
2. **Replace 10 `setdefault(k, []).append(v)` sites with `defaultdict(list)`.** Finding #3. Pure idiom upgrade — drop-in. Reads better, marginally faster.
3. **Replace `OrganizePlan.counts_by_kind` with `Counter`.** Finding #4. Two-line function shrinks to one.
4. **Hoist filter set literals to module-level `frozenset`s in `game_table.py`.** Finding #21. Three constants, no per-call allocation, half a line shorter.
5. **Add `@field_validator` for `DestinationProfile.base_path` and `SystemMapping.folder` rejecting absolute / `..` paths.** Combined finding #6 + security reviewer #1. Six lines of Pydantic, closes a high-severity security finding with no behaviour change for legitimate profiles.
6. **Replace `1000` sentinels in `organizer.py` with `_UNRANKED_SORT_KEY = sys.maxsize`.** Finding #12. Two-line cleanup, four call-site edits.
7. **Switch the four `_MD_MAGIC_*.startswith(...)` chain to `bytes.startswith(_MD_MAGICS)`.** Finding #10. One-line idiom win.
8. **`frozen=True, slots=True` on `HashResult`'s siblings** (`ParsedFilename`, `ScanResult`, `OrganizePlan`, `ExportFilters`, `ExportOptions`, `ExportPreview`, `ExportSummary`). Finding #17. Free memory win, prevents accidental mutation. Leave `OrganizeAction` mutable — execute_plan stamps it.
9. **Move `data/profiles/` into `src/romulus/data/profiles/` and switch to `importlib.resources.files("romulus.data.profiles")`.** Finding #13. Closes the brittle `parents[3]` path *and* makes `pip install` work without additional packaging ceremony. Highest effort of this list (touches the wheel build) but the only stable answer.
10. **Annotate `Iterable[X]` instead of `list[X]` on read-only parameters in `core/exporter.py:400, 466, 502` and `core/organizer.py:347, 357`.** Finding #18. Pure type-precision win; nothing else changes.
