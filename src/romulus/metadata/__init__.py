"""Metadata clients — libretro-thumbnails, Hasheous, LaunchBox, ScreenScraper.

Public entry point: `enrich_library` walks DAT-verified games that have no
metadata yet and tries each source in priority order (Hasheous -> LaunchBox
-> ScreenScraper for metadata; libretro-thumbnails for covers).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypedDict

import httpx

from romulus.db import get_config, queries
from romulus.db.config import DEFAULT_COVER_CACHE_DIR
from romulus.metadata import hasheous, launchbox, libretro, screenscraper
from romulus.metadata.launchbox import LaunchBoxIndex
from romulus.models.system import SYSTEM_REGISTRY

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


@contextlib.contextmanager
def http_client(
    client: httpx.Client | None, timeout: float
) -> Iterator[httpx.Client]:
    """Yield an :class:`httpx.Client`, closing it only if we created it.

    Every metadata client supports caller-injected ``client=`` (for testing
    with :class:`httpx.MockTransport`) AND a fall-back of creating its own
    ``httpx.Client`` per call. This context manager centralizes the "owns_
    client" boilerplate that used to repeat across libretro/hasheous/
    screenscraper. When ``client`` is None a fresh one is constructed with
    the supplied timeout and closed on exit; when ``client`` is supplied it
    is yielded as-is and the caller retains ownership.

    Security defaults pinned explicitly (audit v0.1.0 — networking notes):

    * ``verify=True`` — full TLS certificate verification against the system
      trust store. This is httpx's default, but pinned in code so a future
      refactor can't accidentally toggle it off.
    * ``follow_redirects=False`` — also the default; pinned so a malicious
      response can't redirect a cover-art GET to an attacker host. The
      metadata clients never expect a redirect; if one fires, we treat it as
      a miss.
    """
    if client is not None:
        yield client
        return
    with httpx.Client(
        timeout=timeout, verify=True, follow_redirects=False
    ) as owned:
        yield owned


class RateLimiter:
    """Minimal monotonic-clock rate limiter shared by metadata clients.

    Each instance owns a single ``_last_ts`` float — calling :meth:`wait`
    sleeps just long enough to keep request spacing at or above the
    configured minimum interval, then stamps the new request time. Used from
    a single worker thread (the :class:`EnrichWorker`) so no lock is required.
    """

    __slots__ = ("min_interval", "_last_ts")

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last_ts = 0.0

    def wait(self) -> None:
        """Sleep so the next request is at least ``min_interval`` after the last."""
        elapsed = time.monotonic() - self._last_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_ts = time.monotonic()


class EnrichmentStats(TypedDict):
    """Counts returned from ``enrich_library`` after one run."""

    games_processed: int
    metadata_added: int
    covers_added: int


def _system_libretro_name(system_id: str | None) -> str | None:
    """Look up a system's libretro thumbnail folder name from the registry."""
    if not system_id:
        return None
    for sys_def in SYSTEM_REGISTRY:
        if sys_def.id == system_id:
            return sys_def.libretro_name
    return None


def _resolve_cache_dir(conn: sqlite3.Connection, cache_dir: Path | str | None) -> Path:
    """Pick the on-disk cover cache directory.

    Explicit argument wins; otherwise read ``cover_cache_path`` from config;
    if nothing is configured, fall back to :data:`DEFAULT_COVER_CACHE_DIR`
    (the same default seeded into the config table on first run).
    """
    if cache_dir is not None:
        return Path(cache_dir)
    configured = get_config(conn, "cover_cache_path")
    if configured:
        return Path(configured)
    return DEFAULT_COVER_CACHE_DIR


def _get_sha1_for_game(conn: sqlite3.Connection, game_id: int) -> str | None:
    """Find one SHA-1 belonging to any DAT-matched ROM of this game."""
    row = conn.execute(
        """
        SELECT h.sha1
        FROM roms r
        JOIN hashes h ON h.rom_id = r.id
        WHERE r.game_id = ?
          AND r.match_confidence = 'dat_verified'
          AND h.sha1 IS NOT NULL
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    return row[0] if row else None


def _fetch_metadata_for_game(
    conn: sqlite3.Connection,
    game_id: int,
    title: str,
    system_id: str | None,
    sha1: str | None,
    launchbox_index: LaunchBoxIndex | None,
    credentials: dict[str, str] | None,
    http_client: httpx.Client | None,
) -> bool:
    """Try each metadata provider in priority order. Returns True on success."""
    if sha1:
        result = hasheous.lookup_by_hash(sha1, client=http_client)
        if result:
            queries.upsert_metadata(conn, game_id, result, source="hasheous")
            return True

    if launchbox_index is not None:
        entry = launchbox.match_game(title, system_id, launchbox_index)
        if entry is not None:
            queries.upsert_metadata(
                conn,
                game_id,
                launchbox.entry_to_metadata(entry),
                source="launchbox",
            )
            return True

    if sha1 and screenscraper.has_credentials(credentials):
        result = screenscraper.lookup_game(sha1, system_id, credentials, client=http_client)
        if result:
            queries.upsert_metadata(conn, game_id, result, source="screenscraper")
            return True

    return False


def _fetch_covers_for_game(
    conn: sqlite3.Connection,
    game_id: int,
    system_id: str | None,
    canonical_name: str | None,
    cache_dir: Path,
    http_client: httpx.Client | None,
) -> int:
    """Try to fetch all three cover types from libretro-thumbnails.

    Returns the count of cover rows written (covers already on file are skipped
    without a refetch).
    """
    libretro_name = _system_libretro_name(system_id)
    if not libretro_name or not canonical_name or not system_id:
        return 0
    count = 0
    for cover_type in libretro.COVER_TYPES:
        if queries.has_cover(conn, game_id, cover_type):
            continue
        result = libretro.fetch_cover(
            libretro_name,
            system_id,
            canonical_name,
            cover_type,
            cache_dir,
            client=http_client,
        )
        if result is None:
            continue
        local_path, source_url = result
        queries.insert_cover(
            conn,
            game_id,
            cover_type,
            source_url=source_url,
            local_path=str(local_path),
        )
        count += 1
    return count


def enrich_library(
    conn: sqlite3.Connection,
    cache_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
    launchbox_xml_path: Path | str | None = None,
    http_client: httpx.Client | None = None,
) -> EnrichmentStats:
    """Walk DAT-verified games that have no metadata and try each source.

    Returns a small stats dict: {games_processed, metadata_added, covers_added}.
    Commits after each game so a long enrichment run survives interruption.
    """
    cache_dir = _resolve_cache_dir(conn, cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    credentials: dict[str, str] = {
        "username": get_config(conn, "screenscraper_username") or "",
        "password": get_config(conn, "screenscraper_password") or "",
    }

    launchbox_index: LaunchBoxIndex | None = None
    if launchbox_xml_path is not None and Path(launchbox_xml_path).exists():
        entries = launchbox.parse_launchbox_xml(launchbox_xml_path)
        launchbox_index = launchbox.build_index(entries)
        logger.info("loaded launchbox entries: count=%d", len(entries))

    rows = queries.get_games_needing_enrichment(conn)
    total = len(rows)
    metadata_added = 0
    covers_added = 0

    for idx, row in enumerate(rows, start=1):
        game_id = row["id"]
        title = row["title"]
        system_id = row["system_id"]
        canonical_name = row["canonical_name"] or row["dat_match"] or title

        if progress_callback is not None:
            progress_callback(idx, total, title)

        sha1 = _get_sha1_for_game(conn, game_id)

        if _fetch_metadata_for_game(
            conn,
            game_id,
            title,
            system_id,
            sha1,
            launchbox_index,
            credentials,
            http_client,
        ):
            metadata_added += 1

        covers_added += _fetch_covers_for_game(
            conn,
            game_id,
            system_id,
            canonical_name,
            cache_dir,
            http_client,
        )

        conn.commit()

    return {
        "games_processed": total,
        "metadata_added": metadata_added,
        "covers_added": covers_added,
    }


__all__ = [
    "EnrichmentStats",
    "enrich_library",
    "hasheous",
    "launchbox",
    "libretro",
    "screenscraper",
]
