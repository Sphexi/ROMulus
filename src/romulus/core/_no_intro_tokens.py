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
"""

from __future__ import annotations

import re

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
