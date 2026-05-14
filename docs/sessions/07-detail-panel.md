# Session 7: Game Detail Panel, Search/Filter, Collections

**Type:** Build

**Context for this session:**

You are building the right-side detail panel, wiring up search/filter to the game table, and implementing the collections system (favorites, custom groups).

Detail panel layout:
- Cover art image (QLabel with scaled pixmap, placeholder if no cover)
- Title (bold, large font)
- System name
- Region / Revision
- Genre, Developer, Publisher (from metadata table)
- Description (scrollable QTextEdit, read-only)
- Match status indicator (color-coded: green=DAT verified, yellow=header matched, gray=unmatched)
- Action buttons: ★ Favorite toggle, "Add to Collection..." dropdown

Filter controls above the game table:
- Search bar already exists from Session 5 — wire it to filter by game title
- Region filter dropdown (All, USA, Europe, Japan, World, Other)
- Match status filter (All, Verified, Unmatched)

Collections:
- "Favorites" is a built-in system collection
- Users can create custom collections ("Export to Anbernic", "RPGs", etc.)
- Collections appear in the system sidebar under a separator
- Right-click game → "Add to Collection" context menu

**Tasks:**

- [x] Create `src/romulus/ui/detail_panel.py`:
  - DetailPanel(QWidget) showing cover art, metadata, action buttons
  - `update_game(game_id)` — fetch game, metadata, cover from SQLite, update display
  - Cover art: load from cache path, scale to fit panel width, show placeholder if missing
  - Match status: colored badge (green/yellow/gray)
- [x] Wire detail panel to game table selection:
  - When user clicks a row in GameTable, emit game_selected(game_id) signal
  - MainWindow connects signal to DetailPanel.update_game
- [x] Add filter controls to game table:
  - Region filter dropdown (QComboBox) — filters GameTableModel
  - Match status filter (QComboBox) — filters by match_confidence
  - Wire search bar to filter by title (already exists, may need re-wiring)
- [x] Implement collections system:
  - Add collection queries to `db/queries.py`: create_collection, delete_collection, add_game_to_collection, remove_game_from_collection, get_collection_games, get_collections
  - Create "Favorites" as a system collection on first run (is_system=1)
  - Add ★ Favorite toggle button in DetailPanel — adds/removes from Favorites collection
  - Add "Add to Collection..." button — shows dropdown of user collections
  - Add "New Collection..." option in dropdown
- [x] Update SystemSidebar:
  - Add collections section below systems separator
  - Show collection names with game counts
  - Clicking a collection filters the game table to show only its games
- [x] Add right-click context menu on game table rows:
  - "Add to Favorites"
  - "Add to Collection..." → submenu of collections
  - "Remove from Collection" (when viewing a collection)
- [x] Write tests:
  - `tests/test_collections.py`: test create/delete collection, add/remove games, favorite toggle

**Acceptance criteria:**
- Clicking a game in the table shows its details in the right panel
- Cover art displays if cached, placeholder if not
- Search bar filters game table by title
- Region and match status dropdowns filter the table
- Favorites toggle works
- Custom collections can be created, games added/removed
- Collections appear in sidebar, clicking filters the table
- All tests pass, ruff clean

STOP. Commit with message "Session 7: Detail panel, search/filter, collections". Do not proceed to Session 8.

## Completion Summary
**Status:** COMPLETE
**Date:** 2026-05-14
**What was built/changed:**
- New `DetailPanel` (`src/romulus/ui/detail_panel.py`) with cover-art QLabel, bold title, system/region/revision/genre/developer/publisher labels, scrollable description, ROM list, color-coded match-status badge, ★ Favorite toggle, and "Add to Collection..." menu (existing collections + "New Collection..." entry).
- `GameTable` gained Region / Match dropdown filters, a `game_selected(int)` signal driven by row selection, a right-click context menu (Add to Favorites, Add to Collection... submenu with New Collection..., Remove from Collection when in a collection view), `game_id` propagation on `GameRow`, and `set_collection_context` / `set_available_collections` setters. `GameTableProxy` filters via `filterAcceptsRow` and keeps `setFilterFixedString` compatibility for existing callers.
- `MainWindow` mounts the real `DetailPanel`, wires sidebar system + collection signals, game-selected → detail-panel updates, and add/remove/new-collection slots; collection view restores ROM list filtered to that collection's games.
- DB layer: new queries `get_game_by_id`, `get_rom_by_id`, `get_roms_for_game`, `ensure_favorites_collection`, `create_collection`, `delete_collection` (system-collection protected), `add_game_to_collection` (idempotent), `remove_game_from_collection`, `get_collection_games`, `get_collections` (with `game_count` aggregate, system rows first), `get_collection_by_name`, `is_game_in_collection`. `app.initialize_database` now seeds the Favorites system collection.
- `SystemSidebar.get_collections` is now a thin shim over `queries.get_collections`.
- New tests: 18 in `tests/test_collections.py` (collection queries + favorite toggle + detail lookups) and 18 additional UI tests in `tests/test_ui.py` (region/match filters, selection signal, DetailPanel rendering, cover placeholder + on-disk PNG, favorite toggle round-trip, MainWindow collection wiring).
**Tests:** 351 passed (315 baseline → 351, +36 new). Ruff: clean.
**Config changes:** None — Favorites collection is seeded automatically on first DB initialization.
**Breaking changes:** `GameRow` gained a new (defaulted) `game_id` field — backwards-compatible. `load_rom_rows` accepts an optional `game_ids` filter.
**Carry-forward notes:**
- Heavy Scan (Session 8) will populate `dat_verified` match confidences; the DetailPanel match badge already understands the full ladder (unmatched / fuzzy / header / dat_verified).
- Cover lookup uses the first `covers.local_path` that exists on disk; libretro-thumbnails downloads from Session 6 land in `cover_cache_path`. Heavy Scan / future cover refresh should keep `local_path` accurate.
- New-Collection creation surfaces a `QInputDialog`; tests cover the signal path, not the dialog itself.
- `delete_collection` raises `ValueError` for system collections — any Settings UI (later session) must guard against deleting Favorites.
