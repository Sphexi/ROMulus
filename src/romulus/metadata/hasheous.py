"""Hasheous metadata client.

Free REST API, no key. Looks up game metadata by SHA-1 (or CRC32/MD5).
Rate-limited politely: 1 request/second, with exponential backoff on 429.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HASHEOUS_BASE_URL: str = "https://hasheous.org/api/v1/lookup"
DEFAULT_TIMEOUT: float = 15.0
MIN_REQUEST_INTERVAL: float = 1.0
MAX_RETRIES: int = 3
BACKOFF_BASE: float = 1.0

_last_request_ts: float = 0.0


def _respect_rate_limit() -> None:
    """Sleep just long enough to keep request spacing >= MIN_REQUEST_INTERVAL."""
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_ts = time.monotonic()


def parse_hasheous_response(payload: dict[str, Any]) -> dict[str, Any]:
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

    return {
        "title": _first("title", "name"),
        "description": _first("description", "summary", "overview"),
        "genre": _first("genre", "genres"),
        "developer": _first("developer", "developers"),
        "publisher": _first("publisher", "publishers"),
        "release_date": _first("release_date", "first_release_date", "released"),
        "players": _first("players", "max_players"),
        "rating": _first("rating", "esrb"),
    }


def lookup_by_hash(
    sha1: str,
    hash_type: str = "sha1",
    client: httpx.Client | None = None,
    rate_limit: bool = True,
) -> dict[str, Any] | None:
    """Look up metadata by hash. Returns parsed dict, or None on miss/error."""
    if not sha1:
        return None
    url = f"{HASHEOUS_BASE_URL}/{hash_type}/{sha1.lower()}"

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)

    try:
        for attempt in range(MAX_RETRIES):
            if rate_limit:
                _respect_rate_limit()
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("hasheous request failed: url=%s err=%s", url, exc)
                return None

            if response.status_code == 404:
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
                return None
            return parse_hasheous_response(payload)
    finally:
        if owns_client:
            client.close()

    return None
