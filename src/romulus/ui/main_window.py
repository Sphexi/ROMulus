"""Main window — menu bar, toolbar, three-panel layout."""

from __future__ import annotations

import sqlite3

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
from romulus.ui.detail_panel import DetailPanel
from romulus.ui.enrich_progress import EnrichProgressDialog
from romulus.ui.export_dialog import ExportDialog
from romulus.ui.game_table import GameTable, load_rom_rows
from romulus.ui.organize_preview import OrganizePreviewDialog
from romulus.ui.scan_progress import ScanProgressDialog
from romulus.ui.settings_dialog import SettingsDialog
from romulus.ui.system_sidebar import SystemSidebar
from romulus.ui.workers import (
    EnrichWorker,
    ExportWorker,
    OrganizeWorker,
    ScanWorker,
)


class MainWindow(QMainWindow):
    """Top-level window: menu bar, toolbar, sidebar | game table | detail."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.setWindowTitle("Romulus")
        self.resize(1280, 800)
        self._conn = conn
        self._selected_system: str | None = None
        self._selected_collection: int | None = None
        self._scan_worker: ScanWorker | None = None
        self._scan_dialog: ScanProgressDialog | None = None
        self._enrich_worker: EnrichWorker | None = None
        self._enrich_dialog: EnrichProgressDialog | None = None
        self._organize_worker: OrganizeWorker | None = None
        self._organize_dialog: OrganizePreviewDialog | None = None
        self._export_worker: ExportWorker | None = None
        self._export_dialog: ExportDialog | None = None

        q.ensure_favorites_collection(conn)

        self.sidebar = SystemSidebar(self)
        self.game_table = GameTable(self)
        self.detail_panel = DetailPanel(conn, self)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.game_table)
        splitter.addWidget(self.detail_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
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
        heavy_scan = QAction("&Heavy Scan", self, enabled=False)
        heavy_scan.setToolTip("Available in a later session.")
        tools_menu.addAction(heavy_scan)
        tools_menu.addSeparator()
        enrich_action = QAction("&Enrich", self)
        enrich_action.setToolTip("Fetch cover art and metadata for matched games.")
        enrich_action.triggered.connect(self._on_enrich)
        tools_menu.addAction(enrich_action)
        organize_action = QAction("&Organize", self)
        organize_action.setToolTip("Preview and apply library reorganization.")
        organize_action.triggered.connect(self._on_organize)
        tools_menu.addAction(organize_action)
        export_action = QAction("E&xport", self)
        export_action.setToolTip("Export the library to a destination profile.")
        export_action.triggered.connect(self._on_export)
        tools_menu.addAction(export_action)

        help_menu = menu.addMenu("&Help")
        about_action = QAction("&About Romulus", self)
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
        heavy.setEnabled(False)
        toolbar.addAction(heavy)

        toolbar.addSeparator()
        organize = QAction("Organize", self)
        organize.triggered.connect(self._on_organize)
        toolbar.addAction(organize)
        enrich = QAction("Enrich", self)
        enrich.triggered.connect(self._on_enrich)
        toolbar.addAction(enrich)
        export = QAction("Export", self)
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
            game_id = self.game_table._selected_game_id()
        if game_id is None:
            return
        q.add_game_to_collection(self._conn, collection_id, game_id)
        self.refresh_sidebar()

    def _on_new_collection(self, name: str) -> None:
        game_id = self.detail_panel.current_game_id
        if game_id is None:
            game_id = self.game_table._selected_game_id()
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
        from romulus.app import prompt_for_library_path

        chosen = prompt_for_library_path(self)
        if chosen:
            set_config(self._conn, "library_path", chosen)
            self.status_label.setText(f"Library: {chosen}")

    def _on_open_settings(self) -> None:
        dialog = SettingsDialog(self._conn, self)
        dialog.exec()
        self.refresh_all()

    def _on_about(self) -> None:
        from romulus import __version__

        QMessageBox.about(
            self,
            "About Romulus",
            f"Romulus v{__version__}\nLocal-first ROM collection manager.",
        )

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

    def _on_enrich(self) -> None:
        if self._enrich_worker is not None and self._enrich_worker.isRunning():
            QMessageBox.information(
                self,
                "Enrichment already running",
                "Enrichment is already in progress — please wait for it to finish.",
            )
            return
        cover_cache = get_config(self._conn, "cover_cache_path") or None

        self._enrich_dialog = EnrichProgressDialog(self)
        self._enrich_worker = EnrichWorker(DEFAULT_DB_PATH, cover_cache)

        self._enrich_worker.progress.connect(self._enrich_dialog.on_progress)
        self._enrich_worker.finished_ok.connect(self._enrich_dialog.on_finished)
        self._enrich_worker.failed.connect(self._enrich_dialog.on_failed)
        self._enrich_worker.finished_ok.connect(self._on_enrich_finished_ok)
        self._enrich_worker.failed.connect(self._on_enrich_failed)
        self._enrich_dialog.canceled.connect(self._enrich_worker.cancel)
        self._enrich_worker.finished.connect(self._enrich_worker.deleteLater)

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
                "No destination profiles were found in data/profiles/.",
            )
            return

        self._export_dialog = ExportDialog(self._conn, profiles, self)
        self._export_dialog.export_requested.connect(self._on_export_requested)
        self._export_dialog.exec()

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
        """
        for worker in (
            self._scan_worker,
            self._enrich_worker,
            self._organize_worker,
            self._export_worker,
        ):
            if worker is None or not worker.isRunning():
                continue
            worker.cancel()
            # Bounded wait so a wedged worker never freezes shutdown forever.
            worker.wait(5000)
        super().closeEvent(event)
