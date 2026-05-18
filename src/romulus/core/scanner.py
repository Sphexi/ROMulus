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

# Archive containers accepted as ROMs regardless of the system's native
# extension list. Most retro libraries store cartridge ROMs zipped (smaller,
# de facto standard), so a ``.zip`` inside a recognised system folder is a
# ROM by convention. The hasher cracks the archive open during Heavy Scan to
# match against DAT entries; Quick Scan just enrols the container and lets
# Layer-3 sort identity out.
ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({".zip", ".7z"})

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

# Re-release / platform-port tags that ARE identity-bearing — they distinguish
# a re-release from the original ROM rather than just describing a variant.
# Stripping these into a shared fuzzy_key collapses the cartridge dump and the
# Virtual Console / Switch Online / mini-console / arcade-port version into a
# single game, which is wrong: the underlying bytes are usually different and
# users expect them as distinct entries.
_RELEASE_TYPE_TOKENS: frozenset[str] = frozenset(
    {
        "virtual console",
        "wii virtual console",
        "wii u virtual console",
        "3ds virtual console",
        "switch online",
        "nso",
        "genesis mini",
        "mega drive mini",
        "snes mini",
        "snes classic",
        "nes classic",
        "nes mini",
        "playstation classic",
        "ps classic",
        "sega channel",
        "gametap",
        "eshop",
        "psn",
        "playchoice-10",
        "vs.",
        "broadband adapter",
        "satellaview",
        "sufami turbo",
        "32x",
    }
)

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
    release_type: str | None = None
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

    Archive containers (``.zip``, ``.7z``) are always accepted regardless of the
    system's native extension list, because most retro libraries store
    cartridge ROMs zipped. The hasher inspects archive contents during Heavy
    Scan; Quick Scan just enrols the file under the folder's detected system.
    """
    ext = Path(filename).suffix.lower()
    if ext in ARCHIVE_EXTENSIONS:
        return True
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
            unlicensed, homebrew, release, unknown}.

    ``release`` covers identity-bearing tags like "Virtual Console" or
    "Switch Online" that distinguish a re-release from the original ROM.
    The grouper folds these into the fuzzy_key so the cartridge dump and
    the VC release end up as distinct games.
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
    if lower in _RELEASE_TYPE_TOKENS:
        return "release", stripped
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
    release_type: str | None = None
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
            elif kind == "release" and release_type is None:
                release_type = value

    # Strip every tag group and collapse whitespace.
    clean = _TAG_GROUP_RE.sub("", stem)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Remove dangling separator chars left over from stripping (e.g. trailing '-').
    clean = re.sub(r"[\s_\-]+$", "", clean)

    display = _move_trailing_article_to_front(clean)
    # Re-release tags stay in the display title so the user can tell a VC
    # release apart from the cartridge in the game list.
    if release_type:
        display = f"{display} ({release_type})"

    return ParsedFilename(
        clean_name=clean,
        display_title=display,
        extension=extension,
        region=region,
        revision=revision,
        disc_number=disc_number,
        release_type=release_type,
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


def generate_fuzzy_key(clean_name: str, release_type: str | None = None) -> str:
    """Reduce a parsed title to a stable alphanumeric comparison key.

    Implements the seven normalization steps from ROM-DEDUP-METHODOLOGY.md §3.2.
    The input is the already-extension-stripped, already-tag-stripped title from
    `parse_filename().clean_name`.

    ``release_type`` (e.g. "Virtual Console", "Switch Online", "Genesis Mini")
    is appended to the key when present so re-releases stay distinct from the
    original ROM. Without this, both ``Alien Soldier.zip`` and
    ``Alien Soldier (Virtual Console).zip`` collapse to ``aliensoldier`` and
    the grouper merges them into a single game.
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
    if release_type:
        suffix = re.sub(r"[^a-z0-9]", "", release_type.lower())
        if suffix:
            name = f"{name}__{suffix}"
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
    files_newly_missing: int = 0


def scan_library(
    conn: sqlite3.Connection,
    library_path: str | os.PathLike[str],
    progress_callback: Callable[[int, str], None] | None = None,
    scope_system_id: str | None = None,
) -> ScanResult:
    """Walk `library_path`, enroll ROMs, and group them into logical games.

    `progress_callback(files_so_far, current_filename)` is invoked once per
    enrolled ROM during the walk, AND once per phase transition during the
    post-walk DB work ("Marking missing entries…", "Linking ROMs to games:
    <system>…", "Finalising scan history…") so the user sees activity
    rather than a frozen Cancel button. The callback is optional; in
    tests we usually leave it None.

    ``scope_system_id`` restricts the scan to one platform. When set:

    * Files whose resolved system_id doesn't match are skipped during
      the walk (counted as ``files_skipped``, not ``files_with_system``).
    * The missing-row sweep only flags rows belonging to that system.
    * ``group_into_games`` only runs for that system — other systems'
      unlinked roms are left alone for a global scan to pick up.

    Returns a `ScanResult` summarizing the run. A `scan_history` row is also
    written and finalized before returning. `errors` counts files that were
    skipped specifically because their `stat()` call raised `OSError`
    (typically permission denied or a vanished symlink); files skipped because
    their extension didn't belong to any known system are counted under
    `files_skipped` but NOT as errors.
    """
    library_root = Path(library_path)
    # Canonicalize once so the value stamped on every row matches the value
    # the sweep step queries with. ``resolve()`` falls back to the unresolved
    # path on missing-link errors — Windows drives that aren't currently
    # mounted will fail to resolve, which is fine; we still want to scan the
    # path the user gave us.
    try:
        library_root_canonical = library_root.resolve()
    except OSError:
        library_root_canonical = library_root
    library_root_str = str(library_root_canonical)
    logger.debug(
        "quick scan starting: library_root=%s canonical=%s",
        library_path,
        library_root_str,
    )

    alias_map = get_systems_by_alias(conn)
    extensions_by_system = get_extensions_by_system(conn)
    logger.debug(
        "quick scan registry: aliases=%d systems=%d",
        len(alias_map),
        len(extensions_by_system),
    )

    started_at = datetime.now(UTC).isoformat()
    scan_id = queries.insert_scan_history(
        conn,
        {
            "scan_type": "quick",
            "started_at": started_at,
            "root_path": library_root_str,
        },
    )

    files_found = 0
    files_with_system = 0
    files_skipped = 0
    errors = 0
    systems_seen: set[str] = set()
    visited_rom_ids: set[int] = set()

    # os.walk defaults to followlinks=False — we deliberately do NOT follow
    # symlinks. This prevents a symlinked subdirectory inside the library
    # from being used to traverse outside library_root.
    for root, _dirs, files in os.walk(library_root):
        root_path = Path(root)
        # Resolve the system context from the directory tree once per directory.
        system_id = _resolve_system_for_directory(root_path, library_root, alias_map)
        logger.debug(
            "scan dir: path=%s system=%s files=%d",
            root_path,
            system_id,
            len(files),
        )

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

            # Scoped scan: drop any file whose resolved system isn't the
            # caller's chosen scope. Counted as a skip, not an error —
            # the file is presumably valid for some OTHER system in the
            # library and a global scan would enroll it normally.
            if scope_system_id is not None and system_id != scope_system_id:
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
            fuzzy = generate_fuzzy_key(parsed.clean_name, parsed.release_type)
            logger.debug(
                "scan enroll: path=%s system=%s fuzzy=%s size=%d",
                file_path,
                system_id,
                fuzzy or "<empty>",
                stat.st_size,
            )

            rom_id = queries.upsert_rom(
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
                    "library_root": library_root_str,
                },
            )
            visited_rom_ids.add(rom_id)
            files_found += 1
            files_with_system += 1
            systems_seen.add(system_id)
            if progress_callback is not None:
                progress_callback(files_found, filename)

    conn.commit()

    # Post-walk DB phases — emit a progress label for each so the user
    # sees activity instead of a frozen Cancel button. The file count
    # stays at ``files_found``; only the second-line text changes.
    if progress_callback is not None:
        progress_callback(files_found, "Marking missing entries…")

    # Tombstone sweep: any row under THIS library_root that we didn't visit is
    # a file that's gone missing since the last scan (deleted, moved, drive
    # unmounted). Mark them missing=1 so the UI can show a "N missing entries"
    # badge and the user can choose to clean them up. Re-scanning after a
    # reconnect will flip missing back to 0 via upsert_rom's path-keyed UPSERT,
    # so this is non-destructive.
    #
    # In a scoped scan we restrict the sweep to roms belonging to the
    # scope system, so other systems' rows aren't tombstoned by a
    # single-system rescan.
    files_newly_missing = queries.mark_missing_under_root(
        conn,
        library_root_str,
        visited_rom_ids,
        scope_system_id=scope_system_id,
    )
    if files_newly_missing:
        logger.info(
            "scan flagged %d previously-known files as missing under %s",
            files_newly_missing,
            library_root_str,
        )
    conn.commit()

    # Self-heal: a previous scan may have crashed (e.g. the v0.3.0
    # ``too many SQL variables`` regression in ``mark_missing_under_root``)
    # after upserting roms but BEFORE reaching this game-grouping step,
    # leaving the affected system's roms with NULL ``game_id`` forever
    # — they can't be enriched, can't be exported, and the right-click
    # menu had nothing to bind to.
    #
    # ``systems_seen`` only catches systems whose roms were walked
    # this scan; that's the happy path. The query below catches the
    # crash-recovery case: any system with at least one rom that has
    # a fuzzy_key but no linked game. ``group_into_games`` is
    # idempotent — it re-uses existing game rows when the fuzzy_key
    # already maps to one — so the union doesn't cause duplication.
    # Self-heal is intentionally library-wide on a global scan (catches
    # damage from older partial-scan crashes regardless of which systems
    # the user walked this time), but a SCOPED scan should only touch
    # its own system — we don't want a "rescan Atari 7800" to start
    # rewriting linkage rows for NES.
    if scope_system_id is None:
        unlinked_systems_rows = conn.execute(
            """
            SELECT DISTINCT system_id
            FROM roms
            WHERE game_id IS NULL
              AND system_id IS NOT NULL
              AND fuzzy_key IS NOT NULL
              AND fuzzy_key != ''
            """
        ).fetchall()
    else:
        unlinked_systems_rows = []
    systems_to_group = set(systems_seen)
    systems_to_group.update(row[0] for row in unlinked_systems_rows)
    if unlinked_systems_rows:
        logger.info(
            "scan: self-heal — grouping %d system(s) with unlinked roms "
            "from a prior partial scan",
            len(unlinked_systems_rows),
        )
    for system_id in systems_to_group:
        if progress_callback is not None:
            progress_callback(
                files_found, f"Linking ROMs to games: {system_id}…"
            )
        group_into_games(conn, system_id)

    if progress_callback is not None:
        progress_callback(files_found, "Finalising scan history…")

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
    logger.debug(
        "quick scan finished: scan_id=%d found=%d skipped=%d errors=%d missing=%d",
        scan_id,
        files_found,
        files_skipped,
        errors,
        files_newly_missing,
    )

    return ScanResult(
        scan_id=scan_id,
        files_found=files_found,
        files_with_system=files_with_system,
        files_skipped=files_skipped,
        errors=errors,
        systems_seen=systems_seen,
        files_newly_missing=files_newly_missing,
    )
