"""Main window — menu bar, toolbar, three-panel layout."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QToolBar,
)

from romulus.core.exporter import load_all_profiles
from romulus.core.organizer import analyze_library
from romulus.db import DEFAULT_DB_PATH, get_config, set_config
from romulus.db import queries as q
from romulus.ui.dest_scan_progress import DestScanProgressDialog
from romulus.ui.detail_panel import DetailPanel
from romulus.ui.enrich_options_dialog import EnrichOptionsDialog
from romulus.ui.enrich_progress import EnrichProgressDialog
from romulus.ui.export_dialog import ExportDialog
from romulus.ui.game_table import GameTable, load_rom_rows
from romulus.ui.heavy_scan_progress import HeavyScanProgressDialog
from romulus.ui.local_cover_progress import LocalCoverProgressDialog
from romulus.ui.organize_preview import OrganizePreviewDialog
from romulus.ui.scan_progress import ScanProgressDialog
from romulus.ui.settings_dialog import SettingsDialog
from romulus.ui.system_sidebar import SystemSidebar
from romulus.ui.workers import (
    DestInventoryWorker,
    EnrichWorker,
    ExportWorker,
    HeavyScanWorker,
    LocalCoverFinderWorker,
    OrganizeWorker,
    ScanWorker,
    SyncWorker,
)

# Bundled DATs directory — resolved at import time relative to this file so the
# path is correct whether the app is run from source or installed as a package.
_BUNDLED_DATS_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "data" / "dats"
)


class MainWindow(QMainWindow):
    """Top-level window: menu bar, toolbar, sidebar | game table | detail."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.setWindowTitle("ROMulus")
        self.resize(1280, 800)
        self._conn = conn
        self._selected_system: str | None = None
        self._selected_collection: int | None = None
        self._scan_worker: ScanWorker | None = None
        self._scan_dialog: ScanProgressDialog | None = None
        self._heavy_scan_worker: HeavyScanWorker | None = None
        self._heavy_scan_dialog: HeavyScanProgressDialog | None = None
        self._enrich_worker: EnrichWorker | None = None
        self._enrich_dialog: EnrichProgressDialog | None = None
        self._organize_worker: OrganizeWorker | None = None
        self._organize_dialog: OrganizePreviewDialog | None = None
        self._export_worker: ExportWorker | None = None
        self._export_dialog: ExportDialog | None = None
        self._local_cover_worker: LocalCoverFinderWorker | None = None
        self._local_cover_dialog: LocalCoverProgressDialog | None = None
        self._dest_inventory_worker: DestInventoryWorker | None = None
        self._dest_scan_dialog: DestScanProgressDialog | None = None
        self._sync_worker: SyncWorker | None = None
        self._sync_preview_dialog: object | None = None

        q.ensure_favorites_collection(conn)

        self.sidebar = SystemSidebar(self)
        self.game_table = GameTable(self)
        self.detail_panel = DetailPanel(conn, self)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.game_table)
        splitter.addWidget(self.detail_panel)
        # Stretch ratios kick in on resize; explicit setSizes pins the
        # *initial* layout. Without setSizes the sidebar is too narrow at
        # startup and system names like "Sega Mega Drive / Genesis" elide
        # to "Seg..." until the user drags the splitter manually.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([260, 700, 360])
        self.setCentralWidget(splitter)

        self.status_label = QLabel("Ready")
        self.statusBar().addPermanentWidget(self.status_label)

        self._build_menu()
        self._build_toolbar()

        self.sidebar.system_selected.connect(self._on_system_selected)
        self.sidebar.collection_selected.connect(self._on_collection_selected)
        self.game_table.game_selected.connect(self._on_game_selected)
        self.game_table.add_to_favorites_requested.connect(
            self._on_add_to_favorites
        )
        self.game_table.add_to_collection_requested.connect(
            self._on_add_to_collection
        )
        self.game_table.new_collection_requested.connect(
            self._on_new_collection
        )
        self.game_table.remove_from_collection_requested.connect(
            self._on_remove_from_collection
        )
        self.detail_panel.favorite_toggled.connect(self._on_favorite_toggled)

        # Scoped game-table actions.
        # Single-game enrich is the *force* path — it bypasses both
        # silent filters so the user can re-enrich a fuzzy or already-
        # metadata-bearing game without first deleting any rows.
        self.game_table.enrich_game_requested.connect(
            lambda gid: self._enrich_scoped(
                game_ids=[gid],
                include_fuzzy=True,
                include_already_enriched=True,
            )
        )
        self.game_table.heavy_scan_game_requested.connect(
            lambda gid: self._heavy_scan_scoped(game_id=gid)
        )
        self.game_table.find_local_covers_game_requested.connect(
            lambda gid: self._find_local_covers_scoped(game_id=gid)
        )

        # Scoped sidebar actions — system.
        # Quick Scan for a system triggers a full library quick scan (path
        # filtering is not implemented at the scan engine level yet).
        self.sidebar.quick_scan_system_requested.connect(
            lambda _sid: self._on_quick_scan()
        )
        self.sidebar.heavy_scan_system_requested.connect(
            lambda sid: self._heavy_scan_scoped(system_id=sid)
        )
        self.sidebar.enrich_system_requested.connect(
            lambda sid: self._enrich_scoped(system_id=sid)
        )
        self.sidebar.find_covers_system_requested.connect(
            lambda sid: self._find_local_covers_scoped(system_id=sid)
        )

        # Scoped sidebar actions — collection.
        self.sidebar.enrich_collection_requested.connect(
            lambda cid: self._enrich_scoped(collection_id=cid)
        )
        self.sidebar.heavy_scan_collection_requested.connect(
            lambda cid: self._heavy_scan_scoped(collection_id=cid)
        )
        self.sidebar.find_covers_collection_requested.connect(
            lambda cid: self._find_local_covers_scoped(collection_id=cid)
        )

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        open_action = QAction("&Open Library...", self)
        open_action.triggered.connect(self._on_open_library)
        file_menu.addAction(open_action)
        settings_action = QAction("&Settings...", self)
        settings_action.triggered.connect(self._on_open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menu.addMenu("&View")
        view_menu.addAction(QAction("Toggle Columns (TBD)", self, enabled=False))

        tools_menu = menu.addMenu("&Tools")
        quick_scan = QAction("&Quick Scan", self)
        quick_scan.triggered.connect(self._on_quick_scan)
        tools_menu.addAction(quick_scan)
        heavy_scan = QAction("&Heavy Scan", self)
        heavy_scan.setToolTip(
            "Hash and identify every ROM against the bundled DAT database."
        )
        heavy_scan.triggered.connect(self._on_heavy_scan)
        tools_menu.addAction(heavy_scan)
        tools_menu.addSeparator()
        enrich_action = QAction("&Enrich", self)
        enrich_action.setToolTip("Fetch cover art and metadata for matched games.")
        enrich_action.triggered.connect(self._on_enrich)
        tools_menu.addAction(enrich_action)
        local_cover_action = QAction("&Find Local Covers", self)
        local_cover_action.setToolTip(
            "Scan the library tree for image files matching enrolled ROMs and link"
            " them as covers."
        )
        local_cover_action.triggered.connect(self._on_find_local_covers)
        tools_menu.addAction(local_cover_action)
        organize_action = QAction("&Organize", self)
        organize_action.setToolTip("Preview and apply library reorganization.")
        organize_action.triggered.connect(self._on_organize)
        tools_menu.addAction(organize_action)
        export_action = QAction("E&xport / Sync", self)
        export_action.setToolTip(
            "Export or sync the library to a destination profile."
        )
        export_action.triggered.connect(self._on_export)
        tools_menu.addAction(export_action)
        tools_menu.addSeparator()
        clean_missing_action = QAction("Clean &Missing Entries...", self)
        clean_missing_action.setToolTip(
            "Permanently remove database rows for ROM files that no longer "
            "exist on disk. Quick Scan flags missing files automatically; this "
            "action deletes them."
        )
        clean_missing_action.triggered.connect(self._on_clean_missing)
        tools_menu.addAction(clean_missing_action)

        help_menu = menu.addMenu("&Help")
        about_action = QAction("&About ROMulus", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        quick = QAction("Quick Scan", self)
        quick.triggered.connect(self._on_quick_scan)
        toolbar.addAction(quick)

        heavy = QAction("Heavy Scan", self)
        heavy.setToolTip(
            "Hash and identify every ROM against the bundled DAT database."
        )
        heavy.triggered.connect(self._on_heavy_scan)
        toolbar.addAction(heavy)

        toolbar.addSeparator()
        organize = QAction("Organize", self)
        organize.triggered.connect(self._on_organize)
        toolbar.addAction(organize)
        enrich = QAction("Enrich", self)
        enrich.triggered.connect(self._on_enrich)
        toolbar.addAction(enrich)
        find_covers = QAction("Find Local Covers", self)
        find_covers.setToolTip(
            "Scan the library tree for image files matching enrolled ROMs and link"
            " them as covers."
        )
        find_covers.triggered.connect(self._on_find_local_covers)
        toolbar.addAction(find_covers)
        export = QAction("Export / Sync", self)
        export.triggered.connect(self._on_export)
        toolbar.addAction(export)

        toolbar.addSeparator()
        settings = QAction("Settings", self)
        settings.triggered.connect(self._on_open_settings)
        toolbar.addAction(settings)

    # ------------------------------------------------------------------
    # Public refresh helpers
    # ------------------------------------------------------------------

    def refresh_sidebar(self) -> None:
        """Repaint the system sidebar from the database."""
        self.sidebar.populate(self._conn)

    def refresh_game_table(self) -> None:
        """Reload visible games for the active system/collection filter."""
        game_ids: list[int] | None = None
        if self._selected_collection is not None:
            game_ids = q.get_collection_games(
                self._conn, self._selected_collection
            )
        rows = load_rom_rows(
            self._conn, self._selected_system, game_ids=game_ids
        )
        self.game_table.set_rows(rows)
        self.game_table.set_collection_context(
            self._selected_collection is not None
        )
        # Provide collection list to the table for the right-click submenu.
        user_collections = [
            (int(r["id"]), str(r["name"]))
            for r in q.get_collections(self._conn)
            if not int(r["is_system"])
        ]
        self.game_table.set_available_collections(user_collections)
        total = self._conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
        missing = q.count_missing_roms(self._conn)
        if missing:
            self.status_label.setText(
                f"{total} ROMs ({missing} missing — Tools > Clean Missing Entries)"
            )
        else:
            self.status_label.setText(f"{total} ROMs")
        # Reset the detail panel whenever the row set changes.
        self.detail_panel.update_game(None)

    def refresh_all(self) -> None:
        """Repaint both the sidebar and the game table."""
        self.refresh_sidebar()
        self.refresh_game_table()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_system_selected(self, system_id: object) -> None:
        self._selected_system = system_id if isinstance(system_id, str) else None
        self._selected_collection = None
        self.refresh_game_table()

    def _on_collection_selected(self, collection_id: int) -> None:
        """Filter the game table to the games in a specific collection."""
        self._selected_collection = int(collection_id)
        self._selected_system = None
        self.refresh_game_table()

    def _on_game_selected(self, game_id: object) -> None:
        """Forward a game-table row selection to the detail panel."""
        if isinstance(game_id, int):
            self.detail_panel.update_game(game_id)
        else:
            self.detail_panel.update_game(None)

    def _on_add_to_favorites(self, game_id: int) -> None:
        favorites_id = q.ensure_favorites_collection(self._conn)
        q.add_game_to_collection(self._conn, favorites_id, game_id)
        # If the detail panel is showing this game, refresh its toggle state.
        if self.detail_panel.current_game_id == game_id:
            self.detail_panel.update_game(game_id)

    def _on_add_to_collection(self, collection_id: int) -> None:
        game_id = self.detail_panel.current_game_id
        if game_id is None:
            # Fall back to the game-table selection if the panel is blank.
            game_id = self.game_table.selected_game_id()
        if game_id is None:
            return
        q.add_game_to_collection(self._conn, collection_id, game_id)
        self.refresh_sidebar()

    def _on_new_collection(self, name: str) -> None:
        game_id = self.detail_panel.current_game_id
        if game_id is None:
            game_id = self.game_table.selected_game_id()
        try:
            collection_id = q.create_collection(self._conn, name)
        except sqlite3.IntegrityError:
            existing = q.get_collection_by_name(self._conn, name)
            if existing is None:
                return
            collection_id = int(existing["id"])
        if game_id is not None:
            q.add_game_to_collection(self._conn, collection_id, game_id)
        self.refresh_sidebar()

    def _on_remove_from_collection(self, game_id: int) -> None:
        if self._selected_collection is None:
            return
        q.remove_game_from_collection(
            self._conn, self._selected_collection, game_id
        )
        self.refresh_game_table()

    def _on_favorite_toggled(self, _game_id: int, _is_favorite: bool) -> None:
        """Refresh the sidebar so the Favorites count stays accurate."""
        self.refresh_sidebar()

    def _on_open_library(self) -> None:
        from pathlib import Path as _Path

        from romulus.app import prompt_for_library_path

        chosen = prompt_for_library_path(self)
        if not chosen:
            return
        # Canonicalize the same way the scanner does so the value we compare
        # against ``roms.library_root`` matches exactly.
        try:
            chosen_canonical = str(_Path(chosen).resolve())
        except OSError:
            chosen_canonical = chosen

        stale_count = q.count_roms_with_other_library_root(
            self._conn, chosen_canonical
        )
        if stale_count > 0:
            choice = QMessageBox.question(
                self,
                "Switch library?",
                f"This will replace your current library with the one at:\n\n"
                f"{chosen}\n\n"
                f"{stale_count} ROM entries from previous libraries will be "
                f"removed from the database. (Files on disk are not touched.)\n\n"
                f"Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
            q.delete_roms_with_other_library_root(self._conn, chosen_canonical)
            q.prune_orphan_games(self._conn)
            self._conn.commit()

        set_config(self._conn, "library_path", chosen)
        self.status_label.setText(f"Library: {chosen}")
        # Existing rows for this library may include entries the user hasn't
        # rescanned recently — leave them in place; the next Quick Scan will
        # tombstone any that are gone.
        self.refresh_all()

    def _on_clean_missing(self) -> None:
        """Permanently delete every row currently flagged ``missing = 1``."""
        count = q.count_missing_roms(self._conn)
        if count == 0:
            QMessageBox.information(
                self,
                "No missing entries",
                "Nothing to clean — every ROM in the database is present on "
                "disk under the current library root.",
            )
            return
        choice = QMessageBox.question(
            self,
            "Clean missing entries?",
            f"Permanently remove {count} ROM entries that no longer exist on "
            f"disk?\n\n"
            f"This drops their enrichment, metadata, and hash cache from the "
            f"database. Files on disk are not touched. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        deleted = q.delete_missing_roms(self._conn)
        pruned = q.prune_orphan_games(self._conn)
        self._conn.commit()
        self.status_label.setText(
            f"Removed {deleted} missing entries ({pruned} games pruned)"
        )
        self.refresh_all()

    def _on_open_settings(self) -> None:
        dialog = SettingsDialog(self._conn, self)
        dialog.exec()
        self.refresh_all()

    def _on_about(self) -> None:
        from romulus import __version__

        QMessageBox.about(
            self,
            "About ROMulus",
            f"ROMulus v{__version__}\nLocal-first ROM collection manager.",
        )

    # ------------------------------------------------------------------
    # Worker lifetime — clear-slots
    # ------------------------------------------------------------------

    def _clear_scan_worker(self) -> None:
        """Slot — nulls the Python reference once the QThread has been deleted."""
        self._scan_worker = None

    def _clear_heavy_scan_worker(self) -> None:
        self._heavy_scan_worker = None

    def _clear_enrich_worker(self) -> None:
        self._enrich_worker = None

    def _clear_organize_worker(self) -> None:
        self._organize_worker = None

    def _clear_export_worker(self) -> None:
        self._export_worker = None

    def _clear_local_cover_worker(self) -> None:
        self._local_cover_worker = None

    def _clear_dest_inventory_worker(self) -> None:
        self._dest_inventory_worker = None

    def _clear_sync_worker(self) -> None:
        self._sync_worker = None

    def _on_quick_scan(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            QMessageBox.information(
                self,
                "Scan already running",
                "A scan is already in progress — please wait for it to finish.",
            )
            return
        library_path = get_config(self._conn, "library_path") or ""
        if not library_path:
            QMessageBox.warning(
                self,
                "No library configured",
                "Set a library folder via File > Open Library first.",
            )
            return

        self._scan_dialog = ScanProgressDialog(self)
        self._scan_worker = ScanWorker(DEFAULT_DB_PATH, library_path)

        self._scan_worker.progress.connect(self._scan_dialog.on_progress)
        self._scan_worker.finished_ok.connect(self._scan_dialog.on_finished)
        self._scan_worker.failed.connect(self._scan_dialog.on_failed)
        self._scan_worker.finished_ok.connect(self._on_scan_finished_ok)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_dialog.canceled.connect(self._scan_worker.cancel)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_worker.finished.connect(self._clear_scan_worker)

        self._scan_worker.start()
        self._scan_dialog.exec()

    def _on_scan_finished_ok(
        self,
        _scan_id: int,
        _files_found: int,
        _files_with_system: int,
        _files_skipped: int,
        _systems_seen: list[str],
    ) -> None:
        self.refresh_all()

    def _on_scan_failed(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_heavy_scan(self) -> None:
        if (
            self._heavy_scan_worker is not None
            and self._heavy_scan_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Heavy Scan already running",
                "A heavy scan is already in progress — "
                "please wait for it to finish.",
            )
            return

        confirm = QMessageBox.question(
            self,
            "Start Heavy Scan?",
            "Heavy Scan hashes every ROM and matches it against the bundled "
            "DAT database.\n\n"
            "This can take 30+ minutes for a large library over a network "
            "drive. On first run it also loads the bundled DATs (~6 s).\n\n"
            "Continue?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        library_path = get_config(self._conn, "library_path") or ""
        if not library_path:
            QMessageBox.warning(
                self,
                "No library configured",
                "Set a library folder via File > Open Library first.",
            )
            return

        scan_threads = int(get_config(self._conn, "scan_threads") or 8)

        self._heavy_scan_dialog = HeavyScanProgressDialog(self)
        self._heavy_scan_worker = HeavyScanWorker(
            DEFAULT_DB_PATH,
            library_path,
            _BUNDLED_DATS_PATH,
            workers=scan_threads,
        )

        self._heavy_scan_worker.progress.connect(
            self._heavy_scan_dialog.on_progress
        )
        self._heavy_scan_worker.finished_ok.connect(
            self._heavy_scan_dialog.on_finished
        )
        self._heavy_scan_worker.failed.connect(
            self._heavy_scan_dialog.on_failed
        )
        self._heavy_scan_worker.finished_ok.connect(
            self._on_heavy_scan_finished_ok
        )
        self._heavy_scan_worker.failed.connect(self._on_heavy_scan_failed)
        self._heavy_scan_dialog.canceled.connect(self._heavy_scan_worker.cancel)
        self._heavy_scan_worker.finished.connect(
            self._heavy_scan_worker.deleteLater
        )
        self._heavy_scan_worker.finished.connect(self._clear_heavy_scan_worker)

        self._heavy_scan_worker.start()
        self._heavy_scan_dialog.exec()

    def _on_heavy_scan_finished_ok(
        self,
        _total_hashed: int,
        _total_matched: int,
        _errors: int,
    ) -> None:
        self.refresh_all()

    def _on_heavy_scan_failed(self, message: str) -> None:
        self.status_label.setText(message)

    def _heavy_scan_scoped(
        self,
        game_id: int | None = None,
        system_id: str | None = None,
        collection_id: int | None = None,
    ) -> None:
        """Start a heavy scan scoped to a game / system / collection."""
        if (
            self._heavy_scan_worker is not None
            and self._heavy_scan_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Heavy Scan already running",
                "A heavy scan is already in progress — "
                "please wait for it to finish.",
            )
            return

        library_path = get_config(self._conn, "library_path") or ""
        if not library_path:
            QMessageBox.warning(
                self,
                "No library configured",
                "Set a library folder via File > Open Library first.",
            )
            return

        # Resolve scope to ROM ids.
        scope_rom_ids: list[int] | None = None
        if game_id is not None:
            scope_rom_ids = q.get_rom_ids_for_scope(self._conn, game_id=game_id)
        elif system_id is not None:
            scope_rom_ids = q.get_rom_ids_for_scope(self._conn, system_id=system_id)
        elif collection_id is not None:
            scope_rom_ids = q.get_rom_ids_for_scope(
                self._conn, collection_id=collection_id
            )

        if game_id is not None:
            scope_label = f"Heavy Scan: game {game_id}"
        elif system_id is not None:
            scope_label = f"Heavy Scan: {system_id}"
        elif collection_id is not None:
            scope_label = f"Heavy Scan: collection {collection_id}"
        else:
            scope_label = "Preparing heavy scan..."

        scan_threads = int(get_config(self._conn, "scan_threads") or 8)

        self._heavy_scan_dialog = HeavyScanProgressDialog(self)
        self._heavy_scan_dialog.setLabelText(scope_label)
        self._heavy_scan_worker = HeavyScanWorker(
            DEFAULT_DB_PATH,
            library_path,
            _BUNDLED_DATS_PATH,
            workers=scan_threads,
            scope_rom_ids=scope_rom_ids,
        )

        self._heavy_scan_worker.progress.connect(
            self._heavy_scan_dialog.on_progress
        )
        self._heavy_scan_worker.finished_ok.connect(
            self._heavy_scan_dialog.on_finished
        )
        self._heavy_scan_worker.failed.connect(
            self._heavy_scan_dialog.on_failed
        )
        self._heavy_scan_worker.finished_ok.connect(
            self._on_heavy_scan_finished_ok
        )
        self._heavy_scan_worker.failed.connect(self._on_heavy_scan_failed)
        self._heavy_scan_dialog.canceled.connect(self._heavy_scan_worker.cancel)
        self._heavy_scan_worker.finished.connect(
            self._heavy_scan_worker.deleteLater
        )
        self._heavy_scan_worker.finished.connect(self._clear_heavy_scan_worker)

        self._heavy_scan_worker.start()
        self._heavy_scan_dialog.exec()

    def _on_enrich(self) -> None:
        self._enrich_scoped()

    def _prompt_for_enrich_options(
        self,
        *,
        game_ids: list[int] | None,
        system_id: str | None,
        collection_id: int | None,
    ) -> tuple[bool, bool, bool] | None:
        """Show :class:`EnrichOptionsDialog`; return (fuzzy, already, online).

        Returns ``None`` when the user cancels. The scope label fed
        into the dialog is the same human-readable phrase used on the
        progress dialog title so the user sees consistent wording in
        both places.
        """
        if game_ids is not None:
            scope_label = (
                "the selected game"
                if len(game_ids) == 1
                else f"the {len(game_ids)} selected games"
            )
        elif system_id is not None:
            scope_label = f"every game on {system_id}"
        elif collection_id is not None:
            # No first-class get_collection_by_id helper; a one-shot SELECT
            # for the display name is fine and avoids adding a query for
            # this single label.
            row = self._conn.execute(
                "SELECT name FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            name = row["name"] if row is not None else f"collection {collection_id}"
            scope_label = f'every game in the "{name}" collection'
        else:
            scope_label = "the entire library"

        dialog = EnrichOptionsDialog(scope_label, parent=self)
        if dialog.exec() != EnrichOptionsDialog.DialogCode.Accepted:
            return None
        return (
            dialog.include_fuzzy,
            dialog.include_already_enriched,
            dialog.include_online,
        )

    def _enrich_scoped(
        self,
        game_ids: list[int] | None = None,
        system_id: str | None = None,
        collection_id: int | None = None,
        *,
        include_fuzzy: bool = False,
        include_already_enriched: bool = False,
        include_online: bool = True,
    ) -> None:
        """Start enrichment, optionally scoped to a game / system / collection.

        ``include_fuzzy`` / ``include_already_enriched`` loosen the silent
        filters in :func:`get_games_needing_enrichment`. ``include_online``
        gates the network-touching providers (Hasheous, ScreenScraper,
        TheGamesDB) — when False only the bundled offline sources
        (libretro-database, GameDB, LaunchBox XML) run.

        Callers reaching this method directly with both ``include_fuzzy``
        and ``include_already_enriched`` True bypass the per-batch
        prompt — that's the single-game right-click "force" path. Every
        other call path (system, collection, global toolbar) goes through
        :meth:`_prompt_for_enrich_options` first, which surfaces the
        three flags as user checkboxes.
        """
        if self._enrich_worker is not None and self._enrich_worker.isRunning():
            QMessageBox.information(
                self,
                "Enrichment already running",
                "Enrichment is already in progress — please wait for it to finish.",
            )
            return

        # Single-game force path skips the batch-options prompt entirely;
        # the user explicitly chose "enrich this game" so the gates are
        # already deliberately ignored. Every other path asks first.
        is_single_force = (
            game_ids is not None
            and len(game_ids) == 1
            and include_fuzzy
            and include_already_enriched
        )
        if not is_single_force:
            chosen = self._prompt_for_enrich_options(
                game_ids=game_ids,
                system_id=system_id,
                collection_id=collection_id,
            )
            if chosen is None:
                return
            include_fuzzy, include_already_enriched, include_online = chosen

        # Pre-flight: count using the same filters the run will apply.
        # Without this the user could opt in to "re-enrich existing" and
        # still hit the "no games" bail-out because the default filters
        # excluded everything.
        eligible_rows = q.get_games_needing_enrichment(
            self._conn,
            include_fuzzy=include_fuzzy,
            include_already_enriched=include_already_enriched,
        )
        # If the caller scoped to specific ids, narrow the eligibility
        # check to that scope too — otherwise a system-scoped run reports
        # eligibility for the whole library and may erroneously bail.
        if game_ids is not None:
            allowed = frozenset(game_ids)
            eligible_rows = [r for r in eligible_rows if r["id"] in allowed]
        elif system_id is not None:
            eligible_rows = [
                r for r in eligible_rows if r["system_id"] == system_id
            ]
        elif collection_id is not None:
            coll_ids = frozenset(
                q.get_collection_games(self._conn, collection_id)
            )
            eligible_rows = [r for r in eligible_rows if r["id"] in coll_ids]
        if not eligible_rows:
            QMessageBox.information(
                self,
                "No games ready for enrichment",
                "No eligible games found for this scope and option set.\n\n"
                "Run Heavy Scan to match more ROMs against the DAT database, "
                "or tick the looser-filter checkboxes when prompted.",
            )
            return

        cover_cache = get_config(self._conn, "cover_cache_path") or None

        # Build a human-readable scope label for the progress dialog title.
        if game_ids is not None:
            scope_label = (
                f"Enriching game {game_ids[0]}..."
                if len(game_ids) == 1
                else f"Enriching {len(game_ids)} games..."
            )
        elif system_id is not None:
            scope_label = f"Enriching {system_id}..."
        elif collection_id is not None:
            scope_label = f"Enriching collection {collection_id}..."
        else:
            scope_label = "Preparing enrichment..."

        self._enrich_dialog = EnrichProgressDialog(self)
        self._enrich_dialog.setLabelText(scope_label)
        self._enrich_worker = EnrichWorker(
            DEFAULT_DB_PATH,
            cover_cache,
            game_ids=game_ids,
            system_id=system_id,
            collection_id=collection_id,
            include_fuzzy=include_fuzzy,
            include_already_enriched=include_already_enriched,
            include_online=include_online,
        )

        self._enrich_worker.progress.connect(self._enrich_dialog.on_progress)
        self._enrich_worker.finished_ok.connect(self._enrich_dialog.on_finished)
        self._enrich_worker.failed.connect(self._enrich_dialog.on_failed)
        self._enrich_worker.finished_ok.connect(self._on_enrich_finished_ok)
        self._enrich_worker.failed.connect(self._on_enrich_failed)
        self._enrich_dialog.canceled.connect(self._enrich_worker.cancel)
        self._enrich_worker.finished.connect(self._enrich_worker.deleteLater)
        self._enrich_worker.finished.connect(self._clear_enrich_worker)

        self._enrich_worker.start()
        self._enrich_dialog.exec()

    def _on_enrich_finished_ok(
        self,
        _games_processed: int,
        _metadata_added: int,
        _covers_added: int,
    ) -> None:
        self.refresh_all()

    def _on_enrich_failed(self, message: str) -> None:
        self.status_label.setText(message)

    # ------------------------------------------------------------------
    # Find Local Covers
    # ------------------------------------------------------------------

    def _on_find_local_covers(self) -> None:
        """Scan the library tree for local image files and link them as covers."""
        self._find_local_covers_scoped()

    def _find_local_covers_scoped(
        self,
        game_id: int | None = None,
        system_id: str | None = None,
        collection_id: int | None = None,
    ) -> None:
        """Start local cover discovery, optionally scoped to a game/system/collection."""
        if (
            self._local_cover_worker is not None
            and self._local_cover_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Local cover discovery already running",
                "Local cover discovery is already in progress — "
                "please wait for it to finish.",
            )
            return

        library_path = get_config(self._conn, "library_path") or ""
        if not library_path:
            QMessageBox.warning(
                self,
                "No library configured",
                "Set a library folder via File > Open Library first.",
            )
            return

        # Resolve scope to ROM ids.
        scope_rom_ids: list[int] | None = None
        if game_id is not None:
            scope_rom_ids = q.get_rom_ids_for_scope(self._conn, game_id=game_id)
        elif system_id is not None:
            scope_rom_ids = q.get_rom_ids_for_scope(self._conn, system_id=system_id)
        elif collection_id is not None:
            scope_rom_ids = q.get_rom_ids_for_scope(
                self._conn, collection_id=collection_id
            )

        if game_id is not None:
            scope_label = f"Finding local covers: game {game_id}..."
        elif system_id is not None:
            scope_label = f"Finding local covers: {system_id}..."
        elif collection_id is not None:
            scope_label = f"Finding local covers: collection {collection_id}..."
        else:
            scope_label = "Scanning for local cover images..."

        self._local_cover_dialog = LocalCoverProgressDialog(self)
        self._local_cover_dialog.setLabelText(scope_label)
        self._local_cover_worker = LocalCoverFinderWorker(
            DEFAULT_DB_PATH,
            library_path,
            scope_rom_ids=scope_rom_ids,
        )

        self._local_cover_worker.progress.connect(
            self._local_cover_dialog.on_progress
        )
        self._local_cover_worker.finished_ok.connect(
            self._local_cover_dialog.on_finished
        )
        self._local_cover_worker.failed.connect(
            self._local_cover_dialog.on_failed
        )
        self._local_cover_worker.finished_ok.connect(
            self._on_local_cover_finished_ok
        )
        self._local_cover_worker.failed.connect(self._on_local_cover_failed)
        self._local_cover_dialog.canceled.connect(self._local_cover_worker.cancel)
        self._local_cover_worker.finished.connect(
            self._local_cover_worker.deleteLater
        )
        self._local_cover_worker.finished.connect(self._clear_local_cover_worker)

        self._local_cover_worker.start()
        self._local_cover_dialog.exec()

    def _on_local_cover_finished_ok(
        self,
        _roms_scanned: int,
        _covers_found: int,
        _covers_skipped: int,
        _errors: int,
    ) -> None:
        self.refresh_all()

    def _on_local_cover_failed(self, message: str) -> None:
        self.status_label.setText(message)

    # ------------------------------------------------------------------
    # Organize
    # ------------------------------------------------------------------

    def _on_organize(self) -> None:
        """Build a plan, show the preview dialog, and (on Apply) run the worker."""
        if self._organize_worker is not None and self._organize_worker.isRunning():
            QMessageBox.information(
                self,
                "Organize already running",
                "An organize operation is already in progress — "
                "please wait for it to finish.",
            )
            return

        plan = analyze_library(self._conn)
        self._organize_dialog = OrganizePreviewDialog(plan, self)
        self._organize_dialog.actions_approved.connect(
            self._on_organize_actions_approved
        )
        self._organize_dialog.exec()

    def _on_organize_actions_approved(self, actions: list[object]) -> None:
        """Spawn an :class:`OrganizeWorker` once the user clicks Apply."""
        if self._organize_dialog is None:
            return
        # `actions` arrives via a Qt signal as a generic list — narrow defensively.
        from romulus.core.organizer import OrganizeAction

        approved = [a for a in actions if isinstance(a, OrganizeAction)]
        if not approved:
            return

        self._organize_worker = OrganizeWorker(DEFAULT_DB_PATH, approved)
        self._organize_worker.progress.connect(self._organize_dialog.on_progress)
        self._organize_worker.finished_ok.connect(
            self._organize_dialog.on_finished
        )
        self._organize_worker.failed.connect(self._organize_dialog.on_failed)
        self._organize_worker.finished_ok.connect(self._on_organize_finished_ok)
        self._organize_worker.failed.connect(self._on_organize_failed)
        self._organize_worker.finished.connect(
            self._organize_worker.deleteLater
        )
        self._organize_worker.finished.connect(self._clear_organize_worker)
        self._organize_worker.start()

    def _on_organize_finished_ok(
        self,
        _applied: int,
        _skipped: int,
        _failed: int,
        _errors: list[str],
    ) -> None:
        self.refresh_all()

    def _on_organize_failed(self, message: str) -> None:
        self.status_label.setText(message)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export(self) -> None:
        """Open the :class:`ExportDialog` and wire up worker lifecycle."""
        if self._export_worker is not None and self._export_worker.isRunning():
            QMessageBox.information(
                self,
                "Export already running",
                "An export is already in progress — "
                "please wait for it to finish.",
            )
            return

        profiles = load_all_profiles()
        if not profiles:
            QMessageBox.warning(
                self,
                "No destination profiles",
                "No destination profiles were found. The bundled profiles "
                "ship inside the ROMulus package; if you see this message "
                "the install is incomplete.",
            )
            return

        self._export_dialog = ExportDialog(self._conn, profiles, self)
        self._export_dialog.export_requested.connect(self._on_export_requested)
        self._export_dialog.sync_scan_requested.connect(
            self._on_sync_scan_requested
        )
        self._export_dialog.exec()

    def _on_sync_scan_requested(
        self,
        profile: object,
        target_path: str,
        mode: str,
        deep_verify: bool,
        dest_id: int,
    ) -> None:
        """Spawn a :class:`DestInventoryWorker` to scan the destination.

        On completion the :class:`SyncPreviewDialog` is shown with the diff
        the worker produced; the user then either Applies (which spawns the
        :class:`SyncWorker`) or cancels.
        """
        from romulus.core.sync import build_plan
        from romulus.models.profile import DestinationProfile
        from romulus.ui.sync_preview import SyncPreviewDialog

        if not isinstance(profile, DestinationProfile):
            return
        if (
            self._dest_inventory_worker is not None
            and self._dest_inventory_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Destination scan already running",
                "A destination scan is already in progress.",
            )
            return

        # If the user didn't pick a saved destination (legacy ``-1``
        # sentinel — the dropdown no longer exposes that option but the
        # signal contract is preserved) upgrade to a real row before the
        # worker starts. Both ``upsert_dest_inventory`` and
        # ``insert_sync_plan`` have a FOREIGN KEY ref on
        # ``sync_destinations(id)`` — passing ``-1`` previously caused
        # every per-action DB write to fail with
        # ``FOREIGN KEY constraint failed`` even though the file copy on
        # disk had already succeeded.
        if dest_id <= 0:
            dest_id = q.ensure_sync_destination_by_path(
                self._conn,
                target_path,
                profile.id,
            )
            self._conn.commit()
            # Refresh the dropdown so the auto-saved destination shows up
            # on next launch / next time the user opens this dialog.
            if self._export_dialog is not None and hasattr(
                self._export_dialog, "_populate_destination_combo"
            ):
                self._export_dialog._populate_destination_combo(  # type: ignore[attr-defined]
                    select_dest_id=dest_id
                )

        # Spin up the progress dialog BEFORE the worker so the user gets
        # immediate visual feedback. Previously this slot ran the walk
        # without any UI cue at all — the user reported a ~30 s "frozen
        # window" because :class:`DestInventoryWorker` itself runs on its
        # own QThread but no dialog was displayed while it worked.
        self._dest_scan_dialog = DestScanProgressDialog(self)

        self._dest_inventory_worker = DestInventoryWorker(
            DEFAULT_DB_PATH,
            dest_id,
            target_path,
            deep_verify=deep_verify,
        )

        def _on_inventory_done(inventory: object) -> None:
            from romulus.core.dest_inventory import DestInventory

            if not isinstance(inventory, DestInventory):
                return
            # Dismiss the scan progress dialog before the preview opens so
            # we don't stack two modals on top of each other.
            if self._dest_scan_dialog is not None:
                self._dest_scan_dialog.close()
                self._dest_scan_dialog = None
            library_path = get_config(self._conn, "library_path") or None
            plan = build_plan(
                self._conn,
                dest_id,
                profile,
                target_path,
                inventory,
                mode,  # type: ignore[arg-type]
                library_path=library_path,
            )
            dialog = SyncPreviewDialog(
                plan,
                destination_label=target_path,
                parent=self,
            )
            dialog.actions_approved.connect(
                lambda approved: self._on_sync_actions_approved(
                    approved, profile, target_path, plan, dest_id
                )
            )
            self._sync_preview_dialog = dialog
            dialog.exec()

        self._dest_inventory_worker.progress.connect(
            self._dest_scan_dialog.on_progress
        )
        self._dest_inventory_worker.finished_ok.connect(
            self._dest_scan_dialog.on_finished
        )
        self._dest_inventory_worker.failed.connect(
            self._dest_scan_dialog.on_failed
        )
        self._dest_inventory_worker.finished_ok.connect(_on_inventory_done)
        self._dest_inventory_worker.failed.connect(self._on_sync_scan_failed)
        self._dest_scan_dialog.canceled.connect(
            self._dest_inventory_worker.cancel
        )
        self._dest_inventory_worker.finished.connect(
            self._dest_inventory_worker.deleteLater
        )
        self._dest_inventory_worker.finished.connect(
            self._clear_dest_inventory_worker
        )
        self._dest_inventory_worker.start()
        self._dest_scan_dialog.show()

    def _on_sync_scan_failed(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_sync_actions_approved(
        self,
        approved_actions: list[object],
        profile: object,
        target_path: str,
        plan: object,
        dest_id: int,
    ) -> None:
        """Spawn a :class:`SyncWorker` once the user clicks Apply on the preview."""
        from romulus.core.sync import SyncAction, SyncPlan
        from romulus.models.profile import DestinationProfile

        if not isinstance(profile, DestinationProfile):
            return
        if not isinstance(plan, SyncPlan):
            return
        actions = [a for a in approved_actions if isinstance(a, SyncAction)]
        if not actions:
            return
        if self._sync_worker is not None and self._sync_worker.isRunning():
            QMessageBox.information(
                self,
                "Sync already running",
                "A sync is already in progress.",
            )
            return
        library_path = get_config(self._conn, "library_path") or None
        self._sync_worker = SyncWorker(
            DEFAULT_DB_PATH,
            dest_id,
            profile,
            target_path,
            plan,
            actions,
            library_path=library_path,
        )
        if self._sync_preview_dialog is not None and hasattr(
            self._sync_preview_dialog, "on_progress"
        ):
            preview_dialog = self._sync_preview_dialog
            self._sync_worker.progress.connect(preview_dialog.on_progress)  # type: ignore[attr-defined]
            self._sync_worker.finished_ok.connect(
                lambda applied, skipped, failed, _errors: preview_dialog.on_finished(  # type: ignore[attr-defined]
                    applied, skipped, failed
                )
            )
            self._sync_worker.failed.connect(preview_dialog.on_failed)  # type: ignore[attr-defined]
        self._sync_worker.finished_ok.connect(self._on_sync_finished_ok)
        self._sync_worker.failed.connect(self._on_sync_failed)
        self._sync_worker.finished.connect(self._sync_worker.deleteLater)
        self._sync_worker.finished.connect(self._clear_sync_worker)
        self._sync_worker.start()

    def _on_sync_finished_ok(
        self,
        _applied: int,
        _skipped: int,
        _failed: int,
        _errors: list[str],
    ) -> None:
        self.refresh_all()

    def _on_sync_failed(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_export_requested(
        self,
        profile: object,
        target_path: str,
        filters: object,
        options: object,
    ) -> None:
        """Spawn an :class:`ExportWorker` once the user clicks Export."""
        if self._export_dialog is None:
            return
        from romulus.core.exporter import ExportFilters, ExportOptions
        from romulus.models.profile import DestinationProfile

        if not isinstance(profile, DestinationProfile):
            return
        export_filters = filters if isinstance(filters, ExportFilters) else None
        export_options = options if isinstance(options, ExportOptions) else None

        self._export_worker = ExportWorker(
            DEFAULT_DB_PATH,
            profile,
            target_path,
            export_filters,
            export_options,
        )
        self._export_worker.progress.connect(self._export_dialog.on_progress)
        self._export_worker.finished_ok.connect(self._export_dialog.on_finished)
        self._export_worker.failed.connect(self._export_dialog.on_failed)
        self._export_worker.finished_ok.connect(self._on_export_finished_ok)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.finished.connect(self._export_worker.deleteLater)
        self._export_worker.finished.connect(self._clear_export_worker)
        self._export_worker.start()

    def _on_export_finished_ok(
        self,
        _files_copied: int,
        _files_skipped: int,
        _bytes_copied: int,
        _systems: list[str],
        _errors: list[str],
    ) -> None:
        # No library state changes from an export — refresh is a no-op but
        # cheap, and matches the pattern of the other workers.
        self.refresh_all()

    def _on_export_failed(self, message: str) -> None:
        self.status_label.setText(message)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Stop any running workers cleanly before the window goes away.

        QThread workers hold an open SQLite connection; tearing down the main
        window without waiting on them leaks the thread and (worse) can leave
        WAL files in an inconsistent state. We request cooperative cancel,
        then wait up to a few seconds for the thread to drain.

        The ``try/except RuntimeError`` guards against the rare case where the
        Python worker reference is still set (the clear-slot hasn't fired yet)
        but the underlying C++ QThread object has already been deleted by Qt's
        event loop. Calling ``isRunning()`` on a dead wrapper raises
        ``RuntimeError: libshiboken: Internal C++ object already deleted``.
        """
        for worker in (
            self._scan_worker,
            self._heavy_scan_worker,
            self._enrich_worker,
            self._local_cover_worker,
            self._organize_worker,
            self._export_worker,
            self._dest_inventory_worker,
            self._sync_worker,
        ):
            if worker is None:
                continue
            try:
                if not worker.isRunning():
                    continue
                worker.cancel()
                # Bounded wait so a wedged worker never freezes shutdown forever.
                worker.wait(5000)
            except RuntimeError:
                # Underlying C++ object already deleted — nothing to wait on.
                pass
        super().closeEvent(event)
