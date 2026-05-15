"""Application initialization — database setup and main window launch."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog

from romulus.db import (
    DEFAULT_DB_PATH,
    create_tables,
    get_config,
    get_connection,
    seed_defaults,
    set_config,
)
from romulus.db import queries as q
from romulus.models import seed_systems


def initialize_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the app DB, create tables, and seed systems + defaults + favorites."""
    conn = get_connection(db_path)
    create_tables(conn)
    seed_systems(conn)
    seed_defaults(conn)
    q.ensure_favorites_collection(conn)
    return conn


def prompt_for_library_path(parent=None) -> str:
    """Open a folder picker so the user can choose their ROM library."""
    return QFileDialog.getExistingDirectory(
        parent,
        "Select your ROM library folder",
        str(Path.home()),
    )


def ensure_library_path(conn: sqlite3.Connection, parent=None) -> str:
    """Return the saved library_path, or prompt for one and save it.

    Empty string is returned if the user cancels the dialog.
    """
    current = get_config(conn, "library_path") or ""
    if current:
        return current
    chosen = prompt_for_library_path(parent)
    if chosen:
        set_config(conn, "library_path", chosen)
    return chosen


def run() -> int:
    """Bootstrap QApplication, init the DB, and show the main window."""
    app = QApplication.instance() or QApplication(sys.argv)
    conn = initialize_database(DEFAULT_DB_PATH)
    # Late import is INTENTIONAL: ``MainWindow`` (and the chain of Qt widget
    # classes it pulls in) requires a live QApplication to exist before any
    # QWidget subclass is even imported on some Qt builds. Moving this back to
    # the top of the file regresses headless startup. Do not "clean up".
    from romulus.ui.main_window import MainWindow

    window = MainWindow(conn)
    ensure_library_path(conn, window)
    window.refresh_all()
    window.show()
    return app.exec()
