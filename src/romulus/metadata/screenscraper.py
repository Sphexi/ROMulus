"""ScreenScraper metadata client (optional, requires a free account).

Stub implementation — the full ScreenScraper API surface is large and the
session spec explicitly allows a stub here. We expose just enough to be
invoked from the orchestrator: `lookup_game` returns metadata or None, and
short-circuits cleanly when no credentials are configured.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SCREENSCRAPER_BASE_URL: str = "https://api.screenscraper.fr/api2"
DEFAULT_TIMEOUT: float = 15.0
MIN_REQUEST_INTERVAL: float = 1.0

_last_request_ts: float = 0.0


def _respect_rate_limit() -> None:
    """Sleep just long enough to keep request spacing >= MIN_REQUEST_INTERVAL."""
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_ts = time.monotonic()


def has_credentials(credentials: dict[str, str] | None) -> bool:
    """Return True if both username and password are populated."""
    if not credentials:
        return False
    return bool(credentials.get("username")) and bool(credentials.get("password"))


def parse_screenscraper_response(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the relevant fields out of a ScreenScraper jeuInfos response."""
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    game = response.get("jeu")
    if not isinstance(game, dict):
        return None

    def _localized(field: Any) -> str | None:
        if isinstance(field, list) and field:
            first = field[0]
            if isinstance(first, dict):
                return first.get("text") or first.get("texte")
        if isinstance(field, dict):
            return field.get("text") or field.get("texte")
        if isinstance(field, str):
            return field
        return None

    return {
        "title": _localized(game.get("noms")) or game.get("nom"),
        "description": _localized(game.get("synopsis")),
        "genre": _localized(game.get("genres")),
        "developer": _localized(game.get("developpeur")),
        "publisher": _localized(game.get("editeur")),
        "release_date": _localized(game.get("dates")),
        "players": game.get("joueurs"),
        "rating": game.get("classifications"),
    }


def lookup_game(
    sha1: str,
    system_id: str | None,
    credentials: dict[str, str] | None,
    client: httpx.Client | None = None,
    rate_limit: bool = True,
) -> dict[str, Any] | None:
    """Look up a game by SHA-1 via ScreenScraper. Returns None if disabled/miss."""
    if not has_credentials(credentials):
        return None
    if not sha1:
        return None

    params = {
        "devid": "romulus",
        "devpassword": "",
        "softname": "romulus",
        "output": "json",
        "ssid": credentials["username"],
        "sspassword": credentials["password"],
        "sha1": sha1.lower(),
    }
    url = f"{SCREENSCRAPER_BASE_URL}/jeuInfos.php"

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)

    try:
        if rate_limit:
            _respect_rate_limit()
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("screenscraper request failed: err=%s", exc)
            return None

        if response.status_code != 200:
            logger.warning(
                "screenscraper unexpected status: status=%s", response.status_code
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.warning("screenscraper returned non-JSON body")
            return None
        return parse_screenscraper_response(payload)
    finally:
        if owns_client:
            client.close()
