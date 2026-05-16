"""Hasheous metadata client.

Free REST API, no key. Looks up game metadata by SHA-1 (or CRC32/MD5).
Rate-limited politely: 1 request/second, with exponential backoff on 429.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from romulus.metadata._types import MetadataPayload

logger = logging.getLogger(__name__)

HASHEOUS_BASE_URL = "https://hasheous.org/api/v1/lookup"
DEFAULT_TIMEOUT = 15.0
MIN_REQUEST_INTERVAL = 1.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.0

# Declarative mapping: each output key paired with the synonym keys the
# Hasheous response may carry it under. Adding a new synonym is a one-line edit.
_FIELD_SYNONYMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", ("title", "name")),
    ("description", ("description", "summary", "overview")),
    ("genre", ("genre", "genres")),
    ("developer", ("developer", "developers")),
    ("publisher", ("publisher", "publishers")),
    ("release_date", ("release_date", "first_release_date", "released")),
    ("players", ("players", "max_players")),
    ("rating", ("rating", "esrb")),
)

# Defense-in-depth: reject any hash value that isn't a hex string of a
# plausible length before we interpolate it into the lookup URL. SHA-1 is 40
# chars, MD5 is 32, CRC32 is 8. Anything else is either malformed or a path
# traversal attempt and should never reach the network.
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_VALID_HASH_LENGTHS = (8, 32, 40)


def _is_valid_hash(value: str) -> bool:
    """True if `value` looks like a CRC32 / MD5 / SHA-1 hex digest."""
    lowered = value.lower()
    return bool(_HEX_RE.match(lowered)) and len(lowered) in _VALID_HASH_LENGTHS


# Lazily-built shared rate limiter — deferred init avoids a circular import
# between this module and ``metadata/__init__.py`` (which holds the class).
_rate_limiter: object | None = None


def _respect_rate_limit() -> None:
    """Sleep just long enough to keep request spacing >= MIN_REQUEST_INTERVAL.

    Reads ``MIN_REQUEST_INTERVAL`` at call time so test code that monkey-
    patches it to ``0.0`` (see ``tests/test_metadata.py::TestLookupByHash``)
    works without further changes. Called only from the enrich worker thread,
    so the module-level ``_rate_limiter`` state needs no lock.
    """
    global _rate_limiter
    if _rate_limiter is None:
        from romulus.metadata import RateLimiter

        _rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    # Rebind so monkeypatched ``MIN_REQUEST_INTERVAL`` is honoured per-call.
    _rate_limiter.min_interval = MIN_REQUEST_INTERVAL  # type: ignore[attr-defined]
    _rate_limiter.wait()  # type: ignore[attr-defined]


def parse_hasheous_response(payload: dict[str, Any]) -> MetadataPayload:
    """Pluck the metadata fields we care about out of a Hasheous JSON body.

    Tolerates either a flat structure or one nested under "game"/"data".
    Unknown fields are ignored; missing fields return None.
    """
    game = payload
    for wrapper in ("game", "data", "result"):
        if isinstance(game.get(wrapper), dict):
            game = game[wrapper]
            break

    def _first(*keys: str) -> Any:
        for key in keys:
            value = game.get(key)
            if value not in (None, ""):
                return value
        return None

    return {key: _first(*synonyms) for key, synonyms in _FIELD_SYNONYMS}  # type: ignore[return-value]


def lookup_by_hash(
    sha1: str,
    hash_type: str = "sha1",
    client: httpx.Client | None = None,
    rate_limit: bool = True,
) -> MetadataPayload | None:
    """Look up metadata by hash. Returns parsed dict, or None on miss/error."""
    # Deferred import to avoid a circular import at module load time
    # (metadata/__init__.py imports this module).
    from romulus.metadata import http_client

    if not sha1:
        return None
    if not _is_valid_hash(sha1):
        logger.warning("hasheous lookup rejected: malformed hash value")
        return None
    url = f"{HASHEOUS_BASE_URL}/{hash_type}/{sha1.lower()}"
    logger.debug(
        "hasheous lookup: hash_type=%s hash=%s url=%s",
        hash_type,
        sha1.lower(),
        url,
    )

    with http_client(client, DEFAULT_TIMEOUT) as http:
        for attempt in range(MAX_RETRIES):
            if rate_limit:
                _respect_rate_limit()
            try:
                response = http.get(url)
            except httpx.HTTPError as exc:
                logger.warning("hasheous request failed: url=%s err=%s", url, exc)
                return None

            logger.debug(
                "hasheous response: url=%s status=%d attempt=%d",
                url,
                response.status_code,
                attempt + 1,
            )
            if response.status_code == 404:
                logger.debug("hasheous miss: hash=%s", sha1.lower())
                return None
            if response.status_code == 429:
                wait = BACKOFF_BASE * (2**attempt)
                logger.info("hasheous rate-limited, backing off %.1fs", wait)
                time.sleep(wait)
                continue
            if response.status_code != 200:
                logger.warning(
                    "hasheous unexpected status: url=%s status=%s",
                    url,
                    response.status_code,
                )
                return None

            try:
                payload = response.json()
            except ValueError:
                logger.warning("hasheous returned non-JSON body for %s", url)
                return None
            if not isinstance(payload, dict):
                logger.debug(
                    "hasheous: response is not a dict url=%s type=%s",
                    url,
                    type(payload).__name__,
                )
                return None
            parsed = parse_hasheous_response(payload)
            logger.debug(
                "hasheous match: hash=%s title=%s",
                sha1.lower(),
                parsed.get("title") if isinstance(parsed, dict) else None,
            )
            return parsed

    return None
