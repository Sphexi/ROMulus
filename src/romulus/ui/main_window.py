"""Main window — menu bar, toolbar, three-panel layout."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QToolBar,
    QWidget,
)

from romulus.db import DEFAULT_DB_PATH, get_config, set_config
from romulus.ui.game_table import GameTable, load_rom_rows
from romulus.ui.scan_progress import ScanProgressDialog
from romulus.ui.settings_dialog import SettingsDialog
from romulus.ui.system_sidebar import SystemSidebar
from romulus.ui.workers import ScanWorker


class MainWindow(QMainWindow):
    """Top-level window: menu bar, toolbar, sidebar | game table | detail."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.setWindowTitle("Romulus")
        self.resize(1280, 800)
        self._conn = conn
        self._selected_system: str | None = None
        self._scan_worker: ScanWorker | None = None
        self._scan_dialog: ScanProgressDialog | None = None

        self.sidebar = SystemSidebar(self)
        self.game_table = GameTable(self)
        self.detail_panel = QLabel("Select a game to see details", self)
        self.detail_panel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_panel.setWordWrap(True)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.game_table)
        splitter.addWidget(self._wrap_detail())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        self.setCentralWidget(splitter)

        self.status_label = QLabel("Ready")
        self.statusBar().addPermanentWidget(self.status_label)

        self._build_menu()
        self._build_toolbar()

        self.sidebar.system_selected.connect(self._on_system_selected)

    def _wrap_detail(self) -> QWidget:
        wrapper = QWidget(self)
        from PySide6.QtWidgets import QVBoxLayout

        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.detail_panel)
        return wrapper

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
        for label in ("Organize", "Enrich", "Export"):
            placeholder = QAction(label, self, enabled=False)
            placeholder.setToolTip("Available in a later session.")
            tools_menu.addAction(placeholder)

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
        for label in ("Organize", "Enrich", "Export"):
            action = QAction(label, self)
            action.setEnabled(False)
            toolbar.addAction(action)

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
        """Reload visible games for the currently-selected system filter."""
        rows = load_rom_rows(self._conn, self._selected_system)
        self.game_table.set_rows(rows)
        total = self._conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
        self.status_label.setText(f"{total} ROMs")

    def refresh_all(self) -> None:
        """Repaint both the sidebar and the game table."""
        self.refresh_sidebar()
        self.refresh_game_table()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_system_selected(self, system_id: object) -> None:
        self._selected_system = system_id if isinstance(system_id, str) else None
        self.refresh_game_table()

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
