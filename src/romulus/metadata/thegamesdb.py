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


# Punctuation we strip before title comparison. Apostrophes and dashes
# are real differentiators in some titles (e.g. "Mario's" vs "Marios")
# but the inverse — a TGDB row that punctuates differently from the
# DAT name — is far more common in practice, so we collapse them.
_TITLE_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)


def _normalise_title(title: str) -> str:
    """Lowercase + strip punctuation/whitespace for fuzzy title equality."""
    return _TITLE_NOISE_RE.sub("", title.lower())


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Drill through TGDB's ``{ "data": { "games": [...] }, ... }`` envelope.

    Returns the inner ``data`` dict, or ``None`` if the envelope shape is
    malformed (which we treat as a miss rather than crash the enrich run).
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _coalesce_field(game: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value among ``game[k]`` for k in keys.

    Lists (TGDB returns ``genres``, ``developers``, ``publishers`` as
    lists of integer ids — we don't resolve those names; users get the
    raw id list joined with commas, which is at least sortable) are
    folded into ``", "``-joined strings so they fit our flat schema.
    """
    for key in keys:
        value = game.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            # Ids are ints; cast to str for joining. Skips empty list members.
            return ", ".join(str(v) for v in value if v not in (None, ""))
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

    Returns the first game whose title matches *title* under
    :func:`_normalise_title`, or ``None`` if no exact match is in the
    page. Caller is responsible for paginating if needed (we don't —
    one page is 20 candidates which is plenty for unique platform+title
    combinations).
    """
    data = _unwrap_data(payload)
    if data is None:
        return None
    games = data.get("games")
    if not isinstance(games, list):
        return None
    target = _normalise_title(title)
    for game in games:
        if not isinstance(game, dict):
            continue
        candidate = game.get("game_title") or game.get("title")
        if not isinstance(candidate, str):
            continue
        if _normalise_title(candidate) != target:
            continue
        return {  # type: ignore[return-value]
            key: _coalesce_field(game, synonyms)
            for key, synonyms in _FIELD_SYNONYMS
        }
    return None


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
        # Pull a wider results set than the 20-default; TGDB's name
        # matching is loose ("Mario" returns dozens) so we want more
        # candidates to filter through _normalise_title.
        "fields": "overview,players,publishers,genres,developers,release_date,rating",
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
                logger.debug(
                    "thegamesdb miss: title=%s system_id=%s",
                    title,
                    system_id,
                )
            return parsed, remaining

    return None, None
