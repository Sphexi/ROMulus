"""ScreenScraper metadata client (optional, requires a free account).

Stub implementation — the full ScreenScraper API surface is large and the
session spec explicitly allows a stub here. We expose just enough to be
invoked from the orchestrator: `lookup_game` returns metadata or None, and
short-circuits cleanly when no credentials are configured.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from romulus.metadata._types import MetadataPayload

logger = logging.getLogger(__name__)

SCREENSCRAPER_BASE_URL = "https://api.screenscraper.fr/api2"
DEFAULT_TIMEOUT = 15.0
MIN_REQUEST_INTERVAL = 1.0

# Lazily-built shared rate limiter — deferred init avoids a circular import
# between this module and ``metadata/__init__.py``.
_rate_limiter: object | None = None


def _respect_rate_limit() -> None:
    """Sleep just long enough to keep request spacing >= MIN_REQUEST_INTERVAL.

    Called only from the enrich worker thread, so the module-level
    ``_rate_limiter`` state needs no lock.
    """
    global _rate_limiter
    if _rate_limiter is None:
        from romulus.metadata import RateLimiter

        _rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    # Rebind so monkeypatched ``MIN_REQUEST_INTERVAL`` is honoured per-call.
    _rate_limiter.min_interval = MIN_REQUEST_INTERVAL  # type: ignore[attr-defined]
    _rate_limiter.wait()  # type: ignore[attr-defined]


def has_credentials(credentials: dict[str, str] | None) -> bool:
    """Return True if both username and password are populated."""
    if not credentials:
        return False
    return bool(credentials.get("username")) and bool(credentials.get("password"))


def parse_screenscraper_response(payload: dict[str, Any]) -> MetadataPayload | None:
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


def test_connection(
    username: str,
    password: str,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Validate ScreenScraper credentials by hitting the user-info endpoint.

    Returns ``(ok, message)``. ``ok`` is True only when the API accepts the
    supplied credentials (HTTP 200 with a parsable JSON body containing a
    ``ssuser`` block). ``message`` is a human-readable string suitable for
    surfacing in a settings dialog. The current form values are passed in
    directly — callers should not save credentials first.
    """
    # Deferred import to dodge the circular import with metadata/__init__.py.
    from romulus.metadata import http_client

    if not username or not password:
        return False, "Enter a username and password before testing."

    params = {
        "devid": "romulus",
        "devpassword": "",
        "softname": "romulus",
        "output": "json",
        "ssid": username,
        "sspassword": password,
    }
    url = f"{SCREENSCRAPER_BASE_URL}/ssuserInfos.php"

    # Honour the same 1 req/sec spacing as bulk lookups: the settings-dialog
    # button is disabled during an in-flight request, but a user can still
    # click Test back-to-back across separate dialog opens and rate-limit
    # their own account. See security audit v0.1.0 finding #6.
    _respect_rate_limit()
    with http_client(client, timeout) as http:
        try:
            response = http.get(url, params=params)
        except httpx.HTTPError as exc:
            return False, f"Network error: {exc}"

        if response.status_code == 401 or response.status_code == 403:
            return False, "Invalid username or password."
        if response.status_code != 200:
            return False, f"Unexpected status: HTTP {response.status_code}"

        try:
            payload = response.json()
        except ValueError:
            # ScreenScraper sometimes returns non-JSON error text — treat that
            # as auth failure for our purposes.
            return False, "ScreenScraper returned a non-JSON response."

        response_block = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response_block, dict) or "ssuser" not in response_block:
            return False, "ScreenScraper did not return user info — credentials may be invalid."
        return True, "Connection successful."


def lookup_game(
    sha1: str,
    system_id: str | None,
    credentials: dict[str, str] | None,
    client: httpx.Client | None = None,
    rate_limit: bool = True,
) -> MetadataPayload | None:
    """Look up a game by SHA-1 via ScreenScraper. Returns None if disabled/miss."""
    # Deferred import to dodge the circular import with metadata/__init__.py.
    from romulus.metadata import http_client

    if not has_credentials(credentials):
        logger.debug("screenscraper lookup: skipped (no credentials)")
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
    # NOTE: credentials are intentionally NOT logged here — params dict is
    # consumed by httpx only. See security audit v0.1.0.
    logger.debug(
        "screenscraper lookup: sha1=%s system_id=%s url=%s",
        sha1.lower(),
        system_id,
        url,
    )

    with http_client(client, DEFAULT_TIMEOUT) as http:
        if rate_limit:
            _respect_rate_limit()
        try:
            response = http.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("screenscraper request failed: err=%s", exc)
            return None

        logger.debug(
            "screenscraper response: sha1=%s status=%d size=%d",
            sha1.lower(),
            response.status_code,
            len(response.content),
        )
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
        parsed = parse_screenscraper_response(payload)
        logger.debug(
            "screenscraper match: sha1=%s found=%s title=%s",
            sha1.lower(),
            parsed is not None,
            parsed.get("title") if isinstance(parsed, dict) else None,
        )
        return parsed
