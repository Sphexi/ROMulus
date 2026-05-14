# Session 6: Metadata Enrichment & Cover Art

**Type:** Build

**Context for this session:**

You are building the metadata fetching clients and cover art download system. Three sources, in priority order:

1. **libretro-thumbnails** (cover art only, free, no API key):
- URL: `https://thumbnails.libretro.com/{libretro_name}/Named_Boxarts/{game_name}.png`
- `{libretro_name}` = system.libretro_name, URL-encoded
- `{game_name}` = No-Intro canonical name with `&*/:\<>?\|"` → `_`
- Three types: Named_Boxarts, Named_Snaps, Named_Titles
- Download to `~/.romulus/covers/{system_id}/{cover_type}/{game_name}.png`
- 404 = no cover available, not an error

2. **Hasheous** (metadata, free, no API key):
- Endpoint: `https://hasheous.org/api/v1/lookup/{hash_type}/{hash_value}`
- Returns: title, description, genre, developer, publisher, release date
- Use SHA-1 as hash_type
- Rate: 1 req/sec with backoff on 429

3. **LaunchBox XML** (metadata, offline fallback):
- Downloadable XML database, ~200 MB
- Parse once, match by title + system
- Store in metadata table

4. **ScreenScraper** (optional, user-prompted):
- Only if user has configured credentials in Settings
- API: `https://api.screenscraper.fr/api2/`
- Rate: 1 req/sec max for free tier

Enrichment runs as a background QThread worker. User clicks "Enrich" button. Progress dialog shows per-game updates.

**Tasks:**

- [ ] Create `src/romulus/metadata/libretro.py`:
  - `fetch_cover(system, game_name, cover_type, cache_dir)` — download PNG from libretro-thumbnails
  - `build_thumbnail_url(libretro_name, game_name, cover_type)` — construct URL with character replacements
  - `sanitize_game_name(name)` — replace `&*/:\<>?\|"` with `_`
  - Handle 404 gracefully (no cover available)
- [ ] Create `src/romulus/metadata/hasheous.py`:
  - `lookup_by_hash(sha1)` — call Hasheous API, return metadata dict
  - Parse response into metadata fields (description, genre, developer, publisher, release_date)
  - Rate limiting: 1 req/sec with exponential backoff
- [ ] Create `src/romulus/metadata/launchbox.py`:
  - `parse_launchbox_xml(xml_path)` — parse LaunchBox database XML
  - `match_game(title, system_id, db)` — fuzzy match game title against LaunchBox entries
  - Store matched metadata in SQLite
- [ ] Create `src/romulus/metadata/screenscraper.py`:
  - `lookup_game(sha1, system_id, credentials)` — call ScreenScraper API
  - Only called if credentials are configured
  - Rate limiting: 1 req/sec strict
  - Stub implementation is fine if API details need more research
- [ ] Create enrichment orchestrator in `src/romulus/metadata/__init__.py`:
  - `enrich_library(conn, cache_dir, progress_callback)` — orchestrate enrichment across sources
  - For each DAT-matched game: try libretro-thumbnails for covers, Hasheous for metadata, LaunchBox as fallback
  - Skip games that already have metadata (don't re-fetch)
- [ ] Add metadata/cover queries to `db/queries.py`:
  - `upsert_metadata(conn, game_id, metadata_dict)`, `get_metadata(conn, game_id)`
  - `insert_cover(conn, game_id, cover_type, source_url, local_path)`, `get_covers(conn, game_id)`
  - `get_games_needing_enrichment(conn)` — games with match_confidence="dat_verified" but no metadata
- [ ] Add EnrichWorker to `src/romulus/ui/workers.py`:
  - QThread worker that runs enrich_library, emits progress signals
- [ ] Write tests:
  - `tests/test_metadata.py`: test URL construction for libretro-thumbnails, game name sanitization, Hasheous response parsing, LaunchBox XML parsing. Use mocked HTTP responses (httpx mock or responses library).

**Acceptance criteria:**
- libretro-thumbnails cover art downloads work for DAT-matched games
- Hasheous metadata lookup returns descriptions/genres for known hashes
- LaunchBox XML parser extracts metadata for matched games
- Cover art cached to `~/.romulus/covers/`
- Enrich button triggers background enrichment with progress dialog
- All tests pass, ruff clean

STOP. Commit with message "Session 6: Metadata enrichment and cover art". Do not proceed to Session 7.
