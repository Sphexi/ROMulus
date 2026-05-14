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

- [ ] Create `src/romulus/ui/detail_panel.py`:
  - DetailPanel(QWidget) showing cover art, metadata, action buttons
  - `update_game(game_id)` — fetch game, metadata, cover from SQLite, update display
  - Cover art: load from cache path, scale to fit panel width, show placeholder if missing
  - Match status: colored badge (green/yellow/gray)
- [ ] Wire detail panel to game table selection:
  - When user clicks a row in GameTable, emit game_selected(game_id) signal
  - MainWindow connects signal to DetailPanel.update_game
- [ ] Add filter controls to game table:
  - Region filter dropdown (QComboBox) — filters GameTableModel
  - Match status filter (QComboBox) — filters by match_confidence
  - Wire search bar to filter by title (already exists, may need re-wiring)
- [ ] Implement collections system:
  - Add collection queries to `db/queries.py`: create_collection, delete_collection, add_game_to_collection, remove_game_from_collection, get_collection_games, get_collections
  - Create "Favorites" as a system collection on first run (is_system=1)
  - Add ★ Favorite toggle button in DetailPanel — adds/removes from Favorites collection
  - Add "Add to Collection..." button — shows dropdown of user collections
  - Add "New Collection..." option in dropdown
- [ ] Update SystemSidebar:
  - Add collections section below systems separator
  - Show collection names with game counts
  - Clicking a collection filters the game table to show only its games
- [ ] Add right-click context menu on game table rows:
  - "Add to Favorites"
  - "Add to Collection..." → submenu of collections
  - "Remove from Collection" (when viewing a collection)
- [ ] Write tests:
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
