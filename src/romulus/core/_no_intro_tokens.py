"""Shared No-Intro / Redump / TOSEC parenthesized-token tables and regexes.

Both the scanner (``core/scanner.py``) and the DAT parser
(``core/dat_parser.py``) inspect the ``(tag, group)`` segments of canonical
ROM names. Each uses a slightly different vocabulary:

* The scanner sees real-world filenames, which carry country names (USA,
  Japan), super-region tags (Europe, World), AND language codes (En, Ja, Fr)
  because users often retag for sub-region releases.
* The DAT parser only sees canonical No-Intro headers, where the ``(region)``
  segment is always a country / super-region — language codes never appear.

The two sets are kept distinct on purpose; centralizing them here documents
the why and keeps a future region addition from needing edits in two files.
The same applies to the revision-tag regex.

Public API:
    ``parse_no_intro_tokens(name)`` — parse identity fields from any No-Intro
    / Redump / TOSEC name string (filename stem or DAT canonical name). Returns
    a :class:`ParsedTokens` dataclass. Used by the scanner (filename input) and
    the Heavy Scan DAT matcher (canonical DAT name input) so both paths share
    one implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Country / super-region tokens used by both the scanner and DAT parser.
#: Lowercase for case-insensitive comparison.
REGION_COUNTRY_TOKENS: frozenset[str] = frozenset(
    {
        "usa",
        "europe",
        "japan",
        "world",
        "asia",
        "australia",
        "brazil",
        "canada",
        "china",
        "france",
        "germany",
        "italy",
        "korea",
        "netherlands",
        "spain",
        "sweden",
        "taiwan",
        "uk",
        "unknown",
        "latin america",
        "scandinavia",
        "russia",
        "hong kong",
    }
)

#: Language codes that show up in filenames (and only filenames — DAT canonical
#: names never use these). ISO 639-1 short codes, lowercase.
REGION_LANGUAGE_TOKENS: frozenset[str] = frozenset(
    {
        "en",
        "ja",
        "jp",
        "fr",
        "de",
        "es",
        "it",
        "nl",
        "pt",
        "ru",
        "ko",
        "zh",
        "sv",
        "fi",
        "no",
        "da",
        "pl",
    }
)

#: Union — every token the filename parser must recognize.
FILENAME_REGION_TOKENS: frozenset[str] = REGION_COUNTRY_TOKENS | REGION_LANGUAGE_TOKENS

#: Revision-tag regex: matches ``(Rev 1)``, ``(v1.0)``, ``(1.2a)`` etc. inside
#: the parenthesized body. Used identically by the scanner and DAT parser.
REVISION_RE: re.Pattern[str] = re.compile(
    r"^(rev\s+\S+|v\d+(\.\d+[a-z]?)?|\d+\.\d+[a-z]?)$", re.IGNORECASE
)

# Hack / homebrew bracket tags (GoodTools vocabulary).
_HACK_BRACKET_RE: re.Pattern[str] = re.compile(r"^h\d*$", re.IGNORECASE)
_HOMEBREW_PAREN_TOKENS: frozenset[str] = frozenset({"homebrew", "aftermarket"})

# Compiled regex for ``(tag)`` and ``[tag]`` groups.
_TAG_GROUP_RE: re.Pattern[str] = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")


# ---------------------------------------------------------------------------
# ParsedTokens — public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedTokens:
    """Identity fields extracted from a No-Intro / Redump / TOSEC name string.

    Returned by :func:`parse_no_intro_tokens`.  Used both by the scanner
    (receives filename stems) and by the Heavy-Scan DAT matcher (receives
    canonical DAT game names) so both paths share one parsing implementation.

    Attributes:
        title: Name with all ``(tag)`` / ``[tag]`` groups stripped, whitespace
            collapsed.  For a filename this is the clean display title.
        region: First recognised region/country tag (e.g. ``"USA"``,
            ``"Europe"``), or None.
        revision: First recognised revision/version tag (e.g. ``"Rev 1"``,
            ``"v1.1"``), or None.
        is_hack: True when a GoodTools ``[h]`` / ``[h1]`` tag is present.
        is_homebrew: True when a ``(Homebrew)`` or ``(Aftermarket)`` tag is
            present.
    """

    title: str
    region: str | None
    revision: str | None
    is_hack: bool
    is_homebrew: bool


def parse_no_intro_tokens(name: str) -> ParsedTokens:
    """Parse identity fields from a No-Intro / Redump / TOSEC name string.

    Accepts either a bare filename stem (``"Super Mario World (USA) (Rev 1)"``)
    or a DAT canonical game name (same format).  The function never touches the
    file extension — strip it before calling if the input is a full filename.

    Region and revision parsing uses the same tables as the full
    :func:`romulus.core.scanner.parse_filename` parser; only the subset of
    fields shared between the scanner path and the DAT-match path is returned.

    Args:
        name: Filename stem or DAT game name to parse.

    Returns:
        A :class:`ParsedTokens` instance with the extracted identity fields.
    """
    region: str | None = None
    revision: str | None = None
    is_hack = False
    is_homebrew = False

    for match in _TAG_GROUP_RE.finditer(name):
        paren_content = match.group(1)
        bracket_content = match.group(2)

        if bracket_content is not None:
            lower = bracket_content.strip().lower()
            if _HACK_BRACKET_RE.match(lower):
                is_hack = True
        elif paren_content is not None:
            stripped = paren_content.strip()
            lower = stripped.lower()

            # Revision check first (before region — "Rev 2" is not a region).
            if REVISION_RE.match(stripped) and revision is None:
                revision = stripped
                continue

            # Homebrew / aftermarket.
            if lower in _HOMEBREW_PAREN_TOKENS:
                is_homebrew = True
                continue

            # Region: every comma-separated token must be a known region code.
            parts = [p.strip().lower() for p in stripped.split(",") if p.strip()]
            if parts and all(
                p in REGION_COUNTRY_TOKENS or p in REGION_LANGUAGE_TOKENS
                for p in parts
            ) and region is None:
                region = stripped

    # Strip all tag groups and collapse whitespace for the clean title.
    title = _TAG_GROUP_RE.sub("", name)
    title = " ".join(title.split()).strip()
    # Remove any trailing separator chars left after stripping parens.
    title = title.rstrip(" -_")

    return ParsedTokens(
        title=title,
        region=region,
        revision=revision,
        is_hack=is_hack,
        is_homebrew=is_homebrew,
    )
