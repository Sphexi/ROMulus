"""LaunchBox XML metadata client.

Offline fallback. LaunchBox publishes a downloadable XML database of game
metadata (description / genre / developer / publisher / release date / rating).
We parse it once into in-memory structures and match by (system_id, title).

Uses ``defusedxml.ElementTree`` for the parsing entry point (``iterparse``) so
a malicious LaunchBox-style XML can't trigger a billion-laughs /
quadratic-blowup entity-expansion DoS (see security audit v0.1.0 finding #3).
``defusedxml.ElementTree`` does NOT re-export ``Element``, so type
annotations for already-parsed nodes still use ``xml.etree.ElementTree`` —
that is safe because no parser entry point goes through the stdlib module
anymore.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from defusedxml.ElementTree import iterparse as safe_iterparse

from romulus.metadata._types import MetadataPayload

logger = logging.getLogger(__name__)

# Index keyed by (system_id, normalized_title) for fast lookups.
type LaunchBoxIndex = dict[tuple[str | None, str], "LaunchBoxEntry"]

# LaunchBox uses its own platform names — map a handful of common ones to our
# canonical system ids. Anything not in this map is left as-is (matching falls
# back to title-only across all systems).
_LAUNCHBOX_PLATFORM_MAP: dict[str, str] = {
    "Nintendo Entertainment System": "nes",
    "Super Nintendo Entertainment System": "snes",
    "Nintendo 64": "n64",
    "Nintendo GameCube": "gamecube",
    "Nintendo Game Boy": "gb",
    "Nintendo Game Boy Color": "gbc",
    "Nintendo Game Boy Advance": "gba",
    "Nintendo DS": "nds",
    "Nintendo Virtual Boy": "virtualboy",
    "Sega Genesis": "megadrive",
    "Sega Mega Drive": "megadrive",
    "Sega Master System": "mastersystem",
    "Sega Game Gear": "gamegear",
    "Sega Saturn": "saturn",
    "Sega Dreamcast": "dreamcast",
    "Sega 32X": "sega32x",
    "Sony Playstation": "psx",
    "Sony PSP": "psp",
    "Atari 2600": "atari2600",
    "Atari 7800": "atari7800",
    "Atari Lynx": "lynx",
    "NEC TurboGrafx-16": "pcengine",
    "NEC TurboGrafx-CD": "pcenginecd",
    "SNK Neo Geo Pocket": "ngp",
    "SNK Neo Geo Pocket Color": "ngpc",
    "MAME": "mame",
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class LaunchBoxEntry:
    """One game row from the LaunchBox XML database."""

    title: str
    system_id: str | None
    description: str | None
    genre: str | None
    developer: str | None
    publisher: str | None
    release_date: str | None
    players: str | None
    rating: str | None


def _text(element: ET.Element, tag: str) -> str | None:
    """Return the stripped text of a child element, or None if absent/empty."""
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _normalize_title(title: str) -> str:
    """Lowercase + strip non-alphanumeric for tolerant matching."""
    return _NORMALIZE_RE.sub("", title.lower())


def parse_launchbox_xml(xml_path: Path | str) -> list[LaunchBoxEntry]:
    """Parse a LaunchBox Metadata.xml into LaunchBoxEntry rows.

    The real LaunchBox XML is ~200 MB; we use iterparse so memory stays bounded.
    Only `<Game>` elements are extracted — other top-level elements are skipped.
    """
    path = Path(xml_path)
    logger.debug("launchbox parse: start path=%s", path)
    entries: list[LaunchBoxEntry] = []
    skipped_no_title = 0
    unmapped_platforms: set[str] = set()
    for _event, element in safe_iterparse(str(path), events=("end",)):
        if element.tag != "Game":
            continue
        title = _text(element, "Name")
        if not title:
            skipped_no_title += 1
            element.clear()
            continue
        platform = _text(element, "Platform")
        system_id = _LAUNCHBOX_PLATFORM_MAP.get(platform or "")
        if platform and system_id is None:
            unmapped_platforms.add(platform)
        entries.append(
            LaunchBoxEntry(
                title=title,
                system_id=system_id,
                description=_text(element, "Overview"),
                genre=_text(element, "Genres"),
                developer=_text(element, "Developer"),
                publisher=_text(element, "Publisher"),
                release_date=_text(element, "ReleaseDate"),
                players=_text(element, "MaxPlayers"),
                rating=_text(element, "ESRB"),
            )
        )
        element.clear()
    logger.debug(
        "launchbox parse: done path=%s entries=%d skipped_no_title=%d "
        "unmapped_platforms=%d",
        path,
        len(entries),
        skipped_no_title,
        len(unmapped_platforms),
    )
    if unmapped_platforms:
        logger.debug(
            "launchbox parse: unmapped platforms=%s",
            sorted(unmapped_platforms),
        )
    return entries


def build_index(entries: list[LaunchBoxEntry]) -> LaunchBoxIndex:
    """Index entries by (system_id, normalized_title) for O(1) match lookups."""
    return {(e.system_id, _normalize_title(e.title)): e for e in entries}


def match_game(
    title: str,
    system_id: str | None,
    index: LaunchBoxIndex,
) -> LaunchBoxEntry | None:
    """Look up a LaunchBox entry by normalized title within a system.

    Tries (system_id, title) first, then falls back to a title-only match
    (system_id=None) — handy when the LaunchBox platform doesn't map cleanly.
    """
    normalized = _normalize_title(title)
    entry = index.get((system_id, normalized))
    if entry is not None:
        logger.debug(
            "launchbox match: title=%s system_id=%s via=system_specific",
            title,
            system_id,
        )
        return entry
    fallback = index.get((None, normalized))
    if fallback is not None:
        logger.debug(
            "launchbox match: title=%s system_id=%s via=title_only_fallback",
            title,
            system_id,
        )
    else:
        logger.debug(
            "launchbox match: title=%s system_id=%s -> no match",
            title,
            system_id,
        )
    return fallback


def entry_to_metadata(entry: LaunchBoxEntry) -> MetadataPayload:
    """Convert a LaunchBoxEntry to the dict shape expected by upsert_metadata."""
    return {
        "description": entry.description,
        "genre": entry.genre,
        "developer": entry.developer,
        "publisher": entry.publisher,
        "release_date": entry.release_date,
        "players": entry.players,
        "rating": entry.rating,
    }
