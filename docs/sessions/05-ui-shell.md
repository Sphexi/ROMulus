# Session 5: UI Shell, System Browser & Game Table

**Type:** Build

**Context for this session:**

You are building the main application window with PySide6. The UI has three panels: system sidebar (left), game table (center), and a placeholder detail panel (right, built in Session 7).

Main window layout (from TECHNICAL_PLAN.md §11 — UI Components):
- Menu bar: File (Open Library, Settings, Quit), View (columns toggle), Tools (Quick Scan, Heavy Scan, Organize, Enrich, Export), Help (About)
- Toolbar: Quick Scan, Heavy Scan, Organize, Enrich, Export, Settings buttons
- System sidebar: QTreeView listing all systems with ROM counts, "All" at top, collections section at bottom
- Game table: QTableView with columns: Name, System, Region, Size, Match Status. Sortable. Search bar above.
- Status bar: total ROM count, scan status

The app entry point (`__main__.py`) creates a QApplication, initializes the database (create tables, seed systems, seed config), and shows the main window.

On first launch, if no library_path is configured, show a folder picker dialog: "Select your ROM library folder".

Quick Scan button triggers a scan in a QThread worker, with progress dialog. After scan completes, game table refreshes.

**Tasks:**

- [ ] Update `src/romulus/__main__.py`:
  - Create QApplication
  - Initialize database (create_tables, seed_systems, seed_defaults)
  - Check config for library_path — if empty, show folder picker
  - Show MainWindow
- [ ] Create `src/romulus/app.py`:
  - App initialization logic (DB setup, config loading)
- [ ] Create `src/romulus/ui/main_window.py`:
  - MainWindow(QMainWindow) with menu bar, toolbar, status bar
  - Three-panel layout using QSplitter: sidebar | game table | detail placeholder
  - Connect toolbar buttons to actions
- [ ] Create `src/romulus/ui/system_sidebar.py`:
  - SystemSidebar(QTreeView) backed by a QStandardItemModel
  - "All" entry at top showing total ROM count
  - One entry per system that has ROMs, showing count
  - "Favorites" and collections section at bottom
  - Signal: system_selected(system_id) — filters the game table
- [ ] Create `src/romulus/ui/game_table.py`:
  - GameTable(QTableView) backed by QAbstractTableModel subclass (GameTableModel)
  - Columns: Name, System, Region, Size, Match Status
  - Sortable by clicking column headers
  - Search bar (QLineEdit) above table — filters by game name in real time
  - Lazy-load rows from SQLite (paginate if >5000 games)
- [ ] Create `src/romulus/ui/workers.py`:
  - ScanWorker(QThread) — runs scan_library in background, emits progress/finished signals
  - Connect to ScanProgressDialog
- [ ] Create `src/romulus/ui/scan_progress.py`:
  - ScanProgressDialog(QProgressDialog) — shows file count, current file, cancel button
- [ ] Create `src/romulus/ui/settings_dialog.py`:
  - SettingsDialog(QDialog) with tabs:
    - General: library path (folder picker), theme selector
    - DATs: DAT folder paths (list + add/remove buttons)
    - Metadata: ScreenScraper credentials (username/password fields, test button)
    - Scan: thread count spinner
  - Save all settings to config table
- [ ] Write tests:
  - `tests/test_ui.py`: test GameTableModel data loading, sorting, filtering (can test model without showing UI)

**Acceptance criteria:**
- App launches with `python -m romulus`, shows main window
- First launch prompts for library folder
- Quick Scan button triggers scan with progress dialog
- System sidebar populates with systems that have ROMs
- Game table shows ROM list, sortable and searchable
- Settings dialog reads/writes config table
- All tests pass, ruff clean

STOP. Commit with message "Session 5: UI shell, system sidebar, game table". Do not proceed to Session 6.
