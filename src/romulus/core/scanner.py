"""Filesystem scanner — walks a ROM library, detects platforms, parses filenames.

This module implements Quick Scan layers 1+2 from the dedup methodology:
- Filesystem walk + platform detection by directory name
- Filename tag parsing (region/revision/status/disc per No-Intro/GoodTools/TOSEC)
- Fuzzy key generation (Layer 1 normalization for cheap dedup)

Layer 3 (hashing + DAT matching) lives in `core.identifier` and `core.hasher`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from romulus.core._no_intro_tokens import FILENAME_REGION_TOKENS, REVISION_RE
from romulus.db import queries
from romulus.models.system import get_extensions_by_system, get_systems_by_alias

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Companion files that travel alongside ROMs but are never the ROM itself.
# Some of these extensions (.cue, .m3u) are valid "primary" entries for CD-based
# systems in our system registry, but Quick Scan still skips them: the scanner
# enrolls the underlying .bin/.iso/.chd as the ROM, and disc-grouping logic in a
# later session will reattach cue/m3u files. Saves, screenshots, and metadata
# files should never be enrolled.
SIDE_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".cue",
        ".m3u",
        ".sub",
        ".txt",
        ".nfo",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".xml",
        ".dat",
        ".sav",
        ".srm",
        ".state",
        ".oops",
    }
)

# Articles to fold for fuzzy comparison. Multi-language coverage matches
# ROM-DEDUP-METHODOLOGY.md §3.2.
_ARTICLES: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "le",
        "la",
        "les",
        "el",
        "los",
        "las",
        "der",
        "die",
        "das",
        "il",
        "lo",
        "gli",
    }
)

# Roman numerals to convert during fuzzy key generation. Single-letter numerals
# are intentionally omitted — they collide with common words ("I", "V", "X").
_ROMAN_NUMERALS: dict[str, int] = {
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
}

# Region tokens recognized by No-Intro / Redump / TOSEC. Single source of
# truth lives in ``core/_no_intro_tokens.py``; this alias keeps the local
# name stable for any in-module references.
_REGION_TOKENS: frozenset[str] = FILENAME_REGION_TOKENS

# Status tokens that appear inside parentheses (TOSEC/No-Intro style).
_STATUS_PROTOTYPE: frozenset[str] = frozenset({"proto", "prototype"})
_STATUS_BETA: frozenset[str] = frozenset({"beta"})
_STATUS_DEMO: frozenset[str] = frozenset({"demo"})
_STATUS_SAMPLE: frozenset[str] = frozenset({"sample"})
_STATUS_UNLICENSED: frozenset[str] = frozenset({"unl", "unlicensed"})
_STATUS_HOMEBREW: frozenset[str] = frozenset({"homebrew", "aftermarket"})

# Compiled regexes (reused across thousands of files in a real scan).
# ``_TAG_GROUP_RE`` matches both ``(...)`` and ``[...]`` groups — the scanner
# parses both. The DAT parser only needs the ``(...)`` form (see dat_parser.py).
_TAG_GROUP_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
# Revision regex single source of truth lives in ``core/_no_intro_tokens.py``.
_REVISION_RE = REVISION_RE
_DISC_RE = re.compile(r"^(disc|disk|side)\s+([0-9A-Za-z]+)(\s+of\s+\d+)?$", re.IGNORECASE)
_TRANSLATION_RE = re.compile(r"^t[+-]", re.IGNORECASE)
_BAD_DUMP_RE = re.compile(r"^b\d*$", re.IGNORECASE)
_HACK_RE = re.compile(r"^h\d*$", re.IGNORECASE)
_OVERDUMP_RE = re.compile(r"^o\d*$", re.IGNORECASE)
_ALTERNATE_RE = re.compile(r"^a\d*$", re.IGNORECASE)
_VERSION_SUFFIX_RE = re.compile(
    r"\s*(v\d+(\.\d+[a-z]?)?|\d+\.\d+[a-z]?|rev\s*\d+)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Parsed filename
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedFilename:
    """Structured result of parsing a ROM filename.

    `clean_name` is what's left of the title after stripping every tag group.
    `display_title` additionally folds trailing articles to the front, so
    "Addams Family, The" becomes "The Addams Family" for display.
    """

    clean_name: str
    display_title: str
    extension: str
    region: str | None = None
    revision: str | None = None
    disc_number: int | None = None
    status: list[str] = field(default_factory=list)
    is_hack: bool = False
    is_homebrew: bool = False
    is_unlicensed: bool = False
    is_prototype: bool = False
    is_beta: bool = False
    is_demo: bool = False
    is_bad_dump: bool = False
    is_verified: bool = False
    is_translation: bool = False


# ---------------------------------------------------------------------------
# Side-file and extension checks
# ---------------------------------------------------------------------------


def is_side_file(filename: str) -> bool:
    """Return True if `filename` is a companion file the scanner must skip."""
    ext = Path(filename).suffix.lower()
    return ext in SIDE_FILE_EXTENSIONS


def is_rom_file(filename: str, accepted_extensions: list[str] | set[str]) -> bool:
    """Return True if `filename` has an extension accepted by this system.

    `accepted_extensions` should contain lowercase extensions with leading dots.
    """
    ext = Path(filename).suffix.lower()
    return ext in set(accepted_extensions)


# ---------------------------------------------------------------------------
# System detection
# ---------------------------------------------------------------------------


def detect_system(dirname: str, alias_map: dict[str, str]) -> str | None:
    """Map a directory basename to a system id via the alias map.

    Comparison is lowercase. Returns None if no alias matches.
    """
    return alias_map.get(dirname.lower())


def _resolve_system_for_directory(
    directory: Path,
    library_root: Path,
    alias_map: dict[str, str],
) -> str | None:
    """Walk up from `directory` toward `library_root`, returning the first
    directory whose basename matches a known system alias.

    A typical layout has `/library/snes/Game.sfc`; for files nested deeper
    (`/library/snes/Hacks/Modded.sfc`) the first system-named ancestor wins.
    `library_root` itself is also checked, so pointing the library at
    `/Users/foo/snes` still works.
    """
    # Both ``.resolve()`` calls must succeed or both must fall back to the
    # unresolved values — otherwise the loop below would compare a resolved
    # root against an unresolved current and could loop one extra iteration.
    with contextlib.suppress(OSError):
        library_root = library_root.resolve()
    try:
        current = directory.resolve()
    except OSError:
        current = directory
    while True:
        match = detect_system(current.name, alias_map)
        if match is not None:
            return match
        if current == library_root or current.parent == current:
            return None
        current = current.parent


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------


def _classify_paren_tag(content: str) -> tuple[str, str]:
    """Classify a parenthesized tag group. Returns (kind, normalized_value).

    kind ∈ {region, revision, disc, prototype, beta, demo, sample,
            unlicensed, homebrew, unknown}.
    """
    stripped = content.strip()
    lower = stripped.lower()

    if _REVISION_RE.match(stripped):
        return "revision", stripped
    if _DISC_RE.match(stripped):
        return "disc", stripped
    if lower in _STATUS_PROTOTYPE:
        return "prototype", stripped
    if lower in _STATUS_BETA:
        return "beta", stripped
    if lower in _STATUS_DEMO:
        return "demo", stripped
    if lower in _STATUS_SAMPLE:
        return "sample", stripped
    if lower in _STATUS_UNLICENSED:
        return "unlicensed", stripped
    if lower in _STATUS_HOMEBREW:
        return "homebrew", stripped
    # Region: every comma-separated token must be a known region/language code.
    parts = [p.strip().lower() for p in stripped.split(",") if p.strip()]
    if parts and all(p in _REGION_TOKENS for p in parts):
        return "region", stripped
    return "unknown", stripped


def _parse_disc_number(value: str) -> int | None:
    """Extract a disc number from a tag like 'Disc 1' or 'Disc A'."""
    match = _DISC_RE.match(value.strip())
    if not match:
        return None
    raw = match.group(2)
    if raw.isdigit():
        return int(raw)
    # Letter-coded discs: A→1, B→2…
    if len(raw) == 1 and raw.isalpha():
        return ord(raw.upper()) - ord("A") + 1
    return None


def _move_trailing_article_to_front(name: str) -> str:
    """Convert 'Addams Family, The' → 'The Addams Family'.

    Case-insensitive match on the article; the original capitalization of the
    rest of the title is preserved.
    """
    for article in _ARTICLES:
        pattern = re.compile(rf",\s*({re.escape(article)})\s*$", re.IGNORECASE)
        match = pattern.search(name)
        if match:
            base = name[: match.start()].rstrip()
            article_text = match.group(1)
            # Title-case the article (The, A, La...) for display.
            return f"{article_text.title()} {base}"
    return name


def parse_filename(filename: str) -> ParsedFilename:
    """Decompose a ROM filename into its title and structured tags.

    Handles No-Intro `(USA) (Rev 1)`, GoodTools `[!] [h1]`, and TOSEC
    parenthesized status tags. Unknown tag groups are silently ignored —
    they're stripped from the clean name but not surfaced.
    """
    stem = Path(filename).stem
    extension = Path(filename).suffix.lower()

    region: str | None = None
    revision: str | None = None
    disc_number: int | None = None
    status: list[str] = []

    is_hack = False
    is_homebrew = False
    is_unlicensed = False
    is_prototype = False
    is_beta = False
    is_demo = False
    is_bad_dump = False
    is_verified = False
    is_translation = False

    for match in _TAG_GROUP_RE.finditer(stem):
        paren_content = match.group(1)
        bracket_content = match.group(2)
        if bracket_content is not None:
            bracket = bracket_content.strip()
            lower = bracket.lower()
            if lower == "!":
                is_verified = True
                status.append("verified")
            elif _BAD_DUMP_RE.match(lower):
                is_bad_dump = True
                status.append("bad_dump")
            elif _HACK_RE.match(lower):
                is_hack = True
                status.append("hack")
            elif _TRANSLATION_RE.match(lower):
                is_translation = True
                status.append("translation")
            elif _OVERDUMP_RE.match(lower):
                status.append("overdump")
            elif _ALTERNATE_RE.match(lower):
                status.append("alternate")
            # Unknown bracket tags are dropped silently — this is intentional
            # policy, not an oversight. ROM filenames in the wild carry an
            # open-ended vocabulary of bracketed tags (``[Cracked]``,
            # ``[CT-Mod]``, ``[Trainer +N]``, scene-group abbreviations…) and
            # logging every unknown tag would create noise without value. The
            # paren-tag branch below applies the same policy for parenthesized
            # groups that don't match any known classification.
        else:
            kind, value = _classify_paren_tag(paren_content or "")
            if kind == "region" and region is None:
                region = value
            elif kind == "revision" and revision is None:
                revision = value
            elif kind == "disc" and disc_number is None:
                disc_number = _parse_disc_number(value)
            elif kind == "prototype":
                is_prototype = True
                status.append("prototype")
            elif kind == "beta":
                is_beta = True
                status.append("beta")
            elif kind == "demo":
                is_demo = True
                status.append("demo")
            elif kind == "sample":
                status.append("sample")
            elif kind == "unlicensed":
                is_unlicensed = True
                status.append("unlicensed")
            elif kind == "homebrew":
                is_homebrew = True
                status.append("homebrew")

    # Strip every tag group and collapse whitespace.
    clean = _TAG_GROUP_RE.sub("", stem)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Remove dangling separator chars left over from stripping (e.g. trailing '-').
    clean = re.sub(r"[\s_\-]+$", "", clean)

    display = _move_trailing_article_to_front(clean)

    return ParsedFilename(
        clean_name=clean,
        display_title=display,
        extension=extension,
        region=region,
        revision=revision,
        disc_number=disc_number,
        status=status,
        is_hack=is_hack,
        is_homebrew=is_homebrew,
        is_unlicensed=is_unlicensed,
        is_prototype=is_prototype,
        is_beta=is_beta,
        is_demo=is_demo,
        is_bad_dump=is_bad_dump,
        is_verified=is_verified,
        is_translation=is_translation,
    )


# ---------------------------------------------------------------------------
# Fuzzy key (Layer 1 normalization)
# ---------------------------------------------------------------------------


def generate_fuzzy_key(clean_name: str) -> str:
    """Reduce a parsed title to a stable alphanumeric comparison key.

    Implements the seven normalization steps from ROM-DEDUP-METHODOLOGY.md §3.2.
    The input is the already-extension-stripped, already-tag-stripped title from
    `parse_filename().clean_name`.
    """
    name = clean_name

    # Step 3: trailing article → front, then strip the leading article.
    name = _move_trailing_article_to_front(name)
    parts = name.split()
    if parts and parts[0].lower() in _ARTICLES:
        parts = parts[1:]
    name = " ".join(parts)

    # Step 4: convert multi-letter Roman numerals to Arabic. Preserve single-letter
    # tokens — they conflict with common words.
    converted: list[str] = []
    for token in name.split():
        # Trim trailing punctuation for the lookup but preserve unconverted token
        # otherwise.
        bare = re.sub(r"[^A-Za-z0-9]", "", token).lower()
        if bare in _ROMAN_NUMERALS:
            converted.append(str(_ROMAN_NUMERALS[bare]))
        else:
            converted.append(token)
    name = " ".join(converted)

    # Step 5: strip trailing version suffixes (NOT bare sequel numbers).
    name = _VERSION_SUFFIX_RE.sub("", name).strip()

    # Step 6 + 7: lowercase, strip non-alphanumeric.
    name = name.lower()
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


# ---------------------------------------------------------------------------
# Game grouping
# ---------------------------------------------------------------------------


def group_into_games(conn: sqlite3.Connection, system_id: str) -> int:
    """Group ROMs in `system_id` into logical games by their fuzzy_key.

    For each unique non-empty fuzzy_key, either reuse an existing game (linked
    via another ROM with the same key) or create a new one using the first
    ROM's parsed title. All ROMs in the group are then linked to that game.

    Returns the number of distinct games this call linked ROMs into.
    """
    rows = conn.execute(
        """
        SELECT id, filename, fuzzy_key
        FROM roms
        WHERE system_id = ? AND fuzzy_key IS NOT NULL AND fuzzy_key != ''
        ORDER BY filename
        """,
        (system_id,),
    ).fetchall()

    groups: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        rom_id, filename, fuzzy = row[0], row[1], row[2]
        groups[fuzzy].append((rom_id, filename))

    games_touched = 0
    for fuzzy, rom_list in groups.items():
        # Prefer an already-linked game for this fuzzy_key.
        existing_id = queries.find_game_id_for_fuzzy_key(conn, system_id, fuzzy)
        if existing_id is not None:
            game_id = existing_id
        else:
            _rom_id, first_filename = rom_list[0]
            parsed = parse_filename(first_filename)
            title = parsed.display_title or parsed.clean_name or first_filename
            game_id = queries.upsert_game(
                conn,
                {
                    "title": title,
                    "system_id": system_id,
                    "region": parsed.region,
                    "revision": parsed.revision,
                    "is_hack": parsed.is_hack,
                    "is_homebrew": parsed.is_homebrew,
                },
            )
        for rom_id, _ in rom_list:
            queries.link_rom_to_game(conn, rom_id, game_id)
        games_touched += 1

    conn.commit()
    return games_touched


# ---------------------------------------------------------------------------
# Main scan entrypoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Summary of a single scan invocation."""

    scan_id: int
    files_found: int
    files_with_system: int
    files_skipped: int
    errors: int
    systems_seen: set[str]


def scan_library(
    conn: sqlite3.Connection,
    library_path: str | os.PathLike[str],
    progress_callback: Callable[[int, str], None] | None = None,
) -> ScanResult:
    """Walk `library_path`, enroll ROMs, and group them into logical games.

    `progress_callback(files_so_far, current_filename)` is invoked once per
    enrolled ROM. The callback is optional; in tests we usually leave it None.

    Returns a `ScanResult` summarizing the run. A `scan_history` row is also
    written and finalized before returning. `errors` counts files that were
    skipped specifically because their `stat()` call raised `OSError`
    (typically permission denied or a vanished symlink); files skipped because
    their extension didn't belong to any known system are counted under
    `files_skipped` but NOT as errors.
    """
    library_root = Path(library_path)
    alias_map = get_systems_by_alias(conn)
    extensions_by_system = get_extensions_by_system(conn)

    started_at = datetime.now(UTC).isoformat()
    scan_id = queries.insert_scan_history(
        conn,
        {
            "scan_type": "quick",
            "started_at": started_at,
            "root_path": str(library_root),
        },
    )

    files_found = 0
    files_with_system = 0
    files_skipped = 0
    errors = 0
    systems_seen: set[str] = set()

    # os.walk defaults to followlinks=False — we deliberately do NOT follow
    # symlinks. This prevents a symlinked subdirectory inside the library
    # from being used to traverse outside library_root.
    for root, _dirs, files in os.walk(library_root):
        root_path = Path(root)
        # Resolve the system context from the directory tree once per directory.
        system_id = _resolve_system_for_directory(root_path, library_root, alias_map)

        for filename in files:
            if is_side_file(filename):
                files_skipped += 1
                continue

            ext = Path(filename).suffix.lower()
            if system_id is None or not is_rom_file(
                filename, extensions_by_system.get(system_id, [])
            ):
                files_skipped += 1
                continue

            file_path = root_path / filename
            try:
                stat = file_path.stat()
            except OSError as exc:
                # Real failure to read file metadata — permission denied,
                # broken symlink, file vanished between listing and stat.
                # Count separately from "wrong extension" skips so the scan
                # history surfaces partial failures to the UI. Log at debug
                # level so operators investigating a "N errors" badge can
                # surface which files failed without leaking error volume
                # into the default info-level log stream (audit v0.1.0
                # finding #13).
                logger.debug(
                    "scan stat failed: path=%s err=%s", file_path, exc
                )
                errors += 1
                continue

            parsed = parse_filename(filename)
            fuzzy = generate_fuzzy_key(parsed.clean_name)

            queries.upsert_rom(
                conn,
                {
                    "path": str(file_path),
                    "filename": filename,
                    "extension": ext,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "system_id": system_id,
                    "scan_id": scan_id,
                    "fuzzy_key": fuzzy,
                    "match_confidence": "fuzzy" if fuzzy else "unmatched",
                },
            )
            files_found += 1
            files_with_system += 1
            systems_seen.add(system_id)
            if progress_callback is not None:
                progress_callback(files_found, filename)

    conn.commit()

    for system_id in systems_seen:
        group_into_games(conn, system_id)

    finished_at = datetime.now(UTC).isoformat()
    queries.update_scan_history(
        conn,
        scan_id,
        {
            "finished_at": finished_at,
            "files_found": files_found,
            "files_matched": files_with_system,
            "files_new": files_with_system,
            "errors": errors,
        },
    )
    conn.commit()

    return ScanResult(
        scan_id=scan_id,
        files_found=files_found,
        files_with_system=files_with_system,
        files_skipped=files_skipped,
        errors=errors,
        systems_seen=systems_seen,
    )
