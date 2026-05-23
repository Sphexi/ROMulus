"""Metadata clients — libretro-thumbnails, Hasheous, LaunchBox, ScreenScraper.

Public entry points:

* :func:`enrich_library` — walks ROM rows that have no metadata yet and
  tries each source in priority order (local-first, network-last).
* :func:`fetch_online_covers_for_scope` — fetches libretro thumbnails for
  every ROM in a given scope.

Both functions are rom-keyed: they operate on ``roms.id`` directly.  There is
no longer a ``games`` table — each ROM owns its own metadata and cover rows.

Sibling-copy optimisation: before any network source is attempted,
:func:`_fetch_metadata_for_rom` and :func:`fetch_online_covers_for_scope`
check whether any *other* ROM with the same identity (SHA-1 → canonical_name
→ fuzzy_key) already has a metadata/cover row.  If one exists the row is
copied verbatim — same data, no API call.  This keeps TheGamesDB quota
consumption proportional to the number of *distinct* titles rather than the
number of file copies.
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

from romulus.db import get_config, set_config
from romulus.db import queries as q
from romulus.db.config import DEFAULT_COVER_CACHE_DIR
from romulus.metadata import (
    gamedb,
    hasheous,
    launchbox,
    libretro,
    libretro_metadat,
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


def _get_sha1_for_rom(conn: sqlite3.Connection, rom_id: int) -> str | None:
    """Return the SHA-1 for a ROM from the hashes table, or None.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to look up.
    """
    row = conn.execute(
        "SELECT sha1 FROM hashes WHERE rom_id = ? AND sha1 IS NOT NULL LIMIT 1",
        (rom_id,),
    ).fetchone()
    return row[0] if row else None


def _get_crc32_for_rom(conn: sqlite3.Connection, rom_id: int) -> str | None:
    """Return the CRC32 for a ROM from the hashes table, or None.

    Unlike :func:`_get_sha1_for_rom` this does *not* require
    ``dat_verified`` — GameDB's CRC index is independent of our DAT
    pipeline, so it's worth trying even on fuzzy-matched games when the
    caller has opted in to include_fuzzy.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to look up.
    """
    row = conn.execute(
        "SELECT crc32 FROM hashes WHERE rom_id = ? AND crc32 IS NOT NULL LIMIT 1",
        (rom_id,),
    ).fetchone()
    return row[0] if row else None


def _try_libretro_metadat(
    conn: sqlite3.Connection,
    rom_id: int,
    system_id: str | None,
    crc32: str | None,
) -> bool:
    """CRC32-keyed libretro-database lookup. Returns True on a useful hit.

    Libretro carries per-dimension metadata (genre, developer, publisher,
    releaseyear, players, rating) for ~50 systems. Coverage is broader
    per-field than GameDB so this fires *first* in the enrichment
    chain — if a CRC matches and at least one user-facing dimension
    populates, we commit and stop. Identifier-only hits never happen
    here because the lookup is *per-dimension*: if no dimension carried
    the CRC, the merged dict is empty and we fall through.

    Args:
        conn: SQLite connection.
        rom_id: The ROM to attach metadata to.
        system_id: System identifier for index selection.
        crc32: CRC32 hash string (may be None).
    """
    if not system_id or not crc32:
        return False
    index = libretro_metadat.get_index_for_system(system_id)
    if index is None:
        return False
    entry = libretro_metadat.lookup_by_crc32(crc32, index)
    if entry is None:
        return False
    payload = libretro_metadat.entry_to_metadata(entry)
    # Defensive: empty payload (e.g. an only-franchise hit, which we
    # don't surface in the UI yet) should fall through to the next
    # provider rather than locking the ROM out of further enrichment.
    if not any(
        payload.get(k)
        for k in (
            "genre", "developer", "publisher",
            "release_year", "players", "rating",
        )
    ):
        return False
    q.upsert_metadata(conn, rom_id, payload, source="libretro_metadat")
    return True


def _try_gamedb(
    conn: sqlite3.Connection,
    rom_id: int,
    title: str,
    system_id: str | None,
    crc32: str | None,
    canonical_name: str | None,
) -> bool:
    """Local-first GameDB lookup. Returns True when metadata was written.

    Match order: CRC32 (precise, only if Heavy Scan has run for this rom)
    -> canonical_name (closest to GameDB's release_name format) ->
    title (last-ditch fuzzy fallback).

    Args:
        conn: SQLite connection.
        rom_id: The ROM to attach metadata to.
        title: Display title to use as a fuzzy fallback.
        system_id: System identifier for index selection.
        crc32: CRC32 hash string (may be None).
        canonical_name: DAT-derived canonical name (may be None).
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
    # Otherwise we'd insert an effectively-empty row that locks the ROM
    # out of subsequent richer providers under the default filters.
    if not any(payload.get(k) for k in ("publisher", "release_date", "release_year")):
        return False
    q.upsert_metadata(conn, rom_id, payload, source="gamedb")
    return True


def _fetch_metadata_for_rom(
    conn: sqlite3.Connection,
    rom_id: int,
    title: str,
    system_id: str | None,
    sha1: str | None,
    crc32: str | None,
    canonical_name: str | None,
    launchbox_index: LaunchBoxIndex | None,
    credentials: dict[str, str] | None,
    http_client_inst: httpx.Client | None,
    tgdb_state: _TgdbRunState,
    *,
    include_online: bool,
) -> bool:
    """Try each metadata provider in priority order. Returns True on success.

    The sibling-copy gate runs FIRST — before any source is attempted.  If
    another ROM with the same identity (SHA-1 → canonical_name → fuzzy_key)
    already has a metadata row, we copy it verbatim and return immediately.
    This prevents TheGamesDB quota from being burnt linearly by duplicate ROM
    copies.

    Source order when no sibling is found:

    0. libretro-database (metadat) — bundled clrmamepro DATs, offline.
       CRC32-keyed; carries genre, developer, publisher, release year,
       players, rating across ~50 systems. Tried *first* because per-
       field coverage is the richest of the local sources.
    1. GameDB — bundled JSON, offline, free; CRC32 + fuzzy title.
       Covers consoles libretro doesn't (PSX, GC, Wii). May supply
       publisher / release_date for the systems where libretro missed.
    2. Hasheous — hash-keyed lookup, free, no quota; precise match.
       *Online* — skipped when ``include_online`` is False.
    3. LaunchBox — offline XML, no network at all; precise when present.
    4. ScreenScraper — hash-keyed but requires a (free) user account.
       *Online* — skipped when ``include_online`` is False.
    5. TheGamesDB — name+platform, monthly quota — *last* so we only
       spend quota on games every cheaper source missed.
       *Online* — skipped when ``include_online`` is False.

    When ``include_online`` is False every network-touching block is
    bypassed; only libretro / GameDB / LaunchBox run. ROMs with no
    offline match return False (the caller reports them as
    processed-but-not-enriched in the run stats).

    Args:
        conn: SQLite connection.
        rom_id: The ROM to enrich.
        title: Display title (fuzzy fallback for name-based sources).
        system_id: Platform identifier.
        sha1: SHA-1 hash (may be None — Heavy Scan populates this).
        crc32: CRC32 hash (may be None).
        canonical_name: DAT-derived canonical name (may be None).
        launchbox_index: Pre-loaded LaunchBox index or None.
        credentials: ScreenScraper username/password dict.
        http_client_inst: Shared httpx.Client for this run (may be None).
        tgdb_state: Per-run TheGamesDB quota tracker.
        include_online: When False, skip all network sources.
    """
    # -----------------------------------------------------------------------
    # Sibling-copy gate — MUST run before any source attempt.
    # If another ROM with the same identity already has metadata, copy it.
    # -----------------------------------------------------------------------
    sibling = q.find_sibling_metadata(conn, rom_id)
    if sibling is not None:
        q.copy_metadata(conn, source_rom_id=sibling["rom_id"], dest_rom_id=rom_id)
        logger.debug(
            "enrich: sibling-copied metadata rom_id=%d from=%d",
            rom_id,
            sibling["rom_id"],
        )
        return True

    if _try_libretro_metadat(conn, rom_id, system_id, crc32):
        return True

    if _try_gamedb(conn, rom_id, title, system_id, crc32, canonical_name):
        return True

    if include_online and sha1:
        result = hasheous.lookup_by_hash(sha1, client=http_client_inst)
        if result:
            q.upsert_metadata(conn, rom_id, result, source="hasheous")
            return True

    if launchbox_index is not None:
        entry = launchbox.match_game(title, system_id, launchbox_index)
        if entry is not None:
            q.upsert_metadata(
                conn,
                rom_id,
                launchbox.entry_to_metadata(entry),
                source="launchbox",
            )
            return True

    if include_online and sha1 and screenscraper.has_credentials(credentials):
        result = screenscraper.lookup_game(sha1, system_id, credentials, client=http_client_inst)
        if result:
            q.upsert_metadata(conn, rom_id, result, source="screenscraper")
            return True

    if include_online and tgdb_state.is_active():
        payload, remaining = thegamesdb.lookup_game(
            title, system_id, tgdb_state.apikey, client=http_client_inst
        )
        tgdb_state.update_allowance(remaining)
        if payload:
            q.upsert_metadata(conn, rom_id, payload, source="thegamesdb")
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


def _fetch_covers_for_rom(
    conn: sqlite3.Connection,
    rom_id: int,
    system_id: str | None,
    canonical_name: str | None,
    cache_dir: Path,
    http_client_inst: httpx.Client | None,
) -> int:
    """Try to fetch all three cover types from libretro-thumbnails for one ROM.

    A sibling-cover gate runs before any network request: if another ROM
    with the same identity already has cover rows, those rows are copied to
    this ROM (reusing the same on-disk files) and the network is not touched.

    Returns the count of cover rows written (covers already on file, or
    sibling-copied, are not counted here — they return 0 to the caller so
    the running total only reflects *new* disk operations).

    Args:
        conn: SQLite connection.
        rom_id: The ROM to attach covers to.
        system_id: Platform identifier for the libretro folder lookup.
        canonical_name: Game title used to build the libretro thumbnail URL.
        cache_dir: Root directory for cached cover images.
        http_client_inst: Shared httpx.Client for this run (may be None).
    """
    # Sibling-cover gate: reuse an existing ROM's cover rows when possible.
    sibling_covers = q.find_sibling_covers(conn, rom_id)
    if sibling_covers:
        # All sibling cover rows belong to a single source ROM; use its id.
        source_id = int(sibling_covers[0]["rom_id"])
        q.copy_covers(conn, source_rom_id=source_id, dest_rom_id=rom_id)
        logger.debug(
            "covers: sibling-copied rom_id=%d from=%d types=%d",
            rom_id,
            source_id,
            len(sibling_covers),
        )
        return 0  # no new disk I/O

    libretro_name = _system_libretro_name(system_id)
    if not libretro_name or not canonical_name or not system_id:
        return 0
    count = 0
    for cover_type in libretro.COVER_TYPES:
        if q.has_cover(conn, rom_id, cover_type):
            continue
        result = libretro.fetch_cover(
            libretro_name,
            system_id,
            canonical_name,
            cover_type,
            cache_dir,
            client=http_client_inst,
        )
        if result is None:
            continue
        local_path, source_url = result
        q.insert_cover(
            conn,
            rom_id,
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
    http_client: httpx.Client | None = None,  # noqa: A002 — intentional shadow for caller API compat
    rom_ids: list[int] | None = None,
    system_id: str | None = None,
    collection_id: int | None = None,
    *,
    include_fuzzy: bool = False,
    include_already_enriched: bool = False,
    include_online: bool = True,
) -> EnrichmentStats:
    """Walk ROM rows that have no metadata and try each source in priority order.

    Returns a small stats dict: {games_processed, metadata_added, covers_added}.
    Commits after each ROM so a long enrichment run survives interruption.

    Optional scope filters (narrowest wins):
        rom_ids: Limit enrichment to these specific ROM ids.
        system_id: Limit enrichment to ROMs in this system.
        collection_id: Limit enrichment to ROMs in this collection.
    When none are supplied the full library is enriched.

    Filter-loosening flags (forwarded to ``get_roms_needing_enrichment``):
        include_fuzzy: also enrich fuzzy/header matched ROMs.
        include_already_enriched: re-enrich ROMs that already have a
            metadata row (e.g. to top up after adding a new provider).

    Network gate:
        include_online: when False, only the bundled offline sources
            (libretro-database, GameDB, LaunchBox XML) run. Hasheous,
            ScreenScraper, and TheGamesDB are skipped — ROMs with no
            offline match get reported as processed-but-not-enriched.
            Cover-art lookups (libretro thumbnails) ARE still online;
            ``include_online`` only governs metadata providers.

    Args:
        conn: SQLite connection.
        cache_dir: Override for the cover-image cache directory.
        progress_callback: ``(current, total, title)`` callback per ROM.
        launchbox_xml_path: Path to a LaunchBox Metadata.xml file.
        http_client: Shared httpx.Client (passed through to clients).
        rom_ids: Scope to specific ROM ids.
        system_id: Scope to a single system.
        collection_id: Scope to a collection.
        include_fuzzy: Include fuzzy/header-matched ROMs.
        include_already_enriched: Include ROMs that already have metadata.
        include_online: Enable online metadata sources.
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

    rows = q.get_roms_needing_enrichment(
        conn,
        include_fuzzy=include_fuzzy,
        include_already_enriched=include_already_enriched,
    )

    # Apply scope filter — narrow the candidate list to the requested scope.
    if rom_ids is not None:
        allowed = frozenset(rom_ids)
        rows = [r for r in rows if r["id"] in allowed]
    elif system_id is not None:
        rows = [r for r in rows if r["system_id"] == system_id]
    elif collection_id is not None:
        coll_rom_ids = frozenset(q.get_collection_roms(conn, collection_id))
        rows = [r for r in rows if r["id"] in coll_rom_ids]

    total = len(rows)
    metadata_added = 0

    for idx, row in enumerate(rows, start=1):
        rom_id = row["id"]
        title = row["title"]
        cur_system_id = row["system_id"]
        canonical_name = row["canonical_name"] or row["dat_match"] or title

        if progress_callback is not None:
            progress_callback(idx, total, title)

        sha1 = _get_sha1_for_rom(conn, rom_id)
        crc32 = _get_crc32_for_rom(conn, rom_id)

        if _fetch_metadata_for_rom(
            conn,
            rom_id,
            title,
            cur_system_id,
            sha1,
            crc32,
            canonical_name,
            launchbox_index,
            credentials,
            http_client,
            tgdb_state,
            include_online=include_online,
        ):
            metadata_added += 1

        conn.commit()

    # Persist whatever quota counter we ended on so the next session
    # short-circuits if we burnt through the monthly budget.
    tgdb_state.persist()

    return {
        "games_processed": total,
        "metadata_added": metadata_added,
        # ``covers_added`` is kept in the stats dict for backwards-
        # compatible signal signatures (EnrichWorker.finished_ok still
        # emits the same three ints) but cover fetching is now driven
        # by ``fetch_online_covers_for_scope`` — the enrich path no
        # longer touches the cover cache.
        "covers_added": 0,
    }


def fetch_online_covers_for_scope(
    conn: sqlite3.Connection,
    scope_rom_ids: list[int] | None = None,
    cache_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
    http_client: httpx.Client | None = None,  # noqa: A002 — intentional shadow
) -> int:
    """Fetch libretro thumbnails for every ROM in ``scope_rom_ids``.

    Walks every ROM in the scope (or every ROM with a fuzzy_key when
    ``scope_rom_ids`` is None) and issues libretro-thumbnail lookups for
    missing cover types.  A sibling-cover gate inside
    :func:`_fetch_covers_for_rom` short-circuits network calls for ROMs
    whose identity matches another ROM that already has covers.

    Returns the count of new cover rows inserted (sibling-copied rows are
    not counted — they required no network I/O).

    Pairs with :func:`romulus.core.local_cover_finder.discover_local_covers`
    on the offline side — the UI's "Find Covers" workflow runs one, the
    other, or both based on the user's dialog choices.

    Args:
        conn: SQLite connection.
        scope_rom_ids: Explicit list of ROM ids to process, or None for all.
        cache_dir: Override for the cover-image cache directory.
        progress_callback: ``(current, total, title)`` callback per ROM.
        http_client: Shared httpx.Client (passed through to libretro client).
    """
    cache_dir = _resolve_cache_dir(conn, cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if scope_rom_ids is None:
        rows = conn.execute(
            """
            SELECT r.id       AS rom_id,
                   r.system_id,
                   r.canonical_name,
                   COALESCE(r.title, r.filename) AS title
            FROM roms r
            WHERE r.fuzzy_key IS NOT NULL AND r.fuzzy_key != ''
            ORDER BY r.system_id, r.id
            """
        ).fetchall()
    elif not scope_rom_ids:
        return 0
    else:
        rows = []
        chunk_size = 500
        for i in range(0, len(scope_rom_ids), chunk_size):
            chunk = scope_rom_ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            cursor = conn.execute(
                f"""
                SELECT r.id       AS rom_id,
                       r.system_id,
                       r.canonical_name,
                       COALESCE(r.title, r.filename) AS title
                FROM roms r
                WHERE r.id IN ({placeholders})
                  AND r.fuzzy_key IS NOT NULL AND r.fuzzy_key != ''
                ORDER BY r.system_id, r.id
                """,
                chunk,
            )
            rows.extend(cursor.fetchall())

    total = len(rows)
    covers_added = 0
    for idx, row in enumerate(rows, start=1):
        rom_id = int(row["rom_id"])
        title = str(row["title"] or "")
        canonical_name = row["canonical_name"] or title
        if progress_callback is not None:
            progress_callback(idx, total, title)
        covers_added += _fetch_covers_for_rom(
            conn,
            rom_id,
            row["system_id"],
            canonical_name,
            cache_dir,
            http_client,
        )
        conn.commit()
    return covers_added


__all__ = [
    "EnrichmentStats",
    "enrich_library",
    "fetch_online_covers_for_scope",
    "gamedb",
    "hasheous",
    "launchbox",
    "libretro",
    "libretro_metadat",
    "screenscraper",
    "thegamesdb",
]
