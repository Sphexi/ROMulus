# Session 10: Export Engine & Destination Profiles

**Type:** Build

**Context for this session:**

You are building the export engine that copies ROMs to a destination in a device-specific folder structure, and the destination profile system.

Profile format (YAML) — see TECHNICAL_PLAN.md §10 for the full YAML schema. Key fields per profile: id, name, case_sensitive, base_path, gamelist_format, artwork_subdir, multi_disc, systems map (system_id → {folder, extensions}).

Built-in profiles to create (6 YAML files in `data/profiles/`):
1. batocera.yaml — base_path: "roms", gamelist: emulationstation_xml
2. retropie.yaml — base_path: "roms", gamelist: emulationstation_xml
3. onionos.yaml — base_path: "ROMS", gamelist: null (Onion OS uses its own scraper)
4. muos.yaml — base_path: "ROMS", gamelist: null
5. mister.yaml — base_path: "games", gamelist: null
6. analogue-pocket.yaml — base_path: "Assets/{pocket_folder}/common", gamelist: null

System→folder mappings should match Igir's token output for compatibility. Reference Igir's docs for exact folder names per target.

Export workflow:
1. User selects profile + target path + optional filters
2. Preview shows file count, size, folder tree
3. Export copies files with progress
4. Optional: generate gamelist.xml per system folder
5. Optional: copy artwork to target's artwork directory
6. Optional: generate .m3u for multi-disc games

gamelist.xml format — see TECHNICAL_PLAN.md §10 for the XML structure.

**Carry-forward from prior sessions (sessions 5–8):**

- **Atomic file copies.** Every file written by `export_collection` (the ROMs themselves, gamelist.xml, .m3u playlists, copied artwork) MUST use the `tempfile.mkstemp` + `os.replace` pattern from [src/romulus/metadata/libretro.py](../../src/romulus/metadata/libretro.py) `fetch_cover` (Session 6 / Session 8). Copy to a temp file in the destination directory, then `os.replace` to the final path. Prevents partially-written ROMs if the user cancels mid-export or the disk fills up.
- **ExportWorker signal contract.** Mirror [src/romulus/ui/workers.py](../../src/romulus/ui/workers.py) `ScanWorker` (Session 5) and `EnrichWorker` (Session 6): thread-local `sqlite3.Connection`, emit `progress(int, str)` / `finished_ok(...)` / `failed(str)`, support cooperative cancel via a private exception raised inside the progress callback.
- **MainWindow integration.** Add an `isRunning()` guard to the Export toolbar handler and extend `closeEvent` to `requestInterruption` + `wait(5000)` on the export worker before the window closes — same hardening pattern Session 8 applied to scan/enrich.
- **DATs are still placeholders.** [data/dats/](../../data/dats/) contains only synthetic 2-game Logiqx files (Session 3 carry-forward). `games.canonical_name` will be NULL for nearly every game until real No-Intro DATs are committed. gamelist.xml generation must fall back to the parsed `games.title` when `canonical_name` is NULL — do not assume canonical names are populated.
- **Profile coverage.** The system registry (Session 1) defines 33 systems. Each built-in profile should list folder mappings for every system that target supports; for systems the target does *not* support, mark them as unsupported explicitly (e.g. an empty `folder` or a `supported: false` key) rather than omitting them silently, so a test can verify all 33 systems have an explicit decision per profile.

**Tasks:**

- [ ] Create `src/romulus/core/exporter.py`:
  - `load_profile(yaml_path)` — parse YAML into DestinationProfile model
  - `load_all_profiles(builtin_dir, user_dir)` — load all profiles from both locations
  - `preview_export(conn, profile, target_path, filters)` — return file count, total size, folder tree without copying anything
  - `export_collection(conn, profile, target_path, filters, options, progress_callback)` — copy files to target
  - `generate_gamelist_xml(conn, system_id, system_folder, target_path)` — write gamelist.xml
  - `generate_m3u_playlists(conn, system_id, system_folder, target_path)` — write .m3u for multi-disc
  - `copy_artwork(conn, system_id, profile, target_path)` — copy covers to target artwork dir
- [ ] Create 6 built-in YAML profiles in `data/profiles/`:
  - Each profile must define system folder mappings for all ~30 systems in the registry
  - Use Igir-compatible folder names
- [ ] Create `src/romulus/ui/export_dialog.py`:
  - ExportDialog(QDialog):
    - Profile selector (QComboBox listing all loaded profiles)
    - Target path (QLineEdit + folder picker button)
    - System filter (list of checkboxes, all checked by default)
    - Collection filter (dropdown: "All games" or specific collection)
    - Region filter (checkboxes: USA, Europe, Japan, World, Other)
    - Options checkboxes: Include artwork, Generate gamelist.xml/.lpl
    - "Preview" button — shows file count, estimated size, folder tree in a QTextEdit
    - "Export" button — runs export with progress bar
    - Summary after completion: "Exported N games across M systems (X GB)"
- [ ] Add ExportWorker to `src/romulus/ui/workers.py`:
  - QThread worker that runs export_collection, emits progress signals
- [ ] Wire "Export" toolbar button to open ExportDialog
- [ ] Write tests:
  - `tests/test_exporter.py`:
    - Test profile YAML loading
    - Test export preview (file count, size calculation)
    - Test file copy to correct folder structure (use tmp_path)
    - Test gamelist.xml generation (validate XML structure)
    - Test m3u playlist generation
    - Test system filtering (export only selected systems)

**Acceptance criteria:**
- 6 built-in profiles load correctly
- Export preview shows accurate file count and size
- Files copied to correct folder structure for each profile
- gamelist.xml generated with metadata for EmulationStation targets
- .m3u generated for multi-disc games
- Artwork copied if option selected
- Progress dialog shows per-file updates
- All tests pass, ruff clean

STOP. Commit with message "Session 10: Export engine and destination profiles". Do not proceed to Session 11.
