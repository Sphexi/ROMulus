"""Logiqx XML DAT parser — bundled and user DAT files.

Parses No-Intro / Redump / TOSEC style XML into `DatEntry` records, loads them
into the `dat_entries` table, and matches hashed ROMs back to canonical games.
Uses ``defusedxml.ElementTree`` rather than the stdlib parser to block billion-
laughs / quadratic-blowup entity-expansion DoS attacks against user-supplied
DAT XML files (see security audit v0.1.0 finding #3).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

# ``ET`` mirrors the stdlib alias the previous implementation used so existing
# callers (``ET.parse``, ``ET.ParseError``) stay unchanged. The N817 lint is
# explicitly suppressed for this single case because it's the well-known
# stdlib-compatible alias.
import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

from romulus.core._no_intro_tokens import (
    REGION_COUNTRY_TOKENS,
    REVISION_RE,
    ParsedTokens,
    parse_no_intro_tokens,
)
from romulus.db import queries
from romulus.models.system import SYSTEM_REGISTRY

logger = logging.getLogger(__name__)

# Region tokens we recognize inside parenthesized DAT name groups. Subset of the
# scanner's list — DAT canonical names only carry country/super-region names,
# not language codes. Single source of truth lives in
# ``core/_no_intro_tokens.py``.
_DAT_REGION_TOKENS: frozenset[str] = REGION_COUNTRY_TOKENS

_TAG_GROUP_RE = re.compile(r"\(([^()]*)\)")
# Revision regex single source of truth — see ``core/_no_intro_tokens.py``.
_REVISION_RE = REVISION_RE


@dataclass(frozen=True)
class DatEntry:
    """A single DAT row: a canonical game + its primary ROM file's hashes."""

    dat_file: str
    system_id: str | None
    game_name: str
    rom_name: str
    size_bytes: int | None
    crc32: str | None
    md5: str | None
    sha1: str | None
    region: str | None
    revision: str | None
    is_bios: bool


# ---------------------------------------------------------------------------
# Region / revision parsing
# ---------------------------------------------------------------------------


def parse_region_from_name(game_name: str) -> str | None:
    """Pull a region tag out of a canonical DAT name like 'Foo (USA, Europe)'.

    Returns the first parenthesized group whose comma-separated tokens are all
    known region names (case-insensitive). The raw group text is returned so
    callers see exactly what the DAT wrote.
    """
    for match in _TAG_GROUP_RE.finditer(game_name):
        body = match.group(1).strip()
        parts = [p.strip().lower() for p in body.split(",") if p.strip()]
        if parts and all(p in _DAT_REGION_TOKENS for p in parts):
            return body
    return None


def _parse_revision_from_name(game_name: str) -> str | None:
    for match in _TAG_GROUP_RE.finditer(game_name):
        body = match.group(1).strip()
        if _REVISION_RE.match(body):
            return body
    return None


# ---------------------------------------------------------------------------
# system_id resolution from DAT header name
# ---------------------------------------------------------------------------


def _system_id_from_dat_name(dat_name: str | None) -> str | None:
    """Match a DAT `<header><name>` against `dat_name` or any `dat_name_aliases`.

    Real-world No-Intro DATs ship variants like
    ``"Nintendo - Super Nintendo Entertainment System (Combined)"`` and
    ``"Nintendo - Nintendo 64 (BigEndian)"`` — same logical system, different
    header suffix. The registry's `dat_name_aliases` lets one SystemDef cover
    all of them.
    """
    if not dat_name:
        return None
    target = dat_name.strip().lower()
    for sys_def in SYSTEM_REGISTRY:
        if sys_def.dat_name and sys_def.dat_name.lower() == target:
            return sys_def.id
        if any(alias.lower() == target for alias in sys_def.dat_name_aliases):
            return sys_def.id
    return None


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _coerce_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _normalize_hash(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower() or None


def parse_dat_file(filepath: str | os.PathLike[str]) -> list[DatEntry]:
    """Parse a single Logiqx XML DAT file into a list of DatEntry records.

    Returns an empty list on parse errors so a single bad file doesn't break a
    bulk load. Each `<rom>` element produces one entry; multi-rom games (rare in
    No-Intro, common in MAME) produce one row per rom.
    """
    path = Path(filepath)
    logger.debug("parse_dat_file: start path=%s", path)
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError, DefusedXmlException) as exc:
        # ``DefusedXmlException`` covers billion-laughs / external-entity /
        # external-DTD attacks blocked by defusedxml; the other two cover
        # malformed XML and filesystem errors. Returning an empty list lets a
        # bulk loader skip the bad file without aborting siblings.
        logger.debug(
            "parse_dat_file: parse failed path=%s err=%s err_type=%s",
            path,
            exc,
            type(exc).__name__,
        )
        return []

    root = tree.getroot()
    header_name = None
    header = root.find("header")
    if header is not None:
        name_el = header.find("name")
        if name_el is not None:
            header_name = (name_el.text or "").strip()
    system_id = _system_id_from_dat_name(header_name)
    if header_name and system_id is None:
        logger.debug(
            "parse_dat_file: unrecognized DAT header path=%s header_name=%s",
            path,
            header_name,
        )

    entries: list[DatEntry] = []
    for game in root.iter("game"):
        game_name = (game.get("name") or "").strip()
        if not game_name:
            continue
        is_bios = (game.get("isbios") or "no").lower() == "yes"
        region = parse_region_from_name(game_name)
        revision = _parse_revision_from_name(game_name)
        for rom in game.findall("rom"):
            rom_name = (rom.get("name") or "").strip()
            if not rom_name:
                continue
            entries.append(
                DatEntry(
                    dat_file=path.name,
                    system_id=system_id,
                    game_name=game_name,
                    rom_name=rom_name,
                    size_bytes=_coerce_int(rom.get("size")),
                    crc32=_normalize_hash(rom.get("crc")),
                    md5=_normalize_hash(rom.get("md5")),
                    sha1=_normalize_hash(rom.get("sha1")),
                    region=region,
                    revision=revision,
                    is_bios=is_bios,
                )
            )
    logger.debug(
        "parse_dat_file: done path=%s header_name=%s system_id=%s entries=%d",
        path,
        header_name,
        system_id,
        len(entries),
    )
    return entries


# ---------------------------------------------------------------------------
# Bulk load + match
# ---------------------------------------------------------------------------


def _iter_dat_files(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """Flatten a mix of dat file paths and directories into a deduped file list."""

    def _expand(p: Path) -> Iterable[Path]:
        if p.is_dir():
            # Both ``.dat`` and ``.xml`` are recognized because publishers
            # disagree on the extension: No-Intro and TOSEC use ``.dat``,
            # while Redump and some MAME packs ship Logiqx-XML under ``.xml``.
            # Both formats are identical underneath. Do not "fix" this by
            # dropping ``.xml`` — it loses Redump DATs.
            return chain(sorted(p.rglob("*.dat")), sorted(p.rglob("*.xml")))
        if p.is_file():
            return (p,)
        return ()

    candidates = chain.from_iterable(_expand(Path(raw)) for raw in paths)
    return list({f.resolve(): f for f in candidates}.values())


def load_all_dats(
    conn: sqlite3.Connection,
    dat_paths: Iterable[str | os.PathLike[str]],
) -> int:
    """Parse every DAT under `dat_paths` and insert rows into `dat_entries`.

    Accepts a mix of file paths and directory roots; directories are scanned for
    .dat and .xml files recursively. Returns the number of inserted entries.
    Callers can pass both bundled (`data/dats/`) and user (`~/.romulus/dats/`)
    roots in a single call.
    """
    inserted = 0
    dat_files = _iter_dat_files(dat_paths)
    logger.debug("load_all_dats: discovered dat_files=%d", len(dat_files))
    for dat_path in dat_files:
        entries = parse_dat_file(dat_path)
        for entry in entries:
            queries.insert_dat_entry(conn, entry)
            inserted += 1
        logger.debug(
            "load_all_dats: loaded dat=%s entries=%d running_total=%d",
            dat_path.name,
            len(entries),
            inserted,
        )
    conn.commit()
    logger.debug("load_all_dats: complete inserted=%d", inserted)
    return inserted


def _update_identity_from_dat(
    conn: sqlite3.Connection,
    rom_id: int,
    tokens: ParsedTokens,
    entry: sqlite3.Row,
) -> None:
    """Write DAT-authoritative identity columns onto a matched roms row.

    Called immediately after :func:`~romulus.db.queries.update_rom_match`
    stamps ``dat_match`` + ``match_confidence``.  Updates ``canonical_name``,
    ``region``, ``revision``, and ``is_bios`` in place.  ``title`` is also
    set to the cleaned DAT title (parens stripped) so the game list shows a
    tidy display name once Heavy Scan completes.

    ``COALESCE`` logic is NOT used here — the DAT hit is authoritative, so
    we overwrite filename-derived values unconditionally.

    Args:
        conn: SQLite connection (caller owns the transaction).
        rom_id: The rom row to update.
        tokens: Result of ``parse_no_intro_tokens(entry["game_name"])``.
        entry: The matched ``dat_entries`` row.
    """
    canonical_name = str(entry["game_name"])
    is_bios = bool(entry["is_bios"]) if entry["is_bios"] else False
    conn.execute(
        """
        UPDATE roms
        SET canonical_name = ?,
            title          = ?,
            region         = COALESCE(?, region),
            revision       = COALESCE(?, revision),
            is_bios        = CASE WHEN ? THEN 1 ELSE is_bios END
        WHERE id = ?
        """,
        (
            canonical_name,
            tokens.title or canonical_name,
            tokens.region,
            tokens.revision,
            is_bios,
            rom_id,
        ),
    )


def match_hashes(
    conn: sqlite3.Connection,
    scope_rom_ids: list[int] | None = None,
) -> int:
    """Resolve hashed ROMs against `dat_entries` and stamp them as DAT-verified.

    For every (rom, hash) pair, look up by SHA-1 first, then CRC32+size as
    fallback. On hit, update `roms.dat_match` to the canonical game name and
    upgrade `match_confidence` to `dat_verified`. Returns the number of ROMs
    newly matched.

    Args:
        scope_rom_ids: When supplied, only consider ROMs whose id is in this
            list. Out-of-scope rows are left untouched even if they would
            otherwise match. Mirrors :func:`romulus.core.hasher.hash_library`
            so a scoped Heavy Scan never touches state outside its scope.
    """
    rows = conn.execute(
        """
        SELECT r.id, r.size_bytes, h.sha1, h.crc32
        FROM roms r
        JOIN hashes h ON h.rom_id = r.id
        WHERE r.match_confidence != 'dat_verified'
        """
    ).fetchall()
    if scope_rom_ids is not None:
        allowed = frozenset(scope_rom_ids)
        rows = [r for r in rows if int(r["id"]) in allowed]
    logger.debug(
        "match_hashes: candidates rows=%d scoped=%s",
        len(rows),
        scope_rom_ids is not None,
    )

    matched = 0
    for row in rows:
        rom_id = row["id"]
        sha1 = row["sha1"]
        crc32 = row["crc32"]
        size_bytes = row["size_bytes"]

        entry = None
        if sha1:
            entry = queries.get_dat_by_sha1(conn, sha1)
        matched_by = "sha1" if entry is not None else None
        if entry is None and crc32 and size_bytes is not None:
            entry = queries.get_dat_by_crc_size(conn, crc32, size_bytes)
            if entry is not None:
                matched_by = "crc32+size"

        if entry is None:
            logger.debug(
                "match_hashes: no match rom_id=%s sha1=%s crc32=%s size=%s",
                rom_id,
                sha1,
                crc32,
                size_bytes,
            )
            continue
        logger.debug(
            "match_hashes: matched rom_id=%s by=%s game_name=%s dat_file=%s",
            rom_id,
            matched_by,
            entry["game_name"],
            entry["dat_file"],
        )
        queries.update_rom_match(conn, rom_id, entry["game_name"], "dat_verified")

        # Write identity fields derived from the canonical DAT name onto the
        # roms row so the title, region, and revision are DAT-authoritative
        # after Heavy Scan.  parse_no_intro_tokens parses the canonical game
        # name (e.g. "Super Mario World (USA) (Rev 1)") the same way the
        # filename parser would parse a user filename.
        dat_tokens = parse_no_intro_tokens(str(entry["game_name"]))
        _update_identity_from_dat(conn, rom_id, dat_tokens, entry)
        matched += 1

    conn.commit()
    logger.debug("match_hashes: complete matched=%d candidates=%d", matched, len(rows))
    return matched
