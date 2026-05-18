"""TheGamesDB v1 metadata client.

Free JSON API with monthly per-IP request budget. Strategy this module
encodes:

* The user supplies their own API key (TGDB ToS — don't embed a shared
  one). Without a key configured, :func:`lookup_game` no-ops and returns
  ``None``.
* Calls are spaced with the shared :class:`RateLimiter` — same 1 req/s
  cadence as :mod:`romulus.metadata.hasheous`. The TGDB ToS only document
  a *monthly* allowance (1000 for public keys, 6000 lifetime for private),
  so the per-second pacing here is purely "be polite" — the real guard is
  the monthly tracker below.
* Every response carries ``remaining_monthly_allowance``; we log it on
  every call and short-circuit further calls once it drops to zero.
* Identity matching is name + platform. TGDB's ``Games/ByGameName``
  accepts a ``filter[platform]`` parameter; we look the platform id up
  from a small built-in map keyed by ROMulus system id (see
  :data:`SYSTEM_TO_TGDB_PLATFORM`). Systems with no mapping skip TGDB.
* Results are scored by exact title match (case + punctuation
  normalised). A pass that doesn't surface an exact match is treated as
  a miss — fuzzy fallbacks would burn quota chasing wrong games.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from romulus.metadata._types import MetadataPayload

logger = logging.getLogger(__name__)

THEGAMESDB_BASE_URL = "https://api.thegamesdb.net/v1"
DEFAULT_TIMEOUT = 15.0
MIN_REQUEST_INTERVAL = 1.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.0

# ROMulus system id -> TheGamesDB platform id. Covers the systems with
# both a bundled DAT and a TGDB platform entry. Built from
# https://api.thegamesdb.net/v1/Platforms (manual lookup, 2026-05); add
# entries here when extending the registry. Systems absent from this map
# get skipped at lookup time — TGDB isn't a meaningful source without
# the platform filter (cross-platform title collisions are common).
SYSTEM_TO_TGDB_PLATFORM: dict[str, int] = {
    # Nintendo
    "nes": 7,
    "snes": 6,
    "n64": 3,
    "n64dd": 3,
    "gamecube": 2,
    "wii": 9,
    "wiiu": 38,
    "virtualboy": 4918,
    "gb": 4,
    "gbc": 41,
    "gba": 5,
    "nds": 8,
    "n3ds": 4912,
    "dsiware": 4914,  # DSiWare maps to TGDB's Nintendo DSi
    "pokemini": 4957,
    # Sega
    "megadrive": 36,
    "mastersystem": 35,
    "gamegear": 20,
    "saturn": 17,
    "dreamcast": 16,
    "sega32x": 33,
    "sg1000": 4949,
    "segapico": 4958,
    # Sony
    "psx": 10,
    "ps3": 12,
    "psp": 13,
    "psvita": 39,
    # Microsoft
    "xbox360": 15,
    # Atari
    "atari2600": 22,
    "atari5200": 26,
    "atari7800": 27,
    "jaguar": 28,
    "lynx": 4924,
    "atarist": 4937,
    # NEC
    "pcengine": 34,
    "pcenginecd": 4955,
    "supergrafx": 4951,
    # SNK
    "neogeo": 24,
    "ngp": 4922,
    "ngpc": 4923,
    # Bandai
    "wonderswan": 4925,
    "wonderswancolor": 4926,
    # Home computers
    "msx": 4929,
    "amiga": 4911,
    "c64": 40,
    "vic20": 4945,
    "zxspectrum": 4913,
    "amstradcpc": 4914,
    # Arcade
    "mame": 23,
    "fbneo": 23,
    # Classics & niche
    "colecovision": 31,
    "intellivision": 32,
    "vectrex": 4939,
    "odyssey2": 4927,
    "channelf": 4928,
    "gamecom": 4940,
    "supervision": 4959,
}


# Same field-synonym pattern as hasheous.py — TGDB returns a single shape
# but the wrapper format may shift across API versions.
_FIELD_SYNONYMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", ("game_title", "title")),
    ("description", ("overview", "description")),
    ("genre", ("genres", "genre")),
    ("developer", ("developers", "developer")),
    ("publisher", ("publishers", "publisher")),
    ("release_date", ("release_date",)),
    ("players", ("players",)),
    ("rating", ("rating",)),
)


# Lazily-built shared rate limiter (mirrors hasheous.py).
_rate_limiter: object | None = None


def _respect_rate_limit() -> None:
    """Sleep just long enough to keep request spacing >= MIN_REQUEST_INTERVAL.

    See ``hasheous._respect_rate_limit`` — same pattern; same reason the
    re-bind on every call is intentional (test monkeypatching).
    """
    global _rate_limiter
    if _rate_limiter is None:
        from romulus.metadata import RateLimiter

        _rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    _rate_limiter.min_interval = MIN_REQUEST_INTERVAL  # type: ignore[attr-defined]
    _rate_limiter.wait()  # type: ignore[attr-defined]


# Parenthesised / bracketed segments stripped from the title before
# normalising. Catches all of No-Intro ``(USA)``, ``(Disc 1)``,
# language tags ``(En,Fr,De)``, GoodTools ``[!]``, TOSEC ``[demo]`` etc
# in one sweep. The DAT-derived game title carries these tags, but TGDB
# titles never do.
_PARENTHESISED_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*", re.UNICODE)

# Punctuation we strip before title comparison. Apostrophes and dashes
# are real differentiators in some titles (e.g. "Mario's" vs "Marios")
# but the inverse — a TGDB row that punctuates differently from the
# DAT name — is far more common in practice, so we collapse them.
_TITLE_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)

# Minimum normalised-length for the substring fallback to fire. Short
# titles like "Tetris" (6 chars) would substring-match into "Tetris
# Worlds", "New Tetris", "Tetris Plus" etc — wrong games. 12 chars is
# enough to make a "the candidate contains our query" rule safe.
_SUBSTRING_FALLBACK_MIN_LEN = 12


def _normalise_title(title: str) -> str:
    """Lowercase + strip parenthesised tags + strip punctuation/whitespace.

    Run the parenthesised-segment strip first so the punctuation removal
    that follows doesn't fuse the inside of e.g. ``(USA)`` with the
    surrounding title text (``Super Mario World (USA)`` ->
    ``Super Mario World `` -> ``supermarioworld`` rather than
    ``supermarioworldusa``).
    """
    stripped = _PARENTHESISED_RE.sub(" ", title)
    return _TITLE_NOISE_RE.sub("", stripped.lower())


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Drill through TGDB's ``{ "data": { "games": [...] }, ... }`` envelope.

    Returns the inner ``data`` dict, or ``None`` if the envelope shape is
    malformed (which we treat as a miss rather than crash the enrich run).
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data


# Map from our flat metadata key -> (TGDB field for ids, the matching
# include-block key under data.include or top-level include). TGDB's
# response shape varies between API versions; we tolerate both.
_LIST_FIELD_INCLUDES: dict[str, tuple[str, str]] = {
    "genre": ("genres", "genres"),
    "developer": ("developers", "developers"),
    "publisher": ("publishers", "publishers"),
}


def _extract_include_lookup(
    payload: dict[str, Any], data: dict[str, Any], include_key: str
) -> dict[str, str]:
    """Build an ``{id_str -> name}`` table for one include block.

    TGDB has nested the include block under both ``data.include.<key>``
    (older) and top-level ``include.<key>`` (newer) at various points;
    we accept either. Returns an empty dict when the block isn't
    present — that's normal when the caller didn't request includes.
    """
    candidates: list[Any] = [
        data.get("include"),
        payload.get("include"),
    ]
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        block = raw.get(include_key)
        if not isinstance(block, dict):
            continue
        # TGDB nests the real lookup under ``data:`` within each block.
        inner = block.get("data") if isinstance(block.get("data"), dict) else block
        if not isinstance(inner, dict):
            continue
        result: dict[str, str] = {}
        for key, entry in inner.items():
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    result[str(key)] = name
        if result:
            return result
    return {}


def _coalesce_field(
    game: dict[str, Any],
    keys: tuple[str, ...],
    *,
    name_lookup: dict[str, str] | None = None,
) -> Any:
    """Return the first non-empty value among ``game[k]`` for k in keys.

    Lists of integer ids (TGDB returns genres/developers/publishers this
    way) are resolved through ``name_lookup`` when one is supplied —
    that's how we turn "8, 12" into "Platformer, Adventure". When no
    lookup is given, or an id isn't in the table, we fall back to the
    raw id string so the row still carries *something* identifiable.
    """
    for key in keys:
        value = game.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            parts: list[str] = []
            for v in value:
                if v in (None, ""):
                    continue
                resolved = name_lookup.get(str(v)) if name_lookup else None
                parts.append(resolved if resolved else str(v))
            return ", ".join(parts) if parts else None
        return value
    return None


def _log_remaining_allowance(payload: dict[str, Any]) -> int | None:
    """Pull the remaining-allowance counter out of a TGDB envelope.

    TGDB reports it at the top level as ``remaining_monthly_allowance``
    AND inside ``pages``/``include`` for some endpoints. We accept either.
    Logs the value (INFO when low, DEBUG otherwise) and returns it so the
    orchestrator can persist or short-circuit.
    """
    remaining = payload.get("remaining_monthly_allowance")
    if not isinstance(remaining, int):
        return None
    if remaining <= 10:
        logger.warning("thegamesdb allowance low: remaining=%d", remaining)
    else:
        logger.debug("thegamesdb allowance: remaining=%d", remaining)
    return remaining


def parse_response(payload: dict[str, Any], title: str) -> MetadataPayload | None:
    """Extract one game's metadata from a Games/ByGameName response.

    Matching is two-phase: an exact normalised-equality pass, then a
    substring fallback (TGDB candidate normalised *contains* our query
    normalised, or vice versa) for titles long enough that the
    substring rule is safe — see :data:`_SUBSTRING_FALLBACK_MIN_LEN`.

    The substring pass exists because TGDB often carries series-prefix
    names (``"James Bond 007 - Everything or Nothing"``) while ROMulus
    has the disc-tin name (``"007 - Everything or Nothing"``); without
    it the exact pass misses every such pair.

    Returns ``None`` on any envelope shape mismatch — treated as a miss
    rather than an error to keep the enrich run resilient.
    """
    data = _unwrap_data(payload)
    if data is None:
        return None
    games = data.get("games")
    if not isinstance(games, list):
        return None

    target = _normalise_title(title)

    # Build the candidate list once, with both raw + normalised forms.
    candidates: list[tuple[dict[str, Any], str]] = []
    for game in games:
        if not isinstance(game, dict):
            continue
        title_raw = game.get("game_title") or game.get("title")
        if not isinstance(title_raw, str):
            continue
        candidates.append((game, _normalise_title(title_raw)))

    matched: dict[str, Any] | None = None
    for game, candidate_norm in candidates:
        if candidate_norm == target:
            matched = game
            break

    if matched is None and len(target) >= _SUBSTRING_FALLBACK_MIN_LEN:
        for game, candidate_norm in candidates:
            if len(candidate_norm) < _SUBSTRING_FALLBACK_MIN_LEN:
                continue
            if target in candidate_norm or candidate_norm in target:
                matched = game
                logger.debug(
                    "thegamesdb substring match: query=%r candidate=%r",
                    title,
                    game.get("game_title") or game.get("title"),
                )
                break

    if matched is None:
        return None

    # Build the include lookup tables once per response. Empty when the
    # caller didn't request them via ``include=`` — in which case
    # _coalesce_field falls back to raw ids.
    name_lookups: dict[str, dict[str, str]] = {
        key: _extract_include_lookup(payload, data, include_key)
        for key, (_field, include_key) in _LIST_FIELD_INCLUDES.items()
    }

    result: MetadataPayload = {}  # type: ignore[typeddict-item]
    for key, synonyms in _FIELD_SYNONYMS:
        result[key] = _coalesce_field(  # type: ignore[literal-required]
            matched,
            synonyms,
            name_lookup=name_lookups.get(key),
        )
    return result


def lookup_game(
    title: str,
    system_id: str | None,
    apikey: str | None,
    *,
    client: httpx.Client | None = None,
    rate_limit: bool = True,
) -> tuple[MetadataPayload | None, int | None]:
    """Look up TGDB metadata for one (title, system_id) pair.

    Returns a ``(payload, remaining_allowance)`` tuple. ``payload`` is
    ``None`` on miss, network error, malformed response, or any case
    where the platform is not in :data:`SYSTEM_TO_TGDB_PLATFORM`.
    ``remaining_allowance`` is the value from the TGDB response when
    available, or ``None`` when it wasn't reported or the call never
    happened (e.g. missing apikey / unmapped platform).

    The orchestrator uses ``remaining_allowance`` to decide whether to
    keep calling TGDB on subsequent games this run.
    """
    from romulus.metadata import http_client

    if not apikey:
        logger.debug("thegamesdb skipped: no apikey configured")
        return None, None
    if not title:
        return None, None
    platform_id = SYSTEM_TO_TGDB_PLATFORM.get(system_id or "")
    if platform_id is None:
        logger.debug(
            "thegamesdb skipped: no platform mapping for system_id=%s",
            system_id,
        )
        return None, None

    url = f"{THEGAMESDB_BASE_URL}/Games/ByGameName"
    params = {
        "apikey": apikey,
        "name": title,
        # TGDB accepts a comma-separated platform id list under
        # ``filter[platform]``; we only ever filter on one.
        "filter[platform]": str(platform_id),
        # Request every per-game field we ever surface in the UI.
        "fields": "overview,players,publishers,genres,developers,release_date,rating",
        # ``include`` returns the lookup tables for the integer-id
        # fields (genres/developers/publishers) alongside the games
        # payload — without it those fields are unusable raw ids.
        "include": "boxart,platform,Genres,Developers,Publishers",
    }
    logger.debug(
        "thegamesdb lookup: title=%s system_id=%s platform_id=%d",
        title,
        system_id,
        platform_id,
    )

    with http_client(client, DEFAULT_TIMEOUT) as http:
        for attempt in range(MAX_RETRIES):
            if rate_limit:
                _respect_rate_limit()
            try:
                response = http.get(url, params=params)
            except httpx.HTTPError as exc:
                logger.warning("thegamesdb request failed: err=%s", exc)
                return None, None

            logger.debug(
                "thegamesdb response: status=%d attempt=%d",
                response.status_code,
                attempt + 1,
            )
            if response.status_code == 403:
                logger.warning("thegamesdb 403 — apikey invalid or quota exhausted")
                return None, 0
            if response.status_code == 429:
                wait = BACKOFF_BASE * (2**attempt)
                logger.info("thegamesdb rate-limited, backing off %.1fs", wait)
                import time

                time.sleep(wait)
                continue
            if response.status_code != 200:
                logger.warning(
                    "thegamesdb unexpected status: status=%s body=%s",
                    response.status_code,
                    response.text[:200],
                )
                return None, None

            try:
                payload = response.json()
            except ValueError:
                logger.warning("thegamesdb returned non-JSON body")
                return None, None
            if not isinstance(payload, dict):
                return None, None
            remaining = _log_remaining_allowance(payload)
            parsed = parse_response(payload, title)
            if parsed:
                logger.info(
                    "thegamesdb match: title=%s system_id=%s",
                    title,
                    system_id,
                )
            else:
                # Sample the first few candidate titles so the user can
                # see *why* it didn't match (typically a series-prefix
                # mismatch we couldn't auto-resolve).
                data = payload.get("data")
                sample: list[str] = []
                if isinstance(data, dict):
                    raw_games = data.get("games")
                    if isinstance(raw_games, list):
                        for g in raw_games[:3]:
                            if isinstance(g, dict):
                                t = g.get("game_title") or g.get("title")
                                if isinstance(t, str):
                                    sample.append(t)
                logger.info(
                    "thegamesdb miss: title=%s system_id=%s candidates=%s",
                    title,
                    system_id,
                    sample,
                )
            return parsed, remaining

    return None, None
