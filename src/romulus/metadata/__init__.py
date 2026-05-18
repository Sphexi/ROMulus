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

from romulus.db import get_config, queries, set_config
from romulus.db.config import DEFAULT_COVER_CACHE_DIR
from romulus.metadata import (
    gamedb,
    hasheous,
    launchbox,
    libretro,
    screenscraper,
    thegamesdb,
)
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


def _get_crc32_for_game(conn: sqlite3.Connection, game_id: int) -> str | None:
    """Find one CRC32 belonging to any hashed ROM of this game.

    Unlike :func:`_get_sha1_for_game` this does *not* require
    ``dat_verified`` — GameDB's CRC index is independent of our DAT
    pipeline, so it's worth trying even on fuzzy-matched games when the
    caller has opted in to include_fuzzy.
    """
    row = conn.execute(
        """
        SELECT h.crc32
        FROM roms r
        JOIN hashes h ON h.rom_id = r.id
        WHERE r.game_id = ?
          AND h.crc32 IS NOT NULL
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    return row[0] if row else None


def _try_gamedb(
    conn: sqlite3.Connection,
    game_id: int,
    title: str,
    system_id: str | None,
    crc32: str | None,
    canonical_name: str | None,
) -> bool:
    """Local-first GameDB lookup. Returns True when metadata was written.

    Match order: CRC32 (precise, only if Heavy Scan has run for this rom)
    -> canonical_name (closest to GameDB's release_name format) ->
    game.title (last-ditch fuzzy fallback).
    """
    if not system_id:
        return False
    index = gamedb.get_index_for_system(system_id)
    if index is None:
        return False
    entry = gamedb.lookup_by_crc32(crc32, index) if crc32 else None
    if entry is None and canonical_name:
        entry = gamedb.lookup_by_title(canonical_name, index)
    if entry is None and title:
        entry = gamedb.lookup_by_title(title, index)
    if entry is None:
        return False
    payload = gamedb.entry_to_metadata(entry)
    # GameDB carries identifier-only fields for most consoles; only
    # commit metadata when at least one user-facing field is populated.
    # Otherwise we'd insert an effectively-empty row that locks the game
    # out of subsequent richer providers under the default filters.
    if not any(payload.get(k) for k in ("publisher", "release_date", "release_year")):
        return False
    queries.upsert_metadata(conn, game_id, payload, source="gamedb")
    return True


def _fetch_metadata_for_game(
    conn: sqlite3.Connection,
    game_id: int,
    title: str,
    system_id: str | None,
    sha1: str | None,
    crc32: str | None,
    canonical_name: str | None,
    launchbox_index: LaunchBoxIndex | None,
    credentials: dict[str, str] | None,
    http_client: httpx.Client | None,
    tgdb_state: _TgdbRunState,
) -> bool:
    """Try each metadata provider in priority order. Returns True on success.

    Order is deliberate:

    0. GameDB — bundled JSON, offline, free; CRC32 + fuzzy title match.
       Tried *first* so we never burn API quota on games that the
       offline source can answer.
    1. Hasheous — hash-keyed lookup, free, no quota; precise match.
    2. LaunchBox — offline XML, no network at all; precise when present.
    3. ScreenScraper — hash-keyed but requires a (free) user account.
    4. TheGamesDB — name+platform, monthly quota — *last* so we only
       spend quota on games every cheaper source missed.
    """
    if _try_gamedb(conn, game_id, title, system_id, crc32, canonical_name):
        return True

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

    if tgdb_state.is_active():
        payload, remaining = thegamesdb.lookup_game(
            title, system_id, tgdb_state.apikey, client=http_client
        )
        tgdb_state.update_allowance(remaining)
        if payload:
            queries.upsert_metadata(conn, game_id, payload, source="thegamesdb")
            return True

    return False


class _TgdbRunState:
    """Per-run state for the TheGamesDB provider.

    Holds the api key (None if unconfigured), the last reported
    remaining-monthly-allowance counter, and a sticky disabled flag set
    when the counter drops to zero. Persists the final counter back to
    config so the next enrich session starts informed.

    ``is_active`` returns False as soon as we know the quota is out;
    callers check it before each TGDB call.
    """

    __slots__ = ("apikey", "_remaining", "_disabled", "_conn")

    def __init__(self, conn: sqlite3.Connection, apikey: str) -> None:
        self._conn = conn
        self.apikey = apikey
        # Restore last-known counter (empty string -> unknown -> try once).
        cached = get_config(conn, "thegamesdb_remaining_allowance") or ""
        try:
            self._remaining: int | None = int(cached) if cached else None
        except ValueError:
            self._remaining = None
        # Hard-disable when we already know we're out of quota AND we
        # have a key. Without a key there's nothing to disable — the
        # client short-circuits on its own.
        self._disabled = bool(apikey) and self._remaining == 0

    def is_active(self) -> bool:
        """True when TGDB has a configured key AND remaining allowance > 0."""
        return bool(self.apikey) and not self._disabled

    def update_allowance(self, remaining: int | None) -> None:
        """Record the latest remaining-allowance counter from a TGDB response.

        ``None`` (no counter reported) is a no-op. ``0`` flips the
        sticky disabled flag.
        """
        if remaining is None:
            return
        self._remaining = remaining
        if remaining <= 0:
            self._disabled = True

    def persist(self) -> None:
        """Save the last-seen allowance counter back to config."""
        if self._remaining is None:
            return
        set_config(
            self._conn,
            "thegamesdb_remaining_allowance",
            str(self._remaining),
        )


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
    game_ids: list[int] | None = None,
    system_id: str | None = None,
    collection_id: int | None = None,
    *,
    include_fuzzy: bool = False,
    include_already_enriched: bool = False,
) -> EnrichmentStats:
    """Walk DAT-verified games that have no metadata and try each source.

    Returns a small stats dict: {games_processed, metadata_added, covers_added}.
    Commits after each game so a long enrichment run survives interruption.

    Optional scope filters (Approach 1 — single code path):
        game_ids: Limit enrichment to these specific game ids.
        system_id: Limit enrichment to games belonging to this system.
        collection_id: Limit enrichment to games in this collection.
    When multiple are supplied game_ids wins, then system_id, then collection_id.
    When none are supplied the full library is enriched.

    Filter-loosening flags (forwarded to ``get_games_needing_enrichment``):
        include_fuzzy: also enrich fuzzy/header matched games.
        include_already_enriched: re-enrich games that already have a
            metadata row (e.g. to top up after adding a new provider).
    """
    cache_dir = _resolve_cache_dir(conn, cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    credentials: dict[str, str] = {
        "username": get_config(conn, "screenscraper_username") or "",
        "password": get_config(conn, "screenscraper_password") or "",
    }
    tgdb_state = _TgdbRunState(
        conn, get_config(conn, "thegamesdb_api_key") or ""
    )

    launchbox_index: LaunchBoxIndex | None = None
    if launchbox_xml_path is not None and Path(launchbox_xml_path).exists():
        entries = launchbox.parse_launchbox_xml(launchbox_xml_path)
        launchbox_index = launchbox.build_index(entries)
        logger.info("loaded launchbox entries: count=%d", len(entries))

    rows = queries.get_games_needing_enrichment(
        conn,
        include_fuzzy=include_fuzzy,
        include_already_enriched=include_already_enriched,
    )

    # Apply scope filter — narrow the candidate list to the requested scope.
    if game_ids is not None:
        allowed = frozenset(game_ids)
        rows = [r for r in rows if r["id"] in allowed]
    elif system_id is not None:
        rows = [r for r in rows if r["system_id"] == system_id]
    elif collection_id is not None:
        coll_game_ids = frozenset(queries.get_collection_games(conn, collection_id))
        rows = [r for r in rows if r["id"] in coll_game_ids]

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
        crc32 = _get_crc32_for_game(conn, game_id)

        if _fetch_metadata_for_game(
            conn,
            game_id,
            title,
            system_id,
            sha1,
            crc32,
            canonical_name,
            launchbox_index,
            credentials,
            http_client,
            tgdb_state,
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

    # Persist whatever quota counter we ended on so the next session
    # short-circuits if we burnt through the monthly budget.
    tgdb_state.persist()

    return {
        "games_processed": total,
        "metadata_added": metadata_added,
        "covers_added": covers_added,
    }


__all__ = [
    "EnrichmentStats",
    "enrich_library",
    "gamedb",
    "hasheous",
    "launchbox",
    "libretro",
    "screenscraper",
    "thegamesdb",
]
