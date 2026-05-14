"""libretro-thumbnails cover art client.

Free, no API key. Downloads PNG boxart/snap/title images keyed by the
canonical No-Intro game name. 404 means "no cover available" and is treated
as a non-error skip.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

THUMBNAIL_BASE_URL: str = "https://thumbnails.libretro.com"
COVER_TYPES: tuple[str, ...] = ("Named_Boxarts", "Named_Snaps", "Named_Titles")
DEFAULT_TIMEOUT: float = 15.0

# Characters libretro-thumbnails replaces with `_` in the filename portion of
# the URL. From the session-6 spec: `&*/:\<>?\|"`. After de-duplicating the
# `\` that appears twice in the spec, the unique set is the 10 chars below.
_SANITIZE_CHARS: str = '&*/:\\<>?|"'


def sanitize_game_name(name: str) -> str:
    """Replace characters libretro-thumbnails forbids with underscores."""
    result = name
    for ch in _SANITIZE_CHARS:
        result = result.replace(ch, "_")
    return result


def build_thumbnail_url(libretro_name: str, game_name: str, cover_type: str) -> str:
    """Construct the libretro-thumbnails URL for a (system, game, type) triple."""
    if cover_type not in COVER_TYPES:
        raise ValueError(f"unknown cover_type: {cover_type!r}")
    system_part = quote(libretro_name, safe="")
    game_part = quote(sanitize_game_name(game_name), safe="")
    return f"{THUMBNAIL_BASE_URL}/{system_part}/{cover_type}/{game_part}.png"


def cover_cache_path(
    cache_dir: Path | str,
    system_id: str,
    cover_type: str,
    game_name: str,
) -> Path:
    """Compute the on-disk path for a cached cover image."""
    safe_name = sanitize_game_name(game_name)
    return Path(cache_dir) / system_id / cover_type / f"{safe_name}.png"


def fetch_cover(
    libretro_name: str,
    system_id: str,
    game_name: str,
    cover_type: str,
    cache_dir: Path | str,
    client: httpx.Client | None = None,
) -> tuple[Path, str] | None:
    """Download a cover PNG and write it to the cache.

    Returns (local_path, source_url) on success, None on 404 / network error.
    Skips the download (and returns the existing path) if the file is already
    on disk — covers are immutable, no need to refetch.
    """
    url = build_thumbnail_url(libretro_name, game_name, cover_type)
    dest = cover_cache_path(cache_dir, system_id, cover_type, game_name)
    if dest.exists() and dest.stat().st_size > 0:
        return dest, url

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("libretro cover fetch failed: url=%s err=%s", url, exc)
        return None
    finally:
        if owns_client:
            client.close()

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.warning(
            "libretro cover unexpected status: url=%s status=%s",
            url,
            response.status_code,
        )
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest, url
